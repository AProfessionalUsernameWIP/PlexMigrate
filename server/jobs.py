"""
Single-worker job queue that drives the Hestia-MediaManager engine.

The engine writes to global state (``services.state._dashboard``,
``services.state._lib_successes``, etc.) and the dashboard uses
module-level singletons throughout. Running two engine calls in
parallel against the same Python process would corrupt that shared
state, and Plex itself doesn't particularly like a single account
running two parallel migrations against the same server either.

This module enforces *one engine call at a time* by funnelling every
inbound job through a single worker thread. Requests submitted while
the worker is busy are queued FIFO; ``/api/job/stop`` flips the
engine's internal stop_event via :mod:`server.runtime_patches` so the
running job winds down cleanly.

Design notes
------------
* The worker thread is started lazily on first submission rather than
  at module import - that keeps unit tests from leaking a thread.
* The :class:`JobRecord` exposed via the REST endpoint is a plain
  dataclass copied out of the live state under lock, so the consumer
  never sees a torn read.
* Each job calls the same orchestration functions the CLI uses
  (:func:`services.snapshot.plex_native.snapshotter.run_snapshot`, :func:`services.restore.plex_native.run_restore`).
  No engine logic is duplicated here - this file is a *driver*.
"""

from __future__ import annotations

import logging
import queue
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import services.state as state
from services.auth import _make_session
from services.snapshot.plex_native.snapshotter import run_snapshot
from services.restore.plex_native import run_restore
from services.logging_ops import setup_logging

from server import runtime_patches
from services.direct_transfer.engine import run_direct_transfer
from services.fan_out.coordinator import (
    FanOutResult,
    clear_active_result as _clear_fan_out_active,
    run_fan_out_direct,
    run_fan_out_restore,
)
from server.persistence import load_settings
from server.log_scrubber import safe_error, scrub
from server.run_context import _build_logger, _finalize_run, _set_run_timestamp
from server.server_registry import (
    backend_aware_slug,
    connect_registered_server,
    decrypt_server_token,
    get_server_by_name,
    safe_server_name,
)


log = logging.getLogger("plexmigrate.server.jobs")


# ── Job state model ──────────────────────────────────────────────────────────

# Possible values of JobRecord.state. Centralised so other modules
# can import the strings rather than hard-coding magic values.
STATE_QUEUED = "queued"
STATE_RUNNING = "running"
# "stopping" is the intermediate state between the user clicking Stop and
# the engine actually returning. The job stays in this state until the
# current library finishes; see services.snapshot.plex_native.snapshotter.run_snapshot's stop
# semantics. Surfaces to the frontend so the Stop button can re-label
# itself "Stopping…" and disable, giving the user immediate feedback.
STATE_STOPPING = "stopping"
STATE_COMPLETED = "completed"
# Terminal state for jobs that completed the engine work
# successfully (primary data is in media.db; the migration ran end to
# end) but a non-fatal post-engine step failed. The textbook case is
# the snapshot-artifact capture: media.db ingest succeeded, but writing
# the snapshot .db / registering it in snapshots.db raised. Re-running
# is safe (engine work is idempotent). The dashboard treats this as a
# yellow / amber outcome - distinct from green "completed" and from
# red "failed". ``rec.error`` carries the message either way.
STATE_COMPLETED_WITH_ERRORS = "completed_with_errors"
STATE_FAILED = "failed"
STATE_CANCELLED = "cancelled"


@dataclass
class JobRecord:
    """
    One record per submitted job. Held in :class:`JobQueue._history`
    indefinitely until the server restarts (history is in-memory and
    bounded by ``_HISTORY_MAX``).
    """

    job_id: str
    mode: str                                 # "snapshot" or "restore"
    params: Dict[str, Any]
    state: str = STATE_QUEUED
    queued_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    error: Optional[str] = None
    run_log_dir: Optional[str] = None
    # Run summary captured at completion. Currently carries
    # ``tier_counts`` for direct-transfer jobs (and any other run
    # whose resolver fires). Populated in the worker loop's finally
    # block from the active dashboard state. None for jobs that
    # never started or that completed before the summary capture
    # landed in the codebase.
    summary: Optional[Dict[str, Any]] = None
    # Item count for a ``playlist_copy_batch`` job, set once at submit
    # time so ``cancel_batch_item`` can range-check an item index
    # without racing the worker that builds the batch. 0 otherwise.
    batch_items_len: int = 0
    # Per-job cooperative stop flag. The worker creates it when the job
    # starts running; ``request_stop`` sets it. The adapter (Jellyfin /
    # Emby) engines read it via ``getattr(rec, "_stop_event")`` - the
    # Plex-only ``state._active_stop_event`` keyboard-thread path does
    # not cover them. ``repr=False`` keeps it out of log/WS payloads.
    _stop_event: Optional[threading.Event] = field(default=None, repr=False)


_HISTORY_MAX = 50
# Cap on per-item error text shown inline in the activity feed; the
# full message stays available on the per-item drill-down.
_ACTIVITY_MSG_MAX_CHARS = 240


# ── Job queue ────────────────────────────────────────────────────────────────

