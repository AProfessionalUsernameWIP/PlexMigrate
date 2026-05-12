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
  at module import — that keeps unit tests from leaking a thread.
* The :class:`JobRecord` exposed via the REST endpoint is a plain
  dataclass copied out of the live state under lock, so the consumer
  never sees a torn read.
* Each job calls the same orchestration functions the CLI uses
  (:func:`services.exporter.run_export`, :func:`services.importer.run_import`).
  No engine logic is duplicated here — this file is a *driver*.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import services.state as state
from services.auth import _make_session
from services.exporter import run_export
from services.importer import run_import
from services.logging_ops import setup_logging

from server import runtime_patches
from server.direct_transfer import run_direct_transfer
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
# current library finishes; see services.exporter.run_export's stop
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
    mode: str                                 # "export" or "import"
    params: Dict[str, Any]
    state: str = STATE_QUEUED
    queued_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    error: Optional[str] = None
    run_log_dir: Optional[str] = None


_HISTORY_MAX = 50


# ── Job queue ────────────────────────────────────────────────────────────────

class JobQueue:
    """
    The single-writer queue. Public surface:

    * :meth:`submit_export` / :meth:`submit_import` — enqueue a job
      and return its ``JobRecord`` immediately. The actual engine run
      happens on the worker thread.
    * :meth:`current` — the currently running or most recently finished
      job (used by both the REST status endpoint and the WebSocket
      snapshot builder).
    * :meth:`history` — the in-memory job history.
    * :meth:`request_stop` — flip the engine's stop flag so a running
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
        self._lock = threading.Lock()

        # The worker thread is started lazily on first submit().
        self._worker: Optional[threading.Thread] = None
        self._worker_started = False

    # ── Public submission API ────────────────────────────────────────

    def submit_export(self, params: Dict[str, Any]) -> JobRecord:
        return self._submit("export", params)

    def submit_import(self, params: Dict[str, Any]) -> JobRecord:
        return self._submit("import", params)

    def submit_direct(self, params: Dict[str, Any]) -> JobRecord:
        """
        Enqueue a server-to-server direct transfer job. Params must
        carry ``source_server_name`` and ``dest_server_name``; the
        rest of the dict mirrors :class:`server.models.DirectTransferIn`.
        """
        return self._submit("direct", params)

    def _submit(self, mode: str, params: Dict[str, Any]) -> JobRecord:
        rec = JobRecord(job_id=str(uuid.uuid4()), mode=mode, params=dict(params))
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

    def busy(self) -> bool:
        with self._lock:
            return self._current is not None and self._current.state == STATE_RUNNING

    def request_stop(self) -> bool:
        """
        Ask the running job to wind down. Returns False if no job is
        running (in which case the caller should respond 409).

        Flips the live JobRecord into STATE_STOPPING *before* the
        engine's stop_event so the next WS snapshot the frontend reads
        already reflects the user's click. The actual state transition
        to COMPLETED / CANCELLED happens when the worker loop's
        finally-block runs, which can be seconds-to-minutes later
        depending on what's in flight.
        """
        if not self.busy():
            return False
        with self._lock:
            if self._current is not None and self._current.state == STATE_RUNNING:
                self._current.state = STATE_STOPPING
        return runtime_patches.signal_stop()

    # ── Worker thread ────────────────────────────────────────────────

    def _ensure_worker(self) -> None:
        # Start exactly one worker thread for the life of the process.
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
        worker thread itself never dies — a single bad job should
        not block the rest of the queue.
        """
        # Imported lazily so importing this module without the engine
        # installed (e.g. in some unit tests) still works.
        from services.dashboard import DashboardState

        while True:
            rec = self._inbox.get()
            with self._lock:
                self._current = rec
                rec.state = STATE_RUNNING
                rec.started_at = time.time()

            # ── Pre-flight DashboardState ─────────────────────────────
            # The engine's slow start-up (Plex connect → home-user auth
            # → playlist cache warm) can take 10–60 s on a large server
            # before run_export / run_import construct their own
            # DashboardState. Without a placeholder, the WS payload
            # broadcasts ``dashboard: null`` during that window and the
            # browser shows "No job is running" misleadingly. We create
            # an empty DashboardState here so the panel paints
            # immediately, and the engine augments it (rather than
            # replacing it) once pre-flight finishes.
            try:
                state._dashboard = DashboardState(log_dir="")
                state._dashboard.push_activity(
                    "started", "—",
                    f"{rec.mode.upper()} job initialising…",
                )
            except Exception:  # pragma: no cover (defensive)
                pass

            try:
                if rec.mode == "export":
                    self._run_export(rec)
                elif rec.mode == "import":
                    self._run_import(rec)
                elif rec.mode == "direct":
                    self._run_direct(rec)
                else:
                    raise ValueError(f"Unknown job mode {rec.mode!r}")
                rec.state = STATE_COMPLETED
            except _JobCancelled:
                rec.state = STATE_CANCELLED
            except Exception as exc:
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
                self._record_history(rec)

    def _record_history(self, rec: JobRecord) -> None:
        with self._lock:
            self._history.append(rec)
            if len(self._history) > _HISTORY_MAX:
                # Trim the oldest entries first.
                del self._history[: len(self._history) - _HISTORY_MAX]

    # ── Engine invocation: export ────────────────────────────────────

    def _run_export(self, rec: JobRecord) -> None:
        """
        Build a fresh logger + Plex connection, then call run_export().

        Multi-server (v0.9.0): the connection is resolved from
        ``source_server_name`` against the registered server list.
        For backward compatibility with ad-hoc CLI calls, raw
        ``plex_url`` + ``plex_token`` in the params still work.
        ``state._run_timestamp`` is prefixed with the server's
        filename-safe slug so log dirs and export filenames don't
        collide between servers.
        """
        settings = _merge_settings(rec.params, mode="export")

        # Resolve the connection. Either a registered server name
        # was supplied (preferred path) or a raw URL+token pair.
        server, url, token, owner, server_slug = _resolve_source_connection(
            settings, logger=logging.getLogger("plexmigrate")
        )
        settings["plex_url"] = url
        settings["plex_token"] = token
        settings["resolved_server_slug"] = server_slug
        rec.params = {k: v for k, v in settings.items() if k != "plex_token"}

        _set_run_timestamp(server_slug)
        logger, run_log_dir = _build_logger(settings["log_dir"], settings["verbose"])
        rec.run_log_dir = run_log_dir
        state.MAX_WORKERS = int(settings["workers"])
        state.SCROBBLE_WORKERS = int(settings["scrobble_workers"])
        state._session = _make_session()
        state._plex_base_url = url
        state._plex_token = token
        state._plex_owner_name = owner
        _populate_run_user_context(server, source_name=settings.get("source_server_name"))
        # Run-trigger labels: stamped into the export JSON so the
        # Exports tab can show how each backup was initiated. Defaults
        # to "manual" when the API call carries no explicit marker
        # (covers any future caller that forgets to set it).
        state._run_trigger = str(settings.get("_trigger") or "manual")
        state._run_schedule_name = str(settings.get("_schedule_name") or "")

        # Resolve the library *names* sent by the client to the
        # python-plexapi LibrarySection objects ``run_export`` expects.
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

        # Hand off to the engine. ``run_export`` returns when every
        # library is done (or stop_event is set, in which case it
        # finishes the in-flight ones and returns).
        run_export(
            server,
            selected,
            settings["output_dir"],
            logger,
            run_log_dir,
            url,
        )

        _close_logger(logger, run_log_dir)

        # Mirror the CLI's PASS/FAIL log rename so per-run log
        # directories on disk stay consistent across CLI and server.
        _finalise_run_dir(run_log_dir)

    # ── Engine invocation: import ────────────────────────────────────

    def _run_import(self, rec: JobRecord) -> None:
        settings = _merge_settings(rec.params, mode="import")

        # Multi-server resolution. ``dest_server_name`` selects the
        # destination registered server; falls back to ad-hoc
        # url/token from the legacy CLI shape if not present.
        # We adapt the source/dest naming on the fly so the same helper
        # is used for both export (source) and import (dest).
        if settings.get("dest_server_name"):
            settings["source_server_name"] = settings["dest_server_name"]
        server, url, token, owner, server_slug = _resolve_source_connection(
            settings, logger=logging.getLogger("plexmigrate")
        )
        settings["plex_url"] = url
        settings["plex_token"] = token
        settings["resolved_server_slug"] = server_slug
        rec.params = {k: v for k, v in settings.items() if k != "plex_token"}

        _set_run_timestamp(server_slug)
        logger, run_log_dir = _build_logger(settings["log_dir"], settings["verbose"])
        rec.run_log_dir = run_log_dir
        state.MAX_WORKERS = int(settings["workers"])
        state.SCROBBLE_WORKERS = int(settings["scrobble_workers"])
        state._session = _make_session()
        state._plex_base_url = url
        state._plex_token = token
        state._plex_owner_name = owner
        # Imports run against the destination server — pull its
        # display-name map so the dashboard's current_user attribution
        # uses the right side's friendly names.
        _populate_run_user_context(server, source_name=settings.get("dest_server_name") or settings.get("source_server_name"))

        # Verify each requested input file exists before kicking off
        # the engine — fail fast with a useful message rather than
        # mid-run with a stack trace.
        #
        # The web frontend's Run-Job form sends bare filenames pulled
        # from the export browser (e.g. "Movies_20260510.plexbackup.json")
        # because the directory is implied by the configured output
        # directory. The CLI may send absolute paths. We try the value
        # as-is first, then fall back to joining it against the
        # configured output_dir, so both call shapes work without
        # the client having to know the engine's working directory
        # inside the container.
        output_dir = settings.get("output_dir") or "./plex_exports"
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
            missing.append(f)
            logger.error(f"Backup file not found: {f} (also tried {scoped})")
        if not valid:
            raise ValueError(f"No valid backup files found. Missing: {missing}")

        remap: Optional[Tuple[str, str]] = None
        if settings.get("remap_old") and settings.get("remap_new"):
            remap = (settings["remap_old"], settings["remap_new"])

        run_import(
            server,
            valid,
            settings["plex_token"],
            settings["plex_url"],
            logger,
            run_log_dir,
            remap,
            bool(settings["strict_match"]),
        )

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
        """
        settings = _merge_settings(rec.params, mode="direct")

        src_name = settings.get("source_server_name")
        dst_name = settings.get("dest_server_name")
        if not src_name:
            raise ValueError("source_server_name is required for a direct transfer.")
        if not dst_name:
            raise ValueError("dest_server_name is required for a direct transfer.")
        if src_name == dst_name:
            raise ValueError("Source and destination must be different registered servers.")

        # Resolve both connections up front so we fail fast if either
        # is unreachable, rather than half-way through library 1.
        boot_logger = logging.getLogger("plexmigrate")
        if state._dashboard:
            state._dashboard.push_activity(
                "phase", "—", f"Connecting to source Plex '{src_name}'…",
            )
        src_server, src_row = connect_registered_server(src_name, boot_logger)
        if state._dashboard:
            state._dashboard.push_activity(
                "phase", "—", f"Connecting to destination Plex '{dst_name}'…",
            )
        dst_server, dst_row = connect_registered_server(dst_name, boot_logger)

        src_slug = safe_server_name(src_row["name"])
        dst_slug = safe_server_name(dst_row["name"])
        combined_slug = f"{src_slug}-to-{dst_slug}"
        _set_run_timestamp(combined_slug)

        # Decrypt source + destination tokens once, at the point of
        # use. The plaintexts live in local variables ``src_token`` /
        # ``dst_token`` for the duration of this run; settings["plex_token"]
        # holds the dest plaintext only because the engine's direct-HTTP
        # helpers read it from ``state._plex_token`` (documented residual
        # exposure — see services/state.py).
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
        # are not relevant here — users on the destination match by
        # raw identifier, not friendly name.
        _populate_run_user_context(src_server, source_name=src_name)

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
        # subset of users — i.e. direct transfer with a non-empty
        # filter. Standard export / import / unscoped direct transfer
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
        )

        _close_logger(logger, run_log_dir)
        _finalise_run_dir(run_log_dir)


# ── Helpers used by both export and import paths ──────────────────────────────

class _JobCancelled(Exception):
    """Raised when stop_event is set before the engine call completes."""


def _merge_settings(params: Dict[str, Any], *, mode: str) -> Dict[str, Any]:
    """
    Merge inbound request params on top of saved settings.

    Anything the client omitted (``None``) falls back to the saved
    settings document; anything the client supplied wins. The result
    is a flat dict that's safe to pass to the engine.

    Multi-server (v0.9.0): ``plex_url`` and ``plex_token`` are no
    longer required at this layer — the connection is normally
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
    connection. Strict — requires ``source_server_name`` to identify
    a registered row, and raises if it's missing or unmatched.

    v0.9.1 change: the previous build had a "legacy ad-hoc" fallback
    that used raw ``plex_url`` / ``plex_token`` from ``settings.json``
    when no ``source_server_name`` was supplied. That fallback caused
    the symptom of "every operation hits the first/default server" —
    a request that *should* fail loudly (no server selected) was
    silently succeeding against whichever server happened to be in
    legacy settings. The fallback is gone from this API path; the CLI
    still supports ad-hoc URL+token via its own ``--server`` /
    ``--token`` flags (see ``plexmigrate.py``), which does not go
    through this function.

    Returns ``(PlexServer, url, token, owner_name, slug)``. ``slug``
    is the filename-safe form of the friendly server name, used by
    :func:`_set_run_timestamp` to prefix log dirs and export filenames.
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
    if state._dashboard:
        state._dashboard.push_activity(
            "phase", "—", f"Connecting to Plex source '{name}'…",
        )
    server, fresh = connect_registered_server(name, logger)
    if state._dashboard:
        state._dashboard.push_activity(
            "started", "—", f"Connected to '{name}' as {fresh.get('owner_name') or '?'}",
        )
    # ``fresh["token"]`` is ciphertext (servers.json is encrypted at
    # rest). Decrypt here so the caller — which assigns the result
    # to ``state._plex_token`` for use by direct-HTTP helpers
    # (/:/scrobble, /:/rate, etc.) — gets plaintext.
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

    Both operations are best-effort — failures here must not block
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
    if state._dashboard is not None and source_name:
        try:
            row = get_server_by_name(source_name, include_token=False)
            if row is not None:
                state._dashboard.set_user_display_names(
                    row.get("user_display_names") or {}
                )
        except Exception:
            pass


