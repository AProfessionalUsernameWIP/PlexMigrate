"""
Dashboard UI, keyboard handling, and progress utilities for PlexMigrate.

Contains DashboardState (the thread-safe state model), _build_dashboard
(the Rich Panel renderer), _keyboard_thread (raw key capture), and helpers
used by both the snapshot and import pipelines.
"""

import contextlib
import contextvars
import logging
import os
import subprocess
import sys
import threading
import time
import webbrowser
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

from rich.live import Live
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeRemainingColumn,
)
from rich.text import Text

import services.state as state
from services.state import VERSION, PLEX_PORT, console


# ── Dashboard Data Classes ────────────────────────────────────────────────────

@dataclass
class ActivityEntry:
    """One entry in the live activity feed.

    The deque holding these entries is sized to keep enough history
    for the operator to scroll through a full snapshot run's events
    (see ``DashboardState.__init__`` for the maxlen). Pre-v0.14 the
    cap was 8 — only the most recent events stayed visible — which
    matched the dashboard's old 8-line text widget but lost detail on
    longer runs. The frontend's ``.feed`` panel already caps its
    rendered height at 240px with ``overflow-y: auto``, so a larger
    backend buffer translates directly into scrollable history without
    growing the dashboard window.

    ``server_name`` was added in PR-2 / Phase C (ex-Phase A activity-feed
    scoping) to support filtering out entries that belong to servers
    not participating in the currently active job. An empty string
    (the default) means "unscoped" - the WS payload always emits these
    regardless of which job is running. Entries tagged with a specific
    server name are emitted only when that server is a participant in
    the active job; outside an active job they're emitted unchanged.
    The tagging is opt-in per ``push_activity`` call site so existing
    call sites stay untouched.
    """
    timestamp: str
    action_type: str
    library: str
    title: str
    server_name: str = ""


@dataclass
class LibraryProgress:
    """Per-library tracking for progress bars in the dashboard."""
    name: str
    total: int = 0
    completed: int = 0
    status: str = "queued"   # queued | active | done | error
    phase: str = ""
    start_time: float = 0.0


@dataclass
class RateLimitEntry:
    """
    One 429 / 503 event recorded in the rate-limit feed (v0.9.6).

    Distinct from :class:`ActivityEntry` so a burst of throttle events
    doesn't push useful engine events out of the activity feed (the
    feed caps at 8 entries). The Network panel renders this stream
    next to the cumulative 2C status block.
    """
    timestamp: str
    library: str
    status_code: int
    retry_after_seconds: Optional[float]
    detail: str = ""


# ── HTTP attribution ContextVar (v0.9.6) ─────────────────────────────────────
# Set by ``submit_with_context`` (and the per-library task entry in
# importer/snapshotter) to tag every outbound Plex API call with the
# library it's working on. Read by the requests response hook in
# services/auth.py to build per-library status / latency histograms.
#
# Why a ContextVar instead of a threading.local: ``concurrent.futures``'s
# ``ThreadPoolExecutor`` does not propagate threading.local across
# worker submissions, so per-library attribution would be lost in the
# inner resolve / scrobble / rate worker pools. ContextVar copies
# correctly via ``contextvars.copy_context().run(...)``, which
# ``submit_with_context`` does for us.
#
# Empty-string default means "no library context" - the hook
# attributes those calls to the ``__all__`` cumulative bucket only.
_http_lib_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "plexmigrate_http_library",
    default="",
)


def submit_with_context(executor, fn: Callable, *args, **kwargs):
    """
    Drop-in replacement for ``executor.submit(fn, *args, **kwargs)``
    that copies the calling context into the worker thread so any
    ContextVar set on the submitter (notably :data:`_http_lib_var`)
    is visible inside ``fn``.

    Cost is a single ``copy_context()`` per submission - microseconds -
    and it's the cleanest way to make per-library HTTP attribution
    survive the engine's nested ThreadPoolExecutor pattern.
    """
    ctx = contextvars.copy_context()
    return executor.submit(ctx.run, fn, *args, **kwargs)


@contextlib.contextmanager
def library_http_context(library: str):
    """
    Set :data:`_http_lib_var` to ``library`` for the duration of the
    ``with`` block, then restore the prior value on exit. Used by the
    library-level task entry points (``import_export_file``,
    ``snapshot_library``) to seed the context so every HTTP call inside
    - including those issued from nested worker pools - gets
    attributed to the right library.
    """
    token = _http_lib_var.set(library)
    try:
        yield
    finally:
        _http_lib_var.reset(token)


@dataclass
class CurrentItem:
    """
    What one worker thread is processing right now, surfaced on the
    dashboard's "Currently Processing" panel.

    ``phase`` distinguishes *what kind of work* the thread is doing on
    this item - e.g. ``"resolving"`` (looking it up on the target),
    ``"scrobbling"`` (writing the view count), ``"rating"``,
    ``"merging"`` (adding to a playlist/collection), ``"capturing"``
    (reading from source), ``"indexing"`` (scan-cache build),
    ``"fetching"`` (playlist enumeration warmup). The dashboard
    renders this as a column so the user can tell a worker stuck
    mid-resolve apart from one mid-write.
    """
    library: str
    item_type: str
    title: str
    started_at: float
    phase: str = ""
    # Spec Section 4.5 / Section 1.3 - phase age clock. Resets when
    # the worker transitions to a new phase so the dashboard can
    # colour-code the row by how long this specific phase has been
    # running, not the whole item's elapsed time. Stamped by
    # set_current_item on every call - the new value lands whenever
    # the worker enters a new phase OR re-enters the same phase on
    # a new item (the per-item baseline is the same in both cases).
    phase_started_at: float = 0.0


# ── Dashboard State ───────────────────────────────────────────────────────────

# Rule 4: cap on per-container detail rows surfaced through the
# dashboard payload. A playlist with thousands of unavailable items
# would otherwise inflate the live frame; we cap the detail list and
# leave the full record to the run log.
_CONTAINER_SKIP_DETAIL_CAP = 25


