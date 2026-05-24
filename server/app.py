"""
FastAPI application - entry point for the Hestia-MediaManager server.

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

Phase 3a decomposition (2026-05-21): the 130-plus REST handlers that
previously lived inside ``_register_routes`` now live one-per-group
under ``server/routers/``. The lifecycle hooks moved to
``server.lifecycle``. The shared route helpers (``_apply_preflight_ack``,
``_auto_sync_managed_users``) moved to ``server.routers._deps``. The
two boot-time helpers (``_archive_old_media_db_if_needed``,
``_playlist_copy_job_to_dict`` etc.) that pre-existing tests + the
lifecycle module import directly stay in this module so the public
import surface is preserved.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from server import (
    SERVER_API_VERSION,
    auth_router as _auth_router_module,
    managed_users_router as _managed_users_router_module,
    dev_console_router as _dev_console_router_module,
    runtime_patches,
    server_registry,
)
from server.dev_console_ws import get_dev_console_manager
from server.ws import get_manager


# Apply headless-mode patches as early as possible. The job worker
# also calls this, but doing it at import time avoids any race with
# direct calls into the engine from REST handlers (e.g. /api/libraries).
runtime_patches.enable_headless_mode()


log = logging.getLogger("plexmigrate.server")


# ── media.db boot-time auto-archive (v0.15) ─────────────────────────────────
#
# The v0.15 schema break introduced ``library_sections`` as a NOT NULL
# anchor referenced by every per-server row. Older media.db files cannot
# be migrated in place because the original library identity is gone -
# the rows were written without a section_key column, so there is no
# safe value to backfill with. The right behaviour is to retire the old
# database and let the next snapshot run rebuild it from live Plex
# data; existing snapshot .db files are not affected (they remain
# readable until the end user chooses to re-capture).
#
# This function runs ONCE per process at boot, BEFORE
# ``media_db.init_media_db()``. It is deliberately a top-level module
# function (not nested inside ``create_app``) so the boot sequence is
# easy to read in stack traces and so end users inspecting startup can
# see the archive step by name.


def _archive_old_media_db_if_needed(logger: logging.Logger) -> None:
    """
    Inspect the on-disk ``media.db`` file. If its highest applied
    schema_version is below :data:`server.media_db.CURRENT_SCHEMA_VERSION`,
    rename the file (and any ``-wal`` / ``-shm`` sidecars) to a
    timestamped ``.pre-vN.bak`` so the subsequent ``init_media_db()``
    call builds a fresh database.

    No-op when:
      * ``media.db`` does not exist (first-ever boot).
      * The file's schema_version is already at or above the
        current build's requirement.
      * The file is unreadable as SQLite (corruption) - we log
        and leave it alone so the end user can recover manually.
    """
    import sqlite3
    import time

    # Lazy import: we MUST NOT call ``init_media_db`` from here, but
    # the path helper + version constant are safe to read.
    from server import media_db

    db_path = media_db._db_path()
    if not db_path.is_file():
        logger.info(
            "media.db not present at %s; will be created fresh on init.",
            db_path,
        )
        return

    # Read-only probe via URI form so an interrupted previous boot's
    # journal can't be auto-played here.
    try:
        probe = sqlite3.connect(
            f"file:{db_path}?mode=ro", uri=True, timeout=5.0,
        )
    except sqlite3.OperationalError:
        logger.warning(
            "media.db at %s exists but cannot be opened read-only for "
            "the schema-version probe; leaving in place. init_media_db "
            "will raise loudly if the file is incompatible.",
            db_path,
        )
        return

    try:
        try:
            row = probe.execute(
                "SELECT MAX(version) AS v FROM schema_version"
            ).fetchone()
            observed = int(row[0] or 0) if row else 0
        except sqlite3.OperationalError:
            # No schema_version table - the file is pre-v0.12.0 (which
            # introduced media.db) or otherwise unidentifiable. Treat
            # as version 0 and archive.
            observed = 0
    finally:
        try:
            probe.close()
        except Exception:
            pass

    required = int(media_db.CURRENT_SCHEMA_VERSION)
    if observed >= required:
        logger.info(
            "media.db schema_version=%d is current (required >=%d); no archive needed.",
            observed, required,
        )
        return

    # Compose the archive suffix. Timestamp guards against multiple
    # archives in the same upgrade cycle (e.g. end user restarts the
    # container mid-upgrade).
    ts = time.strftime("%Y%m%d-%H%M%S")
    archive_suffix = f".pre-v{required}-{ts}.bak"
    archive_path = db_path.with_suffix(db_path.suffix + archive_suffix)
    wal_path = db_path.with_name(db_path.name + "-wal")
    shm_path = db_path.with_name(db_path.name + "-shm")

    # Rename the main file first. If that fails, abort the archive
    # entirely - half-renamed sidecars without the main DB would leave
    # init_media_db confused. The rename is atomic on POSIX; on Windows
    # a target-exists check is required, but our timestamped suffix
    # makes a collision improbable.
    try:
        if archive_path.exists():
            # Should not happen given the timestamp, but be defensive.
            raise FileExistsError(
                f"archive target already exists: {archive_path}"
            )
        db_path.rename(archive_path)
    except OSError as exc:
        logger.error(
            "Could not archive outdated media.db (%s, schema_version=%d, "
            "required>=%d): %s. init_media_db will run against the "
            "existing file and likely raise.",
            db_path, observed, required, exc,
        )
        return

    # Best-effort sidecar moves: WAL / SHM are coordination files for
    # the active connection only; if the move fails they will be
    # recreated by the next open. Don't fail the boot for these.
    for sidecar in (wal_path, shm_path):
        if not sidecar.exists():
            continue
        try:
            sidecar.rename(
                sidecar.with_name(sidecar.name + archive_suffix)
            )
        except OSError:
            try:
                sidecar.unlink()
            except OSError:
                pass

    logger.warning(
        "Archived outdated media.db (schema_version=%d, required>=%d) to %s. "
        "A fresh database will be initialised; existing snapshot .db files "
        "are unaffected and will continue to function until re-captured.",
        observed, required, archive_path,
    )


# ── Re-exports kept for external import-compatibility ─────────────────────────
#
# Pre-decomp these names were defined locally inside ``server/app.py``.
# Phase-3a moves the implementations to dedicated modules, but the
# names stay reachable here so any caller (tests, future helpers,
# the ``server.app._foo`` patterns the planning docs reference)
# still resolves the same symbol.
from server.routers._deps import (  # noqa: E402,F401
    _apply_preflight_ack,
    _auto_sync_managed_users,
)


# ── Auth middleware ──────────────────────────────────────────────────────────

# Paths that bypass JWT auth entirely. Anything else requires a valid
# bearer token. WebSocket auth lives in the route handler itself
# (Starlette doesn't route websocket frames through HTTP middleware),
# so ``/ws/`` is also exempt here.
#
# This list is the minimum bootstrap surface the unauthenticated
# browser needs to render the login / setup screens plus the public
# health probe. The ``/admin/*`` and ``/login-account/*`` endpoints
# are deliberately NOT here - they require a root_admin JWT, gated
# inside the handlers via the ``require_role`` dependency.
_AUTH_PUBLIC_PREFIXES = (
    "/api/health",
    "/api/auth/status",
    "/api/auth/setup",
    # /api/auth/setup-v2: the two-step first-boot wizard the frontend's
    # SetupPage actually calls. The prefix matcher uses
    # ``path == p or path.startswith(p + "/")`` so "/api/auth/setup"
    # does NOT cover "/api/auth/setup-v2"; without this explicit entry
    # every fresh install returns 401 on first-boot setup. The handler
    # itself self-locks on has_any_users(), so exposing the path
    # doesn't open a second-setup escape hatch.
    "/api/auth/setup-v2",
    "/api/auth/login",
    "/api/auth/logout",
    # /api/auth/refresh is reachable without a bearer JWT - the
    # refresh-token cookie is the sole credential. The handler itself
    # validates and 401s if the cookie is missing or invalid.
    "/api/auth/refresh",
    "/ws",
    # /docs, /openapi.json and /redoc are intentionally NOT public:
    # the API schema requires a valid JWT like every other route, so
    # the endpoint surface can't be enumerated unauthenticated.
)


class _AuthMiddleware(BaseHTTPMiddleware):
    """
    Validate ``Authorization: Bearer <jwt>`` on every request that
    isn't on the public-prefix list.

    Auth is always on: every non-public request must carry a valid
    JWT or it's rejected with 401. There is no enable/disable toggle.
    """

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        # Exact-match or true path-segment prefix only. A bare
        # ``startswith(p)`` would treat ``/api/healthcheck-internal``
        # or ``/openapi.json.bak`` as public and bypass JWT auth.
        if any(path == p or path.startswith(p + "/")
               for p in _AUTH_PUBLIC_PREFIXES):
            return await call_next(request)

        # Bearer token comes from ``Authorization: Bearer X`` only.
        # There is deliberately no ``?access_token=X`` query-param
        # fallback - query strings are logged by uvicorn, reverse
        # proxies, and browser history, and the log scrubber doesn't
        # redact them, so a JWT in the URL would be a credential leak.
        # The WebSocket ``?token=`` is unavoidable (browsers can't set
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


# ── App factory ──────────────────────────────────────────────────────────────


def create_app() -> FastAPI:
    """
    Build and return the FastAPI app. The module-level ``app`` is the
    canonical instance; this factory exists so tests can build their
    own isolated copy.
    """
    app = FastAPI(
        title="Hestia-MediaManager Server",
        version=SERVER_API_VERSION,
        description="Web layer wrapping the Hestia-MediaManager engine.",
    )

    @app.exception_handler(HTTPException)
    async def _scrubbed_http_exception_handler(
        request: Request, exc: HTTPException,
    ):
        """Redact credential-bearing text from an HTTPException detail
        before it reaches the client. plexapi / requests exceptions
        embed the tokened Plex URL in their string form, so a handler
        raising ``HTTPException(detail=str(exc))`` would otherwise leak
        the token in the JSON error body. Companion to the logging
        scrubber, which only covers text routed through a log handler."""
        from fastapi.exception_handlers import http_exception_handler
        from server.log_scrubber import scrub
        if isinstance(exc.detail, str):
            exc.detail = scrub(exc.detail)
        return await http_exception_handler(request, exc)

    # In Docker the frontend is served by nginx and proxies /api and
    # /ws to this backend, so same-origin browser requests never hit
    # CORS preflight. For ``make cli`` users who run the backend
    # directly and a Vite dev server on another port, the default
    # allowlist below covers the standard localhost dev/prod origins
    # (backend 8000, nginx 8080, Vite 5173). This server holds a Plex
    # token and is meant to be reachable from localhost only, so a
    # wildcard default is the wrong posture.
    #
    # Tighten or widen via the PLEXMIGRATE_CORS_ORIGINS env
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

    # Managed-users management API. End user+ JWT for reads,
    # db_admin gate on writes (separate credential pair re-validated
    # per request).
    app.include_router(_managed_users_router_module.router)

    # Server Commands developer console. Every route is root_admin
    # gated AND 404s when the ``dev_console_enabled`` tunable is off.
    app.include_router(_dev_console_router_module.router)

    # Phase-3a router modules. Each carries its full paths verbatim
    # so the include order doesn't matter for path resolution; FastAPI
    # walks routes in registration order for collisions but no two
    # router modules export overlapping paths.
    from server.routers import (
        database as _database_router,
        jobs as _jobs_router,
        library_mapping as _library_mapping_router,
        logs as _logs_router,
        misc as _misc_router,
        playlist_mgmt as _playlist_mgmt_router,
        server_mirror as _server_mirror_router,
        servers as _servers_router,
        snapshots as _snapshots_router,
        sync as _sync_router,
    )
    app.include_router(_misc_router.router)
    app.include_router(_servers_router.router)
    app.include_router(_jobs_router.router)
    app.include_router(_playlist_mgmt_router.router)
    app.include_router(_snapshots_router.router)
    app.include_router(_logs_router.router)
    app.include_router(_database_router.router)
    app.include_router(_server_mirror_router.router)
    app.include_router(_library_mapping_router.router)
    app.include_router(_sync_router.router)

    _register_websocket_routes(app)
    _register_lifecycle(app)
    return app


# ── Lifecycle wiring ─────────────────────────────────────────────────────────


def _register_lifecycle(app: FastAPI) -> None:
    """
    Start / stop the scheduler thread and the WebSocket broadcaster
    in lockstep with the ASGI server lifecycle.

    Uses ``app.on_event`` (decorator form) for parity with the
    pre-3a registration. ``add_event_handler`` was removed from
    Starlette in the 0.40-series so the imperative spelling no
    longer works; ``on_event`` is the only surface that still
    accepts plain handler functions without forcing a lifespan
    rewrite (the planning doc explicitly defers the lifespan
    migration to a future change).
    """
    from server.lifecycle import on_startup, on_shutdown
    app.on_event("startup")(on_startup)
    app.on_event("shutdown")(on_shutdown)


# ── WebSocket endpoints ──────────────────────────────────────────────────────
#
# Kept here (not in a router module) because ``@app.websocket(...)``
# binds directly to the FastAPI instance and the planning doc for the
# Phase-3a split explicitly carved them out alongside ``_AuthMiddleware``.


def _register_websocket_routes(app: FastAPI) -> None:

    @app.websocket("/ws/dashboard")
    async def ws_dashboard(ws: WebSocket) -> None:
        """
        Bidirectional socket. The server pushes a JSON snapshot every
        250 ms; the client may send back ping frames (we ignore the
        content, but the read keeps the connection alive on browsers
        that idle-close after 60 seconds without traffic).

        Auth is always on. The token is read from the
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

    @app.websocket("/ws/dev-console")
    async def ws_dev_console(ws: WebSocket) -> None:
        """Live channel for the Server Commands developer console.

        Triple-checked before the socket is accepted: a valid JWT
        (``?token=``), the live ``root_admin`` role, and the
        ``dev_console_enabled`` tunable. 4001 = auth failed,
        4003 = forbidden (non-root or tunable off) - the same shape
        the REST routes use, so the frontend can branch cleanly.
        """
        token = (ws.query_params.get("token") or "").strip()
        payload = _auth_router_module.decode_token(token)
        if not payload:
            await ws.close(code=4001)
            return
        # Re-read the live role so a demotion takes effect at once.
        from server import auth_db
        from services import tunables
        username = payload.get("sub") or ""
        user = auth_db.get_user(username) if username else None
        if not user or (user.get("role") or "") != "root_admin":
            await ws.close(code=4003)
            return
        if not tunables.dev_console_enabled():
            await ws.close(code=4003)
            return

        manager = get_dev_console_manager()
        await manager.connect(ws)
        try:
            while True:
                raw = await ws.receive_text()
                # The client tells us which server it is viewing via
                # ``{"watch": "<server_id>"}`` so the heartbeat can be
                # scoped to that panel. Any other inbound frame is a
                # keep-alive and ignored.
                try:
                    msg = json.loads(raw)
                    if isinstance(msg, dict) and "watch" in msg:
                        manager.set_watch(ws, str(msg.get("watch") or ""))
                except Exception:
                    pass
        except WebSocketDisconnect:
            pass
        finally:
            await manager.disconnect(ws)


# ── Module-level app instance ─────────────────────────────────────────────────
# uvicorn imports this as ``server.app:app``.
app = create_app()