def _set_run_timestamp(slug: str) -> None:
    """
    Re-derive ``state._run_timestamp`` so the current run's log dir
    and export filenames are prefixed with the server's slug.

    The engine reads ``state._run_timestamp`` lazily inside
    :func:`services.logging_ops.setup_logging` and
    :func:`services.exporter.export_library`, so we can reassign it
    here without touching either of those modules.

    Example: ``slug="Plex1"`` →
        log dir   : plex_logs/run_Plex1_20260510_135425/
        filename  : Movies_Plex1_20260510_135425.plexbackup.json
    """
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    state._run_timestamp = f"{slug}_{ts}" if slug and slug != "adhoc" else ts


def _build_logger(log_dir: str, verbose: bool) -> Tuple[logging.Logger, str]:
    """
    Set up the per-run logger the same way :func:`plexmigrate.main` does.
    Returns ``(logger, run_log_dir)``.
    """
    logger = setup_logging(log_dir, verbose)
    run_log_dir = str(state._run_log_dir) if state._run_log_dir else log_dir
    return logger, run_log_dir


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


def _finalise_run_dir(run_log_dir: str) -> None:
    """
    Mirror :func:`plexmigrate.main`'s end-of-run PASS/FAIL rename so a
    job invoked over the API leaves the same on-disk artefact a CLI
    run does. Best-effort: a rename failure on a locked file is logged
    and ignored — the logs themselves are still readable.
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
