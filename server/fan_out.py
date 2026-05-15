"""
Fan-out coordinator: drive one job that targets multiple destinations.

v0.10.0 - Feature 1 (parallel execution)
----------------------------------------
A single direct-transfer or import job can target several registered
destination servers in parallel. Each destination runs in its own
thread with its own DashboardState, log directory, accumulator dicts,
and Plex connection - engine code reads those values through the
ContextVar-backed fields on :mod:`services.state`, which Python's
:mod:`contextvars` propagates to child workers when the engine submits
them via :func:`services.dashboard.submit_with_context`.

Each destination thread is spawned with ``threading.Thread()``. Per
PEP 567 a new thread starts in a fresh context, so writes to
``state._plex_base_url`` etc. inside the destination thread do not
leak into sibling destinations. Engine worker pools spawned from
inside the destination see the destination's context (via the
``submit_with_context`` helper the engine already uses everywhere).

Failure isolation
-----------------
A destination that fails (network blip, auth revoked, the server
going offline mid-run) marks only its own card as ``failed`` and the
sibling destinations continue running. The overall job state is
``completed`` if every destination succeeds, ``failed`` if at least
one destination raised, and ``cancelled`` if the user clicked Stop
while any destinations were still pending.

WS payload, single-destination, and CLI mode are unchanged.
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from plexapi.server import PlexServer

import services.state as state
from services.dashboard import DashboardState


log = logging.getLogger("plexmigrate.server.fan_out")


# ── Result model ─────────────────────────────────────────────────────────────

@dataclass
class FanOutDestResult:
    """
    Outcome for one destination inside a fan-out job.

    The DashboardState is kept on the result object for the duration
    of the job so :mod:`server.ws` can keep reading its snapshot until
    the job's grace period elapses.
    """
    dest_name: str
    log_dir: str
    state: str = "queued"  # queued | running | completed | failed | cancelled
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    error: Optional[str] = None
    dashboard: Optional[DashboardState] = field(repr=False, default=None)
    # v0.13.x: when this destination's fan-out leg was a Replace and the
    # safety belt fired successfully, this carries the snapshot_id of
    # the pre-Replace rollback point. Surfaced into the parent JobRecord's
    # summary["pre_replace_snapshots"] map so the operator can find every
    # destination's recovery point on the Snapshots tab.
    pre_replace_snapshot_id: Optional[str] = None


@dataclass
class FanOutResult:
    """
    Aggregated outcome of a fan-out job. Returned to
    :mod:`server.jobs` so the worker loop can choose the right
    terminal state for the JobRecord.
    """
    destinations: List[FanOutDestResult] = field(default_factory=list)
    cancelled: bool = False

    def has_failures(self) -> bool:
        return any(d.state == "failed" for d in self.destinations)

    def first_error(self) -> Optional[str]:
        for d in self.destinations:
            if d.error:
                return f"{d.dest_name}: {d.error}"
        return None


# ── Registry of in-flight fan-out jobs (for ws.py to read) ───────────────────

_active_lock = threading.Lock()
_active_result: Optional[FanOutResult] = None


def get_active_result() -> Optional[FanOutResult]:
    """
    Return the currently-running fan-out job's result object, or
    ``None`` if no fan-out is in flight. Used by
    :func:`server.ws.build_dashboard_frame` to enrich the WS payload with
    per-destination dashboards.
    """
    with _active_lock:
        return _active_result


def _set_active_result(result: Optional[FanOutResult]) -> None:
    global _active_result
    with _active_lock:
        _active_result = result


def clear_active_result() -> None:
    """
    Drop the fan-out registry slot immediately.

    Called by :meth:`server.jobs.JobQueue._worker_loop` at the top of
    every new job so a single-destination job submitted within the
    fan-out grace window can't inherit the previous fan-out's
    ``fan_out`` array in the WS payload. The frontend's snapshot
    retention covers the visible grace period; the backend slot only
    needs to live until the NEXT job claims the worker.
    """
    global _active_result
    with _active_lock:
        _active_result = None


# Grace period after a fan-out completes during which the WS still
# carries the final ``fan_out`` array so the frontend can show the
# end-of-run status on every destination card before the layout
# switches back. The frontend already retains the last snapshot for
# ~30 s, so this can be short - we keep it at 8 s so the registry
# clears cleanly even if the browser tab is closed during the
# retention window.
_FAN_OUT_GRACE_SECONDS = 8.0


def _finalise_fan_out_state(result: "FanOutResult") -> None:
    """
    Tear down the cross-thread surfaces a fan-out job leaves behind.

    Called from the ``finally`` clause of each fan-out entry point so
    a single-destination job submitted immediately after a fan-out
    completes does not inherit the previous run's state - without
    this hook the WS broadcaster keeps reporting the prior fan-out
    in its ``fan_out`` array and the next single-destination
    dashboard never gets to render in its proper layout.

    Two cleanups happen:

    1. The cross-thread dashboard mirror in :mod:`services.state`
       (the global that backs ``get_dashboard()`` for readers outside
       the engine's thread tree, e.g. the WebSocket broadcaster) is
       cleared. During the run each destination's thread wrote its
       own dashboard via the ContextVar and the mirror picked up the
       last write - leaving it stale after completion. The worker
       loop sets a fresh placeholder at the start of every job, so
       wiping it here means readers see ``None`` (idle) in the gap.

    2. ``_active_result`` is cleared after a short grace period so
       the final WS tick still surfaces per-destination state to the
       frontend (each card shows ``completed`` / ``failed`` /
       ``cancelled`` before the layout switches back). A
       :class:`threading.Timer` fires the clear after
       ``_FAN_OUT_GRACE_SECONDS`` - daemon thread so it never
       prevents process exit.
    """
    # Clear the cross-thread dashboard mirror immediately. The
    # frontend's POST_FINISH_RETAIN_MS holds the last WS snapshot
    # for 30 s anyway, so the cards stay visually intact until the
    # retention expires or the next job starts.
    try:
        state._dashboard = None
    except Exception:  # pragma: no cover (defensive)
        pass

    # Close every file handler attached to the named engine loggers.
    # Each destination's setup_logging added its own set; we
    # deliberately deferred closing them per-destination because that
    # path would have torn down sibling destinations' handlers
    # mid-flight. By the time we get here every destination has
    # finished (the executor join above ensures that), so closing
    # everything at once is safe.
    for lg in (
        logging.getLogger("plexmigrate"),
        logging.getLogger("plexmigrate.media"),
    ):
        for h in lg.handlers[:]:
            try:
                h.close()
            except Exception:  # pragma: no cover (defensive)
                pass
            try:
                lg.removeHandler(h)
            except Exception:  # pragma: no cover (defensive)
                pass

    # Schedule the registry clear after the grace period. Guard
    # against double-clearing by snapshotting the result identity at
    # schedule time - a NEW fan-out submitted within the grace
    # window installs a fresh ``_active_result`` and we must not
    # blow it away.
    target_id = id(result)

    def _clear() -> None:
        with _active_lock:
            global _active_result
            if _active_result is not None and id(_active_result) == target_id:
                _active_result = None

    timer = threading.Timer(_FAN_OUT_GRACE_SECONDS, _clear)
    timer.daemon = True
    timer.start()


# ── Public entry points ──────────────────────────────────────────────────────

def run_fan_out_direct(
    *,
    source_name: str,
    dest_names: List[str],
    libraries: List[str],
    user_filter: Optional[List[str]],
    remap: Optional[Tuple[str, str]],
    strict_match: bool,
    output_dir: Optional[str],
    workers: int,
    scrobble_workers: int,
    verbose: bool,
    log_dir_root: str,
    stop_event: Optional[threading.Event],
    run_trigger: str,
    schedule_name: str,
    skip_collections: bool = False,
    fast_collection_detection: bool = False,
    skip_playlists: bool = False,
    # PR-3 / Phase D - four-flag data-type filter (fan-out direct).
    include_watch_history: bool = True,
    include_ratings: bool = True,
    include_playlists: bool = True,
    include_collections: bool = True,
    # v0.13.x: restore-side knobs forwarded to every destination. Per-job
    # value (e.g. mode=replace) applies uniformly to all destinations in
    # this fan-out; per-destination overrides are not supported.
    mode: str = "merge",
    merge_watch_strategy: str = "higher",
    # v0.13.x: when Replace + auto-capture is requested, this dict carries
    # the per-destination safety-belt settings (include_* flags, log_dir,
    # output_dir, verbose). Forwarded into each destination worker which
    # uses it to capture a pre-Replace rollback snapshot before the
    # engine fires. ``None`` skips the belt for the whole fan-out (when
    # mode != "replace" OR auto_capture_before_replace is False).
    pre_replace_settings: Optional[Dict[str, Any]] = None,
) -> FanOutResult:
    """
    Run one direct-transfer job that copies from ``source_name`` into
    every server in ``dest_names`` in parallel.

    The source is connected ONCE in the calling thread and that
    PlexServer instance is shared across destination workers - its
    internal session is thread-safe and read-only from each
    destination's perspective. Each destination runs in its own
    thread, sets its own ContextVar values
    (``state._plex_base_url``, ``state._plex_token``, etc.) and is
    fully isolated from siblings.
    """
    from server.direct_transfer import run_direct_transfer
    from server.server_registry import (
        connect_registered_server,
        decrypt_server_token,
        safe_server_name,
    )
    from services.auth import get_home_users, _make_session

    result = FanOutResult()
    for name in dest_names:
        result.destinations.append(FanOutDestResult(
            dest_name=name,
            log_dir="",
            dashboard=_make_pending_dashboard(name),
        ))
    _set_active_result(result)

    boot_logger = logging.getLogger("plexmigrate")

    # ── Connect once to the source ───────────────────────────────────
    src_server, src_row = connect_registered_server(source_name, boot_logger)
    src_token = decrypt_server_token(src_row)
    try:
        src_home_users = get_home_users(src_server, src_row["url"], boot_logger)
    except Exception as e:
        boot_logger.warning("Could not enumerate source home users: %s", e)
        src_home_users = []

    # ── Per-destination loop, parallel ────────────────────────────────
    # v0.13.x: destination_workers caps the pool. 0 (default) means
    # "no cap" - one worker per destination, today's behavior. A
    # positive value (e.g. 1) serialises destinations.
    max_parallel = len(dest_names)
    if destination_workers > 0:
        max_parallel = min(max_parallel, destination_workers)
    max_parallel = max(1, max_parallel)
    try:
        with ThreadPoolExecutor(
            max_workers=max_parallel,
            thread_name_prefix="fanout-direct",
        ) as pool:
            futs = []
            for idx, dest_name in enumerate(dest_names):
                dest_result = result.destinations[idx]
                if stop_event is not None and stop_event.is_set():
                    dest_result.state = "cancelled"
                    result.cancelled = True
                    continue
                futs.append(pool.submit(
                    _run_one_direct_destination,
                    source_name=source_name,
                    source_server=src_server,
                    source_url=src_row["url"],
                    source_token=src_token,
                    source_owner=src_row.get("owner_name") or "Plex Owner",
                    source_home_users=src_home_users,
                    dest_name=dest_name,
                    dest_result=dest_result,
                    libraries=libraries,
                    user_filter=user_filter,
                    remap=remap,
                    strict_match=strict_match,
                    output_dir=output_dir,
                    workers=workers,
                    scrobble_workers=scrobble_workers,
                    verbose=verbose,
                    log_dir_root=log_dir_root,
                    stop_event=stop_event,
                    run_trigger=run_trigger,
                    schedule_name=schedule_name,
                    make_session=_make_session,
                    skip_collections=skip_collections,
                    fast_collection_detection=fast_collection_detection,
                    skip_playlists=skip_playlists,
                    include_watch_history=include_watch_history,
                    include_ratings=include_ratings,
                    include_playlists=include_playlists,
                    include_collections=include_collections,
                    mode=mode,
                    merge_watch_strategy=merge_watch_strategy,
                    pre_replace_settings=pre_replace_settings,
                ))
            # Drain completions. Exceptions from a destination land on
            # the dest_result and never re-raise - the per-destination
            # worker catches everything so siblings keep running. We
            # still iterate ``as_completed`` so the with-block exits in
            # completion order (cleaner shutdown if the user clicks
            # Stop mid-job).
            for _ in as_completed(futs):
                pass
    finally:
        _finalise_fan_out_state(result)

    return result


def run_fan_out_restore(
    *,
    dest_names: List[str],
    input_files: List[str],
    remap: Optional[Tuple[str, str]],
    strict_match: bool,
    workers: int,
    scrobble_workers: int,
    verbose: bool,
    log_dir_root: str,
    output_dir: Optional[str],
    stop_event: Optional[threading.Event],
    include_playlists: bool = True,
    # PR-3 / Phase D - additional include_* flags (fan-out import).
    include_watch_history: bool = True,
    include_ratings: bool = True,
    include_collections: bool = True,
    # v0.13.x: restore-side knobs forwarded to every destination. See
    # run_fan_out_direct for the per-destination uniformity rationale.
    mode: str = "merge",
    merge_watch_strategy: str = "higher",
    # v0.13.x: per-destination safety-belt settings. See
    # run_fan_out_direct for the contract.
    pre_replace_settings: Optional[Dict[str, Any]] = None,
    # v0.13.x: library-level concurrency forwarded into each
    # destination's own run_restore. The two axes are independent:
    # destination_workers caps how many destinations run at once;
    # library_workers caps how many libraries each destination
    # processes in parallel.
    library_workers: int = 3,
    # v0.13.x: cap on the per-destination thread pool. ``0`` (default)
    # means no cap - one worker per destination, current behavior.
    # ``1`` serialises destinations.
    destination_workers: int = 0,
    # v0.14 — per-job user filter. Forwarded verbatim to each
    # destination's _run_one_import_destination.
    user_filter: Optional[List[str]] = None,
) -> FanOutResult:
    """
    Run one import job that loads the same ``input_files`` into each
    destination in ``dest_names`` in parallel.

    Per-destination input-file resolution mirrors the single-destination
    path: a missing file becomes that destination's failure rather than
    aborting the whole job before any card has started.
    """
    from server.server_registry import (
        connect_registered_server,
        decrypt_server_token,
        safe_server_name,
    )
    from services.auth import get_home_users, _make_session

    result = FanOutResult()
    for name in dest_names:
        result.destinations.append(FanOutDestResult(
            dest_name=name,
            log_dir="",
            dashboard=_make_pending_dashboard(name),
        ))
    _set_active_result(result)

    # v0.13.x: destination_workers caps the pool. See run_fan_out_direct
    # above for the contract.
    max_parallel = len(dest_names)
    if destination_workers > 0:
        max_parallel = min(max_parallel, destination_workers)
    max_parallel = max(1, max_parallel)
    try:
        with ThreadPoolExecutor(
            max_workers=max_parallel,
            thread_name_prefix="fanout-import",
        ) as pool:
            futs = []
            for idx, dest_name in enumerate(dest_names):
                dest_result = result.destinations[idx]
                if stop_event is not None and stop_event.is_set():
                    dest_result.state = "cancelled"
                    result.cancelled = True
                    continue
                futs.append(pool.submit(
                    _run_one_import_destination,
                    dest_name=dest_name,
                    dest_result=dest_result,
                    input_files=input_files,
                    remap=remap,
                    strict_match=strict_match,
                    workers=workers,
                    scrobble_workers=scrobble_workers,
                    verbose=verbose,
                    log_dir_root=log_dir_root,
                    output_dir=output_dir,
                    stop_event=stop_event,
                    make_session=_make_session,
                    include_playlists=include_playlists,
                    include_watch_history=include_watch_history,
                    include_ratings=include_ratings,
                    include_collections=include_collections,
                    mode=mode,
                    merge_watch_strategy=merge_watch_strategy,
                    pre_replace_settings=pre_replace_settings,
                    library_workers=library_workers,
                    user_filter=user_filter,
                ))
            for _ in as_completed(futs):
                pass
    finally:
        _finalise_fan_out_state(result)

    return result


# ── Per-destination workers ──────────────────────────────────────────────────

def _run_one_direct_destination(
    *,
    source_name: str,
    source_server: PlexServer,
    source_url: str,
    source_token: str,
    source_owner: str,
    source_home_users: List[Tuple[str, str, PlexServer]],
    dest_name: str,
    dest_result: FanOutDestResult,
    libraries: List[str],
    user_filter: Optional[List[str]],
    remap: Optional[Tuple[str, str]],
    strict_match: bool,
    output_dir: Optional[str],
    workers: int,
    scrobble_workers: int,
    verbose: bool,
    log_dir_root: str,
    stop_event: Optional[threading.Event],
    run_trigger: str,
    schedule_name: str,
    make_session: Callable[[], Any],
    skip_collections: bool = False,
    fast_collection_detection: bool = False,
    skip_playlists: bool = False,
    # PR-3 / Phase D - four-flag data-type filter.
    include_watch_history: bool = True,
    include_ratings: bool = True,
    include_playlists: bool = True,
    include_collections: bool = True,
    # v0.13.x: restore-side knobs. Forwarded into run_direct_transfer
    # below; default "merge" / "higher" preserves legacy behaviour for
    # any caller that hasn't been updated yet.
    mode: str = "merge",
    merge_watch_strategy: str = "higher",
    # v0.13.x: when Replace + auto-capture is requested, this dict
    # carries the include_* + log_dir + verbose settings the safety
    # belt needs to capture this destination's rollback snapshot.
    # ``None`` (the default) means no safety belt for this destination,
    # which is correct for mode=="merge" OR auto_capture_before_replace
    # set to False at the caller level.
    pre_replace_settings: Optional[Dict[str, Any]] = None,
    # v0.13.x: forwarded into the destination's own engine call. Per-
    # destination library concurrency is independent of fan-out's
    # destination concurrency.
    library_workers: int = 3,
) -> None:
    """
    Execute one destination of a fan-out direct transfer.

    Runs on its own thread (spawned by the coordinator's ThreadPoolExecutor)
    so the ContextVar writes performed here are isolated from siblings.
    Exceptions are caught and recorded on ``dest_result``; they never
    propagate so failure of one destination does not abort the others.
    """
    from server.direct_transfer import run_direct_transfer
    from server.server_registry import (
        connect_registered_server,
        decrypt_server_token,
        safe_server_name,
    )
    from services.auth import get_home_users

    dest_result.state = "running"
    dest_result.started_at = time.time()

    logger = logging.getLogger(f"plexmigrate.fanout.{safe_server_name(dest_name)}")

    # v0.13.x: install this destination's MDC tag on the worker's
    # ContextVar BEFORE setup_logging runs. The DestinationContextFilter
    # in services/logging_ops.py reads this on every LogRecord; the
    # per-destination admission filter attached to each new FileHandler
    # by setup_logging admits only records carrying this tag. Net
    # effect: every destination's runtime/errors/media file writes
    # only its own records - fixes the cross-contamination caveat the
    # code review's M-class finding flagged.
    state._destination = dest_name

    try:
        # ── Connect to this destination ──────────────────────────────
        dst_server, dst_row = connect_registered_server(dest_name, logger)
        dst_token = decrypt_server_token(dst_row)
        try:
            dst_home_users = get_home_users(dst_server, dst_row["url"], logger)
        except Exception as e:
            logger.warning("Could not enumerate destination home users: %s", e)
            dst_home_users = []

        # ── Per-destination log directory ────────────────────────────
        run_log_dir = _build_per_dest_log_dir(
            log_dir_root, source_name, dest_name, verbose,
        )
        dest_result.log_dir = run_log_dir

        # Install this destination's run-context on the calling thread.
        # Writes hit the thread's ContextVar so engine workers spawned
        # via submit_with_context inherit these values automatically.
        dashboard = DashboardState(log_dir=run_log_dir)
        dest_result.dashboard = dashboard
        state._dashboard = dashboard
        state._plex_base_url = dst_row["url"]
        state._plex_token = dst_token
        state._plex_owner_name = source_owner
        state._current_user_visible = bool(user_filter)
        # MAX_WORKERS / SCROBBLE_WORKERS are plain module globals
        # rather than ContextVars (each destination uses the same
        # caps; they aren't ever set per-destination). Write them
        # once on the main thread before fan-out spawns; written
        # here too would race siblings.

        # v0.13.x: per-destination pre-Replace safety belt. Each fan-out
        # destination gets its own rollback snapshot before the engine
        # overwrites data. Lazy-imported to avoid an import cycle
        # between server.jobs and server.fan_out.
        if mode == "replace" and pre_replace_settings:
            from server.jobs import _capture_pre_replace_snapshot, _resolve_server_id
            dst_id = _resolve_server_id(dst_row.get("name") or dest_name)
            if not dst_id:
                raise ValueError(
                    f"Replace fan-out destination {dest_name!r} has no "
                    "registered server_id - cannot capture a rollback "
                    "snapshot. Re-register the destination from the "
                    "Servers tab."
                )
            pre_id = _capture_pre_replace_snapshot(
                job_id=f"fanout-direct:{dest_name}",
                settings=pre_replace_settings,
                dest_server=dst_server,
                dest_server_id=dst_id,
                dest_server_name=str(dst_row.get("name") or dest_name),
                dest_url=dst_row["url"],
            )
            dest_result.pre_replace_snapshot_id = pre_id

        run_direct_transfer(
            source_server=source_server,
            source_url=source_url,
            source_token=source_token,
            source_owner=source_owner,
            dest_server=dst_server,
            dest_url=dst_row["url"],
            dest_token=dst_token,
            dest_owner=dst_row.get("owner_name") or "Plex Owner",
            library_names=list(libraries or []),
            logger=logger,
            log_dir=run_log_dir,
            remap=remap,
            strict_match=strict_match,
            stop_event=stop_event,
            output_dir=output_dir,
            source_home_users=source_home_users,
            dest_home_users=dst_home_users,
            user_filter=user_filter,
            skip_collections=skip_collections,
            fast_collection_detection=fast_collection_detection,
            skip_playlists=skip_playlists,
            include_watch_history=include_watch_history,
            include_ratings=include_ratings,
            include_playlists=include_playlists,
            include_collections=include_collections,
            mode=mode,
            merge_watch_strategy=merge_watch_strategy,
        )

        # Do NOT call ``_close_logger`` here. It strips handlers from
        # the GLOBAL ``plexmigrate`` / ``plexmigrate.media`` named
        # loggers - but in fan-out mode every destination's
        # setup_logging attached its own FileHandlers to those same
        # named loggers, so closing per-destination would also strip
        # siblings' handlers and break their in-flight log writes.
        # All handlers are closed once at the end of the fan-out by
        # ``_finalise_fan_out_state``.
        _finalise_run_dir(run_log_dir)
        if stop_event is not None and stop_event.is_set():
            dest_result.state = "cancelled"
        else:
            dest_result.state = "completed"
    except Exception as exc:
        dest_result.state = "failed"
        dest_result.error = f"{type(exc).__name__}: {exc}"
        log.error("Fan-out destination %r failed", dest_name, exc_info=True)
        if dest_result.dashboard is not None:
            try:
                dest_result.dashboard.push_activity(
                    "error", "-",
                    f"Destination failed: {dest_result.error}",
                )
            except Exception:
                pass
    finally:
        dest_result.finished_at = time.time()


def _run_one_import_destination(
    *,
    dest_name: str,
    dest_result: FanOutDestResult,
    input_files: List[str],
    remap: Optional[Tuple[str, str]],
    strict_match: bool,
    workers: int,
    scrobble_workers: int,
    verbose: bool,
    log_dir_root: str,
    output_dir: Optional[str],
    stop_event: Optional[threading.Event],
    make_session: Callable[[], Any],
    include_playlists: bool = True,
    # PR-3 / Phase D - additional include_* flags.
    include_watch_history: bool = True,
    include_ratings: bool = True,
    include_collections: bool = True,
    # v0.13.x: restore-side knobs forwarded into run_restore below.
    mode: str = "merge",
    merge_watch_strategy: str = "higher",
    # v0.13.x: see _run_one_direct_destination for the contract.
    pre_replace_settings: Optional[Dict[str, Any]] = None,
    # v0.14 — per-job user filter. Each destination applies the same
    # operator-selected user list; the restore engine drops payload
    # users not on this destination automatically (no user lookup),
    # so a destination missing a user just no-ops for that user.
    user_filter: Optional[List[str]] = None,
) -> None:
    """
    Execute one destination of a fan-out import. Mirrors
    :func:`_run_one_direct_destination` for the file-mediated path.
    """
    from server.server_registry import (
        connect_registered_server,
        decrypt_server_token,
        safe_server_name,
    )
    from services.restorer import run_restore

    dest_result.state = "running"
    dest_result.started_at = time.time()
    logger = logging.getLogger(f"plexmigrate.fanout.{safe_server_name(dest_name)}")

    # v0.13.x: install this destination's MDC tag on the worker's
    # ContextVar BEFORE setup_logging runs (same reason as the
    # direct-transfer worker above). See logging_ops.DestinationContextFilter
    # for the full propagation story.
    state._destination = dest_name

    try:
        dst_server, dst_row = connect_registered_server(dest_name, logger)
        dst_token = decrypt_server_token(dst_row)
        owner_name = dst_row.get("owner_name") or "Plex Owner"

        run_log_dir = _build_per_dest_log_dir(
            log_dir_root, "restore", dest_name, verbose,
        )
        dest_result.log_dir = run_log_dir

        # Per-destination input-file resolution. Three attempts:
        # direct path, then ``<output_dir>/<file>`` (active snapshots),
        # then ``<output_dir>/legacy/<file>`` (JSON-archive picker
        # source). Mirrors the single-destination resolver in jobs.py.
        resolved_inputs: List[str] = []
        missing: List[str] = []
        eff_output_dir = output_dir or "./snapshots"
        for f in input_files or []:
            direct = Path(f)
            if direct.exists():
                resolved_inputs.append(str(direct))
                continue
            scoped = Path(eff_output_dir) / f
            if scoped.exists():
                resolved_inputs.append(str(scoped))
                continue
            legacy_scoped = Path(eff_output_dir) / "legacy" / f
            if legacy_scoped.exists():
                resolved_inputs.append(str(legacy_scoped))
                continue
            missing.append(f)
        if not resolved_inputs:
            raise FileNotFoundError(
                f"No valid export files for {dest_name!r}. Missing: {missing}"
            )

        dashboard = DashboardState(log_dir=run_log_dir)
        dest_result.dashboard = dashboard
        state._dashboard = dashboard
        state._plex_base_url = dst_row["url"]
        state._plex_token = dst_token
        state._plex_owner_name = owner_name

        # v0.13.x: per-destination pre-Replace safety belt. Each fan-out
        # destination gets its own rollback snapshot before the engine
        # overwrites data. Lazy-imported to dodge the jobs↔fan_out cycle.
        if mode == "replace" and pre_replace_settings:
            from server.jobs import _capture_pre_replace_snapshot, _resolve_server_id
            dst_id = _resolve_server_id(dest_name)
            if not dst_id:
                raise ValueError(
                    f"Replace fan-out destination {dest_name!r} has no "
                    "registered server_id - cannot capture a rollback "
                    "snapshot. Re-register the destination from the "
                    "Servers tab."
                )
            pre_id = _capture_pre_replace_snapshot(
                job_id=f"fanout-restore:{dest_name}",
                settings=pre_replace_settings,
                dest_server=dst_server,
                dest_server_id=dst_id,
                dest_server_name=str(dst_row.get("name") or dest_name),
                dest_url=dst_row["url"],
            )
            dest_result.pre_replace_snapshot_id = pre_id

        run_restore(
            dst_server,
            resolved_inputs,
            dst_token,
            dst_row["url"],
            logger,
            run_log_dir,
            remap,
            strict_match,
            include_playlists=include_playlists,
            include_watch_history=include_watch_history,
            include_ratings=include_ratings,
            include_collections=include_collections,
            mode=mode,
            merge_watch_strategy=merge_watch_strategy,
            library_workers=library_workers,
            user_filter=user_filter,
        )

        # Per-destination handler-close suppressed - see note in
        # ``_run_one_direct_destination``. Fan-out-wide cleanup is
        # done from ``_finalise_fan_out_state``.
        _finalise_run_dir(run_log_dir)
        if stop_event is not None and stop_event.is_set():
            dest_result.state = "cancelled"
        else:
            dest_result.state = "completed"
    except Exception as exc:
        dest_result.state = "failed"
        dest_result.error = f"{type(exc).__name__}: {exc}"
        log.error("Fan-out import destination %r failed", dest_name, exc_info=True)
        if dest_result.dashboard is not None:
            try:
                dest_result.dashboard.push_activity(
                    "error", "-",
                    f"Destination failed: {dest_result.error}",
                )
            except Exception:
                pass
    finally:
        dest_result.finished_at = time.time()


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_pending_dashboard(dest_name: str) -> DashboardState:
    """
    Build a placeholder DashboardState for a not-yet-started destination.

    The WS payload includes every destination's dashboard from job start
    - including the queued ones - so the destination card paints
    immediately as "queued" instead of blank-then-pop.
    """
    d = DashboardState(log_dir="")
    try:
        d.push_activity("phase", "-", f"Destination {dest_name} queued")
    except Exception:
        pass
    return d


def _build_per_dest_log_dir(
    log_dir_root: str,
    source_or_kind: str,
    dest_name: str,
    verbose: bool,
) -> str:
    """
    Build the per-destination log directory and configure the engine's
    file handlers against it.

    Each destination thread runs this in its OWN context, so writing
    ``state._run_timestamp = …`` here is isolated from siblings -
    sibling destinations have their own ContextVar value for the same
    name.
    """
    from server.server_registry import safe_server_name
    from services.logging_ops import setup_logging

    slug = (
        f"{safe_server_name(source_or_kind)}-to-{safe_server_name(dest_name)}-fanout"
    )
    ts = time.strftime("%Y%m%d_%H%M%S")
    state._run_timestamp = f"{slug}_{ts}"
    setup_logging(log_dir_root, verbose)
    return str(state._run_log_dir) if state._run_log_dir else log_dir_root


def _close_logger(logger: logging.Logger) -> None:
    """
    Detach every file handler installed by setup_logging on the two
    named engine loggers. Best-effort: handler.close() on Windows can
    raise if the underlying file is still open in another thread; we
    swallow and continue so the per-dest run dir rename still proceeds.

    v0.13.x: the cross-contamination caveat that previous versions of
    this docstring described is now fixed. Each destination worker
    sets ``state._destination`` (a ContextVar) to its dest_name before
    setup_logging runs; the ``DestinationContextFilter`` in
    services/logging_ops.py stamps every LogRecord with that tag, and
    each per-destination FileHandler carries a
    ``_DestinationHandlerFilter`` that admits only matching records.
    Records emitted in sibling destinations' threads are filtered out
    at the handler level, so each destination's runtime/errors/media
    file writes only its own records.
    """
    for lg in (logging.getLogger("plexmigrate"), logging.getLogger("plexmigrate.media")):
        for h in lg.handlers[:]:
            try:
                h.close()
            finally:
                lg.removeHandler(h)


def _finalise_run_dir(run_log_dir: str) -> None:
    """
    PASS/FAIL rename so the per-dest run dir matches the
    single-destination on-disk artefact shape.
    """
    p = Path(run_log_dir)
    if not p.exists():
        return
    errors_file = p / "errors.log"
    passed = not (errors_file.exists() and errors_file.stat().st_size > 0)
    suffix = "PASS" if passed else "FAIL"
    final = p.parent / f"{p.name}_{suffix}"
    try:
        p.rename(final)
    except OSError:
        pass