class DashboardState:
    """
    Single source of truth for the live terminal dashboard.

    All worker threads write to this object; a dedicated display loop reads
    snapshots from it at 4 Hz and renders the panel via Rich Live.
    Separating state from rendering means workers never block on display
    code and the display never sees a half-updated state.
    """

    def __init__(self, log_dir: str = "") -> None:
        self._lock = threading.Lock()
        self.libraries: Dict[str, LibraryProgress] = {}
        self._lib_order: List[str] = []
        # v0.14 — keep enough activity history for the operator to
        # scroll through a full run's events. Pre-v0.14 this was
        # ``maxlen=8`` which matched the CLI's 8-line activity widget
        # but lost detail on longer runs. The web UI's ``.feed`` panel
        # caps its rendered height at 240px with overflow-y: auto, so
        # a larger backend buffer translates into scrollable history
        # without growing the dashboard window. 200 covers a typical
        # multi-library snapshot (started + 4 phase + done per library
        # × 20+ libraries) with headroom; the WS payload size remains
        # tiny (each entry is a handful of short strings).
        self.activity: Deque[ActivityEntry] = deque(maxlen=200)
        self.completed = 0
        self.skipped = 0
        self.failed = 0
        self.guid_hits = 0
        self.filepath_hits = 0
        self.suffix_hits = 0
        self.fuzzy_hits = 0
        self.unresolved = 0
        # Per-tier counters surfaced in the run summary and consumed
        # by the Dashboard's fuzzy-match warning banner. Keyed by
        # tier name as returned by ``resolve_item``: ``"DB"`` (Tier
        # 0), ``"GUID"`` (Tier 1), ``"filepath"`` /
        # ``"filepath-suffix"`` (Tier 2), ``"fuzzy"`` (Tier 3).
        # Distinct from the existing ``guid_hits`` / ``filepath_hits``
        # / etc. accumulators because those are tier-tally PER-ITEM
        # via _record_success_with_tier and would double-count if we
        # reused them. Reset along with the dashboard.
        self.tier_counts: Dict[str, int] = {}
        # Rule 4: per-container import summary. Two parallel lists -
        # playlists and collections - populated by restore_playlists /
        # restore_collections at the end of each container's merge step.
        # Each entry: ``{"name", "library", "user_handle", "total",
        # "restored", "skipped", "skipped_items", "smart", "reason"}``.
        # ``skipped_items`` is capped at ``_CONTAINER_SKIP_DETAIL_CAP``
        # so a 5000-member playlist with 4000 misses doesn't bloat the
        # dashboard payload; the full detail is in the run log.
        self.container_summary: Dict[str, List[Dict[str, Any]]] = {
            "playlists": [],
            "collections": [],
        }
        # Timing engine state (discover-don't-predict model). There is
        # no pre-run estimate: ``rolling_etr_seconds`` is the single
        # headline value, populated only once the rolling tracker has
        # enough real throughput samples to project from. ``None``
        # means "still measuring" - the frontend renders that as
        # "Calculating...".
        self.rolling_etr_seconds: Optional[float] = None
        # Per-batch trackers - populated dynamically as the engine
        # encounters new leaf types. Each entry exposes the tracker's
        # current state (total, completed, etr_seconds) to the frame
        # serialiser without holding the tracker object itself.
        self.batch_etrs: Dict[str, Dict[str, Any]] = {}
        # Total-run rolling tracker. ``None`` until init_etr_tracker
        # is called at job start. Lives behind the lock; the
        # ``rolling_etr_seconds`` mirror above is what the frame
        # serialiser actually reads.
        self._etr_tracker: Optional["Any"] = None
        # Per-batch trackers keyed by batch label ("watch", "rating",
        # "playlist", "collection"). Each one is an independent
        # :class:`services.timing.ETRTracker`. Populated lazily via
        # ``add_batch_total`` / ``tick_batch``.
        self._batch_trackers: Dict[str, "Any"] = {}
        # Per-worker-thread phase clocks. Indexed by threading
        # ident. ``phase_started_at`` is the unix timestamp the
        # current phase entered; the frontend computes phase age =
        # now - phase_started_at and colours the row per
        # STALL_THRESHOLDS in the spec.
        self._threads: Dict[int, str] = {}
        # ── Run-coverage counters (new) ──────────────────────────────
        # How much data this run is touching, broken out by category so
        # the dashboard can show "5 users · 12,481 watched · 47 playlists
        # · 314 collections · 89 ratings" at a glance. These count items
        # *enumerated* (snapshot side) or *processed* (import side), not
        # only items that succeeded - keep them in sync with the run's
        # actual scope.
        self.home_user_count = 0    # includes the Plex owner (set via set_user_count)
        self.watch_count = 0
        self.playlist_count = 0
        self.collection_count = 0
        self.rating_count = 0
        # ── Currently-processing items (new) ─────────────────────────
        # Keyed by threading.get_ident() so updates from worker threads
        # don't collide. The dashboard renders one row per active worker.
        self.current_items: Dict[int, CurrentItem] = {}
        # ── Header context (v0.9.6) ──────────────────────────────────
        # current_user holds the raw identifier (owner email or managed
        # username) of whichever user the engine is processing right
        # now. None when the run has no per-user phase scope (e.g.
        # CLI-only library snapshots without home users) or no run is
        # active. The frontend resolves this through
        # user_display_names before rendering. The backend always
        # writes the raw identifier - log files, success records, and
        # all engine logic consult the raw value.
        self.current_user: Optional[str] = None
        # ── Display-name map (v0.9.6) ────────────────────────────────
        # Copied at job start from the active server's registry row
        # so the frontend can substitute friendly display names for
        # raw identifiers without a separate REST call per WS tick.
        # Keys: owner email or managed username. Values: operator-
        # chosen display string. Empty dict on runs with no map set.
        self.user_display_names: Dict[str, str] = {}
        # ── HTTP telemetry (v0.9.6, Feature 2) ───────────────────────
        # Populated by the requests response hook installed on every
        # session. All updates go through _record_http_response under
        # _lock to keep snapshot() consistent.
        #
        # _http_status_counts outer key is the library name OR the
        # "__all__" sentinel for cumulative totals; inner key is the
        # integer HTTP status code. The double-key shape lets the
        # frontend toggle between "this library only" and "whole run"
        # with one structure.
        self._http_status_counts: Dict[str, Dict[int, int]] = {}
        # Rolling window of recent responses. Each tuple is
        # (timestamp_seconds, elapsed_ms, library, status_code).
        # v0.9.7 Item 1: time-based eviction. The deque holds exactly
        # the last 60 seconds of entries (popped from the left by
        # record_http_response as new entries arrive past the
        # window). A hard ceiling of 100 000 entries is the safety
        # net against pathological traffic rates that would otherwise
        # let the deque grow unbounded; at sustained 1000 req/s that
        # ceiling kicks in at minute one and keeps memory bounded.
        # The previous fixed maxlen=500 caused the original "graph
        # only shows data on the right edge" bug at high rates
        # because old entries got evicted before reaching the left
        # side of the 60-second window.
        self._http_recent: Deque[Tuple[float, float, str, int]] = deque(maxlen=100_000)
        # 2C: rate-limit counters and a separate event feed.
        # _rate_limit_events maxes at 50; the Network panel renders
        # the most recent N. Kept apart from `activity` (8 entries)
        # so a 429 burst doesn't push engine events out of view.
        self._http_rate_limit_count: int = 0
        self._http_retry_count: int = 0
        self._http_backoff_active: bool = False
        self._rate_limit_events: Deque[RateLimitEntry] = deque(maxlen=50)
        self._pause_event = threading.Event()
        self._pause_event.set()
        self.paused = False
        self.start_time = time.time()
        self.log_dir = log_dir
        # ── Run-level finalize phase (Part B) ────────────────────────
        # Set by the job runner during the gap between the engine
        # returning and the JobRecord flipping to COMPLETED - i.e.
        # while close-logger / run-dir finalize / snapshot-DB capture
        # are still running. The engine's per-library rows all read
        # "Done" by then, so without this the dashboard looks frozen
        # at 100%. ``None`` = not finalizing; a string = the current
        # finalize sub-step, shown by the frontend as "Finalizing — …".
        self.finalizing: Optional[str] = None

    def add_library(self, name: str, total: int) -> None:
        with self._lock:
            if name not in self.libraries:
                self.libraries[name] = LibraryProgress(name=name, total=total)
                self._lib_order.append(name)

    def set_library_status(self, name: str, status: str) -> None:
        with self._lock:
            if name in self.libraries:
                lib = self.libraries[name]
                lib.status = status
                if status == "active" and lib.start_time == 0.0:
                    lib.start_time = time.time()

    def set_library_phase(self, name: str, phase: str) -> None:
        with self._lock:
            if name in self.libraries:
                self.libraries[name].phase = phase

    def set_finalizing(self, label: Optional[str]) -> None:
        """
        Set (or clear, with ``None``) the run-level finalize phase.

        Called by the job runner in the post-engine gap so the
        dashboard shows "Finalizing — <label>" instead of looking
        frozen at 100% while close-logger / run-dir finalize /
        snapshot-DB capture finish. Cleared isn't strictly required -
        the JobRecord flips to COMPLETED right after the last call -
        but a ``None`` is accepted for symmetry / defensive resets.
        """
        with self._lock:
            self.finalizing = label or None

    def advance_library(self, name: str, n: int = 1) -> None:
        with self._lock:
            if name in self.libraries:
                lib = self.libraries[name]
                lib.completed = min(lib.completed + n, lib.total)

    def set_library_total(
        self, name: str, total: int, completed: Optional[int] = None,
    ) -> None:
        """
        Reset a library's progress accounting mid-run (v0.9.7 Item 6).

        Direct-transfer needs to widen the per-library bar from the
        placeholder ``total=8`` (4 snapshot phases + 4 import phases) to
        the real item count once the source-side gather has produced
        the payload. Without this, every per-item ``_advance_lib``
        call inside ``import_export_file`` saturates the bar after
        the first handful of items and the top-level ETA stops
        counting down (it climbs alongside elapsed time instead,
        which is the bug Item 6 fixes).

        ``completed`` is optional; when supplied it's clamped to the
        new total so a smaller-than-current new total doesn't leave
        completed > total. ``None`` leaves completed where it is
        (still clamped to the new total).
        """
        with self._lock:
            if name not in self.libraries:
                return
            lib = self.libraries[name]
            lib.total = max(1, int(total))
            if completed is not None:
                lib.completed = min(max(0, int(completed)), lib.total)
            else:
                lib.completed = min(lib.completed, lib.total)

    def finish_library(self, name: str, error: bool = False) -> None:
        with self._lock:
            if name in self.libraries:
                lib = self.libraries[name]
                lib.status = "error" if error else "done"
                lib.completed = lib.total
                lib.phase = "Error" if error else "Done"

    def inc_completed(self) -> None:
        with self._lock:
            self.completed += 1

    def inc_skipped(self) -> None:
        with self._lock:
            self.skipped += 1

    def inc_failed(self) -> None:
        with self._lock:
            self.failed += 1

    def inc_guid(self) -> None:
        with self._lock:
            self.guid_hits += 1

    def inc_filepath(self) -> None:
        with self._lock:
            self.filepath_hits += 1

    def inc_suffix(self) -> None:
        with self._lock:
            self.suffix_hits += 1

    def inc_fuzzy(self) -> None:
        with self._lock:
            self.fuzzy_hits += 1

    def inc_tier(self, tier: str) -> None:
        """
        Increment the per-tier resolution counter. ``tier`` is the
        tier-name string returned by ``resolve_item``: one of
        ``"DB"``, ``"GUID"``, ``"filepath"``, ``"filepath-suffix"``,
        or ``"fuzzy"``. Surfaced in the run summary and consumed by
        the Dashboard's fuzzy-match warning banner.
        """
        if not tier:
            return
        with self._lock:
            self.tier_counts[tier] = self.tier_counts.get(tier, 0) + 1

    def inc_unresolved(self) -> None:
        with self._lock:
            self.unresolved += 1

    # ── Timing engine plumbing (discover-don't-predict) ─────────────

    def init_etr_tracker(self) -> None:
        """
        Create the rolling-window tracker for the total run. Called
        at job start with a total of zero - the engine grows the
        total via ``add_run_total`` as it discovers real work, and
        ``tick_etr`` feeds completion samples. The smoothed seconds
        value is published into ``rolling_etr_seconds`` so the frame
        surfaces it; it stays ``None`` ("Calculating...") until the
        tracker has enough samples to project.
        """
        # Late import to keep dashboard.py free of any test-time
        # dependency on the timing module.
        from services.timing import ETRTracker
        with self._lock:
            self._etr_tracker = ETRTracker()
            self.rolling_etr_seconds = None

    def add_run_total(self, n: int) -> None:
        """
        Grow the total-run tracker's total by ``n``. The discover-
        don't-predict entry point - called by each engine phase as it
        enumerates its real work. No-op when the tracker hasn't been
        initialised.
        """
        if n <= 0:
            return
        with self._lock:
            t = self._etr_tracker
            if t is not None:
                t.grow_total(n)

    def tick_etr(self, n: int = 1) -> None:
        """
        Advance the rolling tracker by ``n`` items and republish the
        smoothed ETR. No-op when the tracker hasn't been initialised
        (callers should call ``init_etr_tracker`` at job start; safe
        to call regardless so engine code doesn't have to gate every
        site).
        """
        if n <= 0:
            return
        with self._lock:
            t = self._etr_tracker
            if t is None:
                return
            t.tick(n)
            self.rolling_etr_seconds = t.etr_seconds

    def add_batch_total(self, kind: str, n: int) -> None:
        """
        Grow one batch's total by ``n``. The discover-don't-predict
        entry point for per-batch trackers - called by each engine
        phase as it enumerates the real count of work for that batch
        (e.g. ``snapshot_ratings`` finding 751 rated items registers
        751 under "rating"). Lazily creates the batch tracker on first
        call. Also refreshes the ``batch_etrs`` frame entry so the
        Process List bar gets a real denominator immediately.
        """
        if not kind or n <= 0:
            return
        from services.timing import ETRTracker
        with self._lock:
            t = self._batch_trackers.get(kind)
            if t is None:
                t = ETRTracker()
                self._batch_trackers[kind] = t
            t.grow_total(n)
            self.batch_etrs[kind] = {
                "total": t.total,
                "completed": t.completed,
                "etr_seconds": t.etr_seconds,
            }

    def tick_batch(self, kind: str, n: int = 1) -> None:
        """
        Advance one batch's rolling tracker by ``n`` completed items
        and refresh the ``batch_etrs`` frame entry. Lazily creates the
        batch tracker if ``add_batch_total`` hasn't run yet (a tick
        before any total is registered just means the bar shows
        progress with a zero denominator until a phase enumerates).
        """
        if not kind or n <= 0:
            return
        from services.timing import ETRTracker
        with self._lock:
            t = self._batch_trackers.get(kind)
            if t is None:
                t = ETRTracker()
                self._batch_trackers[kind] = t
            t.tick(n)
            self.batch_etrs[kind] = {
                "total": t.total,
                "completed": t.completed,
                "etr_seconds": t.etr_seconds,
            }

    # ── Per-container import summary (Rule 4) ───────────────────────

    def record_container_result(
        self,
        *,
        kind: str,
        name: str,
        library: str = "",
        user_handle: str = "",
        total: int = 0,
        restored: int = 0,
        skipped_items: Optional[List[Dict[str, str]]] = None,
        smart: bool = False,
        reason: str = "",
    ) -> None:
        """
        Record one import-side container's restoration result. ``kind``
        is ``"playlist"`` or ``"collection"``. Skipped-items detail is
        capped at ``_CONTAINER_SKIP_DETAIL_CAP`` so the dashboard
        payload stays bounded; the full set of misses lives in the run
        log via ``[UNRESOLVED]`` lines.

        For a smart playlist, set ``smart=True`` and ``reason`` to the
        operator-facing explanation; ``total`` / ``restored`` will be
        zero by design.
        """
        if kind not in ("playlist", "collection"):
            return
        entry: Dict[str, Any] = {
            "name": name,
            "library": library,
            "user_handle": user_handle,
            "total": int(total),
            "restored": int(restored),
            "skipped": max(0, int(total) - int(restored)),
            "smart": bool(smart),
            "reason": reason or "",
        }
        if skipped_items:
            entry["skipped_items"] = list(skipped_items)[:_CONTAINER_SKIP_DETAIL_CAP]
            if len(skipped_items) > _CONTAINER_SKIP_DETAIL_CAP:
                entry["skipped_items_truncated"] = (
                    len(skipped_items) - _CONTAINER_SKIP_DETAIL_CAP
                )
        else:
            entry["skipped_items"] = []
        bucket = "playlists" if kind == "playlist" else "collections"
        with self._lock:
            self.container_summary.setdefault(bucket, []).append(entry)

    # ── Run-coverage counters ────────────────────────────────────────

    def set_user_count(self, n: int) -> None:
        """Record total users covered by this run (includes the owner)."""
        with self._lock:
            self.home_user_count = max(0, int(n))

    def inc_watch(self, n: int = 1) -> None:
        with self._lock:
            self.watch_count += n
        # Spec Section 2.1 Phase 3 - feed the global rolling tracker.
        # Spec Section 2.2 - and the per-batch tracker. Watch-history
        # work cuts across movie / episode / track libraries; we use
        # the generic "watch" key so the operator sees one batch row
        # for the watch-history workstream regardless of library type.
        self.tick_etr(n)
        self.tick_batch("watch", n)

    def inc_playlist(self, n: int = 1) -> None:
        with self._lock:
            self.playlist_count += n
        self.tick_etr(n)
        self.tick_batch("playlist", n)

    def inc_collection(self, n: int = 1) -> None:
        with self._lock:
            self.collection_count += n
        self.tick_etr(n)
        self.tick_batch("collection", n)

    def inc_rating(self, n: int = 1) -> None:
        with self._lock:
            self.rating_count += n
        self.tick_etr(n)
        self.tick_batch("rating", n)

    # ── Currently-processing items ───────────────────────────────────

    def set_current_item(
        self,
        library: str,
        item_type: str,
        title: str,
        phase: str = "",
    ) -> None:
        """
        Mark the calling thread as actively processing one item.

        Callers can call this multiple times to advance the phase
        (e.g. ``"resolving"`` → ``"scrobbling"``) without bumping
        ``started_at`` - the latter is preserved so the Age column on
        the dashboard reflects total time spent on the item, not just
        on the current phase.
        """
        tid = threading.get_ident()
        with self._lock:
            existing = self.current_items.get(tid)
            now = time.time()
            started = existing.started_at if existing else now
            # Spec Section 4.5 phase-age semantics: the phase clock
            # resets whenever the (worker, phase) pair changes. A
            # worker re-entering the same phase on a new item gets a
            # fresh clock too; that matches "this row just started"
            # for the new item's row.
            if existing and existing.phase == phase and existing.title == title:
                phase_started = existing.phase_started_at or now
            else:
                phase_started = now
            self.current_items[tid] = CurrentItem(
                library=library,
                item_type=item_type,
                title=title,
                started_at=started,
                phase=phase,
                phase_started_at=phase_started,
            )

    def clear_current_item(self) -> None:
        with self._lock:
            self.current_items.pop(threading.get_ident(), None)

    # ── Header context (v0.9.6) ──────────────────────────────────────

    def set_current_user(self, user: Optional[str]) -> None:
        """
        Update the header's current-user field. ``None`` hides the
        field in the GUI; any string (owner email or managed
        username) shows up after frontend display-name resolution.
        """
        with self._lock:
            self.current_user = user

    def set_user_display_names(self, mapping: Dict[str, str]) -> None:
        """
        Replace the cached display-name map. Called once at job start
        with the active server's user_display_names dict so the
        frontend can resolve current_user without a per-tick REST hit.
        """
        with self._lock:
            self.user_display_names = dict(mapping) if mapping else {}

    # ── HTTP telemetry (v0.9.6, Feature 2) ───────────────────────────

    def record_http_response(
        self,
        library: str,
        status_code: int,
        elapsed_ms: float,
        retry_after_seconds: Optional[float] = None,
    ) -> None:
        """
        One response observed by the session response hook.

        ``library`` should be ``"__all__"`` (empty context) or the
        actual library name the calling worker was working on (set
        via :data:`_http_lib_var`). Both the per-library counter and
        the cumulative ``"__all__"`` counter are bumped - the
        frontend toggle simply picks which to render.

        429 events also append to the dedicated rate-limit feed and
        increment the cumulative counter.
        """
        ts = time.time()
        # Default any falsy library to the cumulative bucket so the
        # frontend's toggle never sees an empty-string key.
        lib_key = library or "__all__"

        with self._lock:
            # Per-library + cumulative counter pair.
            for key in (lib_key, "__all__"):
                bucket = self._http_status_counts.setdefault(key, {})
                bucket[status_code] = bucket.get(status_code, 0) + 1
            # Raw data for the 60-second rolling latency / rate graph.
            # v0.9.7 Item 1: time-based eviction. Pop entries older
            # than 60 seconds off the left so the deque always covers
            # exactly the latest window - old entries from any prior
            # high-traffic burst no longer evict events we need to
            # plot on the left side of the chart.
            cutoff = ts - 60.0
            while self._http_recent and self._http_recent[0][0] < cutoff:
                self._http_recent.popleft()
            self._http_recent.append((ts, float(elapsed_ms), lib_key, int(status_code)))
            # 429 surfaces as a rate-limit event in its own feed.
            if status_code == 429:
                self._http_rate_limit_count += 1
                self._rate_limit_events.append(RateLimitEntry(
                    timestamp=datetime.now().strftime("%H:%M:%S"),
                    library=lib_key if lib_key != "__all__" else "-",
                    status_code=status_code,
                    retry_after_seconds=retry_after_seconds,
                    detail="",
                ))

    def inc_http_retry(self, n: int = 1) -> None:
        """Bump the cumulative retry counter (one increment per urllib3 retry)."""
        with self._lock:
            self._http_retry_count += int(n)

    def set_http_backoff(self, active: bool) -> None:
        """Set the 'currently sleeping for Retry-After' indicator."""
        with self._lock:
            self._http_backoff_active = bool(active)

    def reset_http_telemetry(self) -> None:
        """
        Wipe all HTTP-telemetry accumulators. Called by
        :func:`services.state.reset_run_state` at job start so a new
        run doesn't inherit the previous run's bars and graphs.
        """
        with self._lock:
            self._http_status_counts.clear()
            self._http_recent.clear()
            self._http_rate_limit_count = 0
            self._http_retry_count = 0
            self._http_backoff_active = False
            self._rate_limit_events.clear()

    def push_activity(
        self,
        action_type: str,
        library: str,
        title: str,
        server_name: str = "",
    ) -> None:
        with self._lock:
            self.activity.append(ActivityEntry(
                timestamp=datetime.now().strftime("%H:%M:%S"),
                action_type=action_type,
                library=library,
                title=title,
                server_name=server_name,
            ))

    def register_thread(self, category: str) -> None:
        with self._lock:
            self._threads[threading.get_ident()] = category

    def unregister_thread(self) -> None:
        with self._lock:
            self._threads.pop(threading.get_ident(), None)

    def toggle_pause(self) -> None:
        with self._lock:
            if self._pause_event.is_set():
                self._pause_event.clear()
                self.paused = True
            else:
                self._pause_event.set()
                self.paused = False

    def wait_if_paused(self) -> None:
        """Block the calling thread when paused. Returns immediately when running."""
        self._pause_event.wait()

    def to_dashboard_frame(self) -> Dict[str, Any]:
        """Returns a JSON-like dict copy of the current state for rendering."""
        now = time.time()
        with self._lock:
            return {
                "libraries": [
                    {
                        "name": lib.name,
                        "total": lib.total,
                        "completed": lib.completed,
                        "status": lib.status,
                        "phase": lib.phase,
                        "start_time": lib.start_time,
                    }
                    for lib in (self.libraries[n] for n in self._lib_order)
                ],
                "activity": [
                    {
                        "timestamp": e.timestamp,
                        "action_type": e.action_type,
                        "library": e.library,
                        "title": e.title,
                        "server_name": e.server_name,
                    }
                    for e in self.activity
                ],
                "completed": self.completed,
                "skipped": self.skipped,
                "failed": self.failed,
                "guid_hits": self.guid_hits,
                "filepath_hits": self.filepath_hits,
                "suffix_hits": self.suffix_hits,
                "fuzzy_hits": self.fuzzy_hits,
                "unresolved": self.unresolved,
                # Per-tier counters used by the run summary and the
                # Dashboard's fuzzy-match warning banner. dict is a
                # shallow copy so mutations on the live dashboard
                # don't bleed into already-serialized frames.
                "tier_counts": dict(self.tier_counts),
                # Rule 4: per-container import summary. Each entry has
                # {name, library, user_handle, total, restored, skipped,
                # skipped_items[], smart, reason}. Empty until the
                # importer records a result; never present on snapshot
                # / direct-transfer runs.
                "container_summary": {
                    "playlists": [dict(p) for p in self.container_summary.get("playlists", [])],
                    "collections": [dict(c) for c in self.container_summary.get("collections", [])],
                },
                # ── Run-coverage (new) ───────────────────────────────
                "home_user_count": self.home_user_count,
                "watch_count": self.watch_count,
                "playlist_count": self.playlist_count,
                "collection_count": self.collection_count,
                "rating_count": self.rating_count,
                # ── Currently-processing (new) ───────────────────────
                "current_items": [
                    {
                        "library": ci.library,
                        "type": ci.item_type,
                        "title": ci.title,
                        "started_at": ci.started_at,
                        "phase": ci.phase,
                        # Spec Section 4.5: phase age clock for the
                        # frontend's per-phase stall colour escalation.
                        "phase_started_at": ci.phase_started_at,
                    }
                    for ci in self.current_items.values()
                ],
                # Timing engine value (discover-don't-predict). The
                # single headline ETR, populated only once the rolling
                # tracker has real throughput samples; ``None`` until
                # then (frontend renders "Calculating..."). Per-batch
                # ETRs feed the Process List panel.
                "rolling_etr_seconds": self.rolling_etr_seconds,
                "batch_etrs": {k: dict(v) for k, v in self.batch_etrs.items()},
                "threads": dict(self._threads),
                "paused": self.paused,
                "start_time": self.start_time,
                "log_dir": self.log_dir,
                "now": now,
                # Run-level finalize phase (Part B). ``None`` unless the
                # job runner is in the post-engine close-out window.
                "finalizing": self.finalizing,
                # ── Header context (v0.9.6) ──────────────────────────
                "current_user": self.current_user,
                "user_display_names": dict(self.user_display_names),
                # ── HTTP telemetry (v0.9.6, Feature 2) ───────────────
                # Cumulative + per-library status code histograms.
                # JSON keys are stringified to keep the on-wire shape
                # consistent (Python int keys would otherwise survive
                # but the frontend Record<string, ...> typing expects
                # strings).
                "http_status_counts": {
                    lib: {str(code): n for code, n in codes.items()}
                    for lib, codes in self._http_status_counts.items()
                },
                # Last-60-seconds rate + average latency, bucketed per
                # second and per library. Built inside the lock so the
                # numbers don't tear under concurrent record_http_response
                # calls. The frontend filters by library based on the
                # 2A/2B toggle.
                "http_latency_series": _aggregate_http_series(
                    self._http_recent, now, window_seconds=60,
                ),
                "http_rate_limits": {
                    "count": self._http_rate_limit_count,
                    "retries": self._http_retry_count,
                    "backing_off": self._http_backoff_active,
                },
                "rate_limit_events": [
                    {
                        "timestamp": e.timestamp,
                        "library": e.library,
                        "status_code": e.status_code,
                        "retry_after_seconds": e.retry_after_seconds,
                        "detail": e.detail,
                    }
                    for e in self._rate_limit_events
                ],
            }


