"""
Single-worker job queue that drives the PlexMigrate engine.

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
  (:func:`services.snapshotter.run_snapshot`, :func:`services.restorer.run_restore`).
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
from services.snapshotter import run_snapshot
from services.restorer import run_restore
from services.logging_ops import setup_logging

from server import runtime_patches
from server.direct_transfer import run_direct_transfer
from server.fan_out import (
    FanOutResult,
    clear_active_result as _clear_fan_out_active,
    run_fan_out_direct,
    run_fan_out_restore,
)
from server.persistence import load_settings
from server.server_registry import (
    connect_registered_server,
    decrypt_server_token,
    get_server_by_name,
    safe_server_name,
)


log = logging.getLogger("plexmigrate.server.jobs")


# ── Job state model ──────────────────────────────────────────────────────────

# Possible values of JobRecord.state. Centralised so other modules
# can import the strings rather than hard-coding magic values.
STATE_IDLE = "idle"
STATE_QUEUED = "queued"
STATE_RUNNING = "running"
# "stopping" is the intermediate state between the user clicking Stop and
# the engine actually returning. The job stays in this state until the
# current library finishes; see services.snapshotter.run_snapshot's stop
# semantics. Surfaces to the frontend so the Stop button can re-label
# itself "Stopping…" and disable, giving the user immediate feedback.
STATE_STOPPING = "stopping"
STATE_COMPLETED = "completed"
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


_HISTORY_MAX = 50


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
        # PR-8: queued jobs awaiting the worker. Kept alongside the
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

    def _submit(self, mode: str, params: Dict[str, Any]) -> JobRecord:
        rec = JobRecord(job_id=str(uuid.uuid4()), mode=mode, params=dict(params))
        # PR-8: track the rec on the public pending list before queuing
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
        Snapshot of every queued JobRecord that hasn't started yet. PR-8.

        Excludes the currently-running record; combine with
        :meth:`current` to get the full active+queued list for the
        Dashboard tab's multi-job sub-tab strip.
        """
        with self._lock:
            return list(self._pending)

    def active_and_queued(self) -> List[JobRecord]:
        """
        Convenience snapshot used by the WS broadcaster (PR-8). The
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
                # PR-8: pop this record from the pending list now that
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

            # ── Pre-flight: clear any leftover fan-out registry ──────
            # If the previous job was a fan-out, ``server.fan_out``'s
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

            try:
                if rec.mode == "snapshot":
                    self._run_snapshot(rec)
                elif rec.mode == "restore":
                    self._run_restore(rec)
                elif rec.mode == "direct":
                    self._run_direct(rec)
                else:
                    raise ValueError(f"Unknown job mode {rec.mode!r}")
                # Stop P2: if request_stop flipped this job to STOPPING
                # (soft Stop), the engine returned because the
                # stop_event was set at an item-level checkpoint - that
                # is a CANCELLED job, not a COMPLETED one.
                rec.state = (
                    STATE_CANCELLED if rec.state == STATE_STOPPING
                    else STATE_COMPLETED
                )
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
                    rec.error = f"{type(exc).__name__}: {exc}"
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
                        # operator can see the wall-clock duration
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
                # v0.13.x: the engine no longer nulls state._dashboard
                # in its finally block (that was killing the dashboard
                # before the post-engine "Finalizing …" labels could
                # reach the WebSocket). The worker now owns the
                # cleanup, here at the very end after summary capture
                # is complete. Fan-out path uses its own delayed
                # cleanup via _finalise_fan_out_state; the unconditional
                # null here is the single-job equivalent.
                try:
                    state._dashboard = None
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

        # v0.13.x: resolve the requested library list BEFORE we stamp
        # the run timestamp so the run-dir name can include the
        # library names (`run_Plex1_Movies_TV-Shows_20260510_135425/`).
        # The full strict resolution / mismatch error happens further
        # down once the engine entrypoint takes over - this pass is
        # just informational for the slug.
        _wanted_libs = sorted({str(n) for n in (settings.get("libraries") or [])})
        _set_run_timestamp(server_slug, libraries=_wanted_libs)
        logger, run_log_dir = _build_logger(settings["log_dir"], settings["verbose"])
        rec.run_log_dir = run_log_dir
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
        # PR-13 fix #3 - publish the registered server's id into
        # state so the engine's per-library payload can ingest
        # straight into media.db. ``_resolve_server_id`` walks
        # server_registry to map ``source_server_name`` -> id; the
        # snapshot job hard-fails below if no row matches (engine
        # is media.db-primary now and refuses to run without a
        # registered server).
        snapshot_server_id = _resolve_server_id(settings.get("source_server_name"))
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
        # operator added a server to the registry between server-boot
        # and the first snapshot for it.
        try:
            from server import media_db, server_registry
            src_row = server_registry.get_server_by_id(snapshot_server_id, include_token=False) or {}
            media_db.upsert_server_row(
                server_id=snapshot_server_id,
                name=src_row.get("name") or settings.get("source_server_name") or "",
                url=src_row.get("url") or url,
                service="plex",
                machine_id=(src_row.get("machine_identifier") or None) or None,
            )
        except Exception:
            logging.getLogger("plexmigrate.server.jobs").exception(
                "Pre-snapshot media.db.servers upsert failed for %r; capture may fail.",
                snapshot_server_id,
            )

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
                # PR-3 / Phase D - four-flag data-type filter forwarded from
                # the request (or schedule). The Pydantic validator on
                # SnapshotJobIn / ScheduleIn already mapped any legacy
                # skip_* fields onto these include_* defaults.
                include_watch_history=bool(settings.get("include_watch_history", True)),
                include_ratings=bool(settings.get("include_ratings", True)),
                include_playlists=bool(settings.get("include_playlists", True)),
                include_collections=bool(settings.get("include_collections", True)),
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
        _close_logger(logger, run_log_dir)

        # Mirror the CLI's PASS/FAIL log rename so per-run log
        # directories on disk stay consistent across CLI and server.
        _finalise_run_dir(run_log_dir)

        # PR-13: capture the snapshot .db file + register it.
        # This is the heaviest post-engine step (it writes the whole
        # snapshot .db from the in-memory payloads) - the dominant
        # cause of the "stuck at the end" feeling, so it gets its own
        # finalize label.
        if _dash is not None:
            _dash.set_finalizing("writing snapshot to database")
        # Best-effort wrapper: failure here doesn't fail the job (the
        # engine already wrote rows to media.db; the operator's data
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
                f"Snapshot artifact capture failed: {type(exc).__name__}: {exc}. "
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

    # ── Engine invocation: import ────────────────────────────────────

    def _run_restore(self, rec: JobRecord) -> None:
        """
        File-mediated import. The destination is named by
        ``dest_server_name`` (single) or ``dest_server_names`` (fan-out).
        v0.10.0 dispatches to :func:`run_fan_out_restore` when more than
        one destination is requested; the single-destination path below
        is unchanged.
        """
        settings = _merge_settings(rec.params, mode="restore")

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

        # v0.13.x: pre-Replace auto-capture safety belt. Runs BEFORE
        # the restore so the destination has a rollback point on disk
        # by the time the engine starts overwriting data. On failure
        # the helper raises and the restore never fires - "no recovery
        # point" is exactly the case the belt is supposed to prevent.
        # The captured snapshot's id is stamped on rec.summary so the
        # operator can find it on the Snapshots tab later. The basic
        # state setup (session, MAX_WORKERS, plex_*) must happen first
        # because the helper reuses them.
        state.MAX_WORKERS = int(settings["workers"])
        state.SCROBBLE_WORKERS = int(settings["scrobble_workers"])
        state._session = _make_session()
        state._plex_base_url = url
        state._plex_token = token
        state._plex_owner_name = owner
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
            )
            # Stash on rec.summary so the audit row + UI can show it.
            rec.summary = {
                **(rec.summary or {}),
                "pre_replace_snapshot_id": pre_replace_snapshot_id,
            }

        # v0.13.x: peek the input files' top-level ``library`` fields so
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

        run_restore(
            server,
            valid,
            settings["plex_token"],
            settings["plex_url"],
            logger,
            run_log_dir,
            remap,
            bool(settings["strict_match"]),
            # PR-3 / Phase D - four-flag data-type filter forwarded from
            # the request. The Pydantic validator already mapped any
            # legacy skip_* fields onto these include_* defaults, so
            # both shapes work without translation here.
            include_playlists=bool(settings.get("include_playlists", True)),
            include_watch_history=bool(settings.get("include_watch_history", True)),
            include_ratings=bool(settings.get("include_ratings", True)),
            include_collections=bool(settings.get("include_collections", True)),
            mode=str(settings.get("mode") or "merge"),
            merge_watch_strategy=str(settings.get("merge_watch_strategy") or "higher"),
        )

        # Part B: run-level finalize phase so the dashboard doesn't
        # look frozen at 100% during the post-engine close-out.
        _dash = state.get_dashboard()
        if _dash is not None:
            _dash.set_finalizing("finalizing run")
        _close_logger(logger, run_log_dir)
        _finalise_run_dir(run_log_dir)

    # ── Engine invocation: direct server-to-server transfer ─────────

    def _run_direct(self, rec: JobRecord) -> None:
        """
        Direct transfer: read from one registered Plex and write to
        another without an intermediate file on disk. The orchestrator
        lives in :mod:`server.direct_transfer`; this method just
        handles connection resolution, logger setup, and stop-flag
        threading.

        Fan-out (v0.10.0): when ``dest_server_names`` carries more than
        one name the call is forwarded to :func:`run_fan_out_direct`
        and the single-destination resolution / engine call below is
        skipped. ``len == 1`` keeps the existing single-destination
        code path verbatim - the model validator collapses
        ``dest_server_name`` + ``dest_server_names`` into a list of one
        for older clients.
        """
        settings = _merge_settings(rec.params, mode="direct")

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
        src_server, src_row = connect_registered_server(src_name, boot_logger)
        if state.get_dashboard():
            state.get_dashboard().push_activity(
                "phase", "-", f"Connecting to destination Plex '{dst_name}'…",
            )
        dst_server, dst_row = connect_registered_server(dst_name, boot_logger)

        src_slug = safe_server_name(src_row["name"])
        dst_slug = safe_server_name(dst_row["name"])
        combined_slug = f"{src_slug}-to-{dst_slug}"
        # v0.13.x: libraries are known up-front for direct transfer
        # (operator picks them from the source). Include in the slug.
        _direct_libs = sorted({str(n) for n in (settings.get("libraries") or [])})
        _set_run_timestamp(combined_slug, libraries=_direct_libs)

        # Decrypt source + destination tokens once, at the point of
        # use. The plaintexts live in local variables ``src_token`` /
        # ``dst_token`` for the duration of this run; settings["plex_token"]
        # holds the dest plaintext only because the engine's direct-HTTP
        # helpers read it from ``state._plex_token`` (documented residual
        # exposure - see services/state.py).
        src_token = decrypt_server_token(src_row)
        dst_token = decrypt_server_token(dst_row)

        settings["plex_url"] = dst_row["url"]
        settings["plex_token"] = dst_token
        settings["source_url"] = src_row["url"]
        settings["resolved_server_slug"] = combined_slug
        rec.params = {
            k: v for k, v in settings.items() if k not in ("plex_token",)
        }

        logger, run_log_dir = _build_logger(settings["log_dir"], settings["verbose"])
        rec.run_log_dir = run_log_dir
        state.MAX_WORKERS = int(settings["workers"])
        state.SCROBBLE_WORKERS = int(settings["scrobble_workers"])
        state._session = _make_session()
        # Direct transfer attributes per-user work to the SOURCE side
        # (users are read from there). The destination's display names
        # are not relevant here - users on the destination match by
        # raw identifier, not friendly name.
        _populate_run_user_context(src_server, source_name=src_name)

        # v0.13.x: pre-Replace safety belt. Direct transfer writes into
        # the destination using the same primitive as restore, so the
        # same overwrite semantics apply when mode == "replace". Capture
        # the destination's pre-state before the engine fires so the
        # operator has a rollback point if they picked the wrong source.
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
        stop_event = runtime_patches._active_stop_event

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
            # PR-3 / Phase D - four-flag data-type filter.
            include_watch_history=bool(settings.get("include_watch_history", True)),
            include_ratings=bool(settings.get("include_ratings", True)),
            include_playlists=bool(settings.get("include_playlists", True)),
            include_collections=bool(settings.get("include_collections", True)),
            mode=str(settings.get("mode") or "merge"),
            merge_watch_strategy=str(settings.get("merge_watch_strategy") or "higher"),
        )

        # Part B: run-level finalize phase so the dashboard doesn't
        # look frozen at 100% during the post-engine close-out.
        _dash = state.get_dashboard()
        if _dash is not None:
            _dash.set_finalizing("finalizing run")
        _close_logger(logger, run_log_dir)
        _finalise_run_dir(run_log_dir)

    # ── Fan-out dispatch (v0.10.0) ───────────────────────────────────

    def _run_direct_fan_out(
        self,
        rec: JobRecord,
        settings: Dict[str, Any],
        src_name: str,
        dest_names: List[str],
    ) -> None:
        """
        Hand off a multi-destination direct transfer to
        :func:`server.fan_out.run_fan_out_direct`.

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

        stop_event = runtime_patches._active_stop_event

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
            # PR-3 / Phase D - four-flag data-type filter (fan-out direct).
            include_watch_history=bool(settings.get("include_watch_history", True)),
            include_ratings=bool(settings.get("include_ratings", True)),
            include_playlists=bool(settings.get("include_playlists", True)),
            include_collections=bool(settings.get("include_collections", True)),
            mode=str(settings.get("mode") or "merge"),
            merge_watch_strategy=str(settings.get("merge_watch_strategy") or "higher"),
            pre_replace_settings=_build_pre_replace_settings(settings),
        )
        _apply_fan_out_result(rec, result)

    def _run_restore_fan_out(
        self,
        rec: JobRecord,
        settings: Dict[str, Any],
        dest_names: List[str],
    ) -> None:
        """
        Hand off a multi-destination import to
        :func:`server.fan_out.run_fan_out_restore`. The single-server
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

        stop_event = runtime_patches._active_stop_event

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
            # PR-3 / Phase D - four-flag data-type filter (fan-out import).
            # The Pydantic validator translates any legacy skip_* into
            # include_* upstream.
            include_playlists=bool(settings.get("include_playlists", True)),
            include_watch_history=bool(settings.get("include_watch_history", True)),
            include_ratings=bool(settings.get("include_ratings", True)),
            include_collections=bool(settings.get("include_collections", True)),
            mode=str(settings.get("mode") or "merge"),
            merge_watch_strategy=str(settings.get("merge_watch_strategy") or "higher"),
            pre_replace_settings=_build_pre_replace_settings(settings),
        )
        _apply_fan_out_result(rec, result)


# ── Helpers used by both snapshot and import paths ──────────────────────────────

class _JobCancelled(Exception):
    """Raised when stop_event is set before the engine call completes."""


def _resolve_dest_names(settings: Dict[str, Any]) -> List[str]:
    """
    Return the ordered destination-server list for a direct-transfer or
    import job.

    The Pydantic model validator already collapses ``dest_server_name``
    and ``dest_server_names`` into the plural form, but this helper
    keeps working for callers that supply the singular form only (CLI
    invocations, scheduler payloads written before v0.10.0). Empty
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
    Translate a :class:`server.fan_out.FanOutResult` into the worker
    loop's terminal-state vocabulary.

    The worker's ``try`` block sets ``rec.state = STATE_COMPLETED``
    after we return cleanly; we raise :class:`_JobCancelled` if the
    fan-out was cancelled, or a plain ``RuntimeError`` if at least one
    destination failed (which the worker turns into ``STATE_FAILED``
    with ``rec.error``).
    """
    # v0.13.x: copy per-destination safety-belt snapshot ids onto the
    # parent JobRecord's summary so the operator can find every
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
        # v0.10.0: fan-out destinations. The Pydantic model collapses
        # singular ``dest_server_name`` into this list at the API
        # boundary, but we keep the legacy key populated for any
        # downstream code that hasn't been migrated yet.
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
    legacy settings. The fallback is gone from this API path; the CLI
    still supports ad-hoc URL+token via its own ``--server`` /
    ``--token`` flags (see ``plexmigrate.py``), which does not go
    through this function.

    Returns ``(PlexServer, url, token, owner_name, slug)``. ``slug``
    is the filename-safe form of the friendly server name, used by
    :func:`_set_run_timestamp` to prefix log dirs and snapshot filenames.
    """
    name = (settings.get("source_server_name") or "").strip()
    if not name:
        raise ValueError(
            "No server selected. Pick a registered Plex server in the "
            "Run Job form (or pass --source-server / --dest-server on "
            "the CLI). The server registry is managed under the "
            "Servers tab in the web UI."
        )
    row = get_server_by_name(name)
    if row is None:
        raise ValueError(
            f"No registered server named {name!r}. Open the Servers tab "
            f"in the web UI (or run `python plexmigrate.py --list-servers`) "
            f"to see registered names."
        )
    # Surface the slow Plex handshake on the dashboard's activity feed
    # so the user knows the job hasn't stalled. connect_registered_server
    # also does a probe / library enumeration which can take several
    # seconds on a large server.
    if state.get_dashboard():
        state.get_dashboard().push_activity(
            "phase", "-", f"Connecting to Plex source '{name}'…",
        )
    server, fresh = connect_registered_server(name, logger)
    if state.get_dashboard():
        state.get_dashboard().push_activity(
            "started", "-", f"Connected to '{name}' as {fresh.get('owner_name') or '?'}",
        )
    # ``fresh["token"]`` is ciphertext (servers.json is encrypted at
    # rest). Decrypt here so the caller - which assigns the result
    # to ``state._plex_token`` for use by direct-HTTP helpers
    # (/:/scrobble, /:/rate, etc.) - gets plaintext.
    plain_token = decrypt_server_token(fresh)
    return (
        server, fresh["url"], plain_token,
        fresh.get("owner_name") or "Plex Owner",
        safe_server_name(fresh["name"]),
    )


