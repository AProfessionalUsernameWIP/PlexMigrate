"""
FastAPI application - entry point for the PlexMigrate server.

Everything user-facing in the server runs through here:

  * REST endpoints for settings, libraries, job control, schedules,
    log browsing, and snapshot browsing.
  * One WebSocket endpoint at ``/ws/dashboard`` that streams the
    engine's live state to the browser at 4 Hz.

The FastAPI app object lives at the module level so uvicorn can find
it via ``server.app:app`` - that string is the ``CMD`` in
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
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from starlette.background import BackgroundTask
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

import services.state as state
from services.auth import connect_to_server, discover_libraries

from server import (
    SERVER_API_VERSION,
    auth_router as _auth_router_module,
    snapshot_browser,
    log_browser,
    managed_users_router as _managed_users_router_module,
    persistence,
    runtime_patches,
    schedules,
    server_registry,
)
from server.jobs import get_queue
from server.models import (
    DirectTransferIn,
    SnapshotJobIn,
    RestoreJobIn,
    RestoreFromSnapshotIn,
    JobStatusOut,
    ScheduleIn,
    ServerIn,
    TestUnsavedIn,
    SettingsIn,
    UserDisplayNameIn,
)
from server.schedules import ensure_next_run_at, get_scheduler, list_schedules
from server.ws import build_dashboard_frame, get_manager


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
    # directly and a Vite dev server on another port, the default
    # allowlist below covers the standard localhost dev/prod origins
    # (backend 8000, nginx 8080, Vite 5173). This server holds a Plex
    # token and is meant to be reachable from localhost only, so a
    # wildcard default is the wrong posture.
    #
    # P2-3 / M4: tighten or widen via the PLEXMIGRATE_CORS_ORIGINS env
    # var. Comma-separated list of origins
    # (e.g. "http://localhost:5173,http://192.168.1.10") overrides the
    # default; the single value "*" explicitly opts back into the
    # wildcard for trusted-network setups. allow_credentials stays
    # False either way since we don't rely on browser cookies for
    # cross-origin auth (the JWT rides the Authorization header, the
    # refresh cookie is same-origin only).
    raw_origins = os.environ.get("PLEXMIGRATE_CORS_ORIGINS", "").strip()
    if raw_origins:
        allowed = [o.strip() for o in raw_origins.split(",") if o.strip()]
    else:
        allowed = [
            "http://localhost:8000", "http://127.0.0.1:8000",
            "http://localhost:8080", "http://127.0.0.1:8080",
            "http://localhost:5173", "http://127.0.0.1:5173",
        ]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # v0.11.0 - opt-in JWT auth. When the env var is off this
    # middleware passes every request through unchanged. When on, it
    # validates ``Authorization: Bearer <jwt>`` on every request
    # except the public probe / setup / login endpoints.
    app.add_middleware(_AuthMiddleware)

    # Mount the auth router (status / setup / login / logout / users).
    # Always mounted - when auth is disabled the public endpoints
    # respond "auth_enabled: false" so the frontend keeps a stable
    # contract and existing code paths never branch on whether the
    # router exists.
    app.include_router(_auth_router_module.router)

    # PR-10 - managed-users management API. Operator+ JWT for reads,
    # db_admin gate on writes (separate credential pair re-validated
    # per request).
    app.include_router(_managed_users_router_module.router)

    _register_routes(app)
    _register_lifecycle(app)
    return app


# ── Auth middleware ──────────────────────────────────────────────────────────

# Paths that bypass JWT auth entirely. Anything else requires a valid
# bearer token. WebSocket auth lives in the route handler itself
# (Starlette doesn't route websocket frames through HTTP middleware),
# so ``/ws/`` is also exempt here.
#
# PR-A2 shrunk this list. The PR-9.1 ``/admin/*`` and
# ``/login-account/*`` entries were removed - those endpoints now
# require a root_admin JWT (gating moved inside the handlers via the
# new ``require_role`` dependency). What's left is the minimum
# bootstrap surface the unauthenticated browser needs to render the
# login / setup screens plus the public health probe.
_AUTH_PUBLIC_PREFIXES = (
    "/api/health",
    "/api/auth/status",
    "/api/auth/setup",
    "/api/auth/login",
    "/api/auth/logout",
    # /api/auth/refresh is reachable without a bearer JWT - the
    # refresh-token cookie is the sole credential. The handler itself
    # validates and 401s if the cookie is missing or invalid.
    "/api/auth/refresh",
    "/ws",
    # Docs surface - useful during development. The HTML itself is
    # read-only; the OpenAPI fetch happens unauthenticated which is
    # fine since the schema isn't sensitive.
    "/docs",
    "/openapi.json",
    "/redoc",
)


class _AuthMiddleware(BaseHTTPMiddleware):
    """
    Validate ``Authorization: Bearer <jwt>`` on every request that
    isn't on the public-prefix list.

    PR-A2: auth is always on. The previous ``PLEXMIGRATE_AUTH_ENABLED``
    short-circuit is gone - every non-public request must carry a
    valid JWT or it's rejected with 401.
    """

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        # M6: exact-match or true path-segment prefix only. A bare
        # ``startswith(p)`` would treat ``/api/healthcheck-internal``
        # or ``/openapi.json.bak`` as public and bypass JWT auth.
        if any(path == p or path.startswith(p + "/")
               for p in _AUTH_PUBLIC_PREFIXES):
            return await call_next(request)

        # M5: bearer token comes from ``Authorization: Bearer X`` only.
        # The previous ``?access_token=X`` query-param fallback was
        # removed - query strings are logged by uvicorn, reverse
        # proxies, and browser history, and the log scrubber didn't
        # redact them, so a JWT in the URL was a credential leak. The
        # WebSocket ``?token=`` is unavoidable (browsers can't set
        # handshake headers) and is validated in the route handler.
        token = ""
        header = request.headers.get("authorization") or ""
        if header.lower().startswith("bearer "):
            token = header.split(" ", 1)[1].strip()

        payload = _auth_router_module.decode_token(token)
        if not payload:
            return JSONResponse(
                status_code=401,
                content={"detail": "Authentication required."},
                headers={"WWW-Authenticate": "Bearer"},
            )

        # Attach the decoded payload to the request so admin-only
        # routes (POST/GET /api/auth/users) can read the role without
        # re-decoding.
        request.state.auth = payload
        return await call_next(request)


# ── Lifecycle hooks ──────────────────────────────────────────────────────────

def _register_lifecycle(app: FastAPI) -> None:
    """
    Start / stop the scheduler thread and the WebSocket broadcaster
    in lockstep with the ASGI server lifecycle.
    """

    @app.on_event("startup")
    async def _startup() -> None:
        # v0.9.5: install the X-Plex-Token scrubber on every existing
        # log handler BEFORE any token-touching code runs. Catches
        # uvicorn's access/error loggers (which can record request
        # URLs containing the token as a query param) plus anything
        # the legacy-migration path logs below.
        try:
            from server.log_scrubber import install_on_all_handlers
            install_on_all_handlers()
        except Exception:  # pragma: no cover (defensive)
            log.exception("Log scrubber install failed; continuing.")

        # PR-A2: initialise the auth database eagerly so the first
        # ``/api/auth/...`` request doesn't pay the cold-start cost.
        # Always runs - auth is mandatory.
        try:
            from server import auth_db
            auth_db.init_auth_db()
        except Exception:  # pragma: no cover (defensive)
            log.exception("Auth database init failed; continuing.")

        # PR-13: rename the default ``plex_exports`` output_dir to
        # ``snapshots`` in settings.json, and relocate any leftover
        # .plexbackup.json files from the old directory into
        # snapshots/legacy/. Both are idempotent; subsequent boots
        # silent-noop.
        try:
            persistence.migrate_output_dir_setting()
            persistence.relocate_legacy_backups()
        except Exception:  # pragma: no cover (defensive)
            log.exception("PR-13 snapshot-dir migration failed; continuing.")

        # PR-13: initialise the snapshot registry (server_data/snapshots.db).
        # Cheap; just creates the file + schema on first run.
        try:
            from server import snapshot_registry
            snapshot_registry.init_registry()
        except Exception:  # pragma: no cover (defensive)
            log.exception("Snapshot registry init failed; continuing.")

        # Backfill media.db's ``servers`` table from the registry. Pre-fix,
        # ``upsert_server_row`` existed but was never called - so newly-captured
        # snapshots tripped the ``SELECT id FROM servers WHERE id = ?`` precondition
        # in ``snapshot_capture.create_snapshot_db`` and silently failed at the
        # post-job hook. This walk fixes existing installs in place; subsequent
        # ``add_server`` / ``update_server`` calls mirror their writes directly.
        try:
            from server import media_db
            registered = server_registry.list_servers(include_tokens=False)
            mirrored = 0
            for row in registered:
                try:
                    media_db.upsert_server_row(
                        server_id=row.get("id") or "",
                        name=row.get("name") or "",
                        url=row.get("url") or "",
                        service="plex",
                        machine_id=(row.get("machine_identifier") or None) or None,
                    )
                    mirrored += 1
                except Exception:  # pragma: no cover (defensive)
                    log.exception(
                        "Backfill failed for server %r; continuing.",
                        row.get("name"),
                    )
            log.info("Backfilled %d server row(s) into media.db.", mirrored)
        except Exception:  # pragma: no cover (defensive)
            log.exception("media.db server backfill step failed; continuing.")

        # Reconcile orphaned snapshot .db files - files that landed on
        # disk during a previous run but never made it to a registry
        # row (process killed, container restarted, disk full mid-INSERT).
        # Runs once at startup, before the queue accepts work, so any
        # recovered rows appear in the Backups panel on first paint.
        # Fast no-op when there are nothing to recover.
        try:
            from server.persistence import load_settings as _ls
            output_dir = _ls().get("output_dir") or "./snapshots"
            stats = snapshot_registry.reconcile_orphaned_snapshots(output_dir)
            if stats.get("recovered") or stats.get("errors"):
                log.info(
                    "Snapshot reconciliation: recovered=%d skipped=%d errors=%d",
                    stats["recovered"], stats["skipped"], stats["errors"],
                )
        except Exception:  # pragma: no cover (defensive)
            log.exception("Snapshot reconciliation failed; continuing.")

        # Refresh-token housekeeping. Reap expired rows once at startup
        # so a container that's been off for longer than the refresh
        # window comes back clean. Then spawn a daemon thread that
        # sleeps 24h between sweeps - matches the SchedulerDaemon
        # pattern used elsewhere in the server.
        try:
            from server import auth_db as _auth_db_for_cleanup
            removed = _auth_db_for_cleanup.cleanup_expired_tokens()
            if removed:
                log.info("Pruned %d expired refresh-token row(s) at startup.", removed)

            import threading as _t_cleanup

            def _refresh_token_cleanup_loop() -> None:
                while True:
                    # Sleep first so a quick reboot/test cycle doesn't
                    # hammer the table; the startup sweep above already
                    # caught anything dangling.
                    import time as _time_inner
                    _time_inner.sleep(24 * 60 * 60)
                    try:
                        n = _auth_db_for_cleanup.cleanup_expired_tokens()
                        if n:
                            log.info("Pruned %d expired refresh-token row(s).", n)
                    except Exception:
                        log.exception("Refresh-token cleanup sweep failed.")

            _t_cleanup.Thread(
                target=_refresh_token_cleanup_loop,
                name="refresh-token-cleanup",
                daemon=True,
            ).start()
        except Exception:  # pragma: no cover (defensive)
            log.exception("Refresh-token cleanup init failed; continuing.")

        # v0.12.0: initialise the media-state database (creates
        # media.db on first boot, runs pending migrations on every
        # boot). Always runs - the DB is the v0.12.x+ data layer
        # foundation regardless of whether auth is enabled.
        try:
            from server import media_db
            media_db.init_media_db()
        except Exception:  # pragma: no cover (defensive)
            log.exception("media_db init failed; continuing without DB.")

        # Rule 2: start the library-walk scheduler. Daemon thread,
        # cadence read from settings.library_walk.interval_seconds
        # at every tick (default 24h, floored at 1h). Walks at most
        # once per server per interval and is the data source for
        # the Prune Missing Items action.
        try:
            from server.library_walk import scheduler as _walk_scheduler
            _walk_scheduler.start()
        except Exception:  # pragma: no cover (defensive)
            log.exception("LibraryWalkScheduler start failed; continuing.")

        # M2: sweep stale chained-transfer temp files
        # (``*.tmp.plexexport.json``) from the configured output dir.
        # A failed chained import deliberately leaves its payload on
        # disk for manual recovery; this clears ones old enough that
        # the operator has moved on, so sensitive payloads don't
        # linger indefinitely.
        try:
            from server import direct_transfer as _dt
            _out_dir = (persistence.load_settings() or {}).get("output_dir") or "./snapshots"
            _dt.sweep_stale_tmp_exports(_out_dir)
        except Exception:  # pragma: no cover (defensive)
            log.exception("Stale tmp-export sweep failed; continuing.")

        # v0.9.0: migrate any legacy plex_url/plex_token in settings.json
        # into the registry as a server called "Default". Idempotent.
        try:
            server_registry.migrate_legacy_settings(log)
        except Exception:  # pragma: no cover (defensive)
            log.exception("Legacy settings migration failed; continuing.")

        # v0.9.5: force-migrate any plaintext rows in servers.json so
        # the file on disk is fully encrypted before uvicorn binds the
        # port. Without this the encryption pass would only run when
        # the first /api/servers request landed - leaving a brief
        # window where someone inspecting the volume could see
        # plaintext.
        try:
            count = server_registry.ensure_encrypted_at_rest()
            log.info("Registry at-rest encryption verified for %d row(s).", count)
        except Exception:  # pragma: no cover (defensive)
            log.exception("Registry encryption ensure step failed; continuing.")

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


# ── Helpers ──────────────────────────────────────────────────────────────────

def _auto_sync_managed_users(server_id: str) -> None:
    """
    PR-11 - fire a best-effort managed-users sync for one server.
    Used by the server-connect hooks (create / update / test) so the
    DB stays warm without operator action. Swallows all exceptions:
    sync failures must NOT block the surrounding server-registry call
    from succeeding.
    """
    try:
        from server import media_db
        result = media_db.sync_managed_users_from_live(server_id, log)
        if result.get("error"):
            log.warning(
                "Auto-sync managed users for server %r returned: %s",
                server_id, result["error"],
            )
        elif result.get("synced"):
            log.info(
                "Auto-synced %d managed user(s) for server %r",
                result["synced"], server_id,
            )
    except Exception:  # pragma: no cover (defensive)
        log.exception(
            "Auto-sync managed users failed unexpectedly for server %r",
            server_id,
        )


# ── Routes ───────────────────────────────────────────────────────────────────

def _register_routes(app: FastAPI) -> None:

    # ── Health ───────────────────────────────────────────────────────

    @app.get("/api/health")
    def health() -> Dict[str, Any]:
        """
        Liveness probe. Docker compose health check hits this.

        ``app_version`` (engine VERSION) is included so the frontend
        Troubleshooting page can stamp bug reports with the right
        build. ``api_version`` is the wire-protocol version (separate
        from the engine release; bumps when REST/WS shapes change).
        """
        return {
            "ok": True,
            "api_version": SERVER_API_VERSION,
            "app_version": state.VERSION,
        }

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

    # ── Database health (v0.12.0) ────────────────────────────────────

    @app.get("/api/db/stats")
    def db_stats() -> Dict[str, Any]:
        """
        Return media.db health: schema version, per-table row counts,
        most-recent ``updated_at`` per content table, on-disk size,
        and the file path inside the container.

        Surfaces in the future Database tab so operators can answer
        "is the DB warm" without opening sqlite3 on the host. Auth-
        protected when auth is enabled (the middleware sits in front
        of every route except the public-prefix list).
        """
        try:
            from server import media_db
            return media_db.get_stats()
        except RuntimeError as e:
            # init_media_db() was never called or failed at startup.
            raise HTTPException(status_code=503, detail=str(e))

    # ── Library walk + prune missing items (Rule 2) ──────────────────

    @app.get("/api/servers/{server_id}/library-walk")
    def get_library_walk_status(server_id: str) -> Dict[str, Any]:
        """
        Most-recent library walk summary for one server plus a flag
        indicating whether a walk is currently running. Powers the
        "Last walked" column on the Servers panel and the "in
        progress…" state on the walk-now button.
        """
        from server import media_db, library_walk
        return {
            "running": library_walk.is_walk_running(server_id),
            "last": media_db.get_last_walk_summary(server_id),
            "recent": media_db.list_library_walks(server_id, limit=10),
        }

    @app.post("/api/servers/{server_id}/library-walk")
    def trigger_library_walk(server_id: str) -> Dict[str, Any]:
        """
        Fire a library walk for one server. M11: the walk runs in a
        background thread - this returns ``{walk_id, started}``
        immediately and the caller polls the GET endpoint to track
        progress, instead of holding a uvicorn worker for the whole
        (minutes-long) walk. Idempotent: a concurrent call returns the
        running walk's id with ``started=0``.
        """
        from server import server_registry, library_walk
        try:
            srv = server_registry.get_server_by_id(
                server_id, include_token=True,
            )
        except Exception:
            log.exception("get_server_by_id failed for %r", server_id)
            raise HTTPException(
                status_code=500,
                detail="Could not load the server record - see the server log.",
            )
        if srv is None:
            raise HTTPException(status_code=404, detail=f"No server with id {server_id!r}.")
        url = srv.get("url") or ""
        token = srv.get("token") or ""
        if not (url and token):
            raise HTTPException(
                status_code=400,
                detail="Server is missing a URL or auth token.",
            )
        return library_walk.start_walk_background(
            server_id=server_id, url=url, token=token,
        )

    @app.get("/api/servers/{server_id}/prune-preview")
    def prune_preview(
        server_id: str,
        older_than_days: float = 7.0,
    ) -> Dict[str, Any]:
        """
        Dry-run preview of the Prune Missing Items action. Returns
        ``{count, sample_items[], last_walk}`` so the UI can show
        "X items not seen in the last N days" plus a sample list,
        before the operator commits to the destructive call.

        The ``last_walk`` field surfaces the latest walk summary so
        the UI can render a freshness warning when no recent walk
        exists (rule-of-thumb: if last_walk is older than
        older_than_days, the prune preview is unreliable).
        """
        from server import media_db
        seconds = max(0.0, float(older_than_days) * 86400.0)
        count = media_db.count_stale_items(
            server_id=server_id, older_than_seconds=seconds,
        )
        # Cap the sample at 100 so the preview payload stays light;
        # the actual prune call is unbounded.
        sample = media_db.list_stale_items(
            server_id=server_id, older_than_seconds=seconds, limit=100,
        )
        return {
            "count": count,
            "sample_items": sample,
            "sample_truncated_at": 100,
            "last_walk": media_db.get_last_walk_summary(server_id),
            "older_than_days": older_than_days,
        }

    @app.post("/api/servers/{server_id}/prune-stale-items")
    def prune_stale_items(
        server_id: str,
        body: Dict[str, Any],
        _admin: Dict[str, Any] = Depends(_auth_router_module.require_role("admin")),
    ) -> Dict[str, Any]:
        """
        Execute the Prune Missing Items action. Two-factor gated -
        destructive across watch_events, ratings, server_items,
        playlist / collection memberships, and orphan items rows.
        Snapshot .db files are never touched (Rule 3).

        M9: requires an admin/root_admin JWT (the ``require_role``
        dependency) AND the separate db_admin credential in the body -
        a stolen login session alone cannot trigger a destructive DB
        op.

        Body: ``{db_admin_username, db_admin_password, older_than_days,
        dry_run}``. ``dry_run=true`` returns counters without writing
        (cheap path for a second-confirm step in the UI).
        """
        from server import auth_db, media_db
        un = str(body.get("db_admin_username") or "")
        pw = str(body.get("db_admin_password") or "")
        if not un or not pw:
            raise HTTPException(
                status_code=401, detail="Database admin credentials are required.",
            )
        verified = auth_db.verify_password(un, pw)
        if verified is None or verified.get("role") != "db_admin":
            raise HTTPException(
                status_code=401, detail="Database admin credentials are invalid.",
            )
        try:
            older_than_days = float(body.get("older_than_days") or 7.0)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="older_than_days must be a number.")
        if older_than_days < 0:
            raise HTTPException(status_code=400, detail="older_than_days must be >= 0.")
        dry_run = bool(body.get("dry_run") or False)
        seconds = older_than_days * 86400.0
        try:
            counters = media_db.prune_stale_items(
                server_id=server_id,
                older_than_seconds=seconds,
                dry_run=dry_run,
            )
        except ValueError as e:
            # H2 walk-gate: no completed library walk for this server.
            raise HTTPException(status_code=409, detail=str(e))
        counters["dry_run"] = dry_run
        counters["older_than_days"] = older_than_days
        return counters

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
        # v0.9.5: refuse Windows host paths early so the operator gets
        # an actionable error in the Settings tab instead of a silent
        # write into the container's ephemeral filesystem.
        try:
            if "output_dir" in patch:
                persistence.validate_container_path(patch["output_dir"], "Output directory")
            if "log_dir" in patch:
                persistence.validate_container_path(patch["log_dir"], "Log directory")
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
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
        Register a new server.

        v0.10.0 - :func:`server_registry.add_server` now probes the
        URL+token internally and refuses to persist a row if either
        (a) the probe fails (server unreachable, token rejected), or
        (b) the probed ``machine_identifier`` is already registered
        under a different friendly name. The same Plex account token
        works on every server that account owns, so the identifier is
        the only reliable signal that the operator typed the right
        URL for the server they meant.

        PR-11: also kicks off a managed-users sync for the newly
        registered server so the User Management panel and the
        JobFormPanel picker (which now reads from the DB) are warm
        immediately. Failure is non-fatal - the server is still
        registered and the operator can run a manual sync later.
        """
        try:
            row = server_registry.add_server(body.name, body.url, body.token, logger=log)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        _auto_sync_managed_users(row["id"])
        # ``add_server`` already populated the probe results onto the row,
        # so a second test_connection call would just round-trip Plex
        # again - return what we have.
        latest = server_registry.get_server_by_id(row["id"], include_token=False)
        return latest or row

    @app.post("/api/servers/test-unsaved")
    def test_unsaved_server(body: TestUnsavedIn) -> Dict[str, Any]:
        """
        Probe a URL+token combination without registering it (v0.10.0).

        Returns:
          * ``ok``                - true if Plex accepted the token and
            we successfully enumerated libraries.
          * ``status`` / ``detail`` - same vocabulary the registry rows
            use (``ok`` / ``unreachable`` / ``auth_error``).
          * ``friendly_name``     - the server's own friendly name.
          * ``machine_identifier`` - Plex's stable install ID.
          * ``owner_name``        - Plex.tv username the token belongs to.
          * ``libraries``         - current catalogue with item counts.
          * ``response_ms``       - round-trip + library enumeration time.
          * ``name_mismatch``     - true when the operator's typed
            friendly name doesn't match what the server reports for
            itself. Surfaced so the UI can show a warning before save.
          * ``duplicate_of``      - friendly name of an existing
            registered server that shares this server's
            ``machine_identifier``. Null when there's no collision.
        """
        probe = server_registry.probe_unsaved(body.url, body.token, log)
        # Mismatch detection: only meaningful when the probe actually
        # connected and surfaced a friendly name. We compare
        # case-insensitively because Plex sometimes title-cases
        # friendly names on its own.
        probed_friendly = (probe.get("friendly_name") or "").strip()
        typed = (body.name or "").strip()
        name_mismatch = bool(
            probe.get("ok") and probed_friendly and typed
            and probed_friendly.lower() != typed.lower()
        )
        # Duplicate-identifier preview.
        duplicate_of: Optional[str] = None
        machine_id = (probe.get("machine_identifier") or "").strip()
        if probe.get("ok") and machine_id:
            for row in server_registry.list_servers():
                if (row.get("machine_identifier") or "") == machine_id:
                    duplicate_of = row.get("name") or ""
                    break
        return {
            **probe,
            "name_mismatch": name_mismatch,
            "duplicate_of": duplicate_of,
        }

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
        empty ``token`` means "leave the saved token unchanged" - the
        same write-only-token pattern Settings uses.

        PR-11: re-credentialling a server can flip its identity (new
        token + new owner = different managed-user set), so we also
        re-sync managed users after the probe completes.
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
        _auto_sync_managed_users(server_id)
        return server_registry.get_server_by_id(server_id, include_token=False) or row

    @app.get("/api/servers/{server_id}/cascade-preview")
    def preview_server_cascade(server_id: str) -> Dict[str, Any]:
        """
        Count what a cascading delete of this server would remove
        without actually deleting anything. Used by the Servers tab
        to populate the confirmation dialog with concrete numbers.
        """
        preview = server_registry.cascade_preview(server_id)
        if preview is None:
            raise HTTPException(status_code=404, detail=f"No server with id {server_id!r}")
        return preview

    @app.delete("/api/servers/{server_id}")
    def delete_one_server(server_id: str) -> Dict[str, Any]:
        """
        Remove a registered server *and cascade-delete* everything
        attributable to it (v0.9.5):

          * Schedules whose ``source_server_name`` matches.
          * ``snapshots/*_<slug>_<ts>.plexbackup.json`` files.
          * ``plex_logs/run_<slug>_*`` directories.

        Returns a summary dict with per-category counts and a list of
        per-file errors that the cascade encountered. Best-effort -
        a failure on one file does not stop the rest of the sweep.
        """
        summary = server_registry.remove_server(server_id)
        if summary is None:
            raise HTTPException(status_code=404, detail=f"No server with id {server_id!r}")
        return summary

    @app.post("/api/servers/{server_id}/test")
    def test_one_server(server_id: str) -> Dict[str, Any]:
        """
        Re-probe a server's connection. The cached status, libraries,
        and last-contacted timestamp on the registry row are all
        refreshed. Used by the Test button in the Servers tab.

        PR-11: re-test is the "reconnected" trigger from the spec, so
        we also re-sync managed users best-effort. Failure is logged
        but doesn't break the test response - status info is the
        primary contract of this endpoint.
        """
        try:
            out = server_registry.test_connection(server_id, log)
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e))
        _auto_sync_managed_users(server_id)
        return out

    @app.post("/api/servers/{server_id}/ping")
    def ping_one_server(server_id: str) -> Dict[str, Any]:
        """
        Lightweight reachability probe (v0.9.1). Issues a single GET
        ``/identity`` against the registered URL with the saved token
        and returns ``{ok, response_ms, status, detail}`` - without
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

    @app.get("/api/servers/{server_id}/users")
    def list_server_users(server_id: str) -> Dict[str, Any]:
        """
        Return the user list for one server (v0.9.6 Feature 3).

        Owner + every managed user surfaces here, each carrying the
        operator's chosen display name from the server's
        ``user_display_names`` map (or empty if none). 404 if the
        server is unregistered; 502 if Plex is unreachable. A
        successful response with a non-null ``error`` field means
        ``systemAccounts()`` failed but the owner row is still
        present - the UI renders "No managed users found".
        """
        try:
            return server_registry.get_server_users(server_id, log)
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except ConnectionError as e:
            raise HTTPException(status_code=502, detail=str(e))

    @app.patch("/api/servers/{server_id}/user-display-name")
    def patch_user_display_name(
        server_id: str, body: UserDisplayNameIn,
    ) -> Dict[str, Any]:
        """
        Set or clear one entry in a server's ``user_display_names``
        map (v0.9.6 Feature 3). An empty ``display_name`` clears the
        mapping. Returns the updated server row (with token redacted).
        """
        try:
            updated = server_registry.set_user_display_name(
                server_id, body.plex_id, body.display_name,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        if updated is None:
            raise HTTPException(status_code=404, detail=f"No server with id {server_id!r}")
        return updated

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
        snap = build_dashboard_frame()
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
        server restart by design - this is a live operations view,
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

    @app.post("/api/job/snapshot")
    def post_job_export(body: SnapshotJobIn) -> Dict[str, Any]:
        """
        Enqueue an snapshot job. Returns the JobRecord immediately;
        progress is reported live via the WebSocket.
        """
        params = body.model_dump(exclude_none=True)
        # v0.9.5: reject Windows host paths early so a misconfigured
        # ad-hoc snapshot per-run output_dir doesn't silently land
        # inside the container's ephemeral filesystem.
        try:
            if "output_dir" in params:
                persistence.validate_container_path(params["output_dir"], "Output directory")
            if "log_dir" in params:
                persistence.validate_container_path(params["log_dir"], "Log directory")
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        # Tag the run as user-initiated so the snapshot JSON (and the
        # Snapshots tab in the GUI) can distinguish it from scheduler
        # fires.
        params["_trigger"] = "manual"
        rec = get_queue().submit_snapshot(params)
        return {"job_id": rec.job_id, "state": rec.state, "mode": rec.mode}

    @app.post("/api/job/restore")
    def post_job_restore(body: RestoreJobIn) -> Dict[str, Any]:
        """
        Enqueue a restore job. Mirrors :func:`post_job_snapshot`.
        """
        params = body.model_dump(exclude_none=True)
        # M10: containment-check every user-supplied path - each import
        # file and the per-run log dir - so a restore job can't be
        # aimed at server_data/ or escape via '..'.
        try:
            for _f in params.get("input_files") or []:
                persistence.validate_container_path(_f, "Import file")
            if "log_dir" in params:
                persistence.validate_container_path(params["log_dir"], "Log directory")
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        rec = get_queue().submit_restore(params)
        return {"job_id": rec.job_id, "state": rec.state, "mode": rec.mode}

    @app.post("/api/job/restore-from-snapshot")
    def post_job_restore_from_snapshot(body: RestoreFromSnapshotIn) -> Dict[str, Any]:
        """
        Enqueue a restore that pulls from a registered snapshot in
        ``snapshots.db`` rather than from an on-disk ``.plexexport.json``.

        Implementation: materialise (or reuse cached) JSON sidecar for
        the snapshot, then forward into the standard restore queue with
        the sidecar path filled into ``input_files``. The engine still
        consumes JSON; we just resolve the path here so the operator
        never has to type one in.
        """
        from server import snapshot_registry
        row = snapshot_registry.get(body.snapshot_id)
        if row is None:
            raise HTTPException(
                status_code=404,
                detail=f"No snapshot with id {body.snapshot_id!r}.",
            )
        sidecar = snapshot_registry.materialise_sidecar(body.snapshot_id)
        if not sidecar:
            raise HTTPException(
                status_code=500,
                detail=(
                    "Could not render snapshot to JSON - the underlying "
                    ".db is missing or the render failed. Try downloading "
                    "the snapshot from the Backups tab to confirm."
                ),
            )
        params = body.model_dump(exclude_none=True)
        params.pop("snapshot_id", None)
        params["input_files"] = [sidecar]
        params["_imported_from_snapshot_id"] = body.snapshot_id
        rec = get_queue().submit_restore(params)
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
    def post_job_stop(hard: bool = False) -> Dict[str, Any]:
        """
        Ask the running job to wind down. Returns 409 if no job is
        currently running.

        Query params:
          * ``hard=true`` (v0.12.1) - force the engine off by tearing
            down the shared HTTP session in addition to setting the
            stop flag. Use when the soft Stop has been pending too
            long and the operator just wants the worker free.
        """
        ok = get_queue().request_stop(hard=hard)
        if not ok:
            raise HTTPException(status_code=409, detail="No job is currently running.")
        return {"stop_requested": True, "hard": bool(hard)}

    # ── Schedules ────────────────────────────────────────────────────

    @app.get("/api/schedules")
    def get_schedules() -> List[Dict[str, Any]]:
        return list_schedules()

    @app.post("/api/schedules")
    def post_schedule(body: ScheduleIn) -> Dict[str, Any]:
        """
        Create a new schedule. ``id`` will be assigned by the server
        - clients should omit it on create.
        """
        doc = body.model_dump()
        doc.pop("id", None)
        # v0.9.5: per-schedule output_dir overrides settings; reject a
        # Windows host path here too so a misconfigured schedule
        # doesn't fire silently into the container's ephemeral fs.
        try:
            if doc.get("output_dir"):
                persistence.validate_container_path(doc["output_dir"], "Schedule output directory")
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        ensure_next_run_at(doc)
        return persistence.upsert_schedule(doc)

    @app.put("/api/schedules/{schedule_id}")
    def put_schedule(schedule_id: str, body: ScheduleIn) -> Dict[str, Any]:
        """
        Replace an existing schedule. The path parameter is the
        authoritative id - body ``id`` is overwritten if it disagrees.
        """
        doc = body.model_dump()
        doc["id"] = schedule_id
        try:
            if doc.get("output_dir"):
                persistence.validate_container_path(doc["output_dir"], "Schedule output directory")
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
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

    @app.get("/api/logs/{run_name}/{file_name}/download")
    def download_log_file(run_name: str, file_name: str) -> FileResponse:
        """
        Stream a log file as a regular HTTP attachment. Bypasses the
        in-browser viewer's 16 MB live-tail cap so an operator can
        grab the full content of a huge log on demand. Path traversal
        is rejected by ``log_browser.resolve_file_path``.
        """
        try:
            path = log_browser.resolve_file_path(run_name, file_name)
        except FileNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return FileResponse(
            path=str(path),
            media_type="text/plain",
            filename=path.name,
        )

    # NOTE: this route is intentionally declared BEFORE the
    # ``/api/logs/{run_name}/{file_name}`` reader below so FastAPI's
    # in-order matching catches the literal ``zip`` suffix first. A
    # log file literally named ``zip`` would otherwise be unreachable
    # via the zip endpoint; in practice the engine never produces
    # one, but order-of-declaration is the cheap insurance.
    @app.get("/api/logs/{run_name}/zip")
    def download_log_run_zip(run_name: str) -> FileResponse:
        """
        Bundle every file in one run directory into a ZIP and stream
        it back as an attachment. The temp zip on disk is removed via
        a BackgroundTask once the response finishes (success or not).
        """
        try:
            zip_path, filename = log_browser.build_run_zip(run_name)
        except FileNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

        def _cleanup(p: str) -> None:
            try:
                os.unlink(p)
            except OSError:
                pass

        return FileResponse(
            path=str(zip_path),
            media_type="application/zip",
            filename=filename,
            background=BackgroundTask(_cleanup, str(zip_path)),
        )

    @app.get("/api/logs/{run_name}/{file_name}")
    def read_log_file(run_name: str, file_name: str, since: int = 0) -> Dict[str, Any]:
        """
        Read a log file's contents. Pass ``?since=<byte-offset>`` to
        fetch only the bytes appended since the previous read - used
        by the frontend's live-tail poll. Omit (or ``since=0``) for
        a full read.
        """
        try:
            return log_browser.read_file(run_name, file_name, since=since)
        except FileNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @app.delete("/api/logs/{run_name}")
    def delete_log_run(run_name: str) -> Dict[str, Any]:
        """
        Permanently remove one run directory. Returns the number of
        files removed and a list of best-effort errors (e.g. a file
        locked by a live engine job).
        """
        try:
            file_count, errors = log_browser.delete_run(run_name)
        except FileNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"deleted": run_name, "file_count": file_count, "errors": errors}

    @app.delete("/api/logs")
    def delete_all_log_runs() -> Dict[str, Any]:
        """
        Wipe every run directory under the configured log dir. Returns
        ``{deleted, errors}``. Per-directory errors do not abort the
        sweep; the operator can retry to mop up anything that was
        locked at the time.
        """
        deleted, errors = log_browser.delete_all_runs()
        return {"deleted": deleted, "errors": errors}

    # ── Snapshot registry (PR-13) ──────────────────────────────────────
    #
    # GET  /api/snapshots                       - list registry rows
    # GET  /api/snapshots/{id}/download         - stream JSON for one
    # DELETE /api/snapshots/{id}                - delete one (db_admin)
    # DELETE /api/snapshots/server/{server_id}  - clear all on server (db_admin)
    # GET  /api/snapshots/legacy                - list pre-rename .plexbackup.json files
    # GET  /api/snapshots/legacy/{name}/download - stream a legacy JSON as-is
    # DELETE /api/snapshots/legacy/{name}       - delete one legacy file (db_admin)

    @app.get("/api/snapshots")
    def list_snapshot_registry() -> Dict[str, Any]:
        """
        Return every registered snapshot, newest first. Rows are
        registry metadata only - no payload data.
        """
        from server import snapshot_registry
        return {"snapshots": snapshot_registry.list_snapshots()}

    @app.get("/api/snapshots/legacy")
    def list_legacy_snapshots() -> List[Dict[str, Any]]:
        """
        List pre-rename ``.plexbackup.json`` files left over from the
        old ``plex_exports/`` era and now under ``snapshots/legacy/``.
        These are read-only artefacts; downloads stream the file as-is.
        """
        return snapshot_browser.list_snapshots()

    @app.get("/api/snapshots/legacy/{file_name}/download")
    def download_legacy_snapshot(file_name: str) -> FileResponse:
        """Stream a legacy ``.plexbackup.json`` as-is."""
        try:
            path = snapshot_browser.snapshot_path(file_name)
        except FileNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return FileResponse(
            path=str(path),
            media_type="application/json",
            filename=path.name,
        )

    @app.delete("/api/snapshots/legacy")
    def delete_all_legacy_archives(
        body: Dict[str, Any],
        _admin: Dict[str, Any] = Depends(_auth_router_module.require_role("admin")),
    ) -> Dict[str, Any]:
        """
        Wipe every ``.plexbackup.json`` archive in
        ``<output_dir>/legacy/``. Two-factor gated (M9: admin JWT +
        db_admin body credential). Returns ``{deleted, errors}``.

        Non-archive files in the same directory (anything not ending
        in ``.plexbackup.json``) are left alone - the operator may
        have dropped unrelated content there.
        """
        from server import auth_db
        un = str(body.get("db_admin_username") or "")
        pw = str(body.get("db_admin_password") or "")
        if not un or not pw:
            raise HTTPException(status_code=401, detail="Database admin credentials are required.")
        verified = auth_db.verify_password(un, pw)
        if verified is None or verified.get("role") != "db_admin":
            raise HTTPException(status_code=401, detail="Database admin credentials are invalid.")
        return snapshot_browser.delete_all_archives()

    @app.delete("/api/snapshots/legacy/{file_name}")
    def delete_legacy_snapshot(
        file_name: str,
        body: Dict[str, Any],
        _admin: Dict[str, Any] = Depends(_auth_router_module.require_role("admin")),
    ) -> Dict[str, str]:
        """
        Delete one legacy ``.plexbackup.json`` file. Two-factor gated
        (M9): admin/root_admin JWT plus ``{db_admin_username,
        db_admin_password}`` in the body.
        """
        from server import auth_db
        un = str(body.get("db_admin_username") or "")
        pw = str(body.get("db_admin_password") or "")
        if not un or not pw:
            raise HTTPException(status_code=401, detail="Database admin credentials are required.")
        verified = auth_db.verify_password(un, pw)
        if verified is None or verified.get("role") != "db_admin":
            raise HTTPException(status_code=401, detail="Database admin credentials are invalid.")
        try:
            snapshot_browser.delete_snapshot(file_name)
        except FileNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"deleted": file_name}

    @app.get("/api/snapshots/{snapshot_id}/download-db")
    def download_snapshot_db(snapshot_id: str) -> FileResponse:
        """
        Stream the per-snapshot ``.db`` file directly. No rendering,
        no caching - the .db lives on disk as the canonical artifact
        and the download is a flat FileResponse over the binary.

        Companion to ``/download`` (which renders + streams JSON).
        Operators get both: ``.db`` for restore-into-another-install,
        ``.plexbackup.json`` for portable archive / inspection.

        Returns 404 if the snapshot id is unknown; 410 if the row
        exists but the .db file is missing on disk (orphan-row case,
        also surfaced via the ``available`` field on list rows);
        500 with the actual exception class in the detail on any
        other failure (so the UI can render something useful instead
        of a bare "Internal Server Error").
        """
        try:
            from server import snapshot_registry
            row = snapshot_registry.get(snapshot_id)
            if row is None:
                raise HTTPException(status_code=404, detail=f"No snapshot with id {snapshot_id!r}")
            db_path = Path(row.get("file_path") or "")
            if not db_path.is_file():
                raise HTTPException(
                    status_code=410,
                    detail=(
                        "Snapshot .db is missing on disk. The registry row "
                        "still exists but the file behind it cannot be "
                        "found - use Remove entry to clean it up."
                    ),
                )
            # Compose a friendly filename from the registry fields
            # rather than relying on snapshot_name. This works
            # uniformly for old rows (which carry the pre-rename
            # snapshot_name like "My-Server_20260513_022609") and new
            # rows (which already have the friendly name baked in).
            friendly = snapshot_registry.format_snapshot_filename(
                server_name=row.get("server_name") or "",
                libraries=row.get("libraries") or [],
                captured_at=row.get("captured_at") or 0,
            )
            return FileResponse(
                path=str(db_path),
                # SQLite has no standard IANA type; vnd.sqlite3 is the de
                # facto convention. Browsers will treat it as a download.
                media_type="application/vnd.sqlite3",
                filename=f"{friendly}.db",
            )
        except HTTPException:
            raise
        except Exception as exc:
            # Log via plexmigrate.server.jobs so the failure shows up in
            # the active run's runtime.log (Fix 2(b) whitelist). The
            # detail string ends up in the UI banner.
            logging.getLogger("plexmigrate.server.jobs").exception(
                "download_snapshot_db failed for %r", snapshot_id,
            )
            # Generic detail only: the exception text can carry
            # filesystem paths / Plex URLs. The full traceback is in
            # the run log above for the operator to inspect.
            raise HTTPException(
                status_code=500,
                detail=".db download failed - see the run log for details.",
            )

    @app.get("/api/snapshots/{snapshot_id}/download")
    def download_snapshot(snapshot_id: str):
        """
        Download a snapshot as ``.plexbackup.json``. First call
        materialises a sidecar next to the ``.db`` and stamps the
        registry row's ``prebuilt_json_path``; subsequent calls stream
        the cached file. Failure to write the sidecar (read-only FS,
        out of space) is non-fatal - the endpoint falls back to a
        live in-memory render so the operator still gets their file.

        Any other render failure (corrupt .db, schema mismatch, etc.)
        surfaces a 500 with the exception class+message in the detail
        so the UI banner is debuggable instead of a bare
        "Internal Server Error".
        """
        try:
            from server import snapshot_registry
            row = snapshot_registry.get(snapshot_id)
            if row is None:
                raise HTTPException(status_code=404, detail=f"No snapshot with id {snapshot_id!r}")

            # First try the cached sidecar. Either pre-existing or
            # just-materialised by this request. ``materialise_sidecar``
            # is idempotent and returns the existing path when one is
            # already on disk.
            friendly = snapshot_registry.format_snapshot_filename(
                server_name=row.get("server_name") or "",
                libraries=row.get("libraries") or [],
                captured_at=row.get("captured_at") or 0,
            )
            sidecar = snapshot_registry.materialise_sidecar(snapshot_id)
            if sidecar:
                p = Path(sidecar)
                if p.is_file():
                    return FileResponse(
                        path=str(p),
                        media_type="application/json",
                        filename=f"{friendly}.plexexport.json",
                    )

            # Sidecar materialisation failed but the operator still wants
            # their JSON. Render in memory and stream it without caching.
            db_path = Path(row.get("file_path") or "")
            if not db_path.is_file():
                raise HTTPException(
                    status_code=410,
                    detail=(
                        "Snapshot file is missing on disk. The registry row "
                        "still exists but the .db file behind it cannot be "
                        "found - it may have been deleted out-of-band."
                    ),
                )
            from server import snapshot_serializer
            payload = snapshot_serializer.build_payload_from_db(
                db_path,
                server_name=row.get("server_name") or "",
                server_id=row.get("server_id") or "",
                libraries=row.get("libraries") or [],
                captured_at_ts=row.get("captured_at"),
            )
            return JSONResponse(
                content=payload,
                headers={
                    "Content-Disposition": (
                        f'attachment; filename="{friendly}.plexexport.json"'
                    ),
                },
            )
        except HTTPException:
            raise
        except Exception as exc:
            logging.getLogger("plexmigrate.server.jobs").exception(
                "download_snapshot (JSON) failed for %r", snapshot_id,
            )
            # Generic detail only - exception text can leak paths /
            # URLs. Full traceback is in the run log above.
            raise HTTPException(
                status_code=500,
                detail="JSON render failed - see the run log for details.",
            )

    @app.delete("/api/snapshots/{snapshot_id}")
    def delete_snapshot_row(
        snapshot_id: str,
        body: Dict[str, Any],
        _admin: Dict[str, Any] = Depends(_auth_router_module.require_role("admin")),
    ) -> Dict[str, Any]:
        """
        Delete one snapshot: registry row + the per-snapshot ``.db``
        file. By default the cached JSON sidecar is removed too;
        when the body carries ``keep_json: true`` the sidecar is
        materialised (if needed) and moved into the JSON-archive
        directory instead, so it survives the snapshot deletion and
        appears in the Backups panel's "JSON Archives" section.

        Two-factor gated (M9): admin/root_admin JWT plus
        ``{db_admin_username, db_admin_password}`` in the body, plus
        the optional ``keep_json`` flag.
        """
        from server import auth_db, snapshot_registry
        un = str(body.get("db_admin_username") or "")
        pw = str(body.get("db_admin_password") or "")
        keep_json = bool(body.get("keep_json") or False)
        if not un or not pw:
            raise HTTPException(status_code=401, detail="Database admin credentials are required.")
        verified = auth_db.verify_password(un, pw)
        if verified is None or verified.get("role") != "db_admin":
            raise HTTPException(status_code=401, detail="Database admin credentials are invalid.")
        try:
            return snapshot_registry.delete(snapshot_id, keep_json=keep_json)
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e))

    @app.delete("/api/snapshots/server/{server_id}")
    def delete_all_snapshots_for_server(
        server_id: str,
        body: Dict[str, Any],
        _admin: Dict[str, Any] = Depends(_auth_router_module.require_role("admin")),
    ) -> Dict[str, Any]:
        """
        Wipe every snapshot for one server: each registry row + each
        ``.db`` file + each pre-built JSON sidecar. Two-factor gated
        (M9): admin/root_admin JWT + db_admin body credential.
        """
        from server import auth_db, snapshot_registry
        un = str(body.get("db_admin_username") or "")
        pw = str(body.get("db_admin_password") or "")
        if not un or not pw:
            raise HTTPException(status_code=401, detail="Database admin credentials are required.")
        verified = auth_db.verify_password(un, pw)
        if verified is None or verified.get("role") != "db_admin":
            raise HTTPException(status_code=401, detail="Database admin credentials are invalid.")
        return snapshot_registry.delete_all_for_server(server_id)

    # ── WebSocket ────────────────────────────────────────────────────

    @app.websocket("/ws/dashboard")
    async def ws_dashboard(ws: WebSocket) -> None:
        """
        Bidirectional socket. The server pushes a JSON snapshot every
        250 ms; the client may send back ping frames (we ignore the
        content, but the read keeps the connection alive on browsers
        that idle-close after 60 seconds without traffic).

        PR-A2 - auth is always on. The token is read from the
        ``?token=<jwt>`` query parameter (browsers can't set custom
        headers on a ``new WebSocket()`` call). An absent or invalid
        token closes the socket with code 4001 before the manager
        ever sees it - the dashboard payload contains run-status data
        which we treat as confidential.
        """
        token = (ws.query_params.get("token") or "").strip()
        payload = _auth_router_module.decode_token(token)
        if not payload:
            # Custom close codes in the 4xxx range are reserved for
            # application use. 4001 = "auth required / failed."
            await ws.close(code=4001)
            return

        manager = get_manager()
        await manager.connect(ws)
        try:
            while True:
                # We never act on inbound messages - just keep the
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