# ── HTTP telemetry aggregation (v0.9.6) ──────────────────────────────────────

def _aggregate_http_series(
    recent: "Deque[Tuple[float, float, str, int]]",
    now: float,
    *,
    window_seconds: int = 60,
) -> Dict[str, List[Dict[str, Any]]]:
    """
    Collapse the rolling 500-entry response deque into the wire shape
    the Network panel renders: per-library buckets, one per second
    over the last ``window_seconds``, each bucket carrying
    ``{t, rps, avg_ms}``.

    Keeps the WS payload small (60 buckets × ~K libraries instead of
    500 raw events) and predictable in size regardless of traffic
    volume. Always returns the cumulative ``"__all__"`` series so the
    frontend can render the default cumulative view without a library
    selection.
    """
    cutoff = now - window_seconds
    # buckets[lib][second_index] = (count, total_elapsed_ms)
    buckets: Dict[str, Dict[int, Tuple[int, float]]] = {"__all__": {}}
    for ts, elapsed_ms, lib_key, _status in recent:
        if ts < cutoff:
            continue
        sec_idx = int(ts - cutoff)  # 0..window_seconds-1
        for key in (lib_key, "__all__"):
            lib_bucket = buckets.setdefault(key, {})
            cur_count, cur_total = lib_bucket.get(sec_idx, (0, 0.0))
            lib_bucket[sec_idx] = (cur_count + 1, cur_total + elapsed_ms)

    out: Dict[str, List[Dict[str, Any]]] = {}
    for lib, sec_map in buckets.items():
        series: List[Dict[str, Any]] = []
        for sec_idx in range(window_seconds):
            count, total_ms = sec_map.get(sec_idx, (0, 0.0))
            # v0.9.7 Item 1: empty buckets emit ``avg_ms = None`` so
            # the frontend's ``spanGaps: true`` line chart draws a
            # gap instead of dropping to the x-axis. RPS stays at 0.0
            # for empty buckets - zero traffic is meaningful data,
            # not a gap.
            avg_ms: Optional[float] = (total_ms / count) if count else None
            series.append({
                # Absolute UNIX timestamp of the bucket centre so the
                # frontend can scroll smoothly without needing to know
                # the server clock skew.
                "t": cutoff + sec_idx,
                "rps": float(count),
                "avg_ms": avg_ms,
            })
        out[lib] = series
    return out


