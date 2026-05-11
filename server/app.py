"""
FastAPI application — entry point for the PlexMigrate server.

Everything user-facing in the server runs through here:

  * REST endpoints for settings, libraries, job control, schedules,
    log browsing, and export browsing.
  * One WebSocket endpoint at ``/ws/dashboard`` that streams the
    engine's live state to the browser at 4 Hz.

The FastAPI app object lives at the module level so uvicorn can find
it via ``server.app:app`` — that string is the ``CMD`` in
``Dockerfile.backend``.

Important: this module imports :mod:`server.runtime_patches` *before*
any engine code runs. Those patches replace
:func:`services.dashboard._check_terminal_size` and
:func:`services.dashboard._keyboard_thread`. They are no-ops if the
engine is never started, so importing them at module top is safe even
for clients that only ever hit ``/api/health``.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

import services.state as state
from services.auth import connect_to_server, discover_libraries

from server import (
    SERVER_API_VERSION,
    export_browser,
    log_browser,
    persistence,
    runtime_patches,
    schedules,
    server_registry,
)
from server.jobs import get_queue
from server.models import (
    DirectTransferIn,
    ExportJobIn,
    ImportJobIn,
    JobStatusOut,
    ScheduleIn,
    ServerIn,
    SettingsIn,
)
from server.schedules import ensure_next_run_at, get_scheduler, list_schedules
from server.ws import build_snapshot, get_manager


# Apply headless-mode patches as early as possible. The job worker
# also calls this, but doing it at import time avoids any race with
# direct calls into the engine from REST handlers (e.g. /api/libraries).
runtime_patches.enable_headless_mode()


log = logging.getLogger("plexmigrate.server")


# ── App factory ──────────────────────────────────────────────────────────────

def create_app() -> FastAPI:
    """
    Build and return the FastAPI app. The module-level ``app`` is the
    canonical instance; this factory exists so tests can build their
    own isolated copy.
    """
    app = FastAPI(
        title="PlexMigrate Server",
        version=SERVER_API_VERSION,
        description="Web layer wrapping the PlexMigrate engine. CLI mode is unaffected.",
    )

    # In Docker the frontend is served by nginx and proxies /api and
    # /ws to this backend, so same-origin browser requests never hit
    # CORS preflight. For ``make cli`` users who run the backend
    # directly and a Vite dev server on another port, we permit any
    # origin by default — this server is meant to be reachable from
    # localhost only (it holds a Plex token).
    #
    # P2-3: tighten via the PLEXMIGRATE_CORS_ORIGINS env var when
    # exposing the backend to a less-trusted network. Comma-separated
    # list of origins (e.g. "http://localhost:5173,http://192.168.1.10")
    # overrides the wildcard. allow_credentials stays False either way
    # since we don't rely on browser cookies (the Plex token is held
    # server-side and never sent to the browser).
    raw_origins = os.environ.get("PLEXMIGRATE_CORS_ORIGINS", "").strip()
    if raw_origins:
        allowed = [o.strip() for o in raw_origins.split(",") if o.strip()]
    else:
        allowed = ["*"]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    _register_routes(app)
    _register_lifecycle(app)
    return app


# ── Lifecycle hooks ──────────────────────────────────────────────────────────

def _register_lifecycle(app: FastAPI) -> None:
    """
    Start / stop the scheduler thread and the WebSocket broadcaster
    in lockstep with the ASGI server lifecycle.
    """

    @app.on_event("startup")
    async def _startup() -> None:
        # v0.9.0: migrate any legacy plex_url/plex_token in settings.json
        # into the registry as a server called "Default". Idempotent.
        try:
            server_registry.migrate_legacy_settings(log)
        except Exception:  # pragma: no cover (defensive)
            log.exception("Legacy settings migration failed; continuing.")

        get_scheduler().start()
        await get_manager().start()
        # Warm the job queue up front so the worker thread is ready by
        # the time the first request arrives. ``get_queue`` is the
        # idempotent singleton accessor.
        get_queue()

        # Fire-and-forget connection probes for every registered server
        # so the Servers tab paints accurate status indicators on first
        # load. Errors are swallowed into the registry's last_status
        # field; this loop never raises.
        import threading as _t
        def _probe_all() -> None:
            for row in server_registry.list_servers(include_tokens=False):
                try:
                    server_registry.test_connection(row["id"], log)
                except Exception:
                    pass
        _t.Thread(target=_probe_all, name="server-probe", daemon=True).start()

        log.info("PlexMigrate server started")

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        get_scheduler().stop()
        await get_manager().stop()
        log.info("PlexMigrate server stopped")


# ── Routes ───────────────────────────────────────────────────────────────────

def _register_routes(app: FastAPI) -> None:

    # ── Health ───────────────────────────────────────────────────────

    @app.get("/api/health")
    def health() -> Dict[str, Any]:
        """
        Liveness probe. Docker compose health check hits this.
        """
        return {"ok": True, "api_version": SERVER_API_VERSION}

    @app.get("/api/server-time")
    def server_time() -> Dict[str, Any]:
        """
        Report the backend's wallclock + timezone so the UI can label
        the schedule editor with the zone the hour/minute fields are
        interpreted in. Without this the user has no signal that a
        misconfigured container TZ (defaults to UTC) is offsetting
        their schedules.
        """
        import datetime as _dt
        import time as _time
        now_local = _dt.datetime.now().astimezone()
        # IANA name from $TZ when set (Docker path); fall back to the
        # abbreviation if the host has no TZ env (rare for containers).
        iana = os.environ.get("TZ") or ""
        abbrev = _time.tzname[_time.localtime().tm_isdst] if _time.tzname else ""
        return {
            "now": _time.time(),
            "tz": iana or abbrev,
            "tz_abbrev": abbrev,
            "iso": now_local.isoformat(timespec="seconds"),
        }

    # ── Settings ─────────────────────────────────────────────────────

    @app.get("/api/settings")
    def get_settings() -> Dict[str, Any]:
        """
        Return the saved settings document with the Plex token redacted.
        The token itself is replaced by a boolean ``has_token``.
        """
        return persistence.redact_settings(persistence.load_settings())

    @app.post("/api/settings")
    def post_settings(body: SettingsIn) -> Dict[str, Any]:
        """
        Partial update: fields the client omits keep their prior value.
        Returns the redacted updated document.
        """
        patch = {k: v for k, v in body.model_dump().items() if v is not None}
        merged = persistence.save_settings(patch)
        return persistence.redact_settings(merged)

    # ── Servers (multi-server registry, v0.9.0) ─────────────────────

    @app.get("/api/servers")
    def list_servers() -> List[Dict[str, Any]]:
        """
        Return every registered server with status fields and the
        cached library list (if any). Tokens are stripped before send.
        """
        return server_registry.list_servers(include_tokens=False)

    @app.post("/api/servers")
    def create_server(body: ServerIn) -> Dict[str, Any]:
        """
        Register a new server. The handler immediately tries a live
        connection so the row is created with a populated status and
        library catalogue. The connection probe error (if any) is
        recorded on the row, not raised — the registry entry is kept
        so the user can fix the token without re-typing the URL.
        """
        try:
            row = server_registry.add_server(body.name, body.url, body.token)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        server_registry.test_connection(row["id"], log)
        # Return the freshly-probed row (with libraries cached, status
        # set) rather than the just-inserted row that has unknown status.
        latest = server_registry.get_server_by_id(row["id"], include_token=False)
        return latest or row

    @app.get("/api/servers/{server_id}")
    def get_one_server(server_id: str) -> Dict[str, Any]:
        row = server_registry.get_server_by_id(server_id, include_token=False)
        if row is None:
            raise HTTPException(status_code=404, detail=f"No server with id {server_id!r}")
        return row

    @app.put("/api/servers/{server_id}")
    def update_one_server(server_id: str, body: ServerIn) -> Dict[str, Any]:
        """
        Rename, change URL, or re-credential an existing server. An
        empty ``token`` means "leave the saved token unchanged" — the
        same write-only-token pattern Settings uses.
        """
        try:
            row = server_registry.update_server(
                server_id,
                name=body.name,
                url=body.url,
                token=body.token if body.token else None,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        # Always probe after update so the status reflects the new creds.
        server_registry.test_connection(server_id, log)
        return server_registry.get_server_by_id(server_id, include_token=False) or row

    @app.delete("/api/servers/{server_id}")
    def delete_one_server(server_id: str) -> Dict[str, Any]:
        """
        Remove a registered server. Export files and log directories
        on disk are **never** touched — this is a registry-only delete.
        """
        ok = server_registry.remove_server(server_id)
        if not ok:
            raise HTTPException(status_code=404, detail=f"No server with id {server_id!r}")
        return {"deleted": server_id}

    @app.post("/api/servers/{server_id}/test")
    def test_one_server(server_id: str) -> Dict[str, Any]:
        """
        Re-probe a server's connection. The cached status, libraries,
        and last-contacted timestamp on the registry row are all
        refreshed. Used by the Test button in the Servers tab.
        """
        try:
            return server_registry.test_connection(server_id, log)
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e))

    @app.post("/api/servers/{server_id}/ping")
    def ping_one_server(server_id: str) -> Dict[str, Any]:
        """
        Lightweight reachability probe (v0.9.1). Issues a single GET
        ``/identity`` against the registered URL with the saved token
        and returns ``{ok, response_ms, status, detail}`` — without
        enumerating libraries. The frontend polls this every 30s for
        the live status dot in the Servers tab and the per-option
        chip in the JobForm source/destination selectors.

        Always returns a body (never raises HTTPException) so the
        client's poll loop has uniform shape across reachable and
        unreachable rows. A non-existent server_id surfaces as
        ``status: "unknown"`` with a detail message.
        """
        return server_registry.ping_server(server_id)

    @app.get("/api/servers/{server_id}/libraries")
    def list_server_libraries(server_id: str) -> List[Dict[str, Any]]:
        """
        Return the freshly-fetched library catalogue for one server.
        Always probes Plex; the registry's cached ``last_libraries``
        is also updated as a side effect.
        """
        try:
            libs = server_registry.refresh_libraries(server_id, log)
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e))
        return libs

    # ── Libraries (legacy single-server alias, deprecated) ────────────
    # Kept for v0.8.x clients that still hit this endpoint. Resolves
    # against the first registered server when present, or against
    # legacy plex_url/plex_token in settings.json otherwise.

    @app.get("/api/libraries")
    def list_libraries_legacy() -> List[Dict[str, Any]]:
        rows = server_registry.list_servers(include_tokens=True)
        if rows:
            return server_registry.refresh_libraries(rows[0]["id"], log)
        settings = persistence.load_settings()
        url = settings.get("plex_url")
        token = settings.get("plex_token")
        if not url or not token:
            raise HTTPException(
                status_code=400,
                detail=(
                    "No servers registered. Add one under the Servers tab "
                    "before requesting libraries."
                ),
            )
        try:
            srv = connect_to_server(url, token, log)
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Plex connection error: {e}")
        if srv is None:
            raise HTTPException(status_code=502, detail=f"Cannot reach Plex at {url}.")
        return discover_libraries(srv, log)

    # ── Job control ──────────────────────────────────────────────────

    @app.get("/api/job", response_model=JobStatusOut)
    def get_job() -> JobStatusOut:
        """
        One-shot snapshot of the current (or most recent) job. The
        WebSocket carries the same data live; this endpoint exists so
        a fresh page load can paint immediately before its socket
        opens.
        """
        snap = build_snapshot()
        job = snap.get("job")
        if job is None:
            return JobStatusOut(state="idle")
        return JobStatusOut(
            state=job["state"],
            job_id=job.get("job_id"),
            mode=job.get("mode"),
            started_at=job.get("started_at"),
            finished_at=job.get("finished_at"),
            error=job.get("error"),
            dashboard=snap.get("dashboard"),
        )

    @app.get("/api/job/history")
    def get_job_history() -> List[Dict[str, Any]]:
        """
        Return the in-memory job history (newest first). Cleared on
        server restart by design — this is a live operations view,
        not an audit log. Per-run on-disk artefacts under
        ``plex_logs/`` are the durable record.
        """
        recs = list(reversed(get_queue().history()))
        out: List[Dict[str, Any]] = []
        for r in recs:
            out.append({
                "job_id": r.job_id,
                "mode": r.mode,
                "state": r.state,
                "queued_at": r.queued_at,
                "started_at": r.started_at,
                "finished_at": r.finished_at,
                "error": r.error,
                "run_log_dir": r.run_log_dir,
                "params": {k: v for k, v in r.params.items() if k != "plex_token"},
            })
        return out

    @app.post("/api/job/export")
    def post_job_export(body: ExportJobIn) -> Dict[str, Any]:
        """
        Enqueue an export job. Returns the JobRecord immediately;
        progress is reported live via the WebSocket.
        """
        params = body.model_dump(exclude_none=True)
        # Tag the run as user-initiated so the export JSON (and the
        # Exports tab in the GUI) can distinguish it from scheduler
        # fires.
        params["_trigger"] = "manual"
        rec = get_queue().submit_export(params)
        return {"job_id": rec.job_id, "state": rec.state, "mode": rec.mode}

    @app.post("/api/job/import")
    def post_job_import(body: ImportJobIn) -> Dict[str, Any]:
        """
        Enqueue an import job. Mirrors :func:`post_job_export`.
        """
        rec = get_queue().submit_import(body.model_dump(exclude_none=True))
        return {"job_id": rec.job_id, "state": rec.state, "mode": rec.mode}

    @app.post("/api/job/direct")
    def post_job_direct(body: DirectTransferIn) -> Dict[str, Any]:
        """
        Enqueue a direct server-to-server transfer job. Both
        ``source_server_name`` and ``dest_server_name`` are required
        and must resolve to different registered servers.
        """
        rec = get_queue().submit_direct(body.model_dump(exclude_none=True))
        return {"job_id": rec.job_id, "state": rec.state, "mode": rec.mode}

    @app.post("/api/job/stop")
    def post_job_stop() -> Dict[str, Any]:
        """
        Ask the running job to wind down at its next safe checkpoint.
        Returns 409 if no job is currently running.
        """
        ok = get_queue().request_stop()
        if not ok:
            raise HTTPException(status_code=409, detail="No job is currently running.")
        return {"stop_requested": True}

    # ── Schedules ────────────────────────────────────────────────────

    @app.get("/api/schedules")
    def get_schedules() -> List[Dict[str, Any]]:
        return list_schedules()

    @app.post("/api/schedules")
    def post_schedule(body: ScheduleIn) -> Dict[str, Any]:
        """
        Create a new schedule. ``id`` will be assigned by the server
        — clients should omit it on create.
        """
        doc = body.model_dump()
        doc.pop("id", None)
        ensure_next_run_at(doc)
        return persistence.upsert_schedule(doc)

    @app.put("/api/schedules/{schedule_id}")
    def put_schedule(schedule_id: str, body: ScheduleIn) -> Dict[str, Any]:
        """
        Replace an existing schedule. The path parameter is the
        authoritative id — body ``id`` is overwritten if it disagrees.
        """
        doc = body.model_dump()
        doc["id"] = schedule_id
        ensure_next_run_at(doc)
        return persistence.upsert_schedule(doc)

    @app.delete("/api/schedules/{schedule_id}")
    def delete_schedule_route(schedule_id: str) -> Dict[str, Any]:
        ok = persistence.delete_schedule(schedule_id)
        if not ok:
            raise HTTPException(status_code=404, detail=f"No schedule with id {schedule_id!r}")
        return {"deleted": schedule_id}

    # ── Log browser ──────────────────────────────────────────────────

    @app.get("/api/logs")
    def list_log_runs() -> List[Dict[str, Any]]:
        return log_browser.list_runs()

    @app.get("/api/logs/{run_name}")
    def list_log_files(run_name: str) -> List[Dict[str, Any]]:
        try:
            return log_browser.list_files(run_name)
        except FileNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @app.get("/api/logs/{run_name}/{file_name}")
    def read_log_file(run_name: str, file_name: str, since: int = 0) -> Dict[str, Any]:
        """
        Read a log file's contents. Pass ``?since=<byte-offset>`` to
        fetch only the bytes appended since the previous read — used
        by the frontend's live-tail poll. Omit (or ``since=0``) for
        a full read.
        """
        try:
            return log_browser.read_file(run_name, file_name, since=since)
        except FileNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    # ── Export browser ───────────────────────────────────────────────

    @app.get("/api/exports")
    def list_export_files() -> List[Dict[str, Any]]:
        return export_browser.list_exports()

    @app.get("/api/exports/{file_name}")
    def download_export(file_name: str) -> FileResponse:
        try:
            path = export_browser.export_path(file_name)
        except FileNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return FileResponse(
            path=str(path),
            media_type="application/json",
            filename=path.name,
        )

    @app.delete("/api/exports/{file_name}")
    def delete_export_file(file_name: str) -> Dict[str, str]:
        """
        Remove one ``.plexbackup.json`` from the configured output
        directory. Returns ``{"deleted": "<name>"}`` on success; 404
        if the file doesn't exist; 400 if the name fails containment
        or the suffix check.
        """
        try:
            export_browser.delete_export(file_name)
        except FileNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"deleted": file_name}

    # ── WebSocket ────────────────────────────────────────────────────

    @app.websocket("/ws/dashboard")
    async def ws_dashboard(ws: WebSocket) -> None:
        """
        Bidirectional socket. The server pushes a JSON snapshot every
        250 ms; the client may send back ping frames (we ignore the
        content, but the read keeps the connection alive on browsers
        that idle-close after 60 seconds without traffic).
        """
        manager = get_manager()
        await manager.connect(ws)
        try:
            while True:
                # We never act on inbound messages — just keep the
                # socket open. If the client disconnects, ``receive``
                # raises and we drop them from the pool.
                await ws.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            await manager.disconnect(ws)


# ── Module-level app instance ─────────────────────────────────────────────────
# uvicorn imports this as ``server.app:app``.
app = create_app()
