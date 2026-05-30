"""
Per-operation run-timing recorder (Feature 1 phase 1.1).

A small, dependency-free module that records start, end, duration, and
optional context (server / library / user / item count) for any operation
inside a snapshot or restore run. The primitive is a context manager,
:func:`time_operation`, which appends a :class:`TimingEntry` to the
current run's in-memory buffer on exit. The buffer flushes to the
persistence layer (``server.run_timings_db``) at run completion.

This module is intentionally separate from :mod:`services.timing`, which
hosts the ETR rolling-window tracker. The ETR tracker computes a live
projection from in-flight samples; this module records discrete
operation timings for post-run analysis and ETR training. They are
distinct concerns with distinct lifecycles, and conflating them in one
module would force every caller of either to import the union of both.

Threading model:

* One buffer per run, shared across all threads in that run. The
  buffer's :class:`threading.Lock` serialises appends. Reads
  (:meth:`RunTimingBuffer.snapshot`) take the lock too and return a
  defensive copy.
* :func:`start_run` and :func:`end_run` are NOT safe with respect to
  concurrent runs in the same process. The engine runs one snapshot
  or restore at a time; this is a documented contract, not enforced.
  If a second :func:`start_run` fires while a buffer is active, the
  prior buffer is dropped and a warning is logged so the regression
  is visible.

Performance:

* Each :func:`time_operation` block costs two ``time.perf_counter()``
  calls, one lock acquire, one list append. Typical overhead is in
  the low microseconds. Safe to wrap per-item loops if needed, though
  batch-level wrapping is the recommended granularity to keep the
  buffer small.
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterator, List, Optional


log = logging.getLogger("plexmigrate.services.run_logs.run_timer")


# Scope vocabulary. Documented here so callers don't invent ad-hoc
# values. Persistence + dashboard filtering keys off these strings.
SCOPE_RUN = "run"               # Top-level wrapper for an entire job
SCOPE_LIBRARY = "library"       # One library inside a run
SCOPE_USER = "user"             # One user phase inside a library
SCOPE_BATCH = "batch"           # One batch of items (e.g. resolver pool dispatch)
SCOPE_OPERATION = "operation"   # Smallest unit (e.g. one Plex API call)

VALID_SCOPES = frozenset({
    SCOPE_RUN, SCOPE_LIBRARY, SCOPE_USER, SCOPE_BATCH, SCOPE_OPERATION,
})


@dataclass
class TimingEntry:
    """
    One record of a timed operation. Mirrors the run_timings table
    schema; :func:`server.run_timings_db.persist_entries` maps fields
    one-to-one.

    ``duration_seconds`` comes from ``time.perf_counter`` deltas; it
    is monotonic, immune to NTP / clock-skew jumps that would corrupt a
    wall-clock subtraction, AND has sub-microsecond resolution on every
    supported platform (``time.monotonic`` has ~15 ms resolution on
    Windows, too coarse for short operations).
    ``started_at`` / ``ended_at`` are ``time.time`` epoch seconds, kept
    for human-readable display and for joining against external logs.
    """

    run_id: str
    scope: str
    label: str
    started_at: float
    ended_at: float
    duration_seconds: float

    server_id: Optional[str] = None
    library: Optional[str] = None
    user_handle: Optional[str] = None
    items_processed: Optional[int] = None
    etr_at_start: Optional[float] = None
    # Free-form annotations: store anything else the caller wants to
    # surface in the log / dashboard but does not warrant a column.
    # Examples: {"strategy": "smart", "bulk_used": True, "http_calls": 12}.
    extra: Dict[str, Any] = field(default_factory=dict)


class RunTimingBuffer:
    """Thread-safe append-only buffer of timing entries for a single
    run. Drained to the persistence backend at run completion."""

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self.started_at = time.time()
        self._entries: List[TimingEntry] = []
        self._lock = threading.Lock()

    def append(self, entry: TimingEntry) -> None:
        with self._lock:
            self._entries.append(entry)

    def snapshot(self) -> List[TimingEntry]:
        """Return a defensive copy. Callers (dashboard) iterate without
        holding the lock; producers must not see their writes pop out
        under them."""
        with self._lock:
            return list(self._entries)

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


# ── Process-wide singleton current-run buffer ───────────────────────────────
# Only one run at a time per process is a hard contract (see module
# docstring). The lock guards the SLOT, not the buffer object itself;
# the buffer has its own internal lock for entry-level concurrency.

_current_run_buffer: Optional[RunTimingBuffer] = None
_buffer_slot_lock = threading.Lock()


def start_run(run_id: Optional[str] = None) -> str:
    """Initialise the timing buffer for a new run. Returns the resolved
    ``run_id`` (caller-supplied or auto-generated).

    If a buffer is already active when this is called, the previous
    buffer is dropped. That should never happen under the documented
    one-run-at-a-time contract; if it does, a warning is logged so the
    regression is visible rather than silent.
    """
    global _current_run_buffer
    with _buffer_slot_lock:
        if _current_run_buffer is not None:
            log.warning(
                "start_run called while a prior run (%s) is still active "
                "with %d entries; dropping the prior buffer. This indicates "
                "a missing end_run call somewhere upstream.",
                _current_run_buffer.run_id, len(_current_run_buffer),
            )
        resolved = run_id or _generate_run_id()
        _current_run_buffer = RunTimingBuffer(resolved)
        return resolved


def end_run(*, persist: bool = True) -> List[TimingEntry]:
    """Snapshot the buffer, optionally persist it to the run_timings DB,
    and clear the slot. Returns the list of entries for any in-process
    post-run reporting the caller wants to do."""
    global _current_run_buffer
    with _buffer_slot_lock:
        if _current_run_buffer is None:
            return []
        entries = _current_run_buffer.snapshot()
        _current_run_buffer = None
    if persist and entries:
        try:
            from server import run_timings_db
            run_timings_db.persist_entries(entries)
        except ImportError:
            # Persistence module not built yet (Phase 1.3 may not have
            # landed in the deployment). The in-memory entries are
            # returned to the caller; only the persistence side is
            # skipped. This keeps the timing recorder usable
            # independently of the persistence layer.
            log.debug(
                "server.run_timings_db not importable; skipping persist "
                "(in-memory entries returned to caller)."
            )
        except Exception:
            # Never let a telemetry write break the run completion path.
            log.exception(
                "run_timings persistence failed for %d entries",
                len(entries),
            )
    return entries


def current_run_id() -> Optional[str]:
    """Return the active run_id, or None if no run is active. Reads
    without acquiring the slot lock; the slot is updated atomically so
    a momentarily-stale read is acceptable."""
    buf = _current_run_buffer
    return buf.run_id if buf is not None else None


def current_buffer() -> Optional[RunTimingBuffer]:
    """Return the active buffer (live reference) for dashboard reads.
    Callers must use :meth:`RunTimingBuffer.snapshot` rather than
    iterating the internal list directly."""
    return _current_run_buffer


# ── The context-manager primitive ────────────────────────────────────────────

@contextlib.contextmanager
def time_operation(
    label: str,
    *,
    scope: str = SCOPE_OPERATION,
    server_id: Optional[str] = None,
    library: Optional[str] = None,
    user_handle: Optional[str] = None,
    items_count_callback: Optional[Callable[[], int]] = None,
    push_to_activity_feed: bool = False,
) -> Iterator[Dict[str, Any]]:
    """
    Time a block and record one :class:`TimingEntry` on exit.

    Usage::

        from services.run_logs.run_timer import time_operation, SCOPE_LIBRARY

        with time_operation("snapshot_watch_history",
                            scope=SCOPE_LIBRARY,
                            library=lib_name) as t:
            results = do_work()
            t["items_processed"] = len(results)
            t["extra"]["strategy"] = "force_bulk"

    The yielded dict is mutable. Callers fill in:

    * ``items_processed`` (int): number of items the operation touched.
      Used by the ETR rework (phase 1.4) to compute per-item rates.
    * ``extra`` (dict): free-form annotations surfaced in the log /
      dashboard. Anything that wouldn't justify a dedicated column.

    ``items_count_callback`` is an alternative to setting
    ``items_processed`` on the yielded dict. Useful when the count is
    only available after the block exits cleanly. If the callback
    raises, the count silently stays ``None`` (logged at debug); a bad
    callback never fails the actual work.

    ``push_to_activity_feed`` opt-in surfaces this timing entry on the
    dashboard's activity feed when the block exits. Defaults False to
    keep per-operation scopes from flooding the 8-slot feed; library
    and user scopes should set this True so the end user sees them.

    Scope must be one of :data:`VALID_SCOPES`; an unknown value is
    coerced to :data:`SCOPE_OPERATION` and a warning is logged. We
    coerce rather than raise so a typo in instrumentation can't take
    down a real snapshot run.

    If no run is active (``start_run`` was never called), the block
    still runs but no entry is recorded. This makes the wrapper safe
    to leave in place when the engine is invoked outside a job (e.g.
    a one-off script or a unit test).
    """
    if scope not in VALID_SCOPES:
        log.warning(
            "time_operation called with unknown scope=%r (label=%r); "
            "coercing to %r. Update the call site to use a constant from "
            "services.run_logs.run_timer.SCOPE_*.",
            scope, label, SCOPE_OPERATION,
        )
        scope = SCOPE_OPERATION

    started_perf = time.perf_counter()
    started_wall = time.time()
    info: Dict[str, Any] = {"items_processed": None, "extra": {}}

    # Best-effort: stamp the server's most recent ping reading into
    # extra so the ETA trainer can fit a per-bucket ping EMA against
    # observed duration. Failures are silent (the registry import may
    # be unavailable during early-init tests, the row may not yet
    # carry last_response_ms, or ping was never sampled).
    if server_id:
        try:
            from server.server_registry import get_server_by_id
            row = get_server_by_id(server_id, include_token=False)
            if row is not None:
                last_ms = row.get("last_response_ms")
                if isinstance(last_ms, (int, float)) and last_ms > 0:
                    info["extra"]["ping_ms_at_start"] = float(last_ms)
        except Exception:
            pass

    # Exceptions inside the block propagate; the timing entry still
    # records the elapsed time up to the failure point so post-mortem
    # tools can see how far a failed operation got. ``extra["error"]``
    # is set so consumers can distinguish a failed run from a slow one.
    block_error: Optional[BaseException] = None
    try:
        yield info
    except BaseException as exc:
        block_error = exc
        raise
    finally:
        ended_perf = time.perf_counter()
        ended_wall = time.time()
        duration = ended_perf - started_perf

        items = info.get("items_processed")
        if items is None and items_count_callback is not None:
            try:
                items = int(items_count_callback())
            except Exception:
                log.debug(
                    "items_count_callback raised for label=%r; "
                    "leaving items_processed unset", label,
                    exc_info=True,
                )
                items = None

        buf = _current_run_buffer
        if buf is None:
            # No active run; the block ran but its timing is dropped.
            # This is intentional; the alternative (raising) would
            # poison library code that's safe to call in either mode.
            return

        extra = dict(info.get("extra") or {})
        if block_error is not None:
            extra.setdefault("error", type(block_error).__name__)

        entry = TimingEntry(
            run_id=buf.run_id,
            scope=scope,
            label=label,
            started_at=started_wall,
            ended_at=ended_wall,
            duration_seconds=duration,
            server_id=server_id,
            library=library,
            user_handle=user_handle,
            items_processed=items,
            etr_at_start=None,
            extra=extra,
        )
        buf.append(entry)

        if push_to_activity_feed:
            _push_to_dashboard_feed(entry)


def _push_to_dashboard_feed(entry: TimingEntry) -> None:
    """Best-effort dashboard activity-feed push. Never raises; a
    dashboard hiccup can never break the timed operation."""
    try:
        from services import state
        dash = state.get_dashboard()
        if dash is None:
            return
        msg_parts = [f"{entry.duration_seconds:.1f}s"]
        if entry.items_processed is not None:
            msg_parts.append(f"{entry.items_processed} item(s)")
        if entry.extra:
            kv = ", ".join(f"{k}={v}" for k, v in entry.extra.items())
            msg_parts.append(kv)
        msg = " | ".join(msg_parts)
        topic = entry.library or entry.user_handle or entry.label
        dash.push_activity("timing", topic, f"{entry.label}: {msg}")
    except Exception:
        log.debug(
            "dashboard activity-feed push failed for timing entry %r",
            entry.label, exc_info=True,
        )


# ── Run-id generator ────────────────────────────────────────────────────────

def _generate_run_id() -> str:
    """Generate a sortable, human-readable run_id. The wall-clock prefix
    lets ``ls -l`` of any per-run artefacts (snapshot files, log files,
    timing rows) line up chronologically without sorting by an opaque
    uuid alone. The 8-char uuid suffix prevents collisions when the
    engine fires two runs in the same second."""
    return f"run_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"


# ── Test seam ───────────────────────────────────────────────────────────────

def _reset_for_tests() -> None:
    """Force the slot back to empty. Test-only helper; not part of the
    public API. Production paths must use :func:`end_run` so persistence
    fires."""
    global _current_run_buffer
    with _buffer_slot_lock:
        _current_run_buffer = None