# ── Thread Category Context Manager ──────────────────────────────────────────

@contextlib.contextmanager
def _thread_category(category: str):
    """
    Registers the calling thread's work category in the dashboard.

    The Thread Pool panel shows how many threads are in each category
    (Play Count, Ratings, Scan Cache, etc.). Wrapping worker work with
    this context manager keeps the tracking out of worker function bodies.
    """
    if state.get_dashboard():
        state.get_dashboard().register_thread(category)
    try:
        yield
    finally:
        if state.get_dashboard():
            state.get_dashboard().unregister_thread()


@contextlib.contextmanager
def _current_item(library: str, item_type: str, title: str, phase: str = ""):
    """
    Mark the calling thread as actively processing one item.

    Wrap the per-item work (resolve + write) so the dashboard's
    "Currently Processing" panel can show what each worker is doing
    right now. Cheap (one lock acquire on entry, one on exit); the
    dashboard reads at 4 Hz so transient items still appear.

    ``phase`` is a short verb describing the kind of work in flight
    - see :class:`CurrentItem` for the canonical strings.

    No-op when no dashboard is attached (CLI fallback / tests).
    """
    if state.get_dashboard():
        state.get_dashboard().set_current_item(library, item_type, title, phase)
    try:
        yield
    finally:
        if state.get_dashboard():
            state.get_dashboard().clear_current_item()