def _populate_run_user_context(
    server: Any,
    source_name: Optional[str] = None,
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
    if state.get_dashboard() is not None and source_name:
        try:
            row = get_server_by_name(source_name, include_token=False)
            if row is not None:
                state.get_dashboard().set_user_display_names(
                    row.get("user_display_names") or {}
                )
        except Exception:
            pass


_LIB_SLUG_STRIP = re.compile(r'[\\/:*?"<>|]+')
_LIBRARY_FIELD_RE = re.compile(rb'"library"\s*:\s*"([^"]+)"')
_LIBRARIES_IN_SLUG = 3  # cap before "+N" overflow kicks in


def _safe_lib_slug(name: str) -> str:
    """Sanitize one library name for use inside a filesystem path.

    Replaces whitespace with hyphens and strips characters that would
    cause trouble on Windows / macOS / Linux. Empty input -> "".
    """
    if not name:
        return ""
    cleaned = _LIB_SLUG_STRIP.sub("", name).strip()
    return re.sub(r"\s+", "-", cleaned) or ""


def _libraries_slug(libraries: Optional[List[str]]) -> str:
    """Compose a short, readable libraries fragment for the run-dir name.

    Caps the visible list at ``_LIBRARIES_IN_SLUG`` and appends ``+N``
    for the rest so long lists don't blow the dir name into something
    unreadable. Returns ``""`` when no libraries are supplied (the
    caller's slug then falls back to server-only).

    Example: ``["Movies", "TV Shows", "Audio-Books", "Music"]`` ->
    ``"Movies_TV-Shows_Audio-Books+1"``.
    """
    if not libraries:
        return ""
    parts: List[str] = []
    for name in libraries:
        slug = _safe_lib_slug(str(name))
        if slug:
            parts.append(slug)
    if not parts:
        return ""
    if len(parts) <= _LIBRARIES_IN_SLUG:
        return "_".join(parts)
    overflow = len(parts) - _LIBRARIES_IN_SLUG
    return "_".join(parts[:_LIBRARIES_IN_SLUG]) + f"+{overflow}"


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


def _set_run_timestamp(
    slug: str,
    libraries: Optional[List[str]] = None,
) -> None:
    """
    Re-derive ``state._run_timestamp`` so the current run's log dir
    and snapshot filenames are prefixed with the server's slug and,
    when known, the libraries the run is about.

    The engine reads ``state._run_timestamp`` lazily inside
    :func:`services.logging_ops.setup_logging` and
    :func:`services.snapshotter.snapshot_library`, so we can reassign it
    here without touching either of those modules.

    Examples:
      ``slug="Plex1", libraries=None`` ->
          log dir   : plex_logs/run_Plex1_20260510_135425/
          filename  : Movies_Plex1_20260510_135425.plexexport.json

      ``slug="Plex1", libraries=["Movies","TV Shows"]`` ->
          log dir   : plex_logs/run_Plex1_Movies_TV-Shows_20260510_135425/

      ``slug="Plex1", libraries=["Movies","TV","Audio","Music","Photos"]`` ->
          log dir   : plex_logs/run_Plex1_Movies_TV_Audio+2_20260510_135425/
    """
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    libs_part = _libraries_slug(libraries)
    if slug and slug != "adhoc":
        if libs_part:
            state._run_timestamp = f"{slug}_{libs_part}_{ts}"
        else:
            state._run_timestamp = f"{slug}_{ts}"
    else:
        state._run_timestamp = f"{libs_part}_{ts}" if libs_part else ts


def _build_logger(log_dir: str, verbose: bool) -> Tuple[logging.Logger, str]:
    """
    Set up the per-run logger the same way :func:`plexmigrate.main` does.
    Returns ``(logger, run_log_dir)``. When the operator has disabled
    run logging via the global ``run_logging_enabled`` setting,
    ``setup_logging`` skips the per-run directory entirely and returns
    a console-only logger; ``run_log_dir`` is then the empty string so
    downstream finalize / library-log writers know to no-op.
    """
    # Resolve the global toggle (default true). Per-server is
    # intentionally out of scope for run logging - one global switch.
    run_logging_enabled = bool(
        (load_settings() or {}).get("run_logging_enabled", True)
        if (load_settings() or {}).get("run_logging_enabled") is not None
        else True
    )
    logger = setup_logging(log_dir, verbose, run_logging_enabled=run_logging_enabled)
    if state._run_log_dir is None:
        return logger, ""
    return logger, str(state._run_log_dir)


def _close_logger(logger: logging.Logger, run_log_dir: str) -> None:
    """
    Close and detach all file handlers from the logger.

    The engine's setup_logging adds rotating file handlers to two
    named loggers; if we don't close them here, the per-run log
    directory rename below fails on Windows because the files are
    still open.
    """
    for lg in (logging.getLogger("plexmigrate"), logging.getLogger("plexmigrate.media")):
        for h in lg.handlers[:]:
            try:
                h.close()
            finally:
                lg.removeHandler(h)


def _resolve_server_id(source_server_name: Optional[str]) -> str:
    """
    Map a friendly ``source_server_name`` (the one operators see in
    the registry) to the registry row's stable ``id`` field used as
    foreign key in media.db. Returns an empty string when no match -
    callers treat that as "no snapshot capture possible."
    """
    if not source_server_name:
        return ""
    try:
        from server.server_registry import list_servers
        for row in list_servers(include_tokens=False):
            if (row.get("name") or "") == source_server_name:
                return str(row.get("id") or "")
    except Exception:
        return ""
    return ""


def _build_pre_replace_settings(settings: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Build the per-destination safety-belt payload for fan-out workers.

    Returns ``None`` when no belt should fire - either Merge mode or
    the operator explicitly disabled auto-capture. Returns a small
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
) -> str:
    """
    v0.13.x: pre-Replace auto-capture safety belt.

    Fires when ``mode == "replace"`` and the operator has the safety
    belt on (the default). Captures a fresh snapshot of the destination
    BEFORE the Replace runs so the operator has a rollback point. The
    snapshot is registered in ``snapshots.db`` with a ``[pre-replace
    safety]`` prefix in its name so it's distinguishable in the
    Snapshots tab.

    Scope: every library on the destination, every data type the
    upcoming Replace will touch (matches the restore's include_*
    flags). The captured payload is what the operator would need to
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
    # operator turned logging off globally - that's fine, the pre-snapshot
    # still runs.
    parent_log_dir = settings.get("log_dir") or "./plex_logs"
    pre_log_root = str(Path(parent_log_dir) / "pre_replace")
    pre_logger, pre_run_log_dir = _build_logger(
        pre_log_root, bool(settings.get("verbose") or False),
    )

    # Distinct run-timestamp slug so the pre-snapshot's filename and
    # run-log directory don't clash with the restore's. The restore
    # phase below re-stamps when it builds its own logger.
    pre_slug = f"{safe_server_name(dest_server_name)}_pre_replace"
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
            # snapshots. The operator wants the .db on disk; the
            # JSON copy is on-demand via the Exports tab if they
            # ever need it.
            "prebuild_json_sidecar": False,
        }
        error: Optional[str] = None
        run_log_dir: str = pre_run_log_dir

    pre_rec = _PreSnapshotRec()
    output_dir = settings.get("output_dir") or "./snapshots"

    # Push an activity-feed entry so the dashboard tells the operator
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
        _close_logger(pre_logger, pre_run_log_dir)
        _finalise_run_dir(pre_run_log_dir)

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
    PR-13 snapshot capture (Rule 1 - payload-direct edition).

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
    the operator clicks Download in the Exports panel - no sidecar
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
    # operator already cares about: server, libraries, captured_at.
    # The result is both the on-disk .db basename AND the registry's
    # ``snapshot_name`` field, so the file on disk reads e.g.
    # ``My Server - Audio-Books, Music - 2026-05-13 02-26.db`` instead
    # of the previous ``My-Server_20260513_022609.db``.
    captured_at_ts = time.time()
    snapshot_name = snapshot_registry.format_snapshot_filename(
        server_name=server_name,
        libraries=libraries,
        captured_at=captured_at_ts,
    )
    # v0.13.x: optional prefix lets callers tag auto-captured snapshots
    # so the Snapshots tab shows them distinctly from manual runs. The
    # pre-Replace safety belt uses "[pre-replace safety]" to mark its
    # rollback points - operators can find them by name when they need
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

    # Pull the operator-supplied display-name map from the registry
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

    # Operator who triggered the run, if auth is on. The job runner
    # stamps this in rec.params under a synthetic underscore-prefixed
    # key so it never collides with the standard SnapshotJobIn fields.
    created_by_user = rec.params.get("_actor_username") or None

    # Fix 3: store absolute paths so list_snapshots / Download / orphan
    # reconciliation never have to second-guess the CWD the FastAPI
    # process was launched from.
    snapshot_db_path = Path(output_dir).resolve() / f"{snapshot_name}.db"
    # Rule 1: snapshot content comes from the in-memory payload list
    # the engine appended during this run, NOT from media.db. The
    # collector is populated by services.snapshotter.snapshot_library
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

    # v0.13.x: granular finalize labels so the dashboard reports each
    # post-engine step. Pre-fix, the only post-100% signal was a single
    # "writing snapshot to database" label set before the heavy build
    # ran - everything else was silent. Now: build_db -> registry insert
    # -> optional sidecar render each get their own labelled phase, and
    # the operator sees progress all the way to STATE_COMPLETED.
    _dash_finalize = state.get_dashboard()
    if _dash_finalize is not None:
        _dash_finalize.set_finalizing("registering snapshot")

    try:
        registered = snapshot_registry.register(
            server_id=server_id,
            server_name=server_name,
            snapshot_name=snapshot_name,
            file_path=str(snapshot_db_path),
            # Pass the same timestamp we baked into snapshot_name so
            # the on-disk filename and the registry's captured_at are
            # consistent (otherwise register() uses time.time() at
            # insert and they could drift by milliseconds, breaking
            # any future round-trip filename reconstruction).
            captured_at=captured_at_ts,
            libraries=libraries,
            user_count=snapshot_capture.count_distinct_users(server_id),
            row_counts=row_counts,
            file_size=file_size,
            prebuilt_json_path=None,
            captured_types=captured_types,
            # Re-use the id we baked into the snapshot file's own
            # snapshot_meta table so the .db is internally consistent
            # with the registry row pointing at it.
            snapshot_id=snapshot_id_for_meta,
        )
        log.info(
            "Snapshot captured for %r: %s (%d bytes, %d libraries, %s users)",
            server_name, snapshot_name, file_size, len(libraries),
            row_counts.get("watch_events", "?"),
        )
        # Operator opt-in: render the .plexexport.json sidecar now so
        # the first Download click is instant. Off by default - this
        # is the legacy v0.11-era behaviour brought back as a checkbox.
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

    # v0.13.x: surface the snapshot id back to the caller. The
    # pre-Replace safety belt path stashes it on rec.summary so the
    # operator can recover the pre-restore state if the Replace
    # turned out to be wrong. Regular snapshot jobs ignore the return.
    return str(registered["id"]) if isinstance(registered, dict) and registered.get("id") else None


def _finalise_run_dir(run_log_dir: str) -> None:
    """
    Mirror :func:`plexmigrate.main`'s end-of-run PASS/FAIL rename so a
    job invoked over the API leaves the same on-disk artefact a CLI
    run does. Best-effort: a rename failure on a locked file is logged
    and ignored - the logs themselves are still readable.
    """
    # Run-logging-disabled path: no run dir to rename.
    if not run_log_dir:
        return
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
        # On Windows, a still-open handle blocks the rename. We've
        # already closed our handlers, but any background scan-cache
        # thread the engine may have spawned could still hold one.
        # Leaving the directory under its temporary name is acceptable.
        pass


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