class JobQueue:
    """
    The single-writer queue. Public surface:

    * :meth:`submit_snapshot` / :meth:`submit_restore` - enqueue a job
      and return its ``JobRecord`` immediately. The actual engine run
      happens on the worker thread.
    * :meth:`current` - the currently running or most recently finished
      job (used by both the REST status endpoint and the WebSocket
      snapshot builder).
    * :meth:`history` - the in-memory job history.
    * :meth:`request_stop` - flip the engine's stop flag so a running
      job winds down at the next worker checkpoint.
    """

    def __init__(self) -> None:
        # Apply the headless patches before any engine code runs.
        # Idempotent, so calling it from the constructor is safe even
        # if app startup already called it.
        runtime_patches.enable_headless_mode()

        # ``queue.Queue`` gives us a thread-safe FIFO + blocking ``get``
        # for the worker loop.
        self._inbox: "queue.Queue[JobRecord]" = queue.Queue()

        # The most recent JobRecord (running or finished). Read by the
        # WebSocket broadcaster, so it's guarded with a lock for
        # consistent multi-field reads.
        self._current: Optional[JobRecord] = None
        self._history: List[JobRecord] = []
        # Queued jobs awaiting the worker. Kept alongside the
        # internal queue.Queue so the WS broadcaster can list them
        # without poking the queue's private deque. Mutated only
        # under ``_lock``: append on submit, pop on _worker_loop
        # pulling the next record.
        self._pending: List[JobRecord] = []
        self._lock = threading.Lock()

        # The worker thread is started lazily on first submit().
        self._worker: Optional[threading.Thread] = None
        self._worker_started = False

    # ── Public submission API ────────────────────────────────────────

    def submit_snapshot(self, params: Dict[str, Any]) -> JobRecord:
        return self._submit("snapshot", params)

    def submit_restore(self, params: Dict[str, Any]) -> JobRecord:
        return self._submit("restore", params)

    def submit_direct(self, params: Dict[str, Any]) -> JobRecord:
        """
        Enqueue a server-to-server direct transfer job. Params must
        carry ``source_server_name`` and ``dest_server_name``; the
        rest of the dict mirrors :class:`server.models.DirectTransferIn`.
        """
        return self._submit("direct", params)

    def submit_playlist_copy(self, params: Dict[str, Any]) -> JobRecord:
        """
        Enqueue a single-playlist copy as a job. Params mirror
        :class:`server.models.PlaylistCopyIn`. Routing Playlist Mgmt
        Deploy through the job queue lets deploys persist beyond
        page-reload, surface on the Dashboard's active-jobs strip, and
        support clone-deploy + multi-deploy queueing.

        Lightweight relative to snapshot/restore/direct: the worker
        method skips the engine pre-flight (dashboard placeholder,
        ETR priming, cache warm) since playlist_copy is a one-shot
        item-level operation. Status reads + cancellation come for
        free via the existing queue infrastructure.
        """
        return self._submit("playlist_copy", params)

    def submit_playlist_copy_batch(self, params: Dict[str, Any]) -> JobRecord:
        """Enqueue a batch of N playlist copies as ONE job. Params
        mirror :class:`server.models.PlaylistCopyBatchIn`:

        * ``items`` - list of dicts each carrying PlaylistCopyIn-shaped
          fields (source_server_id / source_user_id / source_playlist_id
          / dest_server_id / dest_user_id / dest_playlist_name).
        * ``parallelism`` - optional override for the worker pool size;
          falls back to ``playlist_mgmt_batch_workers`` when absent.
        * ``label`` - optional operator-supplied label used in the
          activity feed.

        The batch runs as ONE job through the queue's single-worker
        pump; the internal ThreadPoolExecutor inside
        :func:`services.playlist_copy.copy_playlist_batch` provides
        the per-item parallelism. The queue's serial guarantee is
        preserved - only one batch executes at a time.
        """
        return self._submit(
            "playlist_copy_batch", params,
            batch_items_len=len(params.get("items") or []),
        )

    def submit_smart_playlist_migrate(
        self, params: Dict[str, Any],
    ) -> JobRecord:
        """Enqueue a Smart Playlist Migration job. Params mirror
        :class:`server.models.SmartPlaylistMigrateIn`:

        * ``items`` - list of per-playlist dicts, each PlaylistCopyIn
          shaped (source/dest server + playlist + user).

        The handler reads each source smart playlist's filter,
        translates the server-specific tag ids to names, and
        re-creates it: a true smart playlist on a Plex destination, a
        static materialisation on Jellyfin / Emby. Like the playlist
        batch, this is a lightweight item-level job - it skips the
        snapshot/restore engine pre-flight."""
        return self._submit("smart_playlist_migrate", params)

    def _submit(
        self, mode: str, params: Dict[str, Any], *, batch_items_len: int = 0,
    ) -> JobRecord:
        rec = JobRecord(
            job_id=str(uuid.uuid4()), mode=mode, params=dict(params),
            batch_items_len=batch_items_len,
        )
        # Track the rec on the public pending list before queuing
        # it. The order is important - list-then-queue ensures the WS
        # broadcaster never sees the worker pulling a rec that isn't
        # yet on the pending list.
        with self._lock:
            self._pending.append(rec)
        self._inbox.put(rec)
        self._ensure_worker()
        return rec

    # ── Public read API ──────────────────────────────────────────────

    def current(self) -> Optional[JobRecord]:
        with self._lock:
            return self._current

    def history(self) -> List[JobRecord]:
        with self._lock:
            return list(self._history)

    def pending(self) -> List[JobRecord]:
        """
        Snapshot of every queued JobRecord that hasn't started yet.

        Excludes the currently-running record; combine with
        :meth:`current` to get the full active+queued list for the
        Dashboard tab's multi-job sub-tab strip.
        """
        with self._lock:
            return list(self._pending)

    def active_and_queued(self) -> List[JobRecord]:
        """
        Convenience snapshot used by the WS broadcaster. The
        running job (if any) comes first, followed by every queued
        record in FIFO order. Empty list when the worker is idle and
        nothing is queued - the WS payload then omits the sub-tab strip.
        """
        with self._lock:
            out: List[JobRecord] = []
            if self._current is not None and self._current.state in (
                STATE_QUEUED, STATE_RUNNING, STATE_STOPPING,
            ):
                out.append(self._current)
            out.extend(self._pending)
            return out

    def busy(self) -> bool:
        with self._lock:
            return self._current is not None and self._current.state == STATE_RUNNING

    def request_stop(self, *, hard: bool = False) -> bool:
        """
        Stop the active job AND clear every queued job.

        Both Stop (soft) and Hard Stop clear the pending queue: each
        queued job is marked CANCELLED and moved to history, and the
        worker loop skips any inbox record whose state is already
        CANCELLED when it pulls it. The soft/hard distinction only
        governs how the *running* job is stopped:

        Soft (``hard=False``, default): flips the running JobRecord to
        STATE_STOPPING and sets the engine's stop_event. The engine's
        gather loops check it at item-level checkpoints, so the run
        halts promptly rather than at the next library boundary. The
        worker loop then ends the job as CANCELLED.

        Hard (``hard=True``): same flip, plus tears down the shared HTTP
        session so every in-flight Plex request fails immediately and
        the run collapses within seconds. Items in flight land in the
        per-library failure log; the job still ends CANCELLED.

        Returns True if anything was stopped or cleared, False only
        when there was no running job and nothing queued.
        """
        with self._lock:
            # ── Clear the queue ──────────────────────────────────────
            # Mark every pending job CANCELLED and move it to history.
            # The records are still sitting in ``_inbox``; we do NOT
            # drain the Queue here (that would race the worker's
            # ``_inbox.get()``). Instead the worker loop skips any rec
            # whose state is already CANCELLED when it pulls one.
            now = time.time()
            cleared = len(self._pending)
            for prec in self._pending:
                prec.state = STATE_CANCELLED
                prec.finished_at = now
                self._history.append(prec)
            self._pending.clear()
            # Keep history bounded to the same cap _record_history
            # enforces - clearing a large queue must not grow it past
            # _HISTORY_MAX.
            if len(self._history) > _HISTORY_MAX:
                del self._history[: len(self._history) - _HISTORY_MAX]

            # ── Stop the running job ─────────────────────────────────
            # M8: the check-and-transition is atomic under the lock so
            # we never fire the engine signal for a job that finished
            # in a gap. ``transitioned`` is True only when *this* call
            # actually moved a RUNNING job to STOPPING.
            transitioned = (
                self._current is not None
                and self._current.state == STATE_RUNNING
            )
            if transitioned:
                self._current.state = STATE_STOPPING
                # Set the per-job stop flag the adapter (Jellyfin/Emby)
                # engines poll. The Plex engines have the separate
                # keyboard-thread ``state._active_stop_event`` path and
                # ``runtime_patches.signal_stop()`` below; the adapter
                # engines have neither, so without this a Stop on a
                # Jellyfin/Emby job would never reach the engine.
                _ev = getattr(self._current, "_stop_event", None)
                if _ev is not None:
                    _ev.set()
                if hard:
                    # Annotate so the WS payload + history surface the
                    # hard-stop cause distinctly from a clean Stop.
                    self._current.error = (
                        "Hard stop requested - HTTP session was torn down; "
                        "in-flight items recorded as failures."
                    )
        # Fire the engine-level signal outside the lock.
        if transitioned:
            if hard:
                runtime_patches.signal_hard_stop()
            else:
                runtime_patches.signal_stop()
        return transitioned or cleared > 0

    def cancel_batch_item(self, job_id: str, item_index: int) -> str:
        """Cancel ONE item inside a running playlist_copy_batch job.

        The end user can cancel a single mistakenly-selected playlist
        without nuking the whole batch. The worker plumbs a per-item
        cancel_event dict onto the JobRecord at start time; this method
        finds the right event and sets it.

        Returns a status string the REST handler maps to HTTP:
          * ``"cancelled"`` - event fired (item will short-circuit at
            its next phase boundary OR was pre-start and dropped
            without doing work)
          * ``"not_running"`` — job_id matches but the job has
            finished; nothing to cancel
          * ``"not_batch"`` — job_id matches but the job mode is
            single-copy / snapshot / etc.; the per-item surface only
            applies to batches
          * ``"index_out_of_range"`` — index is negative or >= len(items)
          * ``"not_found"`` — no job with this id (or not the current
            job; pending jobs haven't started so per-item events
            don't exist yet)
        """
        with self._lock:
            cur = self._current
            if cur is None or cur.job_id != job_id:
                return "not_found"
            if cur.state not in (STATE_RUNNING, STATE_STOPPING):
                return "not_running"
            if cur.mode != "playlist_copy_batch":
                return "not_batch"
            events: Optional[Dict[int, threading.Event]] = getattr(
                cur, "_per_item_cancel_events", None,
            )
            items_len = cur.batch_items_len
            if items_len and (item_index < 0 or item_index >= items_len):
                return "index_out_of_range"
            if events is None:
                # Worker hasn't reached the batch-init stage yet; create
                # the dict eagerly so the event-set sticks. The worker
                # will read from this same dict when it starts the
                # ThreadPoolExecutor.
                events = {}
                cur._per_item_cancel_events = events  # type: ignore[attr-defined]
            evt = events.get(item_index)
            if evt is None:
                evt = threading.Event()
                events[item_index] = evt
            evt.set()
            return "cancelled"

    # ── Worker thread ────────────────────────────────────────────────

    def _ensure_worker(self) -> None:
        # Start exactly one worker thread for the life of the process.
        # The check-and-set runs under ``self._lock`` - without it, two
        # callers racing through ``_ensure_worker`` could both see
        # ``_worker_started`` False and each start a worker, defeating
        # the single-writer invariant the whole queue depends on.
        with self._lock:
            if self._worker_started:
                return
            self._worker_started = True
            self._worker = threading.Thread(
                target=self._worker_loop,
                name="plexmigrate-job-worker",
                daemon=True,
            )
            self._worker.start()

    def _worker_loop(self) -> None:
        """
        Pull jobs off the inbox one at a time and run them.

        Any exception raised by the engine is caught here so the
        worker thread itself never dies - a single bad job should
        not block the rest of the queue.
        """
        # Imported lazily so importing this module without the engine
        # installed (e.g. in some unit tests) still works.
        from services.dashboard import DashboardState

        while True:
            rec = self._inbox.get()
            with self._lock:
                # Stop P1: a job cancelled while it was still queued
                # (request_stop cleared the queue) is still sitting in
                # the inbox - request_stop marked it CANCELLED and
                # moved it to history already, so just drop it without
                # running. ``_pending.remove`` is best-effort: the
                # clear() in request_stop already emptied the list.
                if rec.state == STATE_CANCELLED:
                    try:
                        self._pending.remove(rec)
                    except ValueError:
                        pass
                    continue
                # Pop this record from the pending list now that
                # it has been claimed. We match by identity (``is``) so
                # there's never any ambiguity even if two queued jobs
                # have identical params.
                try:
                    self._pending.remove(rec)
                except ValueError:  # pragma: no cover (defensive)
                    # Should not happen - _submit appends before
                    # putting on the queue - but never let a missing
                    # pending entry block the worker loop.
                    pass
                self._current = rec
                rec.state = STATE_RUNNING
                rec.started_at = time.time()
                # Per-job stop flag. The adapter (Jellyfin/Emby)
                # engines honour ``rec._stop_event``; ``request_stop``
                # sets it. Created here so it exists for the whole run.
                rec._stop_event = threading.Event()

            # ── Pre-flight: clear any leftover fan-out registry ──────
            # If the previous job was a fan-out, ``services.fan_out.coordinator``'s
            # ``_active_result`` may still be set (it clears on its
            # own 8 s grace timer for the post-completion display).
            # Wiping it immediately at the start of THIS job so the WS
            # payload returns to ``fan_out: null`` even if THIS job is
            # a single-destination run that lands inside the grace.
            try:
                _clear_fan_out_active()
            except Exception:  # pragma: no cover (defensive)
                pass

            # ── Pre-flight DashboardState ─────────────────────────────
            # The engine's slow start-up (Plex connect → home-user auth
            # → playlist cache warm) can take 10–60 s on a large server
            # before run_snapshot / run_restore construct their own
            # DashboardState. Without a placeholder, the WS payload
            # broadcasts ``dashboard: null`` during that window and the
            # browser shows "No job is running" misleadingly. We create
            # an empty DashboardState here so the panel paints
            # immediately, and the engine augments it (rather than
            # replacing it) once pre-flight finishes.
            try:
                state._dashboard = DashboardState(log_dir="")
                state.get_dashboard().push_activity(
                    "started", "-",
                    f"{rec.mode.upper()} job initialising…",
                )
            except Exception:  # pragma: no cover (defensive)
                pass

            # Feature 1 phase 1.2: bracket the job in a run_timer
            # session so every time_operation block inside the engine
            # records into the same buffer, and the buffer flushes to
            # run_timings.db once the job finishes (in the finally
            # block below). The run_id is the buffer key; we use the
            # auto-generated form which embeds a wall-clock timestamp
            # so chronological sort of timings matches job history.
            from services.run_logs import run_timer
            from services.dashboard import job_http_context
            job_run_id = run_timer.start_run()
            try:
                # One synthetic top-level entry per job so list_recent_runs
                # can report wall-clock duration without scanning every
                # entry. The inner with-block measures the full engine
                # invocation; sub-phase entries land underneath it.
                # job_http_context tags every HTTP
                # response captured by the auth.py hook with rec.job_id,
                # so the Network panel's per-job timeline (/api/network/
                # recent-requests?job_id=...) catches every engine's
                # requests including snapshot/restore/direct/fan-out.
                with run_timer.time_operation(
                    f"job:{rec.mode}",
                    scope=run_timer.SCOPE_RUN,
                ) as _job_t, job_http_context(rec.job_id):
                    if rec.mode == "snapshot":
                        self._run_snapshot(rec)
                    elif rec.mode == "restore":
                        self._run_restore(rec)
                    elif rec.mode == "direct":
                        self._run_direct(rec)
                    elif rec.mode == "playlist_copy":
                        self._run_playlist_copy(rec)
                    elif rec.mode == "playlist_copy_batch":
                        self._run_playlist_copy_batch(rec)
                    elif rec.mode == "smart_playlist_migrate":
                        self._run_smart_playlist_migrate(rec)
                    else:
                        raise ValueError(f"Unknown job mode {rec.mode!r}")
                    _job_t["extra"]["job_id"] = rec.job_id
                # Stop P2: if request_stop flipped this job to STOPPING
                # (soft Stop), the engine returned because the
                # stop_event was set at an item-level checkpoint - that
                # is a CANCELLED job, not a COMPLETED one.
                #
                # Some post-engine helpers (the auto-capture safety
                # belt) swallow their own exceptions and stamp
                # ``rec.error`` instead of re-raising - the engine
                # work succeeded so the rest of the job shouldn't be
                # classified as ``failed``. An error-stamped job is
                # not a clean ``completed`` either, so route it to
                # ``completed_with_errors`` for the amber chip +
                # error message without claiming green success.
                if rec.state == STATE_STOPPING:
                    rec.state = STATE_CANCELLED
                elif rec.error:
                    rec.state = STATE_COMPLETED_WITH_ERRORS
                else:
                    rec.state = STATE_COMPLETED
            except _JobCancelled:
                rec.state = STATE_CANCELLED
            except Exception as exc:
                # Stop P2: a hard stop tears down the HTTP session, so
                # the engine usually exits via an exception. If a stop
                # was requested (state is STOPPING), that's a CANCELLED
                # job - not a FAILED one.
                if rec.state == STATE_STOPPING:
                    rec.state = STATE_CANCELLED
                else:
                    rec.state = STATE_FAILED
                    rec.error = safe_error(exc)
                    # v0.9.5: route the traceback through the standard
                    # logging framework so the X-Plex-Token scrubber
                    # (installed on every handler) can redact any
                    # token-bearing URLs before they hit disk or stdout.
                    # The previous ``traceback.print_exc()`` wrote
                    # straight to stderr and bypassed the scrubber.
                    log.error(
                        "Job worker caught unhandled exception",
                        exc_info=True,
                    )
            finally:
                rec.finished_at = time.time()
                # Capture the run summary from whatever dashboard is
                # still attached (single-job: state._dashboard;
                # fan-out: each destination's dashboard contributes
                # to its own subtab and the top-level summary stays
                # None - the fan-out array is the surface there).
                try:
                    dash = state.get_dashboard()
                    if dash is not None:
                        rec.summary = {
                            "tier_counts": dict(dash.tier_counts),
                            # Rule 4: per-container restoration summary.
                            # Both lists are present (possibly empty)
                            # for every run so the frontend can branch
                            # on length rather than truthiness.
                            "container_summary": {
                                "playlists": [
                                    dict(p) for p in
                                    dash.container_summary.get("playlists", [])
                                ],
                                "collections": [
                                    dict(c) for c in
                                    dash.container_summary.get("collections", [])
                                ],
                            },
                            # Timing facts captured at completion. No
                            # estimate fields - the discover-don't-
                            # predict model has no pre-run baseline to
                            # compare against. Just the wall-clock
                            # facts the Job History view renders.
                            "timing": {
                                "started_at": rec.started_at,
                                "finished_at": rec.finished_at,
                                "actual_seconds": (
                                    (rec.finished_at - rec.started_at)
                                    if (rec.finished_at and rec.started_at)
                                    else None
                                ),
                            },
                        }
                        # One-line run summary to the engine log so the
                        # end user can see the wall-clock duration
                        # without opening the Job History panel.
                        if rec.started_at and rec.finished_at:
                            log.info(
                                "Run %s finished: %.0fs wall-clock "
                                "(%d watched, %d rated, %d playlists, "
                                "%d collections processed).",
                                rec.job_id,
                                max(0.0, rec.finished_at - rec.started_at),
                                int(getattr(dash, "watch_count", 0) or 0),
                                int(getattr(dash, "rating_count", 0) or 0),
                                int(getattr(dash, "playlist_count", 0) or 0),
                                int(getattr(dash, "collection_count", 0) or 0),
                            )
                except Exception:  # pragma: no cover (defensive)
                    pass
                # The worker, not the engine, nulls state._dashboard -
                # nulling it inside the engine's finally block killed
                # the dashboard before the post-engine "Finalizing ..."
                # labels could reach the WebSocket. The worker owns the
                # cleanup, here at the very end after summary capture
                # is complete. Fan-out path uses its own delayed
                # cleanup via _finalise_fan_out_state; the unconditional
                # null here is the single-job equivalent.
                try:
                    state._dashboard = None
                except Exception:
                    pass
                # Feature 1 phase 1.2: flush the run_timer buffer to
                # run_timings.db. Persist=True triggers retention
                # enforcement automatically. Errors here are logged
                # but never re-raised; a telemetry write failure must
                # not poison the job history record.
                try:
                    flushed = run_timer.end_run(persist=True)
                    if rec.summary is not None:
                        rec.summary["run_timing_id"] = job_run_id
                        rec.summary["run_timing_entries"] = len(flushed)
                except Exception:  # pragma: no cover (defensive)
                    log.exception("run_timer flush failed for job %s", rec.job_id)
                    flushed = []
                # Insert a single per-run row into run_history. The
                # dashboard and log views read it. Best-effort; failures
                # are logged inside record_run_history and never
                # re-raised. Reset the affected-users seam for the
                # next run regardless.
                try:
                    _persist_run_history_row(rec, job_run_id)
                except Exception:  # pragma: no cover (defensive)
                    log.exception("run_history persist failed for job %s", rec.job_id)
                finally:
                    try:
                        state._restoration_log_affected_users = []
                    except Exception:
                        pass
                self._record_history(rec)

    def _record_history(self, rec: JobRecord) -> None:
        with self._lock:
            self._history.append(rec)
            if len(self._history) > _HISTORY_MAX:
                # Trim the oldest entries first.
                del self._history[: len(self._history) - _HISTORY_MAX]

    # ── Engine invocation: snapshot ────────────────────────────────────

    def _run_snapshot(self, rec: JobRecord) -> None:
        """
        Build a fresh logger + Plex connection, then call run_snapshot().

        Multi-server (v0.9.0): the connection is resolved from
        ``source_server_name`` against the registered server list.
        For backward compatibility with ad-hoc CLI calls, raw
        ``plex_url`` + ``plex_token`` in the params still work.
        ``state._run_timestamp`` is prefixed with the server's
        filename-safe slug so log dirs and snapshot filenames don't
        collide between servers.
        """
        settings = _merge_settings(rec.params, mode="snapshot")

        # Resolve the connection. Either a registered server name
        # was supplied (preferred path) or a raw URL+token pair.
        server, url, token, owner, server_slug = _resolve_source_connection(
            settings, logger=logging.getLogger("plexmigrate")
        )
        settings["plex_url"] = url
        settings["plex_token"] = token
        settings["resolved_server_slug"] = server_slug
        rec.params = {k: v for k, v in settings.items() if k != "plex_token"}

        # Resolve the requested library list BEFORE we stamp
        # the run timestamp so the run-dir name can include the
        # library names (`run_Plex1_Movies_TV-Shows_20260510_135425/`).
        # The full strict resolution / mismatch error happens further
        # down once the engine entrypoint takes over - this pass is
        # just informational for the slug.
        _wanted_libs = sorted({str(n) for n in (settings.get("libraries") or [])})
        _set_run_timestamp(server_slug, libraries=_wanted_libs)
        logger, run_log_dir = _build_logger(settings["log_dir"], settings["verbose"])
        rec.run_log_dir = run_log_dir
        _log_pin_preflight_ack(rec, logger)
        _dump_run_settings("snapshot", run_log_dir, rec.params, logger)
        state.MAX_WORKERS = int(settings["workers"])
        state.SCROBBLE_WORKERS = int(settings["scrobble_workers"])
        state._session = _make_session()
        state._plex_base_url = url
        state._plex_token = token
        state._plex_owner_name = owner
        _populate_run_user_context(server, source_name=settings.get("source_server_name"))
        # Run-trigger labels: stamped into the snapshot JSON so the
        # Snapshots tab can show how each export was initiated. Defaults
        # to "manual" when the API call carries no explicit marker
        # (covers any future caller that forgets to set it).
        state._run_trigger = str(settings.get("_trigger") or "manual")
        state._run_schedule_name = str(settings.get("_schedule_name") or "")
        # Publish the registered server's id into
        # state so the engine's per-library payload can ingest
        # straight into media.db. ``_resolve_server_id`` walks
        # server_registry to map ``source_server_name`` -> id; the
        # snapshot job hard-fails below if no row matches (engine
        # is media.db-primary and refuses to run without a
        # registered server).
        #
        # Pass prefer_id + service_type so a name collision across
        # backends (e.g.
        # "Jade-TV" registered both on Plex and on Jellyfin) does
        # not land on the wrong row. Without the id + service_type
        # disambiguation the dispatch below could read a
        # non-"plex" service_type from the wrong row and route a
        # Plex snapshot to ``_run_snapshot_via_adapter`` (which
        # then calls ``adapter.server_identity()`` on a plexapi
        # PlexServer and AttributeError-crashes).
        snapshot_server_id = _resolve_server_id(
            settings.get("source_server_name"),
            prefer_id=settings.get("source_server_id"),
            service_type=settings.get("source_service_type"),
        )
        if not snapshot_server_id:
            raise ValueError(
                f"Snapshot requires a registered server. {settings.get('source_server_name')!r} "
                "is not in the registry - register it under the Servers tab first."
            )
        state._snapshot_server_id = snapshot_server_id
        # Defensive last-line guarantee: the post-job snapshot capture
        # hits ``SELECT id FROM servers WHERE id = ?`` against media.db
        # and raises if the row is missing. ``add_server`` /
        # ``update_server`` + the startup backfill cover this on healthy
        # installs; the upsert here also handles the case where the
        # end user added a server to the registry between server-boot
        # and the first snapshot for it.
        src_row: Dict[str, Any] = {}
        try:
            from server import media_db, server_registry
            src_row = server_registry.get_server_by_id(snapshot_server_id, include_token=False) or {}
            media_db.upsert_server_row(
                server_id=snapshot_server_id,
                name=src_row.get("name") or settings.get("source_server_name") or "",
                url=src_row.get("url") or url,
                # Reflect the real backend rather than assuming Plex.
                service=(src_row.get("service_type") or "plex").lower(),
                machine_id=(src_row.get("machine_identifier") or None) or None,
            )
        except Exception:
            logging.getLogger("plexmigrate.server.jobs").exception(
                "Pre-snapshot media.db.servers upsert failed for %r; capture may fail.",
                snapshot_server_id,
            )

        # Non-Plex backends route through the adapter engine. Plex
        # stays on the existing plexapi-driven path so its perf-tuned
        # code paths are untouched. The branch
        # happens here, immediately before any plexapi-specific
        # operation (the section enumeration below would 404 on a
        # JellyfinAdapter / EmbyAdapter).
        _src_row_for_dispatch = src_row  # captured above for the media.db upsert
        _service_type = (
            (_src_row_for_dispatch.get("service_type") if _src_row_for_dispatch else None)
            or "plex"
        ).lower()
        # Defensive guard. If
        # ``server`` is a plexapi PlexServer (i.e. the connection
        # resolution returned a Plex handle) but the registry's
        # service_type says non-plex, treat that as misconfigured
        # state rather than crashing inside snapshotter_adapter on
        # ``adapter.server_identity()``. The plexapi class name is
        # the simplest non-Plex-import-required signal we have at
        # this layer.
        _server_cls_name = type(server).__name__
        if _service_type != "plex" and _server_cls_name == "PlexServer":
            raise ValueError(
                f"Snapshot dispatch refusing to run: server_id "
                f"{snapshot_server_id!r} resolved to a plexapi "
                f"PlexServer but its registry row has "
                f"service_type={_service_type!r}. Most likely the "
                f"server's service_type column is wrong; update it "
                f"via the Servers panel to match the actual backend."
            )
        if _service_type != "plex":
            # Pull the canonical MediaServerAdapter from the
            # server_registry cache (already built by
            # _resolve_source_connection above). For J/E backends
            # ``conn.server == conn.adapter`` so this is also a
            # belt-and-braces lookup; for Plex it would catch the
            # bug above where ``server`` is not the adapter.
            from server import server_registry as _sr
            try:
                _conn = _sr.connect_registered_server(
                    snapshot_server_id, logger,
                )
                _adapter_for_dispatch = _conn.adapter
            except Exception:
                logger.exception(
                    "Snapshot dispatch: could not resolve adapter "
                    "for server_id=%r; falling back to ``server``.",
                    snapshot_server_id,
                )
                _adapter_for_dispatch = server
            self._run_snapshot_via_adapter(
                rec=rec,
                adapter=_adapter_for_dispatch,
                url=url,
                token=token,
                service_type=_service_type,
                server_id=snapshot_server_id,
                settings=settings,
                logger=logger,
                run_log_dir=run_log_dir,
            )
            return

        # Resolve the library *names* sent by the client to the
        # python-plexapi LibrarySection objects ``run_snapshot`` expects.
        all_sections = list(server.library.sections())
        wanted = set(settings["libraries"] or [])
        if wanted:
            selected = [s for s in all_sections if s.title in wanted]
            if not selected:
                raise ValueError(
                    f"None of the requested libraries match. "
                    f"Requested: {sorted(wanted)}. Available: {[s.title for s in all_sections]}"
                )
        else:
            selected = all_sections

        # Per-job watch+ratings strategy override. Sets the ContextVar
        # _resolve_watch_ratings_strategy() reads at tier 0, ahead of
        # per-server / global. Empty string = "no override". The
        # token is reset in the finally below so a subsequent job
        # picked up by the same worker thread inherits a clean state.
        _wr_strategy_override = str(settings.get("watch_ratings_filter_strategy") or "")
        _wr_strategy_token = state._watch_ratings_strategy_override_var.set(_wr_strategy_override)

        try:
            # Hand off to the engine. ``run_snapshot`` returns when every
            # library is done (or stop_event is set, in which case it
            # finishes the in-flight ones and returns).
            run_snapshot(
                server,
                selected,
                settings["output_dir"],
                logger,
                run_log_dir,
                url,
                skip_collections=bool(settings.get("skip_collections") or False),
                fast_collection_detection=bool(settings.get("fast_collection_detection") or False),
                skip_playlists=bool(settings.get("skip_playlists") or False),
                skip_playlist_prebuild=bool(settings.get("skip_playlist_prebuild") or False),
                # Four-flag data-type filter forwarded from
                # the request (or schedule). The Pydantic validator on
                # SnapshotJobIn / ScheduleIn already mapped any legacy
                # skip_* fields onto these include_* defaults.
                include_watch_history=bool(settings.get("include_watch_history", True)),
                include_ratings=bool(settings.get("include_ratings", True)),
                include_playlists=bool(settings.get("include_playlists", True)),
                include_collections=bool(settings.get("include_collections", True)),
                # Per-job user filter. None / missing = include
                # every user the source server reports (the default).
                # When provided, run_snapshot filters
                # home_users + derives owner_included internally.
                user_filter=settings.get("user_filter"),
                # Library-level concurrency cap. 0 (default)
                # inherits state.MAX_WORKERS; a positive value caps
                # libraries-in-parallel
                # without affecting the per-library HTTP worker count.
                library_workers=int(settings.get("snapshot_library_workers") or 0),
                # Per-library metric filter. Forwarded from the
                # JobRecord params (set by the SnapshotJobIn
                # validator expansion, or the end user's explicit map).
                # The engine reads this per library and overrides the
                # global include_* booleans on a per-library basis.
                library_metrics=settings.get("library_metrics") or None,
            )
        finally:
            try:
                state._watch_ratings_strategy_override_var.reset(_wr_strategy_token)
            except Exception:
                pass

        # Part B: the engine has returned (every library row reads
        # "Done"), but the job is NOT done - close-logger, run-dir
        # finalize, and the snapshot-DB capture below all still run
        # before the worker loop flips the JobRecord to COMPLETED.
        # Surface that as a run-level "Finalizing" phase so the
        # dashboard doesn't look frozen at 100%.
        _dash = state.get_dashboard()
        if _dash is not None:
            _dash.set_finalizing("closing run logs")
        # Mirror the CLI's PASS/FAIL log rename so per-run log
        # directories on disk stay consistent across CLI and server.
        _finalize_run(logger, run_log_dir)

        # Capture the snapshot .db file + register it.
        # This is the heaviest post-engine step (it writes the whole
        # snapshot .db from the in-memory payloads) - the dominant
        # cause of the "stuck at the end" feeling, so it gets its own
        # finalize label.
        if _dash is not None:
            _dash.set_finalizing("writing snapshot to database")
        # Best-effort wrapper: failure here doesn't fail the job (the
        # engine already wrote rows to media.db; the end user's data
        # is intact and re-runs are idempotent). We log AND stamp
        # ``rec.error`` so the UI shows a banner alongside the
        # otherwise-successful job state - silent capture failure is
        # the original bug that left the Exports panel empty.
        try:
            _capture_snapshot_after_run(
                rec=rec,
                server_id=snapshot_server_id,
                server_name=settings.get("source_server_name") or "",
                output_dir=settings["output_dir"],
                server_slug=server_slug,
                libraries=[s.title for s in selected],
            )
        except Exception as exc:
            msg = (
                f"Snapshot artifact capture failed: {safe_error(exc)}. "
                "Engine data is in media.db; re-running is safe."
            )
            rec.error = msg
            logging.getLogger("plexmigrate.server.jobs").exception(
                "Snapshot capture failed for job %r; media.db rows are still intact.",
                rec.job_id,
            )
            if state.get_dashboard():
                try:
                    state.get_dashboard().push_activity("error", "-", msg)
                except Exception:
                    pass
            # The snapshot .db artifact IS the deliverable of a
            # snapshot job - without it the Exports panel has nothing
            # to restore from. Fail the job rather than reporting it
            # as amber "completed with errors".
            raise RuntimeError(msg) from exc

        # Post-snapshot mirror write-through. Reads the just-captured
        # snapshot rows from media.db and bulk-upserts them into the
        # mirror so resolvers + the cross-feed bootstrap have fresh
        # data. Tunable-gated (engine_mirror_snapshot_writethrough,
        # default true). Failure here NEVER blocks the snapshot
        # (snapshot artifact already committed above).
        if _dash is not None:
            _dash.set_finalizing("writing mirror writethrough")
        try:
            from services.mirror_sync import writethrough as server_mirror_writethrough
            server_mirror_writethrough.after_snapshot(
                server_id=snapshot_server_id,
                backend="plex",
                logger=logging.getLogger("plexmigrate.server.jobs"),
            )
        except Exception:
            logging.getLogger("plexmigrate.server.jobs").exception(
                "server_mirror writethrough after snapshot failed "
                "(non-blocking); job %r remains successful.",
                rec.job_id,
            )
        # Clear the label once the finalize phases finish. Without this
        # the banner persists into the post-run idle state.
        if _dash is not None:
            _dash.set_finalizing(None)

    # ── Engine invocation: adapter-driven snapshot (Jellyfin / Emby) ──

    def _run_snapshot_via_adapter(
        self,
        *,
        rec: JobRecord,
        adapter: Any,
        url: str,
        token: str,
        service_type: str,
        server_id: str,
        settings: Dict[str, Any],
        logger: logging.Logger,
        run_log_dir: str,
    ) -> None:
        """Non-Plex snapshot path. Mirrors the post-engine flow of
        :func:`_run_snapshot` (close logger, finalise run dir, capture
        snapshot.db) but routes the engine call through
        :func:`services.snapshot.adapter.snapshotter.run_snapshot_adapter`
        instead of the plexapi-driven :func:`services.snapshot.plex_native.snapshotter.run_snapshot`.

        Plex stays on its own perf-tuned path; this helper is the
        parallel engine for Jellyfin / Emby."""
        from types import SimpleNamespace
        from services.snapshot.adapter.snapshotter import run_snapshot_adapter

        # Build a connection-like for the adapter engine. We don't
        # have a ServerConnection in scope here (the worker called
        # connect_registered_server once via _resolve_source_connection
        # and unpacked it), so reuse the parts we already have.
        connection_like = SimpleNamespace(
            adapter=adapter,
            url=url,
            token=token,
            service_type=service_type,
            row={"id": server_id},
        )
        library_names_requested = list(settings.get("libraries") or [])
        stop_event = getattr(rec, "_stop_event", None)

        try:
            payload = run_snapshot_adapter(
                connection=connection_like,
                library_names=library_names_requested or None,
                server_id=server_id,
                include_watch_history=bool(settings.get("include_watch_history", True)),
                include_ratings=bool(settings.get("include_ratings", True)),
                include_playlists=bool(settings.get("include_playlists", True)),
                include_collections=bool(settings.get("include_collections", True)),
                # Per-user fan-out forwarded from job
                # params. ``user_filter=None`` means all managed users;
                # a list narrows to specific usernames. The owner is
                # always captured in the owner-phase pass.
                user_filter=settings.get("user_filter"),
                include_managed_users=bool(
                    settings.get("include_managed_users", True)
                ),
                logger=logger,
                stop_event=stop_event,
            )
        except Exception:
            logger.exception(
                "adapter snapshot engine raised; closing run logs and bubbling up."
            )
            _finalize_run(logger, run_log_dir)
            raise

        # Resolved library list comes back inside the payload so the
        # snapshot.db post-engine capture knows exactly which libraries
        # the run touched.
        resolved_library_names = list(
            (payload.get("snapshot_meta") or {}).get("libraries") or []
        )

        # The Plex
        # snapshot path's snapshot_library() appends each per-library
        # payload to state._snapshot_payloads as it runs; the
        # adapter-driven path (run_snapshot_adapter) instead RETURNS
        # the libraries inside the payload dict without ever touching
        # state._snapshot_payloads. _capture_snapshot_after_run reads
        # from state._snapshot_payloads and bails on empty, so the
        # adapter path would crash every Emby/Jellyfin snapshot at the
        # post-engine capture step. Mirror the Plex behaviour here by
        # extending the global sink with the adapter's per-library
        # payloads (same shape per services/snapshotter_adapter.py:
        # snapshot_library_adapter docstring).
        adapter_libraries = list(payload.get("libraries") or [])
        if adapter_libraries:
            state._snapshot_payloads.extend(adapter_libraries)

        _dash = state.get_dashboard()
        if _dash is not None:
            _dash.set_finalizing("closing run logs")
        _finalize_run(logger, run_log_dir)

        if _dash is not None:
            _dash.set_finalizing("writing snapshot to database")
        try:
            _capture_snapshot_after_run(
                rec=rec,
                server_id=server_id,
                server_name=settings.get("source_server_name") or "",
                output_dir=settings["output_dir"],
                server_slug=settings.get("resolved_server_slug") or "",
                libraries=resolved_library_names,
            )
        except Exception as exc:
            msg = (
                f"Snapshot artifact capture failed: {safe_error(exc)}. "
                "Engine data is in media.db; re-running is safe."
            )
            rec.error = msg
            logging.getLogger("plexmigrate.server.jobs").exception(
                "Snapshot capture (adapter path) failed for job %r; "
                "media.db rows are intact.",
                rec.job_id,
            )
            if state.get_dashboard():
                try:
                    state.get_dashboard().push_activity("error", "-", msg)
                except Exception:
                    pass

        # Adapter-path mirror write-through. Symmetric with
        # _run_snapshot's hook so
        # Jellyfin / Emby snapshots also keep their mirror current.
        # Tunable-gated (engine_mirror_snapshot_writethrough, default
        # true). Failure here NEVER blocks the snapshot.
        try:
            from services.mirror_sync import writethrough as server_mirror_writethrough
            server_mirror_writethrough.after_snapshot(
                server_id=server_id,
                backend=service_type,
                logger=logging.getLogger("plexmigrate.server.jobs"),
            )
        except Exception:
            logging.getLogger("plexmigrate.server.jobs").exception(
                "server_mirror writethrough after adapter snapshot "
                "failed (non-blocking); job %r remains successful.",
                rec.job_id,
            )

    # ── Engine invocation: import ────────────────────────────────────

    def _run_restore(self, rec: JobRecord) -> None:
        """
        File-mediated import. The destination is named by
        ``dest_server_name`` (single) or ``dest_server_names`` (fan-out).
        Dispatches to :func:`run_fan_out_restore` when more than
        one destination is requested; otherwise takes the
        single-destination path below.
        """
        settings = _merge_settings(rec.params, mode="restore")

        # Resolve the end user's
        # mixed-media config once and stash on state so both the Plex
        # engine and the adapter engine see it without per-call kwarg
        # plumbing. Per-user overrides from cross_platform_resolutions
        # are extracted alongside; the engine applies them per source
        # user when transforming playlist rows.
        try:
            _apply_mixed_media_state(settings)
        except Exception:
            log.exception(
                "mixed-media config resolution failed; restore will run "
                "without operator overrides."
            )

        dest_names = _resolve_dest_names(settings)
        if len(dest_names) > 1:
            self._run_restore_fan_out(rec, settings, dest_names)
            return

        if dest_names:
            settings["dest_server_name"] = dest_names[0]

        # Multi-server resolution. ``dest_server_name`` selects the
        # destination registered server; falls back to ad-hoc
        # url/token from the legacy CLI shape if not present.
        # We adapt the source/dest naming on the fly so the same helper
        # is used for both snapshot (source) and import (dest).
        if settings.get("dest_server_name"):
            settings["source_server_name"] = settings["dest_server_name"]
        server, url, token, owner, server_slug = _resolve_source_connection(
            settings, logger=logging.getLogger("plexmigrate")
        )
        settings["plex_url"] = url
        settings["plex_token"] = token
        settings["resolved_server_slug"] = server_slug
        rec.params = {k: v for k, v in settings.items() if k != "plex_token"}

        # Pre-Replace auto-capture safety belt. Runs BEFORE
        # the restore so the destination has a rollback point on disk
        # by the time the engine starts overwriting data. On failure
        # the helper raises and the restore never fires - "no recovery
        # point" is exactly the case the belt is supposed to prevent.
        # The captured snapshot's id is stamped on rec.summary so the
        # end user can find it on the Snapshots tab later. The basic
        # state setup (session, MAX_WORKERS, plex_*) must happen first
        # because the helper reuses them.
        state.MAX_WORKERS = int(settings["workers"])
        state.SCROBBLE_WORKERS = int(settings["scrobble_workers"])
        state._session = _make_session()
        state._plex_base_url = url
        state._plex_token = token
        state._plex_owner_name = owner
        # Cross-backend Replace-mode refusal. Runs BEFORE the
        # pre-Replace safety snapshot so an unrunnable job doesn't
        # burn the auto-capture budget. Same-backend Replace continues
        # unchanged.
        _src_id = settings.get("source_server_id") or _resolve_server_id(
            settings.get("source_server_name"),
        ) or ""
        _dst_id = settings.get("dest_server_id") or _resolve_server_id(
            settings.get("dest_server_name") or settings.get("source_server_name"),
        ) or ""
        _refusal = _cross_backend_replace_requires_mappings(
            source_server_id=_src_id,
            source_service_type=str(settings.get("source_service_type") or "plex"),
            dest_server_id=_dst_id,
            dest_service_type=str(settings.get("dest_service_type") or "plex"),
            mode=str(settings.get("mode") or "merge"),
            ignore_library_mapping=bool(
                settings.get("ignore_library_mapping") or False
            ),
        )
        if _refusal:
            raise ValueError(_refusal)

        pre_replace_snapshot_id: Optional[str] = None
        restore_mode = str(settings.get("mode") or "merge")
        auto_capture = bool(settings.get("auto_capture_before_replace", True))
        if restore_mode == "replace" and auto_capture:
            dest_server_id_for_belt = _resolve_server_id(
                settings.get("dest_server_name") or settings.get("source_server_name"),
            )
            if not dest_server_id_for_belt:
                raise ValueError(
                    "Replace restore with auto-capture requires a registered "
                    "destination server (couldn't resolve a server_id). "
                    "Re-register the destination from the Servers tab."
                )
            pre_replace_snapshot_id = _capture_pre_replace_snapshot(
                job_id=rec.job_id,
                settings=settings,
                dest_server=server,
                dest_server_id=dest_server_id_for_belt,
                dest_server_name=str(settings.get("dest_server_name") or settings.get("source_server_name") or ""),
                dest_url=url,
                dest_service_type=str(settings.get("dest_service_type") or "plex"),
            )
            # Stash on rec.summary so the audit row + UI can show it.
            rec.summary = {
                **(rec.summary or {}),
                "pre_replace_snapshot_id": pre_replace_snapshot_id,
            }

        # Peek the input files' top-level ``library`` fields so
        # the restore run-dir name carries the libraries being restored
        # (`run_Plex1_Movies_TV-Shows_20260510_135425/`). Best-effort -
        # files we can't peek contribute nothing, and the strict
        # existence check happens further down.
        _restore_libs = _peek_libraries_from_inputs(
            list(settings.get("input_files") or []),
            settings.get("output_dir") or "./snapshots",
        )
        _set_run_timestamp(server_slug, libraries=_restore_libs)
        logger, run_log_dir = _build_logger(settings["log_dir"], settings["verbose"])
        rec.run_log_dir = run_log_dir
        _log_pin_preflight_ack(rec, logger)
        _dump_run_settings("restore", run_log_dir, rec.params, logger)
        state.MAX_WORKERS = int(settings["workers"])
        state.SCROBBLE_WORKERS = int(settings["scrobble_workers"])
        state._session = _make_session()
        state._plex_base_url = url
        state._plex_token = token
        state._plex_owner_name = owner
        # Imports run against the destination server - pull its
        # display-name map so the dashboard's current_user attribution
        # uses the right side's friendly names.
        _populate_run_user_context(server, source_name=settings.get("dest_server_name") or settings.get("source_server_name"))

        # Verify each requested input file exists before kicking off
        # the engine - fail fast with a useful message rather than
        # mid-run with a stack trace.
        #
        # Three resolution attempts, in order:
        #   1. The value as given (absolute path, or cwd-relative for CLI).
        #   2. Joined against the configured output_dir - the Run-Job
        #      form sends bare filenames pulled from the active snapshot
        #      registry, whose .db files live directly under output_dir.
        #   3. Joined against ``<output_dir>/legacy/`` - the
        #      "Import from JSON archive" picker sources its filenames
        #      from ``server/snapshot_browser.list_snapshots`` which reads
        #      that legacy archive dir. Bare filenames from that picker
        #      land here. Also covers manually-converted legacy exports.
        output_dir = settings.get("output_dir") or "./snapshots"
        valid: List[str] = []
        missing: List[str] = []
        for f in settings["input_files"] or []:
            direct = Path(f)
            if direct.exists():
                valid.append(str(direct))
                continue
            scoped = Path(output_dir) / f
            if scoped.exists():
                valid.append(str(scoped))
                continue
            legacy_scoped = Path(output_dir) / "legacy" / f
            if legacy_scoped.exists():
                valid.append(str(legacy_scoped))
                continue
            missing.append(f)
            logger.error(
                "Export file not found: %s (tried %s and %s)",
                f, scoped, legacy_scoped,
            )
        if not valid:
            raise ValueError(f"No valid export files found. Missing: {missing}")

        remap: Optional[Tuple[str, str]] = None
        if settings.get("remap_old") and settings.get("remap_new"):
            remap = (settings["remap_old"], settings["remap_new"])

        # Non-Plex destinations route through the
        # adapter restore engine. Plex stays on the perf-tuned engine.
        dest_service = (settings.get("dest_service_type") or "plex").lower()
        if dest_service == "plex":
            # Fallback: read from registry if Pydantic didn't carry the
            # service_type (legacy callers).
            try:
                _dest_row = (server_registry.get_server_by_name(
                    settings.get("dest_server_name") or settings.get("source_server_name") or "",
                    include_token=False,
                ) or {}) if (settings.get("dest_server_name") or settings.get("source_server_name")) else {}
                dest_service = (_dest_row.get("service_type") or "plex").lower()
            except Exception:
                pass
        if dest_service != "plex":
            self._run_restore_via_adapter(
                rec=rec,
                adapter=server,
                url=url,
                token=token,
                service_type=dest_service,
                input_files=valid,
                settings=settings,
                logger=logger,
                run_log_dir=run_log_dir,
            )
            return

        # The Plex restore path
        # (run_restore -> restore_export_file) does not accept
        # ``include_managed_users`` directly, so the flag must be
        # translated here or it is silently dropped on Plex-to-Plex
        # restores and every managed user in the snapshot
        # payload still gets restored. Translate the flag into the
        # existing ``user_filter`` mechanism: an empty filter list
        # drops every managed user (the owner is handled separately
        # in the owner-phase pass + the role lookup, so it still
        # restores). Operator-supplied ``user_filter`` wins when
        # ``include_managed_users=True``; when False we force an empty
        # list regardless of what the operator picked in the user grid.
        _include_managed_users = bool(settings.get("include_managed_users", True))
        if _include_managed_users:
            _restore_user_filter = settings.get("user_filter")
        else:
            _restore_user_filter = []
            logger.info(
                "Restore: include_managed_users=False — every managed "
                "user in the snapshot payload will be skipped; owner-only "
                "restore (owner data still applies)."
            )
        run_restore(
            server,
            valid,
            settings["plex_token"],
            settings["plex_url"],
            logger,
            run_log_dir,
            remap,
            bool(settings["strict_match"]),
            # Four-flag data-type filter forwarded from
            # the request. The Pydantic validator already mapped any
            # legacy skip_* fields onto these include_* defaults, so
            # both shapes work without translation here.
            include_playlists=bool(settings.get("include_playlists", True)),
            include_watch_history=bool(settings.get("include_watch_history", True)),
            include_ratings=bool(settings.get("include_ratings", True)),
            include_collections=bool(settings.get("include_collections", True)),
            mode=str(settings.get("mode") or "merge"),
            merge_watch_strategy=str(settings.get("merge_watch_strategy") or "higher"),
            # Per-job user filter for restore. None = restore
            # every user from the payload that also exists on the
            # destination. When provided, the
            # importer drops managed users whose handle isn't in the list.
            user_filter=_restore_user_filter,
            # End user-tunable library concurrency. Reads from
            # settings (Run Defaults > Concurrency); default 3. Lower it
            # (e.g. to 1) when Plex
            # rate-limits the multi-library API bursts.
            library_workers=int(settings.get("restore_library_workers") or 3),
            # Per-library metric filter (RestoreJobIn-supplied).
            # restore_export_file consults this per library before
            # firing each restore_* primitive.
            library_metrics=settings.get("library_metrics") or None,
            # Forward destination server id (when resolvable) so the
            # per-user fan-out's identity_map resolver actually fires
            # for Plex-to-Plex restores. Source server id is recovered
            # from the snapshot's snapshot_meta inside
            # restore_export_file, so we don't need to pass it here.
            dest_server_id=_resolve_server_id(
                settings.get("dest_server_name") or settings.get("source_server_name") or ""
            ),
            # Per-run power-user override. Hidden behind
            # ``reveal_ignore_library_mapping_toggle`` tunable; default
            # False so every restore consults library_mappings.
            ignore_library_mapping=bool(
                settings.get("ignore_library_mapping") or False
            ),
            # Per-run library name overrides for
            # the engine's library-mapping consult. Operator can set
            # these on the Run Job form when restoring from a snapshot
            # with the source server offline (the case where they
            # can't open Server Syncing to declare a permanent
            # mapping). Engine reads this dict BEFORE the saved
            # library_mappings table.
            library_mapping_overrides=(
                settings.get("library_mapping_overrides") or None
            ),
        )

        # Part B: run-level finalize phase so the dashboard doesn't
        # look frozen at 100% during the post-engine close-out.
        _dash = state.get_dashboard()
        if _dash is not None:
            _dash.set_finalizing("finalizing run")
        _finalize_run(logger, run_log_dir)

    # ── Engine invocation: adapter-driven restore (Jellyfin / Emby) ──

    def _run_restore_via_adapter(
        self,
        *,
        rec: JobRecord,
        adapter: Any,
        url: str,
        token: str,
        service_type: str,
        input_files: List[str],
        settings: Dict[str, Any],
        logger: logging.Logger,
        run_log_dir: str,
    ) -> None:
        """Non-Plex restore path. Loads each input file's snapshot
        payload from disk (or from the snapshot.db registry) and
        applies it to the destination via
        :func:`services.restore.adapter.restorer.restore_payload_adapter`.

        The Plex path stays on the perf-tuned
        :func:`services.restore.plex_native.run_restore`; this is the parallel
        engine for Jellyfin / Emby destinations."""
        from services.restore.adapter.restorer import restore_payload_adapter
        from services.adapters import UserContext

        # When the end user confirmed the
        # user-creation modal at submit time, the request body
        # carries a non-empty user_create_specs list. Walk it via
        # services/user_creation.py BEFORE any item-state write
        # fires. On partial failure the helper raises and we abort
        # the whole run; on success the destination has every user
        # the end user chose and managed_users has the mappings.
        _user_specs = settings.get("user_create_specs") or []
        if _user_specs:
            try:
                from services.user_management.creation import (
                    UserCreationError, create_users_for_job,
                )
                dest_server_id = (
                    settings.get("_resolved_dest_server_id")
                    or settings.get("dest_server_id")
                    or ""
                )
                create_users_for_job(
                    adapter=adapter,
                    specs=_user_specs,
                    dest_server_id=str(dest_server_id),
                    logger=logger,
                )
            except UserCreationError as exc:
                logger.error(
                    "restore (adapter): user_create_specs failed; "
                    "aborting before item-state writes. %s", exc,
                )
                _finalize_run(logger, run_log_dir)
                raise

        # Build the owner-phase UserContext. Restore everything under
        # the destination's admin user context.
        try:
            identity = adapter.server_identity()
            owner_uid = identity.owner_user_id or ""
            owner_username = identity.owner_display or "Owner"
        except Exception:
            owner_uid = ""
            owner_username = "Owner"
        ctx = UserContext(
            backend_user_id=owner_uid,
            username=owner_username,
            auth_token=token,
            is_admin=True,
        )

        # Aggregate counts across every input file so the run summary
        # reflects the whole restore.
        total = {
            "watch_history_written": 0,
            "watch_history_skipped_no_match": 0,
            "watch_history_failed": 0,
            "ratings_written": 0,
            "ratings_skipped_no_match": 0,
            "ratings_failed": 0,
            "ratings_unsupported": 0,
            "playlists_written": 0,
            "playlists_skipped_no_match": 0,
            "playlists_failed": 0,
            "collections_written": 0,
            "collections_skipped_no_match": 0,
            "collections_failed": 0,
            "libraries": 0,
        }
        stop_event = getattr(rec, "_stop_event", None)

        try:
            for input_path in input_files:
                if stop_event is not None and stop_event.is_set():
                    logger.info("restore (adapter): stop requested, halting.")
                    break
                # Forward-compat wrap around _load_snapshot_payload:
                # post-strip the .json branch raises ValueError instead
                # of returning a wrapped dict. Treat both "None" (load
                # failed) and ValueError (unsupported shape) as
                # per-file skips with the failure reason logged.
                try:
                    payload = _load_snapshot_payload(input_path, logger)
                except ValueError as _exc:
                    logger.warning(
                        "restore (adapter): %r uses an unsupported file "
                        "shape (%s); skipping.",
                        input_path, _exc,
                    )
                    continue
                if payload is None:
                    logger.warning(
                        "restore (adapter): could not load %r, skipping.",
                        input_path,
                    )
                    continue
                # When the end user picked a non-default rating-mode,
                # the request body carries favorite_threshold;
                # otherwise it falls through to the engine default
                # (5.0) via the **only-if-set** forward below.
                fav_threshold_kw: Dict[str, Any] = {}
                _ft = settings.get("favorite_threshold")
                if _ft is not None:
                    try:
                        fav_threshold_kw["favorite_threshold"] = float(_ft)
                    except (TypeError, ValueError):
                        pass
                # Forward the
                # favorite-to-rating value (used when a favorited item
                # is restored onto a rating-only backend) from its
                # tunable.
                try:
                    from services import tunables as _tun
                    fav_threshold_kw["favorite_as_rating_value"] = (
                        _tun.favorite_as_rating_value()
                    )
                except Exception:
                    pass
                # Identity-map resolution: pass dest_server_id so the
                # per-user fan-out can translate source handles via
                # media_db.user_identity_map when a direct username
                # match would have missed. source_server_id is
                # auto-derived from the payload's snapshot_meta by
                # restore_payload_adapter.
                _dest_server_id = (
                    str(settings.get("_resolved_dest_server_id") or "")
                    or str(settings.get("dest_server_id") or "")
                    or _resolve_server_id(
                        settings.get("dest_server_name")
                        or settings.get("source_server_name")
                        or ""
                    )
                    or ""
                )
                # Forward end user-authored cross-platform
                # resolutions (Drop / Map / Create / Accept decisions)
                # to the engine.
                # Engine applies them at write time so Drop and
                # Map-no-persist actually take effect on this run.
                _cpr = settings.get("cross_platform_resolutions") or None
                result = restore_payload_adapter(
                    adapter,
                    payload=payload,
                    user_context=ctx,
                    merge_watch_strategy=str(
                        settings.get("merge_watch_strategy") or "higher"
                    ),
                    logger=logger,
                    stop_event=stop_event,
                    include_watch_history=bool(
                        settings.get("include_watch_history", True)
                    ),
                    include_ratings=bool(
                        settings.get("include_ratings", True)
                    ),
                    include_playlists=bool(
                        settings.get("include_playlists", True)
                    ),
                    include_collections=bool(
                        settings.get("include_collections", True)
                    ),
                    # Per-user fan-out + filter forwarded
                    # from job params.
                    include_managed_users=bool(
                        settings.get("include_managed_users", True)
                    ),
                    user_filter=settings.get("user_filter"),
                    dest_server_id=_dest_server_id,
                    cross_platform_resolutions=_cpr,
                    # Forward Replace mode so the adapter
                    # engine runs the dest-only playlist + collection
                    # sweep. Same as on the direct-transfer path.
                    mode=str(settings.get("mode") or "merge"),
                    **fav_threshold_kw,
                )
                total["libraries"] += result.libraries_processed
                total["watch_history_written"] += result.watch_history.written
                total["watch_history_skipped_no_match"] += result.watch_history.skipped_no_match
                total["watch_history_failed"] += result.watch_history.failed
                total["ratings_written"] += result.ratings.written
                total["ratings_skipped_no_match"] += result.ratings.skipped_no_match
                total["ratings_failed"] += result.ratings.failed
                total["ratings_unsupported"] += result.ratings.unsupported
                total["playlists_written"] += result.playlists.written
                total["playlists_skipped_no_match"] += result.playlists.skipped_no_match
                total["playlists_failed"] += result.playlists.failed
                total["collections_written"] += result.collections.written
                total["collections_skipped_no_match"] += result.collections.skipped_no_match
                total["collections_failed"] += result.collections.failed
        finally:
            # Stash the aggregate on rec.summary so the dashboard +
            # UI surface what actually happened.
            rec.summary = {
                **(rec.summary or {}),
                "adapter_restore": total,
            }
            _dash = state.get_dashboard()
            if _dash is not None:
                _dash.set_finalizing("finalizing run")
            _finalize_run(logger, run_log_dir)

    # ── Engine invocation: adapter-driven direct transfer ────────────

    def _run_direct_via_adapter(
        self,
        *,
        rec: JobRecord,
        src_conn: Any,
        dst_conn: Any,
        settings: Dict[str, Any],
        logger: logging.Logger,
        run_log_dir: str,
        stop_event: Optional[threading.Event],
    ) -> None:
        """Cross-backend direct transfer. Snapshots the source via
        :func:`services.snapshot.adapter.snapshotter.run_snapshot_adapter`,
        then immediately restores the in-memory payload to the
        destination via
        :func:`services.restore.adapter.restorer.restore_payload_adapter`.

        Handles all four backend combinations that involve at least
        one non-Plex side: P->J, P->E, J->J, J->P, J->E, E->E, E->P,
        E->J. Plex<->Plex stays on the perf-tuned
        :func:`services.direct_transfer.engine.run_direct_transfer` because
        it parallelises per-library and shares plexapi sessions
        across reads + writes.

        This path captures + restores watch_history + ratings only;
        playlists + collections per-library are out of scope. The
        engine engines already populate empty arrays for those keys,
        so the payload shape is forward-compatible."""
        from types import SimpleNamespace
        from services.snapshot.adapter.snapshotter import run_snapshot_adapter
        from services.restore.adapter.restorer import restore_payload_adapter
        from services.adapters import UserContext

        # Same create-users-first contract
        # as the restore-adapter path. The destination is dst_conn
        # here; specs apply per the modal's confirmation.
        _user_specs = settings.get("user_create_specs") or []
        if _user_specs:
            try:
                from services.user_management.creation import (
                    UserCreationError, create_users_for_job,
                )
                create_users_for_job(
                    adapter=dst_conn.adapter,
                    specs=_user_specs,
                    dest_server_id=str(dst_conn.row.get("id") or ""),
                    logger=logger,
                )
            except UserCreationError as exc:
                logger.error(
                    "direct (adapter): user_create_specs failed; "
                    "aborting before item-state writes. %s", exc,
                )
                _finalize_run(logger, run_log_dir)
                raise

        # Source-side connection-like for the snapshot engine.
        src_connection = SimpleNamespace(
            adapter=src_conn.adapter,
            url=src_conn.url,
            token=src_conn.token,
            service_type=src_conn.service_type,
            row=src_conn.row,
        )
        src_server_id = src_conn.row.get("id") or ""

        try:
            payload = run_snapshot_adapter(
                connection=src_connection,
                library_names=list(settings.get("libraries") or []) or None,
                server_id=src_server_id,
                include_watch_history=bool(settings.get("include_watch_history", True)),
                include_ratings=bool(settings.get("include_ratings", True)),
                include_playlists=bool(settings.get("include_playlists", True)),
                include_collections=bool(settings.get("include_collections", True)),
                # Per-user fan-out on source-side capture.
                user_filter=settings.get("user_filter"),
                include_managed_users=bool(
                    settings.get("include_managed_users", True)
                ),
                logger=logger,
                stop_event=stop_event,
            )
        except Exception:
            logger.exception(
                "adapter direct-transfer: source snapshot raised; halting.",
            )
            _finalize_run(logger, run_log_dir)
            raise

        # Destination-side restore. Owner-phase only.
        try:
            dst_identity = dst_conn.adapter.server_identity()
            dst_uid = dst_identity.owner_user_id or ""
            dst_username = dst_identity.owner_display or "Owner"
        except Exception:
            dst_uid = ""
            dst_username = "Owner"
        dst_ctx = UserContext(
            backend_user_id=dst_uid,
            username=dst_username,
            auth_token=dst_conn.token,
            is_admin=True,
        )

        # Favorite-threshold forwarding (same shape as
        # _run_restore_via_adapter; only-if-set so legacy clients
        # fall through to the engine default).
        dst_fav_threshold_kw: Dict[str, Any] = {}
        _dft = settings.get("favorite_threshold")
        if _dft is not None:
            try:
                dst_fav_threshold_kw["favorite_threshold"] = float(_dft)
            except (TypeError, ValueError):
                pass
        # Forward the
        # favorite-to-rating tunable on the direct-transfer path too.
        try:
            from services import tunables as _tun
            dst_fav_threshold_kw["favorite_as_rating_value"] = (
                _tun.favorite_as_rating_value()
            )
        except Exception:
            pass
        try:
            result = restore_payload_adapter(
                dst_conn.adapter,
                payload=payload,
                user_context=dst_ctx,
                merge_watch_strategy=str(
                    settings.get("merge_watch_strategy") or "higher"
                ),
                logger=logger,
                stop_event=stop_event,
                include_watch_history=bool(settings.get("include_watch_history", True)),
                include_ratings=bool(settings.get("include_ratings", True)),
                include_playlists=bool(settings.get("include_playlists", True)),
                include_collections=bool(settings.get("include_collections", True)),
                # Per-user fan-out on destination-side restore.
                include_managed_users=bool(
                    settings.get("include_managed_users", True)
                ),
                user_filter=settings.get("user_filter"),
                dest_server_id=str(dst_conn.row.get("id") or ""),
                cross_platform_resolutions=settings.get("cross_platform_resolutions") or None,
                # Forward the run-level restore mode so the
                # adapter-side engine runs the dest-only sweep on
                # Replace. Without this, every job through the adapter
                # path is effectively a Merge regardless of the
                # operator's choice on the Run Job form.
                mode=str(settings.get("mode") or "merge"),
                **dst_fav_threshold_kw,
            )
        finally:
            _dash = state.get_dashboard()
            if _dash is not None:
                _dash.set_finalizing("finalizing run")
            _finalize_run(logger, run_log_dir)

        # Stash the aggregate result on rec.summary so the dashboard
        # + run history surface what happened.
        rec.summary = {
            **(rec.summary or {}),
            "adapter_direct": {
                "libraries": result.libraries_processed,
                "watch_history_written": result.watch_history.written,
                "watch_history_skipped_no_match": result.watch_history.skipped_no_match,
                "watch_history_failed": result.watch_history.failed,
                "ratings_written": result.ratings.written,
                "ratings_skipped_no_match": result.ratings.skipped_no_match,
                "ratings_failed": result.ratings.failed,
                "ratings_unsupported": result.ratings.unsupported,
            },
        }

    # ── Engine invocation: single-playlist copy ─────────────────────

    def _run_playlist_copy(self, rec: JobRecord) -> None:
        """Single-playlist copy worker. Routes Playlist Mgmt Deploy
        through the job queue and surfaces live metrics on the
        Dashboard tab (same shape every other job uses).

        Lightweight relative to direct-transfer - no library walk, no
        per-user data scope, no Plex-side cache warm. Just resolve
        source items + write a new dest playlist. The orchestrator
        lives in :func:`services.playlist_copy.copy_playlist`.

        Pushes progress events through to the live DashboardState so
        the Dashboard's activity feed + counters tick during the run.
        Skipped items increment ``state.skipped``; resolved items
        increment ``state.completed``; per-phase transitions push an
        activity entry.

        The job's ``summary`` carries the result dict so the frontend
        can render success / error / per-item miss details from the
        active-deploys panel without a separate roundtrip.
        """
        from services import playlist_copy

        params = rec.params or {}

        # Stash a label for the activity feed.
        dest_label = params.get("dest_user_id") or "(dest)"
        src_label = params.get("source_user_id") or "(source)"
        playlist_label = params.get("source_playlist_id") or "(playlist)"

        def _on_progress(ev: Dict[str, Any]) -> None:
            """Translate copy_playlist's structured events into
            DashboardState push_activity + counter updates. Guarded
            against any dashboard-side raise so the copy itself never
            fails on observer plumbing."""
            try:
                dash = state.get_dashboard()
                if dash is None:
                    return
                kind = ev.get("event")
                if kind == "started":
                    dash.push_activity(
                        "started", "-",
                        f"Copy playlist {playlist_label} from {src_label} → {dest_label}",
                    )
                elif kind == "users-resolved":
                    src_u = ev.get("source_username") or src_label
                    dst_u = ev.get("dest_username") or dest_label
                    dash.push_activity(
                        "phase", "-",
                        f"Resolved users: {src_u} → {dst_u}",
                    )
                elif kind == "source-loaded":
                    name = ev.get("playlist_name") or "(playlist)"
                    n = int(ev.get("item_count") or 0)
                    dash.push_activity(
                        "phase", "-",
                        f"Loaded source playlist {name!r} ({n} item{'s' if n != 1 else ''})",
                    )
                elif kind == "resolving":
                    completed = int(ev.get("completed") or 0)
                    total = int(ev.get("total") or 0)
                    resolved = int(ev.get("resolved") or 0)
                    skipped = int(ev.get("skipped") or 0)
                    # Reflect cumulative counters on the dashboard.
                    # The completed/skipped fields drive the same
                    # Items/Skipped counters every other job uses.
                    with dash._lock:  # type: ignore[attr-defined]
                        dash.completed = resolved
                        dash.skipped = skipped
                    if completed and (completed == total or completed % 25 == 0):
                        dash.push_activity(
                            "phase", "-",
                            f"Resolved {completed}/{total} items "
                            f"({resolved} written, {skipped} skipped)",
                        )
                elif kind == "writing":
                    name = ev.get("name") or "(playlist)"
                    n = int(ev.get("resolved_count") or 0)
                    dash.push_activity(
                        "phase", "-",
                        f"Creating destination playlist {name!r} with {n} item{'s' if n != 1 else ''}…",
                    )
                elif kind == "done":
                    written = int(ev.get("items_written") or 0)
                    skipped = int(ev.get("items_skipped_no_match") or 0)
                    dash.push_activity(
                        "done", "-",
                        f"Copy complete — {written} written, {skipped} skipped",
                    )
                elif kind == "auth-chain":
                    # Single-copy job: mirror the batch worker's auth-
                    # chain surface so legacy single-copy deploys also
                    # show which auth step decided their token.
                    step = ev.get("step") or "?"
                    outcome = ev.get("outcome") or "?"
                    user = ev.get("user") or "?"
                    role_str = ev.get("role") or "?"
                    detail = ev.get("detail") or ""
                    line = f"Auth [{role_str} {user}]: {step} -> {outcome}"
                    if detail:
                        line += f"  ({detail})"
                    dash.push_activity("phase", "-", line)
            except Exception:
                log.exception("playlist_copy progress dashboard push failed")

        try:
            result = playlist_copy.copy_playlist(
                source_server_id=params.get("source_server_id") or "",
                source_user_id=params.get("source_user_id") or "",
                source_playlist_id=params.get("source_playlist_id") or "",
                dest_server_id=params.get("dest_server_id") or "",
                dest_user_id=params.get("dest_user_id") or "",
                dest_playlist_name=params.get("dest_playlist_name") or "",
                progress_cb=_on_progress,
            )
        except playlist_copy.PlaylistCopyError as exc:
            # Structured error — surface code + message via rec.error so
            # the result panel can render it. State transitions to
            # FAILED via the worker_loop's general-exception path.
            rec.error = scrub(f"{exc.code}: {exc}")
            rec.summary = {
                "playlist_copy": {
                    "success": False,
                    "new_playlist_id": None,
                    "items_written": 0,
                    "items_skipped_no_match": 0,
                    "items_failed": 0,
                    "errors": [scrub(f"{exc.code}: {exc}")],
                    "elapsed_seconds": 0.0,
                    "code": exc.code,
                },
                "params": dict(params),
            }
            # Re-raise so the worker loop's exception path marks the
            # job FAILED. A structured copy failure is a failure, not
            # an amber "completed with errors".
            raise
        # Successful or partial-success: stash the full result for the
        # frontend to render.
        rec.summary = {
            "playlist_copy": {
                "success": bool(result.get("success")),
                "new_playlist_id": result.get("new_playlist_id"),
                "items_written": int(result.get("items_written") or 0),
                "items_skipped_no_match": int(result.get("items_skipped_no_match") or 0),
                "items_failed": int(result.get("items_failed") or 0),
                "errors": [scrub(e) for e in (result.get("errors") or [])],
                "elapsed_seconds": float(result.get("elapsed_seconds") or 0.0),
            },
            "params": dict(params),
        }
        # Surface a non-empty rec.error for "succeeded with errors" rows
        # so the worker_loop transitions to COMPLETED_WITH_ERRORS.
        errs = result.get("errors") or []
        if errs and not result.get("success"):
            rec.error = scrub(errs[0]) if isinstance(errs, list) and errs else "playlist copy failed"

    # ── Engine invocation: playlist copy BATCH ──────────────────────

    def _run_smart_playlist_migrate(self, rec: JobRecord) -> None:
        """Smart Playlist Migration worker.

        For each item: read the source Plex smart playlist's filter,
        translate its server-specific tag ids into portable names,
        then re-create it on the destination - a true smart playlist
        on a Plex destination, a static materialisation of the current
        contents on Jellyfin / Emby. Every outcome is written to
        smart_playlist.db and logged to smart_playlist.log (the
        dedicated log the in-panel live view reads)."""
        from services.smart_playlist.log import get_smart_playlist_logger

        sp_log = get_smart_playlist_logger()
        params = rec.params or {}
        items: List[Dict[str, Any]] = [
            dict(it) if not isinstance(it, dict) else it
            for it in (params.get("items") or [])
        ]

        settings = _merge_settings(params, mode="smart_playlist_migrate")
        _set_run_timestamp("smart-playlist")
        logger, run_log_dir = _build_logger(
            settings["log_dir"], settings.get("verbose", False),
        )
        rec.run_log_dir = run_log_dir

        def _log(msg: str, *a: Any) -> None:
            """Log to BOTH the per-run runtime.log (so Servers > Logs
            picks it up) and the dedicated smart_playlist.log (the
            in-panel live view)."""
            try:
                logger.info(msg, *a)
            except Exception:
                pass
            try:
                sp_log.info(msg, *a)
            except Exception:
                pass

        _log("smart_playlist_migrate: job_id=%s items=%d",
             rec.job_id, len(items))

        stop_event = getattr(rec, "_stop_event", None)
        if stop_event is None:
            stop_event = state._active_stop_event

        results: List[Dict[str, Any]] = []
        succeeded = partial = failed = 0
        for idx, item in enumerate(items):
            if stop_event is not None and stop_event.is_set():
                _log("smart_playlist_migrate: stop requested; halting at "
                     "item %d of %d", idx, len(items))
                break
            res = self._migrate_one_smart_playlist(
                item, rec.job_id, logger, _log,
            )
            results.append(res)
            status = res.get("status")
            if status == "success":
                succeeded += 1
            elif status == "partial":
                partial += 1
            else:
                failed += 1
            dash = state.get_dashboard()
            if dash is not None:
                try:
                    dash.push_activity(
                        "error" if status == "failed" else "done", "-",
                        f"Smart playlist {idx + 1}/{len(items)}: "
                        f"{res.get('source_playlist_name') or '?'} "
                        f"-> {status}",
                    )
                except Exception:
                    pass

        rec.summary = {
            "total": len(items),
            "succeeded": succeeded,
            "partial": partial,
            "failed": failed,
            "results": results,
        }
        _log("smart_playlist_migrate: done job_id=%s total=%d succeeded=%d "
             "partial=%d failed=%d",
             rec.job_id, len(items), succeeded, partial, failed)

    def _migrate_one_smart_playlist(
        self,
        item: Dict[str, Any],
        job_id: str,
        run_logger: Any,
        log_fn: Any,
    ) -> Dict[str, Any]:
        """Migrate one smart playlist. Returns a per-item result dict
        and records the outcome in smart_playlist.db. Never raises -
        any failure is captured into the result's ``warning``."""
        from services.smart_playlist import filter_model as smart_playlist
        from services.smart_playlist.log import get_smart_playlist_logger
        from server import server_registry, smart_playlist_db

        src_server_id = str(item.get("source_server_id") or "")
        src_playlist_id = str(item.get("source_playlist_id") or "")
        src_user_id = str(item.get("source_user_id") or "")
        dst_server_id = str(item.get("dest_server_id") or "")
        dst_user_id = str(item.get("dest_user_id") or "")
        dest_name_override = item.get("dest_playlist_name")
        # Per-playlist mode: hard_copy
        # forces the static-materialisation path on every destination
        # backend; otherwise a Plex destination gets the filter
        # re-created as a true smart playlist.
        hard_copy = bool(item.get("hard_copy"))

        result: Dict[str, Any] = {
            "source_server_id": src_server_id,
            "source_playlist_id": src_playlist_id,
            "dest_server_id": dst_server_id,
            "source_playlist_name": "",
            "dest_playlist_id": None,
            "dest_backend": "",
            "mode": "",
            "status": "failed",
            "unresolved": [],
            "warning": "",
            "description": "",
        }
        portable_dict: Optional[Dict[str, Any]] = None
        try:
            # 1. Source - must be Plex; read + decode the filter.
            src_conn = server_registry.connect_registered_server(
                src_server_id, run_logger,
            )
            if getattr(src_conn.adapter, "backend", "") != "plex":
                result["warning"] = (
                    "source is not a Plex server; only Plex has smart "
                    "playlists to migrate"
                )
                log_fn("smart-migrate: %s", result["warning"])
                return result
            raw = src_conn.adapter.read_smart_playlist(src_playlist_id)
            if raw is None:
                result["warning"] = (
                    "playlist is missing or not a smart playlist"
                )
                log_fn("smart-migrate: %s (id=%s)",
                       result["warning"], src_playlist_id)
                return result
            result["source_playlist_name"] = raw.playlist_name
            portable = smart_playlist.to_portable(raw)
            portable_dict = smart_playlist.to_dict(portable)
            result["description"] = portable_dict.get("description") or ""
            log_fn("smart-migrate: %r decoded -> %s",
                   raw.playlist_name, result["description"])

            # 2. Destination.
            dst_conn = server_registry.connect_registered_server(
                dst_server_id, run_logger,
            )
            dest_backend = getattr(dst_conn.adapter, "backend", "") or ""
            result["dest_backend"] = dest_backend
            dest_title = str(
                dest_name_override or raw.playlist_name
                or "Migrated smart playlist"
            )

            if dest_backend == "plex" and not hard_copy:
                # Filter mode, Plex destination: re-create as a true
                # smart playlist OWNED BY THE
                # DESTINATION USER. Resolve that user's write context
                # the same way the copy path does (reusing
                # playlist_copy's resolver): under Plex Home
                # per_user_token mode this threads the user's saved
                # token so the playlist is created as them and its
                # filter evaluates against their own library access;
                # in owner_token mode (or non-strict with no saved
                # token) it resolves to the admin / owner context. A
                # genuinely unknown user, or strict_identity_resolution
                # with no saved token, raises here and the outer
                # handler fails the item with a clear warning.
                from services.playlist_copy import (
                    _find_user, _user_context_for, DestUserNotFound,
                )
                dest_user_spec = _find_user(
                    dst_conn.adapter, dst_user_id,
                    on_missing=DestUserNotFound,
                    this_server_id=dst_server_id,
                )
                dest_ctx = _user_context_for(
                    dst_conn, dest_user_spec,
                    role="dest", server_id=dst_server_id,
                )
                # plexapi resolves each tag NAME to this server's own
                # id at create time.
                spec = smart_playlist.from_portable(portable)
                tag_fields = smart_playlist.referenced_tag_fields(
                    raw.decoded, raw.field_types,
                )
                dest_choices = dst_conn.adapter.read_section_tag_choices(
                    section_name=portable.library_name,
                    section_type=portable.library_type,
                    tag_fields=tag_fields,
                    libtype=portable.libtype,
                    user_context=dest_ctx,
                )
                unresolved = smart_playlist.unresolved_against(
                    portable, dest_choices,
                )
                result["unresolved"] = unresolved
                dest_pl_id = dst_conn.adapter.create_smart_playlist(
                    title=dest_title,
                    section_name=portable.library_name,
                    section_type=portable.library_type,
                    libtype=spec.get("libtype"),
                    filters=spec.get("filters"),
                    sort=spec.get("sort"),
                    limit=spec.get("limit"),
                    user_context=dest_ctx,
                )
                result["mode"] = "smart"
                result["dest_playlist_id"] = dest_pl_id or None
                if unresolved:
                    result["status"] = "partial"
                    result["warning"] = (
                        f"{len(unresolved)} filter term(s) had no match on "
                        f"the destination and were dropped"
                    )
                else:
                    result["status"] = "success"
                log_fn(
                    "smart-migrate: re-created smart playlist %r as user "
                    "%r on %s (id=%s, unresolved=%d)",
                    dest_title, dst_user_id, dst_server_id, dest_pl_id,
                    len(unresolved),
                )
            else:
                # Static-materialisation path. Reached when the
                # operator chose hard_copy (any destination), or when
                # the destination is Jellyfin / Emby (no smart-playlist
                # concept). copy_playlist's materialize_smart_as_static
                # evaluates the smart playlist's current matches and
                # writes them as a normal static playlist, handling
                # every destination backend + per-user ownership.
                from services import playlist_copy
                copy_res = playlist_copy.copy_playlist(
                    source_server_id=src_server_id,
                    source_user_id=src_user_id,
                    source_playlist_id=src_playlist_id,
                    dest_server_id=dst_server_id,
                    dest_user_id=dst_user_id,
                    dest_playlist_name=dest_title,
                    materialize_smart_as_static=True,
                    logger=get_smart_playlist_logger(),
                )
                result["mode"] = "hard_copy" if hard_copy else "static"
                result["dest_playlist_id"] = (
                    str(copy_res.get("dest_playlist_id") or "") or None
                )
                result["status"] = "success"
                result["warning"] = (
                    "" if hard_copy
                    else "destination has no smart-playlist concept; "
                         "migrated as a static snapshot of the current "
                         "contents"
                )
                log_fn(
                    "smart-migrate: %s %r as a static playlist for user "
                    "%r on %s (%s)",
                    "hard-copied" if hard_copy else "materialised",
                    dest_title, dst_user_id, dst_server_id, dest_backend,
                )
        except Exception as exc:
            result["status"] = "failed"
            result["warning"] = str(exc)
            log_fn("smart-migrate: FAILED for playlist %s: %s",
                   src_playlist_id, exc)
        # Record the outcome (best-effort - never fails the migration).
        try:
            smart_playlist_db.record_migration(
                job_id=job_id,
                source_server_id=src_server_id,
                source_playlist_id=src_playlist_id,
                source_playlist_name=result["source_playlist_name"],
                dest_server_id=dst_server_id,
                dest_backend=result["dest_backend"],
                dest_playlist_id=result["dest_playlist_id"],
                mode=result["mode"],
                portable_filter=portable_dict,
                status=result["status"],
                coverage=None,
                unresolved=result["unresolved"],
                warning=result["warning"],
            )
        except Exception:
            pass
        return result

    def _run_playlist_copy_batch(self, rec: JobRecord) -> None:
        """Batch-mode playlist copy worker.

        Runs N copy_playlist calls in parallel via the helper's
        internal ThreadPoolExecutor (see
        :func:`services.playlist_copy.copy_playlist_batch`). The job
        queue stays serial - only one batch executes at a time - but
        per-item parallelism inside the batch is controlled by the
        ``parallelism`` field on the params dict (falls back to the
        ``playlist_mgmt_batch_workers`` tunable).

        Per-item cancellation is wired through ``rec._per_item_cancel_events``
        (a Dict[int, threading.Event]); the public method
        :meth:`cancel_batch_item` sets the event for one index and the
        helper short-circuits that item at its next phase boundary.
        Whole-batch Stop continues to flow through the shared engine
        stop_event (via runtime_patches) the same way every other job
        uses it.

        The job's ``summary`` carries the batch result dict so the
        frontend can render the collapsed/expandable per-item drill-down
        (Plan section 8a Q6) from the active-deploys panel.
        """
        from services import playlist_copy

        params = rec.params or {}
        raw_items = params.get("items") or []
        # Normalise items to plain dicts (the Pydantic layer hands us
        # validated PlaylistCopyIn instances; convert to dict here so
        # the helper's get(key) accesses work without an isinstance
        # branch).
        items: List[Dict[str, Any]] = [
            dict(it) if not isinstance(it, dict) else it
            for it in raw_items
        ]

        # Resolve parallelism: per-submit override clamped to [1,
        # batch_max_size_tunable]; absent -> default tunable.
        from services.tunables import (
            playlist_mgmt_batch_max_size,
            playlist_mgmt_batch_workers,
        )
        requested = params.get("parallelism")
        if requested is None:
            parallelism = playlist_mgmt_batch_workers()
        else:
            try:
                parallelism = max(1, int(requested))
            except (TypeError, ValueError):
                parallelism = playlist_mgmt_batch_workers()
        # Cap at the global max so a runaway client payload can't
        # spin up unbounded threads.
        max_size = playlist_mgmt_batch_max_size()
        if parallelism > max_size:
            parallelism = max_size

        label = params.get("label") or f"batch of {len(items)} playlist(s)"

        # ── Per-run log directory ──
        #
        # Wire batch jobs into the same per-run-log-directory ecosystem
        # snapshot / restore / direct use. The directory name encodes
        # the SOURCE server slug so Servers > Logs picks up the batch
        # log under that server's group automatically (no UI changes
        # needed). When a batch spans multiple sources, the FIRST
        # source's slug is used (alphabetical stability); a synthetic
        # "multisource" slug surfaces under its own group.
        #
        # The standard logger (plexmigrate) lands every log.info()
        # line into runtime.log within the run dir. The auth-chain
        # trace + per-item progress events both call logger.info() so
        # operators can read the full forensic trail off disk.
        settings = _merge_settings(params, mode="playlist_copy_batch")
        unique_source_slugs: List[str] = []
        seen_src_ids: set = set()
        for it in items:
            sid = str(it.get("source_server_id") or "")
            if sid and sid not in seen_src_ids:
                seen_src_ids.add(sid)
                try:
                    from server import server_registry
                    row = server_registry.get_server_by_id(sid, include_token=False)
                    if row:
                        svc = (row.get("service_type") or "plex").lower()
                        slug = backend_aware_slug(row.get("name") or "", svc)
                        if slug:
                            unique_source_slugs.append(slug)
                except Exception:
                    pass
        if len(unique_source_slugs) == 1:
            primary_slug = unique_source_slugs[0]
        elif len(unique_source_slugs) > 1:
            primary_slug = "multisource"
        else:
            primary_slug = "playlist-batch"
        _set_run_timestamp(primary_slug)
        logger, run_log_dir = _build_logger(
            settings["log_dir"], settings.get("verbose", False),
        )
        rec.run_log_dir = run_log_dir
        logger.info(
            "playlist_copy_batch: job_id=%s label=%r items=%d parallelism=%d "
            "primary_source=%s",
            rec.job_id, label, len(items), parallelism, primary_slug,
        )
        for i, it in enumerate(items):
            logger.info(
                "  item[%d]: source=%s/%s playlist=%s -> dest=%s/%s",
                i,
                it.get("source_server_id") or "?",
                it.get("source_user_id") or "?",
                it.get("source_playlist_id") or "?",
                it.get("dest_server_id") or "?",
                it.get("dest_user_id") or "?",
            )

        # Per-item cancel events: read whatever cancel_batch_item may
        # have already set BEFORE the worker reached us (pre-start
        # cancel is supported via the same dict). The public method
        # creates the dict on first call; we attach an empty one here
        # if it doesn't exist yet so subsequent cancel calls find it.
        with self._lock:
            evt_dict = getattr(rec, "_per_item_cancel_events", None)
            if evt_dict is None:
                evt_dict = {}
                rec._per_item_cancel_events = evt_dict  # type: ignore[attr-defined]
            # Pre-create an Event for every item index up front, under
            # the lock. After this the dict is never structurally
            # mutated again: cancel_batch_item only .get()s + .set()s an
            # existing Event (both thread-safe) and the batch worker
            # threads only .get() it, so they need no lock. setdefault
            # preserves any Event a pre-start cancel already inserted.
            for _idx in range(len(items)):
                evt_dict.setdefault(_idx, threading.Event())

        stop_event = getattr(rec, "_stop_event", None)
        if stop_event is None:
            # Fall through to the shared engine stop_event so the
            # operator's existing Stop button works as it does for
            # every other mode.
            stop_event = state._active_stop_event

        # ── Pre-resolve context labels for the activity feed ──
        #
        # "Item N: failed" alone gives
        # no clue what failed. Pre-resolve server names + per-user
        # usernames + per-playlist names ONCE here, then per-item
        # activity emits render rich context like:
        #   "Item 3 'Movies' (alice on Server A -> bob on Server B):
        #    ITEM_FAILED -- DEST_USER_TOKEN_MISSING ..."
        #
        # Lookups are best-effort: any failure (server not registered,
        # cache miss, db error) falls back to the raw id. We never
        # block batch execution on label resolution.
        from server import media_db, playlist_cache_db, server_registry

        # Server name lookup: id -> human label
        server_name_by_id: Dict[str, str] = {}
        unique_server_ids: set = set()
        for it in items:
            for k in ("source_server_id", "dest_server_id"):
                sid = str(it.get(k) or "")
                if sid:
                    unique_server_ids.add(sid)
        for sid in unique_server_ids:
            try:
                row = server_registry.get_server_by_id(sid, include_token=False)
                if row and row.get("name"):
                    server_name_by_id[sid] = str(row["name"])
            except Exception:
                pass

        # Per-(server, user_id) -> username
        # We resolve managed_users.username for each (server, backend_user_id)
        # pair seen across items. Also handle the case where the item
        # passed a username instead of a backend_user_id (the lookup
        # falls through to the raw id in that case).
        username_by_server_and_id: Dict[Tuple[str, str], str] = {}
        per_server_user_ids: Dict[str, set] = {}
        for it in items:
            for sid_key, uid_key in (
                ("source_server_id", "source_user_id"),
                ("dest_server_id", "dest_user_id"),
            ):
                sid = str(it.get(sid_key) or "")
                uid = str(it.get(uid_key) or "")
                if sid and uid:
                    per_server_user_ids.setdefault(sid, set()).add(uid)
        for sid, uids in per_server_user_ids.items():
            try:
                rows = media_db.list_managed_users(server_id=sid) or []
            except Exception:
                rows = []
            # Build (backend_user_id, username) and (username, username) maps
            for r in rows:
                buid = str(r.get("backend_user_id") or "")
                uname = str(r.get("username") or "")
                if not uname:
                    continue
                if buid and buid in uids:
                    username_by_server_and_id[(sid, buid)] = uname
                if uname in uids:
                    username_by_server_and_id[(sid, uname)] = uname

        # Per-(server, user_id, playlist_id) -> playlist name from
        # the playlist_cache. Cache is best-effort; if it's cold the
        # playlist id surfaces alone in the activity feed.
        playlist_name_by_triple: Dict[Tuple[str, str, str], str] = {}
        per_user_playlist_ids: Dict[Tuple[str, str], set] = {}
        for it in items:
            sid = str(it.get("source_server_id") or "")
            uid = str(it.get("source_user_id") or "")
            pid = str(it.get("source_playlist_id") or "")
            if sid and uid and pid:
                per_user_playlist_ids.setdefault((sid, uid), set()).add(pid)
        for (sid, uid), pids in per_user_playlist_ids.items():
            try:
                cached = playlist_cache_db.list_cached_playlists(
                    server_id=sid, user_id=uid,
                ) or []
            except Exception:
                cached = []
            for row in cached:
                pid = str(row.get("playlist_id") or "")
                name = str(row.get("name") or "")
                if pid in pids and name:
                    playlist_name_by_triple[(sid, uid, pid)] = name

        def _label_for_item(idx: int) -> str:
            """Build a human-readable label for one batch item index.
            Falls back to raw ids when a lookup misses."""
            if idx is None or not (0 <= int(idx) < len(items)):
                return f"item #{idx}"
            it = items[int(idx)]
            ssid = str(it.get("source_server_id") or "")
            dsid = str(it.get("dest_server_id") or "")
            suid = str(it.get("source_user_id") or "")
            duid = str(it.get("dest_user_id") or "")
            pid = str(it.get("source_playlist_id") or "")
            s_server = server_name_by_id.get(ssid) or ssid or "?"
            d_server = server_name_by_id.get(dsid) or dsid or "?"
            s_user = username_by_server_and_id.get((ssid, suid)) or suid or "?"
            d_user = username_by_server_and_id.get((dsid, duid)) or duid or "?"
            pl_name = playlist_name_by_triple.get((ssid, suid, pid)) or pid or "?"
            return (
                f"'{pl_name}' ({s_user}@{s_server} -> {d_user}@{d_server})"
            )

        def _on_progress(ev: Dict[str, Any]) -> None:
            """Translate batch + per-item events into DashboardState
            push_activity + counter updates."""
            try:
                dash = state.get_dashboard()
                if dash is None:
                    return
                kind = ev.get("event")
                if kind == "batch-started":
                    dash.push_activity(
                        "started", "-",
                        f"Batch start: {label} ({ev.get('total')} item(s), "
                        f"parallelism={ev.get('parallelism')})",
                    )
                elif kind == "batch-item-running":
                    # Fired AFTER the per-source semaphore acquire so
                    # the feed shows which items are actually in
                    # flight. Makes the parallel-vs-throttled
                    # distinction obvious.
                    idx = ev.get("index")
                    line = (
                        f"Item {idx} running -> "
                        f"{_label_for_item(int(idx or 0))}"
                    )
                    dash.push_activity("phase", "-", line)
                    logger.info("BATCH %s", line)
                elif kind == "auth-chain":
                    # Surface every auth-chain decision (saved_token /
                    # pin / admin_fallback / admin_owner) for every
                    # in-flight item so the operator can see exactly
                    # which step decided which token to use. Missing
                    # token + admin fallback shows up as
                    # 'admin_fallback used' so it's clear writes are
                    # landing under the admin's account, not the
                    # target user.
                    step = ev.get("step") or "?"
                    outcome = ev.get("outcome") or "?"
                    user = ev.get("user") or "?"
                    role_str = ev.get("role") or "?"
                    server_id_ev = ev.get("server_id") or "?"
                    server_label = (
                        server_name_by_id.get(server_id_ev) or server_id_ev
                    )
                    detail = ev.get("detail") or ""
                    line = (
                        f"Auth [{role_str} {user}@{server_label}]: "
                        f"{step} -> {outcome}"
                    )
                    if detail:
                        line += f"  ({detail})"
                    dash.push_activity("phase", "-", line)
                    # Also persist to the run-log so the
                    # operator can read the full chain off disk later
                    # via Servers > Logs > <source server>.
                    logger.info("AUTH-CHAIN %s", line)
                elif kind == "batch-item-done":
                    idx = ev.get("index")
                    code = ev.get("error_code")
                    msg = ev.get("error_message")
                    written = ev.get("items_written")
                    skipped_items = ev.get("items_skipped_no_match")
                    if ev.get("cancelled"):
                        status = "cancelled"
                    elif ev.get("success"):
                        # Successful copies can still surface useful
                        # detail: "ok (12 written, 3 missed)".
                        bits: List[str] = []
                        if written is not None:
                            bits.append(f"{int(written)} written")
                        if skipped_items:
                            bits.append(f"{int(skipped_items)} missed")
                        status = "ok" + (f" ({', '.join(bits)})" if bits else "")
                    else:
                        status = code or "failed"
                    line = f"Item {idx} {status} -> {_label_for_item(int(idx or 0))}"
                    if (not ev.get("success")) and msg:
                        # Trim very long error messages so the
                        # activity feed stays readable; full text is
                        # available on the per-item drill-down.
                        m = str(msg)
                        if len(m) > _ACTIVITY_MSG_MAX_CHARS:
                            m = m[:_ACTIVITY_MSG_MAX_CHARS] + " ..."
                        line += f"  ::  {m}"
                    dash.push_activity("phase", "-", line)
                    # Full event also gets pinned to disk.
                    # ERROR level for failed items so the standard
                    # ERROR filter picks them up into errors.log.
                    if ev.get("success") or ev.get("cancelled"):
                        logger.info("BATCH %s", line)
                    else:
                        logger.error("BATCH %s", line)
                    # Tick the dashboard's completed counter so the UI
                    # shows ongoing progress in the same shape every
                    # other job uses.
                    if ev.get("success") and not ev.get("cancelled"):
                        with dash._lock:  # type: ignore[attr-defined]
                            dash.completed += 1
                elif kind == "batch-done":
                    summary_line = (
                        f"Batch complete: {ev.get('succeeded')} ok / "
                        f"{ev.get('failed')} failed / "
                        f"{ev.get('skipped')} skipped / "
                        f"{ev.get('cancelled')} cancelled "
                        f"(elapsed {ev.get('elapsed_seconds', 0.0):.2f}s)"
                    )
                    dash.push_activity("done", "-", summary_line)
                    logger.info("BATCH %s", summary_line)
                # ── Phase-level file-log entries so the time-cost
                # breakdown is visible without the dashboard. Filters
                # the noisy per-item 'resolving' ticks down to
                # start + end markers.
                elif kind == "source-loaded":
                    idx = ev.get("index")
                    name = ev.get("playlist_name") or "(playlist)"
                    n = ev.get("item_count")
                    logger.info(
                        "PHASE Item %s source-loaded -> %r (%s item(s))",
                        idx, name, n,
                    )
                elif kind == "resolving":
                    completed = int(ev.get("completed") or 0)
                    total = int(ev.get("total") or 0)
                    # Only emit at start (completed=1), midpoint, and
                    # end to keep the file log readable. The dashboard
                    # gets every tick via push_activity above.
                    if total and (
                        completed == 1
                        or completed == total
                        or (total >= 4 and completed == total // 2)
                    ):
                        resolved = int(ev.get("resolved") or 0)
                        skipped_n = int(ev.get("skipped") or 0)
                        idx = ev.get("index")
                        logger.info(
                            "PHASE Item %s resolving %d/%d "
                            "(written=%d skipped=%d)",
                            idx, completed, total, resolved, skipped_n,
                        )
                elif kind == "writing":
                    idx = ev.get("index")
                    name = ev.get("name") or "(playlist)"
                    n = ev.get("resolved_count")
                    logger.info(
                        "PHASE Item %s writing destination playlist "
                        "%r with %s resolved item(s)...",
                        idx, name, n,
                    )
                elif kind == "done":
                    # Per-item 'done' from copy_playlist (distinct
                    # from batch-done above). Marks the end of the
                    # write phase for this item.
                    idx = ev.get("index")
                    written = ev.get("items_written")
                    skipped_n = ev.get("items_skipped_no_match")
                    logger.info(
                        "PHASE Item %s done (written=%s missed=%s)",
                        idx, written, skipped_n,
                    )
                elif kind == "tier-hit":
                    # Per-item resolution tier hit. Useful to see
                    # which tier is doing the work without scrolling
                    # the debug stream.
                    idx = ev.get("index")
                    tier = ev.get("tier") or "?"
                    title = ev.get("title") or ""
                    logger.info(
                        "PHASE Item %s tier-hit %s -> %r",
                        idx, tier, title,
                    )
            except Exception:
                log.exception(
                    "playlist_copy_batch progress dashboard push failed",
                )

        # Tag every HTTP response fired inside this batch
        # with the job_id so the Network panel can filter the per-
        # request timeline by job. The contextvar propagates through
        # ThreadPoolExecutor.submit via copy_context() (the engine's
        # submit_with_context helper does this automatically; pl-batch
        # threads inherit from the calling context).
        from services.dashboard import job_http_context
        try:
            with job_http_context(rec.job_id):
                result = playlist_copy.copy_playlist_batch(
                    items,
                    parallelism=parallelism,
                    stop_event=stop_event,
                    per_item_cancel_events=evt_dict,
                    progress_cb=_on_progress,
                )
        finally:
            # Always close + finalise the run-log dir so the
            # Servers > Logs tab picks up the run with its PASS/FAIL
            # suffix. Mirrors snapshot/restore/direct finally blocks.
            _finalize_run(logger, run_log_dir)

        rec.summary = {
            "playlist_copy_batch": {
                "total": int(result.get("total") or 0),
                "succeeded": int(result.get("succeeded") or 0),
                "failed": int(result.get("failed") or 0),
                "skipped": int(result.get("skipped") or 0),
                "cancelled": int(result.get("cancelled") or 0),
                "elapsed_seconds": float(result.get("elapsed_seconds") or 0.0),
                "results": list(result.get("results") or []),
                "label": label,
                "parallelism": parallelism,
            },
            "params": {
                k: v for k, v in params.items()
                if k != "items"
            },
        }
        # Surface partial-failure semantics:
        #   * any failed item -> rec.error set so worker_loop transitions
        #     to COMPLETED_WITH_ERRORS rather than green COMPLETED
        #   * all-cancelled (failed == 0, succeeded == 0, cancelled == total)
        #     transitions to CANCELLED via the existing STOPPING/state-
        #     check pipeline if a Stop was issued; if cancels were per-item
        #     only (no global Stop), we report it as completed-with-errors
        #     with an informative error message
        if int(result.get("failed") or 0) > 0:
            rec.error = (
                f"batch had {result['failed']} failed item(s) "
                f"out of {result['total']}; see summary.results for codes."
            )
        elif (
            int(result.get("cancelled") or 0) > 0
            and int(result.get("succeeded") or 0) == 0
            and rec.state != STATE_STOPPING
        ):
            # Per-item cancels with no successful items: surface as
            # completed_with_errors so the operator sees the cancel
            # bookkeeping in the result panel.
            rec.error = (
                f"batch cancelled per-item before any item completed "
                f"({result['cancelled']}/{result['total']})."
            )

    # ── Engine invocation: direct server-to-server transfer ─────────

    def _run_direct(self, rec: JobRecord) -> None:
        """
        Direct transfer: read from one registered Plex and write to
        another without an intermediate file on disk. The orchestrator
        lives in :mod:`services.direct_transfer.engine`; this method just
        handles connection resolution, logger setup, and stop-flag
        threading.

        Fan-out: when ``dest_server_names`` carries more than
        one name the call is forwarded to :func:`run_fan_out_direct`
        and the single-destination resolution / engine call below is
        skipped. ``len == 1`` keeps the existing single-destination
        code path verbatim - the model validator collapses
        ``dest_server_name`` + ``dest_server_names`` into a list of one
        for older clients.
        """
        settings = _merge_settings(rec.params, mode="direct")

        # Same state stash as
        # _run_restore so direct-transfer restores see the end user's
        # mixed-media config.
        try:
            _apply_mixed_media_state(settings)
        except Exception:
            log.exception(
                "mixed-media config resolution failed for direct transfer; "
                "running without operator overrides."
            )

        src_name = settings.get("source_server_name")
        dest_names = _resolve_dest_names(settings)
        if not src_name:
            raise ValueError("source_server_name is required for a direct transfer.")
        if not dest_names:
            raise ValueError(
                "dest_server_name or dest_server_names is required for a direct transfer."
            )
        if any(src_name == d for d in dest_names):
            raise ValueError(
                "Source and destination must be different registered servers."
            )
        if len(set(dest_names)) != len(dest_names):
            raise ValueError("Destinations must be unique.")

        if len(dest_names) > 1:
            self._run_direct_fan_out(rec, settings, src_name, dest_names)
            return

        # Single-destination path - keep ``dest_server_name`` populated
        # for the resolution + log helpers downstream.
        dst_name = dest_names[0]
        settings["dest_server_name"] = dst_name

        # Resolve both connections up front so we fail fast if either
        # is unreachable, rather than half-way through library 1.
        boot_logger = logging.getLogger("plexmigrate")
        if state.get_dashboard():
            state.get_dashboard().push_activity(
                "phase", "-", f"Connecting to source Plex '{src_name}'…",
            )
        src_conn = connect_registered_server(src_name, boot_logger)
        src_server, src_row = src_conn.server, src_conn.row
        if state.get_dashboard():
            state.get_dashboard().push_activity(
                "phase", "-", f"Connecting to destination Plex '{dst_name}'…",
            )
        dst_conn = connect_registered_server(dst_name, boot_logger)
        dst_server, dst_row = dst_conn.server, dst_conn.row

        # Artifact slugs are backend-aware so a direct
        # transfer between same-named-different-backend servers never
        # collides with another server's artifacts.
        src_slug = backend_aware_slug(src_row["name"], src_conn.service_type)
        dst_slug = backend_aware_slug(dst_row["name"], dst_conn.service_type)
        combined_slug = f"{src_slug}-to-{dst_slug}"
        # Libraries are known up-front for direct transfer
        # (end user picks them from the source). Include in the slug.
        _direct_libs = sorted({str(n) for n in (settings.get("libraries") or [])})
        _set_run_timestamp(combined_slug, libraries=_direct_libs)

        # Source + destination tokens are already decrypted on the
        # ServerConnection (services.adapters.PlexAdapter holds them
        # for the engine's direct HTTP helpers). The plaintexts live
        # in the local variables ``src_token`` / ``dst_token`` and on
        # ``settings["plex_token"]`` only because the engine's direct-HTTP
        # helpers read from ``state._plex_token`` (documented residual
        # exposure - see services/state.py).
        src_token = src_conn.token
        dst_token = dst_conn.token

        settings["plex_url"] = dst_row["url"]
        settings["plex_token"] = dst_token
        settings["source_url"] = src_row["url"]
        settings["resolved_server_slug"] = combined_slug
        rec.params = {
            k: v for k, v in settings.items() if k not in ("plex_token",)
        }

        logger, run_log_dir = _build_logger(settings["log_dir"], settings["verbose"])
        rec.run_log_dir = run_log_dir
        _log_pin_preflight_ack(rec, logger)
        _dump_run_settings("direct_transfer", run_log_dir, rec.params, logger)
        state.MAX_WORKERS = int(settings["workers"])
        state.SCROBBLE_WORKERS = int(settings["scrobble_workers"])
        state._session = _make_session()
        # Direct transfer attributes per-user work to the SOURCE side
        # (users are read from there). The destination's display names
        # are not relevant here - users on the destination match by
        # raw identifier, not friendly name.
        _populate_run_user_context(src_server, source_name=src_name)

        # Cross-backend Replace-mode refusal. Run BEFORE the
        # pre-replace safety snapshot fires so an unrunnable job
        # doesn't burn the auto-capture budget. Same-backend Replace
        # continues unchanged.
        _refusal = _cross_backend_replace_requires_mappings(
            source_server_id=src_row.get("id") or "",
            source_service_type=src_conn.service_type or "plex",
            dest_server_id=dst_row.get("id") or "",
            dest_service_type=dst_conn.service_type or "plex",
            mode=str(settings.get("mode") or "merge"),
            ignore_library_mapping=bool(
                settings.get("ignore_library_mapping") or False
            ),
        )
        if _refusal:
            raise ValueError(_refusal)

        # Pre-Replace safety belt. Direct transfer writes into
        # the destination using the same primitive as restore, so the
        # same overwrite semantics apply when mode == "replace". Capture
        # the destination's pre-state before the engine fires so the
        # end user has a rollback point if they picked the wrong source.
        pre_replace_snapshot_id: Optional[str] = None
        restore_mode = str(settings.get("mode") or "merge")
        auto_capture = bool(settings.get("auto_capture_before_replace", True))
        if restore_mode == "replace" and auto_capture:
            dest_server_id_for_belt = _resolve_server_id(dst_name)
            if not dest_server_id_for_belt:
                raise ValueError(
                    "Replace direct-transfer with auto-capture requires a "
                    "registered destination server (couldn't resolve a "
                    "server_id). Re-register the destination from the "
                    "Servers tab."
                )
            # Re-stamp plex_* state for the belt's run_snapshot call so
            # it reads against the DESTINATION (the engine helpers read
            # state._plex_token / state._plex_base_url). The restore-phase
            # values are restored immediately after by the existing setup
            # below; direct transfer keeps both sides' tokens in local
            # variables anyway.
            prev_url = state._plex_base_url
            prev_tok = state._plex_token
            state._plex_base_url = dst_row["url"]
            state._plex_token = dst_token
            try:
                pre_replace_snapshot_id = _capture_pre_replace_snapshot(
                    job_id=rec.job_id,
                    settings=settings,
                    dest_server=dst_server,
                    dest_server_id=dest_server_id_for_belt,
                    dest_server_name=str(dst_row.get("name") or dst_name),
                    dest_url=dst_row["url"],
                    dest_service_type=dst_conn.service_type,
                )
            finally:
                state._plex_base_url = prev_url
                state._plex_token = prev_tok
            rec.summary = {
                **(rec.summary or {}),
                "pre_replace_snapshot_id": pre_replace_snapshot_id,
            }

        remap: Optional[Tuple[str, str]] = None
        if settings.get("remap_old") and settings.get("remap_new"):
            remap = (settings["remap_old"], settings["remap_new"])

        # Hand the keyboard-stub stop event over so /api/job/stop
        # propagates into the orchestrator.
        stop_event = state._active_stop_event

        # v0.9.6 Feature 4: resolve per-user tokens on both sides so
        # direct transfer can carry managed-user data. Each home_users
        # tuple is (username, token, PlexServer-bound-to-that-side).
        # Failures (account.users() unavailable on local-admin tokens
        # or transient network errors) degrade gracefully to an empty
        # list, which collapses back to pre-v0.9.6 owner-only
        # behaviour. The dashboard activity feed gets a phase line
        # from inside ``get_home_users`` so the slow per-user auth
        # burst is visible.
        from services.auth import get_home_users
        try:
            src_home_users = get_home_users(src_server, src_row["url"], logger)
        except Exception as e:
            logger.warning("Could not enumerate source home users: %s", e)
            src_home_users = []
        try:
            dst_home_users = get_home_users(dst_server, dst_row["url"], logger)
        except Exception as e:
            logger.warning("Could not enumerate destination home users: %s", e)
            dst_home_users = []

        # ``user_filter`` arrives as either None (include every
        # transferable user) or a list of managed usernames. The model
        # constraint already rejects malformed inputs at the API layer.
        raw_filter = settings.get("user_filter")
        user_filter: Optional[List[str]]
        if raw_filter is None:
            user_filter = None
        elif isinstance(raw_filter, list):
            user_filter = [str(u) for u in raw_filter]
        else:
            user_filter = None

        # v0.9.7 Item 4: only show the dashboard's ``current_user``
        # row when this run is *deliberately* scoped to a specific
        # subset of users - i.e. direct transfer with a non-empty
        # filter. Standard snapshot / import / unscoped direct transfer
        # leaves the row hidden so the header doesn't lock onto one
        # user for minutes at a time.
        state._current_user_visible = bool(user_filter)

        # If EITHER side is non-Plex, route
        # through the adapter engines (snapshot via source adapter,
        # restore via destination adapter, in-memory chain). Plex<->Plex
        # stays on the perf-tuned run_direct_transfer.
        src_service = (src_conn.service_type or "plex").lower()
        dst_service = (dst_conn.service_type or "plex").lower()
        if src_service != "plex" or dst_service != "plex":
            self._run_direct_via_adapter(
                rec=rec,
                src_conn=src_conn,
                dst_conn=dst_conn,
                settings=settings,
                logger=logger,
                run_log_dir=run_log_dir,
                stop_event=stop_event,
            )
            return

        run_direct_transfer(
            source_server=src_server,
            source_url=src_row["url"],
            source_token=src_token,
            source_owner=src_row.get("owner_name") or "Plex Owner",
            dest_server=dst_server,
            dest_url=dst_row["url"],
            dest_token=dst_token,
            dest_owner=dst_row.get("owner_name") or "Plex Owner",
            library_names=list(settings.get("libraries") or []),
            logger=logger,
            log_dir=run_log_dir,
            remap=remap,
            strict_match=bool(settings.get("strict_match", True)),
            stop_event=stop_event,
            # v0.9.1: where chained-fallback temp files (if any) land.
            output_dir=settings.get("output_dir") or None,
            # v0.9.6 Feature 4: managed-user roster + filter.
            source_home_users=src_home_users,
            dest_home_users=dst_home_users,
            user_filter=user_filter,
            skip_collections=bool(settings.get("skip_collections") or False),
            fast_collection_detection=bool(settings.get("fast_collection_detection") or False),
            skip_playlists=bool(settings.get("skip_playlists") or False),
            # Four-flag data-type filter.
            include_watch_history=bool(settings.get("include_watch_history", True)),
            include_ratings=bool(settings.get("include_ratings", True)),
            include_playlists=bool(settings.get("include_playlists", True)),
            include_collections=bool(settings.get("include_collections", True)),
            mode=str(settings.get("mode") or "merge"),
            merge_watch_strategy=str(settings.get("merge_watch_strategy") or "higher"),
            # Per-library metric filter forwarded to direct transfer.
            # The direct-transfer entry point hands this to both the
            # source snapshot phase and the destination restore phase
            # so per-library choices apply end-to-end.
            library_metrics=settings.get("library_metrics") or None,
            # Per-run override that bypasses the library mapping table.
            # Matches the restore-from-snapshot path above. Source +
            # dest server IDs are required so the mapping consult in
            # services.restore.plex_native can identify the pair correctly.
            ignore_library_mapping=bool(
                settings.get("ignore_library_mapping") or False
            ),
            # Per-run library name overrides.
            # Same shape as the restore path's matching field.
            library_mapping_overrides=(
                settings.get("library_mapping_overrides") or None
            ),
            source_server_id=src_row.get("id") or "",
            dest_server_id=dst_row.get("id") or "",
        )

        # Part B: run-level finalize phase so the dashboard doesn't
        # look frozen at 100% during the post-engine close-out.
        _dash = state.get_dashboard()
        if _dash is not None:
            _dash.set_finalizing("finalizing run")
        _finalize_run(logger, run_log_dir)

    # ── Fan-out dispatch ─────────────────────────────────────────────

    def _run_direct_fan_out(
        self,
        rec: JobRecord,
        settings: Dict[str, Any],
        src_name: str,
        dest_names: List[str],
    ) -> None:
        """
        Hand off a multi-destination direct transfer to
        :func:`services.fan_out.coordinator.run_fan_out_direct`.

        The fan-out coordinator owns its own per-destination dashboards
        and log dirs, so this method intentionally does *not* call
        ``_build_logger`` / ``_set_run_timestamp`` / ``state._dashboard
        =`` here - the placeholder DashboardState set in the worker
        loop will be replaced per-destination inside the coordinator.

        The record's ``run_log_dir`` is set to the parent log directory
        root so the run-dir browser surface in the UI still resolves;
        per-destination subdirectories live inside it.
        """
        # Strip the token from the recorded params before publishing.
        rec.params = {k: v for k, v in settings.items() if k != "plex_token"}
        rec.run_log_dir = settings.get("log_dir") or "./plex_logs"

        # MAX_WORKERS / SCROBBLE_WORKERS are plain module globals shared
        # by every destination (per-destination caps don't make sense -
        # these bound the engine's internal thread pools). Set them on
        # the worker-loop thread BEFORE spawning so each destination
        # sees the freshly-requested values rather than whatever the
        # prior job left behind.
        state.MAX_WORKERS = int(settings.get("workers") or state.MAX_WORKERS)
        state.SCROBBLE_WORKERS = int(
            settings.get("scrobble_workers") or state.SCROBBLE_WORKERS
        )
        # Bug fix: the engine's restore_ratings path writes via
        # ``state._session.put(...)``. Single-destination jobs init
        # this in their own _run_* method, but the fan-out dispatch
        # paths did not - so the first ratings write of a fan-out
        # job hit ``NoneType.put`` whenever state._session hadn't
        # been initialised by a prior job. Initialise here, before
        # any destination worker runs.
        state._session = _make_session()

        raw_filter = settings.get("user_filter")
        if raw_filter is None:
            user_filter: Optional[List[str]] = None
        elif isinstance(raw_filter, list):
            user_filter = [str(u) for u in raw_filter]
        else:
            user_filter = None

        remap: Optional[Tuple[str, str]] = None
        if settings.get("remap_old") and settings.get("remap_new"):
            remap = (settings["remap_old"], settings["remap_new"])

        stop_event = state._active_stop_event

        # Dispatch on the source + destination service_types. The
        # existing run_fan_out_direct path is Plex<->Plex only - it
        # connects both ends as PlexServer and passes them through to
        # run_direct_transfer in each worker, which calls
        # ``dest_server.library.sections()`` directly on the plexapi
        # handle. If the source OR any destination is non-Plex, we
        # route to the adapter-aware fan-out which captures the source
        # via the adapter engine and dispatches restores per-destination
        # through ``_run_direct_via_adapter`` - the same dispatch the
        # single-destination path uses for mixed-backend transfers.
        from server import server_registry as _registry
        _src_row = _registry.get_server_by_name(src_name) if src_name else None
        if _src_row is None:
            _src_row = _registry.get_server_by_id(src_name, include_token=False)
        _src_service = (
            (_src_row.get("service_type") if _src_row else None) or "plex"
        ).lower()

        def _dest_service(name: str) -> str:
            row = _registry.get_server_by_name(name) if name else None
            if row is None:
                row = _registry.get_server_by_id(name, include_token=False)
            return (
                (row.get("service_type") if row else None) or "plex"
            ).lower()

        _dst_services = [_dest_service(n) for n in dest_names]
        if _src_service != "plex" or any(s != "plex" for s in _dst_services):
            # Sequential per-destination via _run_direct_via_adapter.
            # Each dest re-reads the source (matches existing fan-out
            # behaviour; capture-once optimisation is a follow-up).
            self._run_direct_fan_out_via_adapter(
                rec=rec,
                src_name=src_name,
                dest_names=dest_names,
                settings=settings,
                stop_event=stop_event,
            )
            return

        result: FanOutResult = run_fan_out_direct(
            source_name=src_name,
            dest_names=list(dest_names),
            libraries=list(settings.get("libraries") or []),
            user_filter=user_filter,
            remap=remap,
            strict_match=bool(settings.get("strict_match", True)),
            output_dir=settings.get("output_dir") or None,
            workers=int(settings.get("workers") or state.MAX_WORKERS),
            scrobble_workers=int(
                settings.get("scrobble_workers") or state.SCROBBLE_WORKERS
            ),
            verbose=bool(settings.get("verbose") or False),
            log_dir_root=settings.get("log_dir") or "./plex_logs",
            stop_event=stop_event,
            run_trigger=str(settings.get("_trigger") or "manual"),
            schedule_name=str(settings.get("_schedule_name") or ""),
            skip_collections=bool(settings.get("skip_collections") or False),
            fast_collection_detection=bool(settings.get("fast_collection_detection") or False),
            skip_playlists=bool(settings.get("skip_playlists") or False),
            # Four-flag data-type filter (fan-out direct).
            include_watch_history=bool(settings.get("include_watch_history", True)),
            include_ratings=bool(settings.get("include_ratings", True)),
            include_playlists=bool(settings.get("include_playlists", True)),
            include_collections=bool(settings.get("include_collections", True)),
            mode=str(settings.get("mode") or "merge"),
            merge_watch_strategy=str(settings.get("merge_watch_strategy") or "higher"),
            pre_replace_settings=_build_pre_replace_settings(settings),
            # Destination concurrency cap. 0 = unlimited.
            # Direct transfer doesn't expose a
            # library_workers axis (the per-destination engine is
            # serial across libraries), so only the destination
            # axis is wired here.
            destination_workers=int(settings.get("fan_out_destination_workers") or 0),
        )
        _apply_fan_out_result(rec, result)

    def _run_direct_fan_out_via_adapter(
        self,
        *,
        rec: JobRecord,
        src_name: str,
        dest_names: List[str],
        settings: Dict[str, Any],
        stop_event: Optional[threading.Event],
    ) -> None:
        """Multi-destination direct transfer when the source OR any
        destination is non-Plex.

        The existing run_fan_out_direct path is Plex<->Plex only - it
        calls ``services.direct_transfer.engine.run_direct_transfer`` which
        pokes plexapi internals (``server.library.sections()``) on
        both ends. This method handles every other backend combination
        (P->J, P->E, J->*, E->*) by dispatching each destination
        sequentially through ``_run_direct_via_adapter``, which uses
        the snapshotter+restorer adapter engines.

        Each destination re-reads from source (matches the wastefulness
        of the existing Plex fan-out path); capture-once + N-restore
        optimisation is a follow-up.

        Per-destination errors are caught and recorded on a
        FanOutResult; one failed destination does not abort the rest.
        """
        from services.fan_out.coordinator import (
            FanOutResult, FanOutDestResult,
            _make_pending_dashboard, _build_per_dest_log_dir,
        )
        from server import server_registry
        log_dir_root = settings.get("log_dir") or "./plex_logs"
        rec.params = {k: v for k, v in settings.items() if k != "plex_token"}
        rec.run_log_dir = log_dir_root

        result = FanOutResult()
        for dest_name in dest_names:
            result.destinations.append(FanOutDestResult(
                dest_name=dest_name,
                log_dir="",
                dashboard=_make_pending_dashboard(dest_name),
            ))

        log = logging.getLogger("plexmigrate.fanout.adapter")
        for idx, dest_name in enumerate(dest_names):
            dest_result = result.destinations[idx]
            if stop_event is not None and stop_event.is_set():
                dest_result.state = "cancelled"
                result.cancelled = True
                continue
            dest_result.state = "running"
            dest_result.started_at = time.time()
            try:
                src_conn = server_registry.connect_registered_server(
                    src_name, log,
                )
                dst_conn = server_registry.connect_registered_server(
                    dest_name, log,
                )
                # Build a per-destination log dir + logger. Each
                # destination gets its own run-log subdir under
                # log_dir_root so the existing log browser surfaces
                # per-destination logs uniformly.
                run_log_dir = _build_per_dest_log_dir(
                    log_dir_root, src_name, dest_name,
                    bool(settings.get("verbose") or False),
                    source_service_type=src_conn.service_type,
                    dest_service_type=dst_conn.service_type,
                )
                dest_result.log_dir = run_log_dir
                logger_for_dest, _resolved = _build_logger(
                    run_log_dir, bool(settings.get("verbose") or False),
                )
                # Per-destination snapshot+restore via the existing
                # single-dest adapter dispatch.
                self._run_direct_via_adapter(
                    rec=rec,
                    src_conn=src_conn,
                    dst_conn=dst_conn,
                    settings=settings,
                    logger=logger_for_dest,
                    run_log_dir=run_log_dir,
                    stop_event=stop_event,
                )
                dest_result.state = "completed"
            except Exception as exc:
                log.exception(
                    "fan-out adapter dispatch: destination %r failed",
                    dest_name,
                )
                dest_result.state = "failed"
                dest_result.error = safe_error(exc)
            finally:
                dest_result.finished_at = time.time()
        _apply_fan_out_result(rec, result)

    def _run_restore_fan_out(
        self,
        rec: JobRecord,
        settings: Dict[str, Any],
        dest_names: List[str],
    ) -> None:
        """
        Hand off a multi-destination import to
        :func:`services.fan_out.coordinator.run_fan_out_restore`. The single-server
        connect+resolve dance is repeated per-destination inside the
        coordinator, so this dispatcher just normalises params.
        """
        rec.params = {k: v for k, v in settings.items() if k != "plex_token"}
        rec.run_log_dir = settings.get("log_dir") or "./plex_logs"

        # MAX_WORKERS / SCROBBLE_WORKERS - see note in
        # ``_run_direct_fan_out`` above. Same rationale, same fix.
        state.MAX_WORKERS = int(settings.get("workers") or state.MAX_WORKERS)
        state.SCROBBLE_WORKERS = int(
            settings.get("scrobble_workers") or state.SCROBBLE_WORKERS
        )
        # Same NoneType.put fix as _run_direct_fan_out - restore_ratings
        # writes via ``state._session.put(...)`` and the fan-out import
        # path was not initialising it before destinations spawned.
        state._session = _make_session()

        remap: Optional[Tuple[str, str]] = None
        if settings.get("remap_old") and settings.get("remap_new"):
            remap = (settings["remap_old"], settings["remap_new"])

        stop_event = state._active_stop_event

        result: FanOutResult = run_fan_out_restore(
            dest_names=list(dest_names),
            input_files=list(settings.get("input_files") or []),
            remap=remap,
            strict_match=bool(settings.get("strict_match", True)),
            workers=int(settings.get("workers") or state.MAX_WORKERS),
            scrobble_workers=int(
                settings.get("scrobble_workers") or state.SCROBBLE_WORKERS
            ),
            verbose=bool(settings.get("verbose") or False),
            log_dir_root=settings.get("log_dir") or "./plex_logs",
            output_dir=settings.get("output_dir"),
            stop_event=stop_event,
            # Four-flag data-type filter (fan-out import).
            # The Pydantic validator translates any legacy skip_* into
            # include_* upstream.
            include_playlists=bool(settings.get("include_playlists", True)),
            include_watch_history=bool(settings.get("include_watch_history", True)),
            include_ratings=bool(settings.get("include_ratings", True)),
            include_collections=bool(settings.get("include_collections", True)),
            mode=str(settings.get("mode") or "merge"),
            merge_watch_strategy=str(settings.get("merge_watch_strategy") or "higher"),
            pre_replace_settings=_build_pre_replace_settings(settings),
            # Per-job user filter forwarded to each
            # destination in the fan-out. The fan-out helper passes
            # it straight to run_restore.
            user_filter=settings.get("user_filter"),
            # Two-axis concurrency for fan-out restore.
            # destination_workers caps how many destinations run at
            # once (0 = no cap). library_workers is
            # forwarded into each destination's own run_restore call
            # so the within-destination library concurrency stays
            # consistent with the single-destination restore path.
            destination_workers=int(settings.get("fan_out_destination_workers") or 0),
            library_workers=int(settings.get("restore_library_workers") or 3),
        )
        _apply_fan_out_result(rec, result)


# ── Helpers used by both snapshot and import paths ──────────────────────────────

class _JobCancelled(Exception):
    """Raised when stop_event is set before the engine call completes."""


def _log_pin_preflight_ack(rec: JobRecord, logger: logging.Logger) -> None:
    """
    Write the end user-acknowledged at-risk user list to the
    per-run logger so the audit trail lives in the run's ``runtime.log``
    alongside the engine's own output. No-op when the end user did
    not see the preflight modal (the flag is False/absent).

    The flag is stamped onto ``rec.params`` by
    :func:`server.app._apply_preflight_ack` at job-submit time and is
    purely informational here: the engine does not change behaviour
    based on it. Visibility is the whole point.
    """
    if not rec.params.get("_pin_preflight_acknowledged"):
        return
    at_risk = rec.params.get("_pin_preflight_at_risk") or []
    at_risk_str = ", ".join(at_risk) if at_risk else "(none specified)"
    logger.warning(
        "PR-12 preflight acknowledged by operator: %d at-risk user(s): %s. "
        "These users have no stored auth token or PIN; the engine will "
        "fall back to admin-token impersonation for them, which may "
        "return incomplete data for PIN-scoped content.",
        len(at_risk), at_risk_str,
    )


def _apply_mixed_media_state(settings: Dict[str, Any]) -> None:
    """Resolve the end user's
    per-run + global mixed-media config and stash on services.state
    so the engine sees it without per-call kwarg plumbing.

    Resolution chain (lowest to highest priority):
      1. tunable defaults (services.tunables)
      2. server_data/settings.json `mixed_media` section
      3. per-run override fields on the job submit body
      4. per-user overrides from cross_platform_resolutions (handled
         in the engine, not here)

    No-op when neither the global nor per-run config provides any
    setting (state.set_mixed_media_config(None) → engine treats every
    row as pass-through, matching legacy behaviour)."""
    from services.restore import mixed_media as _mm
    from services import tunables as _t
    try:
        global_settings = (load_settings() or {}).get("mixed_media") or {}
    except Exception:
        global_settings = {}
    per_run = {
        "behavior": settings.get("mixed_media_behavior"),
        "dominance_threshold": settings.get("mixed_media_dominance_threshold"),
        "video_routing": settings.get("mixed_media_video_routing"),
        "logging": settings.get("mixed_media_logging"),
        "collision_handling": settings.get("mixed_media_collision_handling"),
    }
    tunable_fallbacks = {
        "behavior": _t.mixed_media_behavior(),
        "dominance_threshold": _t.mixed_media_dominance_threshold(),
        "video_routing": _t.mixed_media_video_routing(),
        "logging": _t.mixed_media_logging(),
        "collision_handling": _t.mixed_media_collision_handling(),
    }
    cfg = _mm.resolve_config_chain(
        per_run_overrides=per_run,
        global_settings=global_settings,
        tunable_fallbacks=tunable_fallbacks,
    )
    # Per-user overrides: cross_platform_resolutions[dest_id].per_user[username]
    # may carry a mixed_media block. We flatten by username across all
    # destinations into one map; the engine then picks per source user.
    per_user_configs: Dict[str, Any] = {}
    cpr = settings.get("cross_platform_resolutions") or {}
    if isinstance(cpr, dict):
        for _dest_id, dest_block in cpr.items():
            if not isinstance(dest_block, dict):
                continue
            users_block = dest_block.get("per_user") or {}
            if not isinstance(users_block, dict):
                continue
            for username, user_block in users_block.items():
                if not isinstance(user_block, dict):
                    continue
                mm_override = user_block.get("mixed_media") or {}
                if not isinstance(mm_override, dict) or not mm_override:
                    continue
                per_user_configs[username] = _mm.resolve_config_chain(
                    per_user_overrides=mm_override,
                    per_run_overrides=per_run,
                    global_settings=global_settings,
                    tunable_fallbacks=tunable_fallbacks,
                )
    state.set_mixed_media_config(cfg, per_user_configs or None)


def _resolve_dest_names(settings: Dict[str, Any]) -> List[str]:
    """
    Return the ordered destination-server list for a direct-transfer or
    import job.

    The Pydantic model validator already collapses ``dest_server_name``
    and ``dest_server_names`` into the plural form, but this helper
    keeps working for callers that supply the singular form only (CLI
    invocations, older scheduler payloads). Empty
    strings are dropped; duplicates are preserved as the validator's
    job. Returns an empty list if neither field has any value - the
    caller decides whether that's an error.
    """
    plural = settings.get("dest_server_names") or []
    if isinstance(plural, list) and any(isinstance(n, str) and n.strip() for n in plural):
        return [n.strip() for n in plural if isinstance(n, str) and n.strip()]
    singular = settings.get("dest_server_name")
    if isinstance(singular, str) and singular.strip():
        return [singular.strip()]
    return []


def _apply_fan_out_result(rec: JobRecord, result: "FanOutResult") -> None:
    """
    Translate a :class:`services.fan_out.coordinator.FanOutResult` into the worker
    loop's terminal-state vocabulary.

    The worker's ``try`` block sets ``rec.state = STATE_COMPLETED``
    after we return cleanly; we raise :class:`_JobCancelled` if the
    fan-out was cancelled, or a plain ``RuntimeError`` if at least one
    destination failed (which the worker turns into ``STATE_FAILED``
    with ``rec.error``).
    """
    # Copy per-destination safety-belt snapshot ids onto the
    # parent JobRecord's summary so the end user can find every
    # destination's rollback point on the Snapshots tab. The single-
    # destination paths set ``summary["pre_replace_snapshot_id"]``
    # (scalar); fan-out sets ``summary["pre_replace_snapshots"]`` as
    # ``{dest_name: snapshot_id}`` so the two shapes are
    # distinguishable downstream.
    pre_map: Dict[str, str] = {}
    for d in result.destinations:
        if d.pre_replace_snapshot_id:
            pre_map[d.dest_name] = d.pre_replace_snapshot_id
    if pre_map:
        rec.summary = {
            **(rec.summary or {}),
            "pre_replace_snapshots": pre_map,
        }

    if result.cancelled and not result.has_failures():
        raise _JobCancelled("Fan-out cancelled before all destinations completed.")
    if result.has_failures():
        # The destination errors are already in the run logs and the
        # WS payload; the JobRecord.error field carries the first one
        # so the failed-job header in the UI surfaces a concrete
        # message instead of a bare "Failed."
        raise RuntimeError(result.first_error() or "fan-out: at least one destination failed.")


def _merge_settings(params: Dict[str, Any], *, mode: str) -> Dict[str, Any]:
    """
    Merge inbound request params on top of saved settings.

    Anything the client omitted (``None``) falls back to the saved
    settings document; anything the client supplied wins. The result
    is a flat dict that's safe to pass to the engine.

    Multi-server (v0.9.0): ``plex_url`` and ``plex_token`` are no
    longer required at this layer - the connection is normally
    resolved later via the registered server name. The legacy v0.8.0
    fields stay accepted so an ad-hoc CLI call (--server URL --token X)
    keeps working.
    """
    base = load_settings()
    merged: Dict[str, Any] = {
        "plex_url": base.get("plex_url") or "",
        "plex_token": base.get("plex_token") or "",
        "output_dir": base["output_dir"],
        "log_dir": base["log_dir"],
        "workers": base["workers"],
        "scrobble_workers": base["scrobble_workers"],
        "verbose": base["verbose"],
        "strict_match": base["strict_match"],
        "libraries": [],
        "input_files": [],
        "remap_old": None,
        "remap_new": None,
        "source_server_name": None,
        "dest_server_name": None,
        # Fan-out destinations. The Pydantic model collapses
        # singular ``dest_server_name`` into this list at the API
        # boundary, but the legacy key stays populated for any
        # downstream code that has not been migrated yet.
        "dest_server_names": None,
    }
    for key, value in params.items():
        if value is None:
            continue
        merged[key] = value
    return merged


def _resolve_source_connection(
    settings: Dict[str, Any], *, logger: logging.Logger
) -> Tuple[Any, str, str, str, str]:
    """
    Resolve the registered server named in ``settings`` to a live
    connection. Strict - requires ``source_server_name`` to identify
    a registered row, and raises if it's missing or unmatched.

    v0.9.1 change: the previous build had a "legacy ad-hoc" fallback
    that used raw ``plex_url`` / ``plex_token`` from ``settings.json``
    when no ``source_server_name`` was supplied. That fallback caused
    the symptom of "every operation hits the first/default server" -
    a request that *should* fail loudly (no server selected) was
    silently succeeding against whichever server happened to be in
    legacy settings. The fallback is gone: every operation resolves a
    server explicitly through the registry.

    Returns ``(PlexServer, url, token, owner_name, slug)``. ``slug``
    is the filename-safe form of the friendly server name, used by
    :func:`_set_run_timestamp` to prefix log dirs and snapshot filenames.
    """
    # Prefer the stable server id when
    # the job params carry one; fall back to (name + service_type).
    # Log id-name disagreement
    # and warn when callers send only a name (name-only resolution
    # can land on the wrong server when names collide across backends).
    source_id = (settings.get("source_server_id") or "").strip()
    name = (settings.get("source_server_name") or "").strip()
    source_service = (settings.get("source_service_type") or "plex").strip().lower()
    if not name and not source_id:
        raise ValueError(
            "No server selected. Pick a registered server in the "
            "Run Job form (or pass --source-server / --dest-server on "
            "the CLI). The server registry is managed under the "
            "Servers tab in the web UI."
        )
    row = None
    if source_id:
        from server.server_registry import get_server_by_id
        row = get_server_by_id(source_id, include_token=False)
        if row is None:
            logger.warning(
                "_resolve_source_connection: source_server_id=%r did "
                "not match any registered row; falling back to name "
                "lookup.",
                source_id,
            )
        elif name and row.get("name") and row["name"] != name:
            # ID won; surface the disagreement so the end user can
            # spot a stale frontend cache or a bug where the picker
            # sent mismatched (id, name) pair.
            logger.warning(
                "_resolve_source_connection: source_server_id=%r "
                "resolved to %r, but request also carried "
                "source_server_name=%r. Using the id; name ignored.",
                source_id, row["name"], name,
            )
    if row is None and name:
        # Name-only lookup risks resolving to the wrong server when
        # two registered servers share the friendly name across
        # backends (Plex Jade.TV + Emby Jade.TV is the canonical
        # case). Surface the risk in the run log so the end user
        # can spot the cause when downstream behaviour is wrong.
        logger.warning(
            "_resolve_source_connection: resolving by name only "
            "(source_server_id not supplied). On name collision "
            "across backends the first match wins. Prefer "
            "source_server_id for ad-hoc submissions."
        )
        row = get_server_by_name(
            name, service_type=source_service or None,
        )
    if row is None:
        raise ValueError(
            f"No registered server matches "
            f"{name!r} (service_type={source_service!r}, id={source_id!r}). "
            f"Open the Servers tab in the web UI to verify."
        )
    # Resolve from this point by id (id is stable across renames).
    resolved_id = str(row.get("id") or "")
    resolved_name = row.get("name") or name
    if state.get_dashboard():
        state.get_dashboard().push_activity(
            "phase", "-", f"Connecting to source '{resolved_name}'…",
        )
    conn = connect_registered_server(resolved_id, logger)
    if state.get_dashboard():
        # The message labels the admin token holder explicitly. A
        # bare "Connected to '<server>' as <owner>" on multi-admin
        # Emby installs misleadingly suggests capture runs
        # FOR that admin user, even when a user_filter narrows the
        # actual capture scope to a different user (e.g. Kai with
        # Ares listed as the admin). The connection always
        # authenticates AS the admin token holder; the data
        # subject is decided later by the user_filter. The explicit
        # admin label lets the operator distinguish "admin
        # credential = Ares" from "data subject = Kai" in the
        # activity feed.
        _admin_name = conn.row.get('owner_name') or '?'
        state.get_dashboard().push_activity(
            "started", "-",
            f"Connected to '{resolved_name}' (admin: {_admin_name})",
        )
    # ``conn.token`` is the already-decrypted plaintext; the engine's
    # direct-HTTP helpers (/:/scrobble, /:/rate, etc.) consume it via
    # ``state._plex_token``.
    return (
        conn.server, conn.url, conn.token,
        conn.row.get("owner_name") or "Plex Owner",
        backend_aware_slug(conn.row["name"], conn.service_type),
    )


def _populate_run_user_context(
    server: Any,
    source_name: Optional[str] = None,
    *,
    source_server_id: Optional[str] = None,
    source_service_type: Optional[str] = None,
) -> None:
    """
    Populate the run-scoped user context the dashboard reads from
    (v0.9.6 Feature 1 + 3).

    - ``state._plex_owner_email`` is set to the connected account's
      Plex.tv email so the per-library "owner phase" can attribute
      ``current_user`` to the owner identifier the
      ``user_display_names`` map is keyed by.
    - When ``source_name`` matches a registry row, its cached
      ``user_display_names`` dict is copied into the live
      :class:`services.dashboard.DashboardState` once at run start
      so the frontend can substitute display names without a
      per-tick REST hit. The map is otherwise rebuilt by the next
      ``Servers`` tab visit.

    Both operations are best-effort - failures here must not block
    the actual run.
    """
    try:
        email = getattr(server.myPlexAccount(), "email", None) or ""
        state._plex_owner_email = str(email)
    except Exception:
        # myPlexAccount() requires a Plex.tv-linked token. Local-admin
        # tokens raise; treat the owner as having no public identifier.
        state._plex_owner_email = ""

    # Carry the cached display-name map into the dashboard so the WS
    # snapshot can ship it to the frontend.
    # Prefer the stable id when supplied so a post-submit rename /
    # duplicate name doesn't surface the wrong server's
    # display-name map.
    if state.get_dashboard() is not None and (source_name or source_server_id):
        try:
            row = None
            if source_server_id:
                from server.server_registry import get_server_by_id
                row = get_server_by_id(source_server_id, include_token=False)
            if row is None and source_name:
                row = get_server_by_name(
                    source_name, include_token=False,
                    service_type=(source_service_type or None),
                )
            if row is not None:
                state.get_dashboard().set_user_display_names(
                    row.get("user_display_names") or {}
                )
        except Exception:
            pass


_LIBRARY_FIELD_RE = re.compile(rb'"library"\s*:\s*"([^"]+)"')


def _peek_library_field(path: Path) -> Optional[str]:
    """Read the first few KB of a .plexexport.json / .plexbackup.json
    file and pull the top-level ``"library": "..."`` value out.

    Used by the restore path to enrich the run-dir slug with the
    libraries being restored, since those names live in the export
    files (not the job settings). Robust to large files: never reads
    more than 8KB; never raises.
    """
    try:
        with open(path, "rb") as fh:
            head = fh.read(8192)
        m = _LIBRARY_FIELD_RE.search(head)
        if m:
            return m.group(1).decode("utf-8", errors="replace")
    except Exception:
        return None
    return None


def _load_snapshot_payload(
    input_path: str, logger: logging.Logger,
) -> Optional[Dict[str, Any]]:
    """Load a snapshot file into the per-server payload dict shape the
    adapter restore engine consumes. Supports both modern ``.db``
    snapshots (via ``server.snapshot_serializer.build_payload_from_db``)
    and legacy ``.plexbackup.json`` files (read as the per-library
    dict and wrapped into the per-server shape).

    Returns None on any failure; the caller logs + skips."""
    from pathlib import Path as _Path
    p = _Path(input_path)
    if not p.is_file():
        logger.warning("snapshot payload load: %s not found", p)
        return None
    suffix = p.suffix.lower()
    try:
        if suffix == ".db":
            from server.snapshot_serializer import build_payload_from_db
            return build_payload_from_db(p)
        # Treat anything else (.json / .plexbackup.json / etc.) as
        # legacy single-library JSON. Wrap into the per-server
        # ``libraries`` array shape the adapter restore expects.
        import json as _json
        with open(p, "r", encoding="utf-8") as fh:
            raw = _json.load(fh)
        if isinstance(raw, dict) and "libraries" in raw:
            return raw  # already in per-server shape
        if isinstance(raw, dict):
            return {"libraries": [raw], "snapshot_meta": {}}
    except Exception as exc:
        logger.warning("snapshot payload load %s failed: %s", p, exc)
        return None
    return None


def _peek_libraries_from_inputs(
    input_files: List[str], output_dir: str,
) -> List[str]:
    """Best-effort: walk every input_file path and pull its top-level
    ``library`` field. Mirrors the three-attempt resolution the strict
    file-existence check uses below (direct path, output_dir-scoped,
    legacy/ -scoped). Files that can't be peeked contribute nothing -
    the dir slug just falls back to whatever we did find."""
    libs: List[str] = []
    for f in input_files or []:
        for candidate in (
            Path(f),
            Path(output_dir or "./snapshots") / f,
            Path(output_dir or "./snapshots") / "legacy" / f,
        ):
            if candidate.is_file():
                name = _peek_library_field(candidate)
                if name:
                    libs.append(name)
                break
    return libs


def _persist_run_history_row(rec: JobRecord, job_run_id: str) -> None:
    """
    Best-effort: append one row to ``run_history`` describing the
    just-finished job. The row carries the bits an end user needs to
    answer "what did this run actually do" at a glance: job_type,
    server, libraries, users affected (only users with at least one
    RESTORED entry in the run's restoration log), state, duration,
    plus deep-link flags for the run-settings.log + restoration.log
    files. Reads the JobRecord fields + the per-run state seam
    populated by services.restore.plex_native.

    Called from the worker's central finally block. Never raises:
    failure to record telemetry must not affect the job's recorded
    state.
    """
    if not rec.job_id:
        return
    try:
        from server import run_timings_db
        params = rec.params or {}
        # Determine the per-run server identity. Snapshot + direct
        # transfer carry ``source_server_name``; restore carries
        # ``dest_server_name`` (the destination is the side actually
        # mutated). Fall back to whichever is present.
        server_name = (
            params.get("source_server_name")
            or params.get("dest_server_name")
            or (
                ", ".join(str(d) for d in params.get("dest_server_names") or [])
                if params.get("dest_server_names")
                else ""
            )
        )
        server_id = (
            _resolve_server_id(params.get("source_server_name"))
            or _resolve_server_id(params.get("dest_server_name"))
            or None
        )
        libraries = list(params.get("libraries") or [])
        # ``rec.mode`` is the end user-facing job category. Fan-out
        # paths land here too: rec.mode is the base mode (snapshot /
        # direct / restore) and a multi-destination run is implied by
        # dest_server_names having >1 entry. Surface that in job_type
        # so the panel can distinguish.
        base_type = rec.mode or "unknown"
        is_fan_out = bool(
            params.get("dest_server_names")
            and isinstance(params.get("dest_server_names"), list)
            and len(params.get("dest_server_names") or []) > 1
        )
        if is_fan_out:
            if base_type == "direct":
                job_type = "fan_out_direct"
            elif base_type == "restore":
                job_type = "fan_out_restore"
            else:
                job_type = base_type
        else:
            job_type = base_type

        # Affected-users list comes from the restoration_log writer
        # (only restore + direct populate it; snapshot leaves it empty).
        affected_users: List[str] = []
        try:
            affected_users = list(
                getattr(state, "_restoration_log_affected_users", []) or []
            )
        except Exception:
            affected_users = []

        duration_ms = 0
        if rec.started_at and rec.finished_at:
            duration_ms = int(max(0.0, rec.finished_at - rec.started_at) * 1000)

        # Detect the companion log files alongside the run dir so the
        # Recent Runtimes panel can render deep-link buttons.
        run_log_dir = rec.run_log_dir or ""
        has_settings_log = False
        has_restoration_log = False
        if run_log_dir:
            try:
                _settings = Path(run_log_dir) / "run-settings.log"
                has_settings_log = _settings.is_file()
            except Exception:
                has_settings_log = False
            try:
                _restore = Path(run_log_dir) / "restoration.log"
                has_restoration_log = _restore.is_file()
            except Exception:
                has_restoration_log = False

        run_timings_db.record_run_history(
            run_id=job_run_id or rec.job_id,
            started_at=float(rec.started_at or 0.0),
            finished_at=float(rec.finished_at or time.time()),
            job_type=str(job_type),
            server_id=server_id,
            server_name=server_name,
            libraries=libraries,
            users_affected=len(affected_users),
            users_affected_list=affected_users,
            state=str(rec.state),
            duration_ms=duration_ms,
            run_log_dir=run_log_dir or None,
            has_settings_log=has_settings_log,
            has_restoration_log=has_restoration_log,
            error_summary=(rec.error or None),
        )
    except Exception:
        # Already logged at the call site; swallow here too so a
        # telemetry write hiccup never propagates further.
        log.exception("run_history record_run_history call raised")


def _dump_run_settings(
    job_type: str,
    run_log_dir: str,
    job_params: Optional[Dict[str, Any]],
    logger: logging.Logger,
) -> None:
    """
    Best-effort: write ``run-settings.json`` alongside the run log so a
    future reviewer can see the tunables, persistent settings, and job
    params that were in effect when this run started. Failures are
    swallowed (logged at warning level) and never abort the job.

    ``job_params`` is typically ``rec.params``, which jobs already
    sanitise of the plex_token. The writer scrubs known secret keys
    independently as a defence in depth.
    """
    try:
        from services.run_logs.run_settings import write_run_settings
        write_run_settings(
            run_log_dir=run_log_dir,
            job_type=job_type,
            job_params=job_params,
            logger=logger,
        )
    except Exception:
        try:
            logger.warning("run-settings dump skipped: writer import failed")
        except Exception:
            pass


def _cross_backend_replace_requires_mappings(
    *,
    source_server_id: str,
    source_service_type: str,
    dest_server_id: str,
    dest_service_type: str,
    mode: str,
    ignore_library_mapping: bool,
) -> Optional[str]:
    """Safety belt: refuse a
    cross-backend Replace-mode run when zero library mappings have
    been declared between the two servers.

    Replace mode is destructive - it makes the destination match the
    source exactly, removing destination-side state that's not in
    the source. When source and destination are different backends
    (Plex ↔ Jellyfin / Emby), library equivalence is NOT
    self-evident from library names; the operator must declare which
    library on side A corresponds to which on side B. Without a
    declaration, Replace can't know which destination library to
    overwrite, and the existing per-library skip-on-no-name-match
    would silently produce a no-op that the operator misreads as
    success.

    Returns:
      - None when the run is safe to proceed.
      - A human-readable error message when the run MUST be refused.

    The check only fires when:
      1. mode == "replace"
      2. ignore_library_mapping is False (operator hasn't opted out)
      3. source and dest backends differ (cross-backend)
      4. ZERO mapping rows exist between the two servers

    Same-backend Replace stays unaffected because exact-name matching
    typically works on a same-backend transfer. Operators with a
    same-backend rename can either use the mapping table OR enable
    the per-run ignore_library_mapping override.
    """
    if str(mode or "").lower() != "replace":
        return None
    if ignore_library_mapping:
        return None
    src_svc = (source_service_type or "").strip().lower()
    dst_svc = (dest_service_type or "").strip().lower()
    if not src_svc or not dst_svc:
        return None
    if src_svc == dst_svc:
        return None
    if not source_server_id or not dest_server_id:
        return None
    try:
        from server import library_mapping_db
        rows = library_mapping_db.list_mappings_for_pair(
            source_server_id, dest_server_id,
        ) or []
    except Exception:
        # If the mapping DB can't be read, fail safe by refusing the
        # Replace. Better to error visibly than risk a destructive
        # write with unknown library equivalence.
        return (
            "Cross-backend Replace-mode refused: could not read the "
            "library mapping table to verify equivalence. Re-try after "
            "Server Syncing > Library Mapping loads, or enable the "
            "per-run 'Ignore library mapping' override if you really "
            "want exact-name matching only."
        )
    # Operator-confirmed rows (source='operator', either side mapped
    # to a real dest library OR to the explicit-skip sentinel) count
    # toward the gate; auto rows that have not been confirmed do not,
    # because Replace is destructive and we want the operator to have
    # explicitly looked at the pair.
    confirmed = [
        r for r in rows
        if (r.get("source") or "").lower() == "operator"
    ]
    if confirmed:
        return None
    return (
        f"Cross-backend Replace-mode refused: no operator-confirmed "
        f"library mappings exist between the source ({src_svc}) and "
        f"destination ({dst_svc}) servers. Replace mode is destructive "
        f"and the engine cannot know which destination library matches "
        f"each source library without a declaration. Open Server "
        f"Syncing > Library Mapping, declare the equivalences (or "
        f"explicit skips), then re-run. To bypass this check use the "
        f"per-run 'Ignore library mapping' checkbox + exact-name "
        f"matching."
    )


def _resolve_server_id(
    source_server_name: Optional[str],
    *,
    prefer_id: Optional[str] = None,
    service_type: Optional[str] = None,
) -> str:
    """
    Map a friendly ``source_server_name`` (the one end users see in
    the registry) to the registry row's stable ``id`` field used as
    foreign key in media.db. Returns an empty string when no match -
    callers treat that as "no snapshot capture possible."

    Callers that already have the stable id pass it via ``prefer_id``;
    the function verifies it still resolves and short-circuits.
    ``service_type``
    disambiguates same-named-different-backend rows when no id is
    supplied. Both default to None so legacy single-arg callers keep
    working.
    """
    # Path B: id wins when present and still resolves.
    if prefer_id:
        try:
            from server.server_registry import get_server_by_id
            row = get_server_by_id(prefer_id, include_token=False)
            if row is not None:
                return str(row.get("id") or "")
        except Exception:
            pass
        # Fall through to name lookup when the id no longer resolves
        # (server deleted between schedule-create and fire-time).
    if not source_server_name:
        return ""
    try:
        from server.server_registry import list_servers
        target_service = (service_type or "").lower() if service_type else None
        matches: List[str] = []
        for row in list_servers(include_tokens=False):
            if (row.get("name") or "") != source_server_name:
                continue
            if target_service is not None:
                row_service = (row.get("service_type") or "plex").lower()
                if row_service != target_service:
                    continue
            sid = str(row.get("id") or "")
            if sid:
                matches.append(sid)
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            # Name collision with no service_type to disambiguate
            # (e.g. a Plex and an Emby server sharing a friendly
            # name). Refuse to guess: "" is the documented "no match"
            # return and is safer than resolving to the wrong
            # server's id.
            logging.getLogger("plexmigrate.server.jobs").warning(
                "_resolve_server_id: %r matches %d registered servers "
                "and no service_type was given to disambiguate; "
                "treating as unresolved.",
                source_server_name, len(matches),
            )
    except Exception:
        return ""
    return ""


def _build_pre_replace_settings(settings: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Build the per-destination safety-belt payload for fan-out workers.

    Returns ``None`` when no belt should fire - either Merge mode or
    the end user explicitly disabled auto-capture. Returns a small
    dict with just the fields _capture_pre_replace_snapshot needs
    when the belt should run.

    Per-destination snapshots all share the same data-type filter
    (the upcoming Replace's include_* flags) so they capture exactly
    what's about to be overwritten on each destination. Log dir +
    output dir + verbose are inherited from the parent job; the
    helper writes its own per-destination sub-log-dir underneath.
    """
    if str(settings.get("mode") or "merge") != "replace":
        return None
    if not bool(settings.get("auto_capture_before_replace", True)):
        return None
    return {
        "include_watch_history": bool(settings.get("include_watch_history", True)),
        "include_ratings":       bool(settings.get("include_ratings", True)),
        "include_playlists":     bool(settings.get("include_playlists", True)),
        "include_collections":   bool(settings.get("include_collections", True)),
        "log_dir":               settings.get("log_dir") or "./plex_logs",
        "output_dir":            settings.get("output_dir") or "./snapshots",
        "verbose":               bool(settings.get("verbose") or False),
    }


def _capture_pre_replace_snapshot(
    *,
    job_id: str,
    settings: Dict[str, Any],
    dest_server: Any,
    dest_server_id: str,
    dest_server_name: str,
    dest_url: str,
    dest_service_type: str = "plex",
) -> str:
    """
    Pre-Replace auto-capture safety belt.

    Fires when ``mode == "replace"`` and the end user has the safety
    belt on (the default). Captures a fresh snapshot of the destination
    BEFORE the Replace runs so the end user has a rollback point. The
    snapshot is registered in ``snapshots.db`` with a ``[pre-replace
    safety]`` prefix in its name so it's distinguishable in the
    Snapshots tab.

    Scope: every library on the destination, every data type the
    upcoming Replace will touch (matches the restore's include_*
    flags). The captured payload is what the end user would need to
    restore back to the pre-Replace state, so capturing exactly the
    types about to be overwritten is the right contract.

    Returns the registered snapshot's id on success. **Raises on
    failure** - the caller MUST abort the Replace, since proceeding
    without a recovery point is what the safety belt is supposed to
    prevent. The exception's message is suitable to surface on
    ``rec.error`` directly.

    State handling: this helper restamps ``state._run_timestamp`` to a
    pre-Replace slug, builds its own log subdir, runs the engine
    pipeline, and writes the snapshot artifacts. The caller is then
    free to re-stamp the timestamp + build its own logger for the
    restore phase - the engine's ``reset_run_state`` at the top of
    ``run_restore`` clears the per-run accumulators this helper
    populated.
    """
    log = logging.getLogger("plexmigrate.server.jobs")
    log.info(
        "Pre-Replace safety belt: capturing destination snapshot of %r before Replace.",
        dest_server_name,
    )

    # Build a dedicated sub-log-dir for the pre-snapshot so its runtime/
    # errors/media files don't interleave with the upcoming restore's.
    # Lives under <log_dir>/pre_replace_<slug>_<ts>/ when run_logging
    # is enabled; an empty run_log_dir on the engine's books means the
    # end user turned logging off globally - that's fine, the pre-snapshot
    # still runs.
    parent_log_dir = settings.get("log_dir") or "./plex_logs"
    pre_log_root = str(Path(parent_log_dir) / "pre_replace")
    pre_logger, pre_run_log_dir = _build_logger(
        pre_log_root, bool(settings.get("verbose") or False),
    )

    # Distinct run-timestamp slug so the pre-snapshot's filename and
    # run-log directory don't clash with the restore's. The restore
    # phase below re-stamps when it builds its own logger.
    pre_slug = f"{backend_aware_slug(dest_server_name, dest_service_type)}_pre_replace"
    _set_run_timestamp(pre_slug, libraries=None)

    # Engine state setup. MAX_WORKERS / SCROBBLE_WORKERS / _session
    # are already configured by the caller - the pre-snapshot reuses
    # them. We do switch _snapshot_server_id and _run_trigger so the
    # capture's audit row records the right server + provenance.
    prev_trigger = getattr(state, "_run_trigger", "") or ""
    prev_schedule = getattr(state, "_run_schedule_name", "") or ""
    prev_snapshot_server_id = getattr(state, "_snapshot_server_id", "") or ""
    state._snapshot_server_id = dest_server_id
    state._run_trigger = "pre_replace_safety_belt"
    state._run_schedule_name = ""

    # The pre-snapshot covers every library on the destination - we
    # don't know which subset the Replace payload covers without
    # parsing the input files, so being inclusive is the right
    # default. Data-type filter mirrors the Replace so the rollback
    # captures exactly what's about to change.
    all_sections = list(dest_server.library.sections())

    include_wh = bool(settings.get("include_watch_history", True))
    include_ra = bool(settings.get("include_ratings", True))
    include_pl = bool(settings.get("include_playlists", True))
    include_co = bool(settings.get("include_collections", True))

    # Synthetic rec-shaped object so _capture_snapshot_after_run can
    # read the include_* flags and stamp errors without polluting the
    # parent restore's JobRecord. ``error`` writes are captured here
    # and re-raised below; ``params`` carries the include_* gating.
    pre_job_id = f"{job_id}:pre-replace"

    class _PreSnapshotRec:
        job_id = pre_job_id
        params: Dict[str, Any] = {
            "include_watch_history": include_wh,
            "include_ratings": include_ra,
            "include_playlists": include_pl,
            "include_collections": include_co,
            # Never auto-render the JSON sidecar for pre-Replace
            # snapshots. The end user wants the .db on disk; the
            # JSON copy is on-demand via the Exports tab if they
            # ever need it.
            "prebuild_json_sidecar": False,
        }
        error: Optional[str] = None
        run_log_dir: str = pre_run_log_dir

    pre_rec = _PreSnapshotRec()
    output_dir = settings.get("output_dir") or "./snapshots"

    # Push an activity-feed entry so the dashboard tells the end user
    # what's happening (otherwise the UI would look stuck while the
    # pre-snapshot runs against a large destination).
    if state.get_dashboard() is not None:
        try:
            state.get_dashboard().push_activity(
                "phase", "-",
                f"Pre-Replace safety belt: snapshotting {dest_server_name!r}",
            )
            state.get_dashboard().set_finalizing("capturing pre-Replace safety snapshot")
        except Exception:
            pass

    try:
        run_snapshot(
            dest_server,
            all_sections,
            output_dir,
            pre_logger,
            pre_run_log_dir,
            dest_url,
            include_watch_history=include_wh,
            include_ratings=include_ra,
            include_playlists=include_pl,
            include_collections=include_co,
        )
        _finalize_run(pre_logger, pre_run_log_dir)

        snapshot_id = _capture_snapshot_after_run(
            rec=pre_rec,  # type: ignore[arg-type]
            server_id=dest_server_id,
            server_name=dest_server_name,
            output_dir=output_dir,
            server_slug=pre_slug,
            libraries=[s.title for s in all_sections],
            snapshot_name_prefix="[pre-replace safety]",
        )
    finally:
        # Restore the parent run's trigger / schedule / server-id so
        # the restore phase's audit attribution is correct. The
        # _run_timestamp re-stamp happens in the caller, not here.
        state._run_trigger = prev_trigger
        state._run_schedule_name = prev_schedule
        state._snapshot_server_id = prev_snapshot_server_id

    if not snapshot_id:
        # Either payload was empty (engine never produced output) or
        # snapshot_id wasn't returned. Either way the rollback point
        # is not viable - refuse to proceed.
        err = pre_rec.error or "pre-Replace snapshot produced no registered artifact"
        raise RuntimeError(
            f"Pre-Replace safety snapshot failed: {err}. "
            "Replace aborted to prevent data loss without a recovery point. "
            "Re-run with auto-capture disabled to bypass (NOT recommended)."
        )

    log.info(
        "Pre-Replace safety belt: captured snapshot %s for %r (rollback point).",
        snapshot_id, dest_server_name,
    )
    return snapshot_id


def _capture_snapshot_after_run(
    *,
    rec: JobRecord,
    server_id: str,
    server_name: str,
    output_dir: str,
    server_slug: str,
    libraries: List[str],
    snapshot_name_prefix: str = "",
) -> Optional[str]:
    """
    Snapshot capture (Rule 1 - payload-direct edition).

    The engine fetches every metric live during ``snapshot_library``
    and appends its per-library ``export_data`` to
    ``state._snapshot_payloads``. By the time this wrapper runs we
    have the full set of live-fetched payloads in memory. The
    snapshot .db is built directly from THAT data via
    :func:`snapshot_capture.build_snapshot_db_from_payloads` -
    media.db is no longer the source of truth for snapshot content
    (it remains a cumulative side-effect cache for the resolver's
    Tier-0 GUID/ratingKey lookups).

    Steps:
      1. Build a per-server snapshot ``.db`` from the run's in-memory
         payload list.
      2. Register the snapshot in ``snapshots.db``. Retention
         enforcement runs inside the register call.

    JSON is always generated on demand from the snapshot ``.db`` when
    the end user clicks Download in the Exports panel - no sidecar
    files are written here. The ``prebuild_json`` toggle from Commit
    B/C has been removed.
    """
    log = logging.getLogger("plexmigrate.server.jobs")
    if not server_id:
        msg = "Snapshot artifact capture skipped: no registered server_id."
        rec.error = msg
        log.warning("%s job=%r", msg, rec.job_id)
        return None

    run_ts = state._run_timestamp or ""
    if not run_ts:
        msg = "Snapshot artifact capture skipped: state._run_timestamp empty."
        rec.error = msg
        log.warning("%s job=%r", msg, rec.job_id)
        return None

    from server import snapshot_capture, snapshot_registry
    # Compose a friendly snapshot_name from the registry fields the
    # end user already cares about: server, libraries, captured_at.
    # The result is both the on-disk .db basename AND the registry's
    # ``snapshot_name`` field, so the file on disk reads e.g.
    # ``My Server - Audio-Books, Music - 2026-05-13 02-26.db`` - a
    # human-readable name rather than a bare timestamp slug.
    captured_at_ts = time.time()
    snapshot_name = snapshot_registry.format_snapshot_filename(
        server_name=server_name,
        libraries=libraries,
        captured_at=captured_at_ts,
    )
    # Optional prefix lets callers tag auto-captured snapshots
    # so the Snapshots tab shows them distinctly from manual runs. The
    # pre-Replace safety belt uses "[pre-replace safety]" to mark its
    # rollback points - end users can find them by name when they need
    # to recover from a bad Replace.
    if snapshot_name_prefix:
        snapshot_name = f"{snapshot_name_prefix} {snapshot_name}"

    # Capture-time include_* flags drive BOTH the registry's
    # captured_types_json field AND the per-table copy scope inside
    # the snapshot .db. Moved before create_snapshot_db so the
    # gating is applied at capture rather than after.
    captured_types: List[str] = []
    if bool(rec.params.get("include_watch_history", True)):
        captured_types.append("watch_history")
    if bool(rec.params.get("include_ratings", True)):
        captured_types.append("ratings")
    if bool(rec.params.get("include_playlists", True)):
        captured_types.append("playlists")
    if bool(rec.params.get("include_collections", True)):
        captured_types.append("collections")

    # Pre-generate the snapshot id so snapshot_meta inside the .db
    # carries the SAME id as the registry row we'll insert below.
    # ``register()`` re-uses any pre-generated id when passed; if it
    # isn't (legacy callers), it generates its own. Use a fresh uuid
    # here so the two stay in lockstep.
    snapshot_id_for_meta = uuid.uuid4().hex

    # Pull the end user-supplied display-name map from the registry
    # row so snapshot_users.display_name can be populated for the
    # users who have data. Best-effort - missing fields fall through
    # to NULL display_name on the snapshot row.
    user_display_names: Dict[str, str] = {}
    try:
        from server import server_registry as _sr
        srv_row = _sr.get_server_by_id(server_id, include_token=False) or {}
        raw = srv_row.get("user_display_names") or {}
        if isinstance(raw, dict):
            user_display_names = {str(k): str(v) for k, v in raw.items() if v}
    except Exception:
        # Display-name lookup is purely cosmetic; never fail capture
        # on a registry hiccup.
        pass

    # End user who triggered the run, if auth is on. The job runner
    # stamps this in rec.params under a synthetic underscore-prefixed
    # key so it never collides with the standard SnapshotJobIn fields.
    created_by_user = rec.params.get("_actor_username") or None

    # Fix 3: store absolute paths so list_snapshots / Download / orphan
    # reconciliation never have to second-guess the CWD the FastAPI
    # process was launched from.
    snapshot_db_path = Path(output_dir).resolve() / f"{snapshot_name}.db"
    # Rule 1: snapshot content comes from the in-memory payload list
    # the engine appended during this run, NOT from media.db. The
    # collector is populated by services.snapshot.plex_native.snapshotter.snapshot_library
    # after each live-fetch; reset_run_state primed it as an empty
    # list at the top of the run.
    payloads = state._snapshot_payloads or []
    if not payloads:
        # An empty list here means the engine ran but no library
        # finished a successful capture (every library errored
        # before reaching the append-to-collector site). Don't
        # write an empty .db - that would create a misleading
        # zero-row registry row. Let the outer wrapper surface
        # rec.error instead.
        msg = (
            "Snapshot capture skipped: no per-library payload was "
            "produced by the engine (state._snapshot_payloads is empty)."
        )
        rec.error = msg
        log.warning("%s job=%r", msg, rec.job_id)
        return None
    try:
        # Feature 1 phase 1.2: time the on-disk capture write. This is
        # the post-engine snapshot.db materialisation step; it is the
        # last meaningful operation in a snapshot job and end users
        # have reported it dominating the wall-clock tail of large
        # captures.
        from services.run_logs import run_timer as _run_timer
        with _run_timer.time_operation(
            "build_snapshot_db_from_payloads",
            scope=_run_timer.SCOPE_OPERATION,
            server_id=server_id,
        ) as _cap_t:
            capture_counts = snapshot_capture.build_snapshot_db_from_payloads(
                snapshot_path=snapshot_db_path,
                snapshot_id=snapshot_id_for_meta,
                server_id=server_id,
                server_name=server_name,
                libraries=libraries,
                metrics=captured_types,
                captured_at=captured_at_ts,
                created_by=created_by_user,
                user_display_names=user_display_names,
                payloads=payloads,
            )
            _cap_t["items_processed"] = sum(
                int(v) for k, v in (capture_counts or {}).items()
                if isinstance(v, int) and k != "file_size"
            )
            if "file_size" in (capture_counts or {}):
                _cap_t["extra"]["file_size_bytes"] = int(
                    capture_counts["file_size"]
                )
    except Exception as exc:
        # Re-raise so the outer wrapper in _run_snapshot picks the
        # exception up, stamps rec.error, and pushes the activity-feed
        # entry. The log.exception here also lands in the per-run log
        # because plexmigrate.server.jobs is whitelisted by
        # _EngineOnlyFilter.
        log.exception("Snapshot DB creation failed for %s", snapshot_db_path)
        raise

    row_counts = {k: v for k, v in capture_counts.items() if k != "file_size"}
    file_size = int(capture_counts.get("file_size") or 0)

    # Granular finalize labels so the dashboard reports each
    # post-engine step. build_db -> registry insert
    # -> optional sidecar render each get their own labelled phase, and
    # the end user sees progress all the way to STATE_COMPLETED. A
    # single pre-build label would leave every later step silent.
    _dash_finalize = state.get_dashboard()
    if _dash_finalize is not None:
        _dash_finalize.set_finalizing("registering snapshot")

    try:
        # Synthesize the snapshot description string before register()
        # so the registry row carries the same one-line summary that's
        # already stamped into the .db file's snapshot_meta. Format
        # mirrors snapshot_capture._synthesize_snapshot_description.
        # Counts are pulled from the freshly-written
        # snapshot.db rather than the cumulative media.db. ``total``
        # is the full roster (owner + every managed user the engine
        # attempted); ``with_data`` is the subset that contributed
        # rows to at least one metric table.
        _user_counts = snapshot_capture.count_snapshot_users(snapshot_db_path)
        _user_count_total = _user_counts.get("total", 0)
        _user_count_with_data = _user_counts.get("with_data", 0)
        _desc_parts: List[str] = []
        if server_name:
            _desc_parts.append(server_name)
        if _user_count_total and _user_count_total > 0:
            if _user_count_with_data != _user_count_total:
                _desc_parts.append(
                    f"{_user_count_with_data} of {_user_count_total} users"
                )
            else:
                _desc_parts.append(f"{_user_count_total} user(s)")
        if libraries:
            _first = ", ".join(libraries[:4])
            _more = "" if len(libraries) <= 4 else f", +{len(libraries) - 4} more"
            _desc_parts.append(f"{_first}{_more} ({len(libraries)} library/ies)")
        if captured_types:
            _desc_parts.append(", ".join(captured_types))
        _description = " · ".join(_desc_parts) or None

        registered = snapshot_registry.register(
            server_id=server_id,
            server_name=server_name,
            snapshot_name=snapshot_name,
            file_path=str(snapshot_db_path),
            captured_at=captured_at_ts,
            libraries=libraries,
            user_count=_user_count_total,
            user_count_with_data=_user_count_with_data,
            row_counts=row_counts,
            file_size=file_size,
            prebuilt_json_path=None,
            captured_types=captured_types,
            snapshot_id=snapshot_id_for_meta,
            description=_description,
        )
        log.info(
            "Snapshot captured for %r: %s (%d bytes, %d libraries, %s users)",
            server_name, snapshot_name, file_size, len(libraries),
            row_counts.get("watch_events", "?"),
        )
        # End user opt-in: render the .plexexport.json sidecar now so
        # the first Download click is instant. Off by default; exposed
        # as a checkbox.
        if rec.params.get("prebuild_json_sidecar"):
            if _dash_finalize is not None:
                _dash_finalize.set_finalizing("building JSON sidecar")
            t0 = time.time()
            sidecar = snapshot_registry.materialise_sidecar(registered["id"])
            if sidecar:
                log.info(
                    "Prebuilt JSON sidecar for %r in %.1fs: %s",
                    snapshot_name, time.time() - t0, sidecar,
                )
            else:
                log.warning(
                    "Prebuilt JSON sidecar requested but render returned no path for %r",
                    snapshot_name,
                )
    except Exception:
        # Re-raise so the outer wrapper banners the failure. The .db
        # is already on disk at this point - if the registry insert
        # failed, the next startup's reconcile pass picks the file up
        # as an orphan and recovers it.
        log.exception("snapshot_registry.register failed for %s", snapshot_db_path)
        raise

    # Surface the snapshot id back to the caller. The
    # pre-Replace safety belt path stashes it on rec.summary so the
    # end user can recover the pre-restore state if the Replace
    # turned out to be wrong. Regular snapshot jobs ignore the return.
    return str(registered["id"]) if isinstance(registered, dict) and registered.get("id") else None


# ── Module-level singleton ────────────────────────────────────────────────────
# Importing :mod:`server.app` creates exactly one of these and shares
# it between the route handlers, the scheduler, and the WebSocket
# loop. Tests can construct their own JobQueue instances safely; the
# singleton is opt-in.
_singleton: Optional[JobQueue] = None


def get_queue() -> JobQueue:
    global _singleton
    if _singleton is None:
        _singleton = JobQueue()
    return _singleton