# ── Dashboard Utilities ───────────────────────────────────────────────────────

def _fmt_duration(seconds: float) -> str:
    """Formats elapsed seconds as M:SS or H:MM:SS (e.g. '3:07' or '1:02:45')."""
    s = max(0, int(seconds))
    h, rem = divmod(s, 3600)
    m, sc = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{sc:02d}"
    return f"{m}:{sc:02d}"


def _check_terminal_size() -> bool:
    """
    Returns True if the terminal is too small for the full dashboard.

    The threshold is 80 columns × 22 rows - below that we fall back to the
    simple Rich Progress bars used in v0.4.0. Also returns True when stdout
    is not a TTY (piped output), since Live mode doesn't make sense there.
    """
    if not sys.stdout.isatty():
        return True
    try:
        cols, rows = os.get_terminal_size()
        return cols < 80 or rows < 22
    except OSError:
        return True


def _open_log_folder(log_dir: str) -> None:
    """Opens the log directory in the OS file manager (best-effort, silent on failure)."""
    try:
        target = os.path.abspath(log_dir)
        if sys.platform == "win32":
            subprocess.Popen(["explorer", target])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", target])
        else:
            subprocess.Popen(["xdg-open", target])
    except Exception:
        pass


def _open_plex_server() -> None:
    """Opens the connected Plex server in the default browser, auto-logged in via token."""
    try:
        base = (state._plex_base_url or f"http://localhost:{PLEX_PORT}").rstrip("/")
        if state._plex_token:
            url = f"{base}/web/index.html?X-Plex-Token={state._plex_token}"
        else:
            url = base
        webbrowser.open(url)
    except Exception:
        pass


def _make_progress() -> Progress:
    """
    Builds the Rich Progress instance used for all snapshot and import bars.

    Layout per row:
        [spinner] [library name, 25 chars] [bar, 22 wide] [N/M] [phase label, 22 chars] [ETA]
    """
    return Progress(
        SpinnerColumn(),
        TextColumn("[bold blue]{task.description:<25}"),
        BarColumn(bar_width=22),
        MofNCompleteColumn(),
        TextColumn("[dim]{task.fields[phase]:<22}"),
        TimeRemainingColumn(),
        console=console,
        refresh_per_second=8,
    )


# ── Dashboard Rendering ───────────────────────────────────────────────────────

_ACTION_COLORS: Dict[str, str] = {
    "merged":      "green",
    "created":     "green",
    "appended":    "cyan",
    "skipped":     "dim",
    "rating_set":  "yellow",
    "failed":      "red",
    "unresolved":  "bold red",
    "phase":       "blue",
    "started":     "white",
    "done":        "bold green",
    "error":       "bold red",
}

_ACTION_LABELS: Dict[str, str] = {
    "merged":      "MERGED",
    "created":     "CREATED",
    "appended":    "APPENDED",
    "skipped":     "SKIPPED",
    "rating_set":  "RATED",
    "failed":      "FAILED",
    "unresolved":  "UNRESOLVED",
    "phase":       "PHASE",
    "started":     "STARTED",
    "done":        "DONE",
    "error":       "ERROR",
}

_THREAD_CATEGORIES: Dict[str, str] = {
    "watched":       "Watched",
    "play_count":    "Play Count",
    "playlists":     "Playlists",
    "collections":   "Collections",
    "ratings":       "Ratings",
    "scan_cache":    "Scan Cache",
    "home_user":     "Home User",
    "snapshot":        "Capturing snapshot",
}


def _mini_bar(completed: int, total: int, width: int = 20) -> str:
    """Returns a fixed-width ASCII progress bar string: '████░░░░░░░░░░░░░░░░'."""
    if total <= 0:
        return "░" * width
    filled = int(min(1.0, completed / total) * width)
    return "█" * filled + "░" * (width - filled)


def _build_dashboard(snap: Dict[str, Any], mode: str = "IMPORT") -> Panel:
    """
    Renders the full terminal dashboard from a DashboardState snapshot.

    Returns a Rich Panel containing structured Text. The Panel is passed to
    Live.update() on each display refresh. All column widths are fixed so the
    panel does not shift between frames.
    """
    now_str = datetime.now().strftime("%H:%M:%S")
    elapsed = snap["now"] - snap["start_time"]
    elapsed_str = _fmt_duration(elapsed)
    paused_tag = "  [PAUSED]" if snap["paused"] else ""

    body = Text()

    # ── Header ─────────────────────────────────────────────────────────────────
    body.append(
        f" PlexMigrate v{VERSION}  ·  {mode}  ·  {now_str}  ·  Elapsed {elapsed_str}{paused_tag}\n",
        style="bold",
    )

    # ── Thread Pool summary ────────────────────────────────────────────────────
    cats: Dict[str, int] = {}
    for cat in snap["threads"].values():
        label = _THREAD_CATEGORIES.get(cat, cat)
        cats[label] = cats.get(label, 0) + 1
    thread_str = "  ".join(f"{lbl} ×{n}" for lbl, n in sorted(cats.items())) if cats else "-"
    body.append(" THREADS   ", style="bold dim")
    body.append(thread_str + "\n", style="dim")

    # ── Run stats ──────────────────────────────────────────────────────────────
    body.append(" STATS     ", style="bold dim")
    body.append(
        f"Completed: {snap['completed']:,}  Skipped: {snap['skipped']:,}  "
        f"Failed: {snap['failed']:,}  Unresolved: {snap['unresolved']:,}\n",
        style="dim",
    )

    # ── Match resolution breakdown ─────────────────────────────────────────────
    body.append(" MATCH     ", style="bold dim")
    body.append(
        f"GUID: {snap['guid_hits']:,}  Filepath: {snap['filepath_hits']:,}  "
        f"Suffix: {snap['suffix_hits']:,}  Fuzzy: {snap['fuzzy_hits']:,}\n",
        style="dim",
    )

    # ── Per-library progress bars ──────────────────────────────────────────────
    body.append("─" * 74 + "\n", style="dim")
    body.append(
        f" {'Library':<14}  {'Progress':<20}  {'Items':<10}  {'Phase':<16}  ETA\n",
        style="bold dim",
    )
    for lib in snap["libraries"]:
        name = lib["name"][:14].ljust(14)
        total = lib["total"] or 1
        comp = lib["completed"]
        bar = _mini_bar(comp, total, width=20)
        phase = lib["phase"][:16].ljust(16)
        items_str = f"{comp}/{total} items"

        status = lib["status"]
        if status == "active" and lib["start_time"] > 0:
            elapsed_lib = snap["now"] - lib["start_time"]
            pct = comp / total
            if pct > 0.02 and elapsed_lib > 0:
                eta = elapsed_lib / pct * (1 - pct)
                eta_str = f"ETA {_fmt_duration(eta)}"
            else:
                eta_str = "starting..."
        elif status == "done":
            eta_str = "Done      "
        elif status == "error":
            eta_str = "Error     "
        else:
            eta_str = "Queued    "

        status_style = {
            "done": "green", "error": "red", "active": "white", "queued": "dim",
        }.get(status, "white")
        body.append(
            f" {name}  {bar}  {items_str:<10}  {phase}  {eta_str}\n",
            style=status_style,
        )

    # ── Activity feed ──────────────────────────────────────────────────────────
    body.append("─" * 74 + "\n", style="dim")
    feed = snap["activity"][-4:] if len(snap["activity"]) > 4 else snap["activity"]
    if feed:
        for entry in feed:
            color = _ACTION_COLORS.get(entry["action_type"], "white")
            label = _ACTION_LABELS.get(entry["action_type"], entry["action_type"].upper()).ljust(12)
            lib_short = entry["library"][:10].ljust(10)
            title = entry["title"][:36]
            row = Text()
            row.append(f" {entry['timestamp']}  ", style="dim")
            row.append(label, style=color)
            row.append(f"  {lib_short}  {title}\n")
            body.append_text(row)
    else:
        body.append(" (no activity yet)\n", style="dim")

    # ── Keys strip ─────────────────────────────────────────────────────────────
    body.append("─" * 74 + "\n", style="dim")
    body.append(
        " [Q] Quit   [V] Verbose   [P] Pause/Resume   [L] Open Logs   [S] Open Server   [R] Refresh",
        style="bold dim",
    )

    return Panel(body, border_style="dim", padding=(0, 0))


# ── Keyboard Input Handling ───────────────────────────────────────────────────

def _handle_key(
    key: str,
    log_dir: str,
    logger: Any,
    stop_event: threading.Event,
) -> None:
    """
    Dispatches a single keypress to the appropriate action.

    Key bindings:
        Q - cancel queued work and exit cleanly after running tasks finish
        V - toggle RichHandler console log level between INFO and DEBUG
        P - pause/resume all worker threads at their next checkpoint
        L - open the log directory in the OS file manager
        S - open the connected Plex server in the default web browser
        R - force an immediate dashboard refresh
    """
    k = key.lower()
    if k == "q":
        stop_event.set()
        if state.get_dashboard():
            state.get_dashboard().push_activity("phase", "-", "Stopping (finishing current tasks)…")
    elif k == "v":
        if state._console_handler is not None:
            if state._console_handler.level == logging.DEBUG:
                state._console_handler.setLevel(logging.INFO)
                logger.info("Verbose console logging disabled")
            else:
                state._console_handler.setLevel(logging.DEBUG)
                logger.info("Verbose console logging enabled")
    elif k == "p":
        if state.get_dashboard() is not None:
            state.get_dashboard().toggle_pause()
    elif k == "l":
        _open_log_folder(log_dir)
    elif k == "s":
        _open_plex_server()
    elif k == "r":
        if state._live_instance is not None:
            state._live_instance.refresh()


def _keyboard_thread(
    log_dir: str,
    logger: Any,
    stop_event: threading.Event,
) -> None:
    """
    Background daemon thread that reads keyboard input without blocking the main thread.

    Windows path:
        Uses msvcrt.kbhit() to check for input and msvcrt.getwch() to read one
        wide character without echoing it to the terminal.

    Unix/macOS path:
        Sets the terminal to raw mode (tty.setraw) so characters arrive without
        waiting for Enter, then uses select() with a 50 ms timeout to avoid
        busy-waiting. The original terminal settings are restored in the finally
        block even if the thread is killed by an exception.
    """
    try:
        if sys.platform == "win32":
            import msvcrt
            while not stop_event.is_set():
                if msvcrt.kbhit():
                    ch = msvcrt.getwch()
                    _handle_key(ch, log_dir, logger, stop_event)
                time.sleep(0.05)
        else:
            import tty
            import termios
            import select as _select
            fd = sys.stdin.fileno()
            old_settings = termios.tcgetattr(fd)
            try:
                tty.setraw(fd)
                while not stop_event.is_set():
                    r, _, _ = _select.select([sys.stdin], [], [], 0.05)
                    if r:
                        ch = sys.stdin.read(1)
                        _handle_key(ch, log_dir, logger, stop_event)
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
    except Exception:
        pass


# ── Action Type and Progress Helpers ─────────────────────────────────────────

def _action_type_from_record(record: Dict) -> str:
    """
    Maps a success record's action string to a dashboard activity action_type.

    _record_success() is called from many places with different action strings.
    This helper infers the action_type from the already-present action string
    so no existing call sites need to change.
    """
    action = record.get("action", "")
    if "[CREATED]" in action:
        return "created"
    if "[APPENDED]" in action:
        return "appended"
    if "[SKIPPED" in action or "skipped" in action.lower():
        return "skipped"
    if "[RATING SET]" in action:
        return "rating_set"
    return "merged"


def _advance_lib(lib_name: str) -> None:
    """
    Advances the progress display for lib_name by one step.

    Works in both dashboard mode (_dashboard) and small-terminal fallback mode
    (_live_progress), so import functions only need to call this once instead of
    duplicating the if/elif logic at every progress-advance site.
    """
    if state.get_dashboard():
        state.get_dashboard().advance_library(lib_name)
    elif state._live_progress:
        state._live_progress.update(state._lib_task_ids.get(lib_name), advance=1)
