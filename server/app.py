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
    DbImportIn,
    DevTestRunIn,
    DirectTransferIn,
    EtaBackfillIn,
    EtaPredictIn,
    EtaFlushIn,
    EtaResetIn,
    UserCopyIn,
    UserIdentityMapIn,
    SnapshotJobIn,
    RestoreJobIn,
    RestoreFromSnapshotIn,
    JobStatusOut,
    PinPreflightIn,
    PinMigrationApplyIn,
    ScheduleIn,
    ServerIn,
    TestUnsavedIn,
    SettingsIn,
    UserDisplayNameIn,
    # ── Cross-platform preflight (Plan[CROSS-PLATFORM-PREFLIGHT] step 4) ──
    CrossPlatformPreflightJobIn,
    PreflightResponse,
    CrossPlatformPreflightReport,
    UserResolutionOut,
    UserRowCountsOut,
    DestUserOptionOut,
    LibraryTypeNoteOut,
    TombstoneNoteOut,
    ZeroRowSkipOut,
    InlineCreateUserIn,
    InlineCreateUserResponse,
    ScheduleResolutionsPatchIn,
    # ── Playlist Management (Plan[PLAYLIST-MANAGEMENT]-2026-05-16) ──
    PlaylistSpec as PlaylistSpecOut,
    PlaylistItem as PlaylistItemOut,
    PlaylistDetail,
    PlaylistCopyIn,
    PlaylistCopyResult,
    CacheStatus,
    PlaylistCacheRefreshIn,
    PlaylistCacheRefreshResult,
)
from server.schedules import ensure_next_run_at, get_scheduler, list_schedules
from server.ws import build_dashboard_frame, get_manager


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


def _playlist_copy_job_to_dict(rec: Any) -> Dict[str, Any]:
    """Serialize a playlist_copy JobRecord into the
    /api/playlist-mgmt/copy-jobs response shape. Kept module-level so
    both the list route and any future per-job lookup can share it.

    Excludes the engine's internal fields (run_log_dir is empty for
    these jobs by design; the worker doesn't write per-run logs).
    ``summary`` is the end user-visible payload — ``playlist_copy``
    carries the per-deploy result + ``params`` carries the original
    submission so the panel can render Clone-deploy without local
    memory."""
    summary = rec.summary or {}
    return {
        "job_id": rec.job_id,
        "state": rec.state,
        "queued_at": rec.queued_at,
        "started_at": rec.started_at,
        "finished_at": rec.finished_at,
        "error": rec.error,
        "result": summary.get("playlist_copy"),
        "params": summary.get("params") or rec.params or {},
    }


def _playlist_mgmt_role_from_kind(kind: Optional[str], service_type: str) -> str:
    """Map a ``managed_users.kind`` value to the end user-facing role
    string returned by ``/api/playlist-mgmt/users``. Mirrors the
    three-tier convention from
    ``services.restorer_adapter._normalise_dest_role`` (used by the
    live-API path before the 2026-05-16 cache-first rewrite):

      * Plex owner kind  → "owner"  (single-admin server model)
      * J/E  owner kind  → "admin"  (multi-admin server model)
      * any "managed" kind → "managed"
    """
    k = (kind or "").lower()
    if k == "owner":
        return "owner" if (service_type or "").lower() == "plex" else "admin"
    return "managed"


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

    # PR-10 - managed-users management API. End user+ JWT for reads,
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
    # /api/auth/setup-v2: the two-step first-boot wizard the frontend's
    # SetupPage actually calls. Was missing from this list until
    # developer caught it during the Databases viewer endpoint tests; the
    # prefix matcher uses ``path == p or path.startswith(p + "/")`` so
    # "/api/auth/setup" does NOT cover "/api/auth/setup-v2". Without
    # this entry, every fresh install returns 401 on first-boot setup.
    # The handler itself self-locks on has_any_users(), so exposing the
    # path doesn't open a second-setup escape hatch. /api/auth/upgrade-split
    # stays gated (it requires a root_admin JWT to demote the legacy
    # account; the bearer auth on that route is intentional).
    "/api/auth/setup-v2",
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

        # v0.13.x: stale-sidecar housekeeping. Generated
        # ``.plexexport.json`` sidecars live next to their snapshot
        # ``.db`` and are re-materialisable on demand, so we don't
        # need to hold them on disk after the download window. The
        # TTL is end user-tunable (default 5 min); ``0`` disables.
        # Sweep cadence is a fixed 60 s tick so a 5-minute TTL is
        # honoured within 6 minutes worst-case. Matches the
        # refresh-token cleanup pattern: one startup sweep + a daemon
        # thread that loops on a sleep.
        try:
            from server import snapshot_registry as _sr_for_reap
            import threading as _t_reap

            # Startup sweep: catch anything that lingered across a
            # restart or container reboot.
            try:
                stats = _sr_for_reap.reap_stale_sidecars()
                if stats.get("reaped") or stats.get("missing"):
                    log.info(
                        "Sidecar sweep at startup: reaped=%d missing=%d skipped=%d errors=%d",
                        stats["reaped"], stats["missing"],
                        stats["skipped"], stats["errors"],
                    )
            except Exception:
                log.exception("Sidecar startup sweep failed.")

            def _sidecar_sweep_loop() -> None:
                while True:
                    # Sleep first so a quick reboot/test cycle doesn't
                    # double-sweep on top of the startup pass above.
                    import time as _time_inner
                    _time_inner.sleep(60)
                    try:
                        n = _sr_for_reap.reap_stale_sidecars()
                        if n.get("reaped") or n.get("missing"):
                            log.info(
                                "Sidecar sweep: reaped=%d missing=%d skipped=%d errors=%d",
                                n["reaped"], n["missing"],
                                n["skipped"], n["errors"],
                            )
                    except Exception:
                        log.exception("Sidecar sweep tick failed.")

            _t_reap.Thread(
                target=_sidecar_sweep_loop,
                name="sidecar-sweep",
                daemon=True,
            ).start()
        except Exception:  # pragma: no cover (defensive)
            log.exception("Sidecar sweep init failed; continuing.")

        # v0.12.0: initialise the media-state database (creates
        # media.db on first boot, runs pending migrations on every
        # boot). Always runs - the DB is the v0.12.x+ data layer
        # foundation regardless of whether auth is enabled.
        #
        # v0.15: BEFORE init runs, retire any media.db whose
        # schema_version is below CURRENT_SCHEMA_VERSION. The v0.15
        # break introduced library_sections as a NOT NULL anchor on
        # every per-server row, which cannot be backfilled - the
        # original library identity is gone once the row was written
        # without it. Retiring rather than migrating means the next
        # snapshot run rebuilds media.db cleanly from live Plex data.
        try:
            _archive_old_media_db_if_needed(log)
        except Exception:  # pragma: no cover (defensive)
            log.exception(
                "media.db pre-init archive check failed; init will "
                "still run and may fail loudly if the schema is "
                "incompatible."
            )
        try:
            from server import media_db
            media_db.init_media_db()
        except Exception:  # pragma: no cover (defensive)
            log.exception("media_db init failed; continuing without DB.")

        # Playlist Management cache DB (Plan[PLAYLIST-MANAGEMENT]-2026-05-16).
        # Separate SQLite store from media.db so the cache + its TTL
        # retention live independently. Idempotent + best-effort.
        try:
            from server import playlist_cache_db
            playlist_cache_db.init_playlist_cache_db()
        except Exception:  # pragma: no cover (defensive)
            log.exception(
                "playlist_cache_db init failed; Playlist Management "
                "will run without a cache (live API every call)."
            )

        # ETA trainer auto-backfill. The eta_buckets table starts empty
        # on fresh installs and on builds that bumped the schema; the
        # run_timings table retains the last 200 runs' per-step
        # telemetry. If eta_buckets is empty AND run_timings has rows,
        # replay history into the trainer so the predictor lights up
        # at tier 1 from the end user's existing run history instead
        # of cold-starting at tier 5 defaults.
        try:
            from server.run_timings_db import count_eta_buckets
            from services import eta_training as _eta
            if count_eta_buckets() == 0:
                result = _eta.get_trainer().backfill_from_history(
                    reset_first=False,
                )
                if result.get("entries_read"):
                    log.info(
                        "ETA auto-backfill on empty store: %d entries -> "
                        "%d bucket(s)",
                        result["entries_read"],
                        result["buckets_touched"],
                    )
        except Exception:  # pragma: no cover (defensive)
            log.exception("ETA auto-backfill failed; trainer will cold-start.")

        # Restore the persisted audit-log-enabled state into the
        # in-process cache. Default true on missing or read errors -
        # the audit trail is on by default; only an explicit
        # db_admin-gated disable via /api/settings/audit-log-toggle
        # turns it off.
        try:
            from services import db_access_log as _dal
            _s = persistence.load_settings() or {}
            _audit_on = _s.get("audit_log_enabled")
            _dal.set_enabled(True if _audit_on is None else bool(_audit_on))
        except Exception:  # pragma: no cover (defensive)
            log.exception("audit-log flag init failed; assuming enabled.")

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
        # the end user has moved on, so sensitive payloads don't
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

        # PR-Backends one-time migration: rename legacy bare-slug
        # Plex artifacts to the new backend-suffixed form so cascade
        # deletes are unambiguous and downstream consumers (Servers
        # > Logs grouping, snapshot picker) resolve every artifact
        # to the right server. Idempotent: subsequent boots find
        # nothing to rename.
        try:
            migration_summary = server_registry.migrate_legacy_plex_artifact_slugs()
            if (
                migration_summary["renamed_log_dirs"]
                or migration_summary["renamed_exports"]
                or migration_summary["errors"]
            ):
                log.info(
                    "Plex slug migration summary: %s", migration_summary,
                )
        except Exception:  # pragma: no cover (defensive)
            log.exception("Plex artifact slug migration failed; continuing.")

        # Plan[SERVER-UID-IDENTITY] boot migration: upgrade any
        # bare-UUID server rows to the prefixed
        # ``<service_type>_<uuid>`` form and rewrite cross-table
        # references (schedules, identity_map, managed_users,
        # snapshots) in one pass. Idempotent: already-prefixed rows
        # are skipped, so subsequent boots are silent no-ops.
        try:
            uid_summary = server_registry.migrate_server_ids_add_backend_prefix()
            if uid_summary["servers_upgraded"]:
                log.info(
                    "Server-UID migration summary: %s", uid_summary,
                )
        except Exception:  # pragma: no cover (defensive)
            log.exception(
                "Server-UID migration failed; continuing. Operator "
                "should re-register affected servers if downstream "
                "lookups behave inconsistently."
            )

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

def _auto_sync_managed_users(
    server_id: str,
    *,
    force_capture: bool = False,
    capture_only_if_missing: bool = True,
) -> Dict[str, Any]:
    """
    PR-11 - fire a best-effort managed-users metadata sync for one
    server, then PR-12 fires a best-effort per-user token capture for
    the same server. Used by the server-connect hooks (create / update
    / test) so the DB stays warm without end user action. Swallows
    all exceptions: sync or capture failures must NOT block the
    surrounding server-registry call from succeeding.

    PR-12 - the token-capture half is rate-limited per server (see
    ``user_token_capture_throttle_per_hour`` in settings). When this
    function is called by an explicit end user action that should
    bypass the throttle (e.g. a Refresh-server button), pass
    ``force_capture=True``.

    Item 2 (admin-management plan, 2026-05-15) - the token-capture
    half is additive-only by default: users that already have a
    stored auth_token are skipped, so an existing valid token is
    never overwritten by a refresh sweep. Pass
    ``capture_only_if_missing=False`` only from a deliberate
    rotation path (per-user Rotate-token button or the future
    ``auto_rotate_tokens_on_refresh`` tunable).

    Returns the token-capture summary dict from
    ``user_capture.capture_managed_user_tokens`` (``captured``,
    ``skipped_existing``, ``throttled``, ``errors``). Sync errors are
    logged but not included in the return value because they're
    surfaced via the per-server status display elsewhere.
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

    # PR-12 / Item 2 - per-user token capture, throttled per server.
    # PIN-protected users without a stored PIN are silently skipped
    # here; the preflight check surfaces them to the end user before
    # each job.
    cap_summary: Dict[str, Any] = {
        "captured": 0,
        "skipped_existing": 0,
        "throttled": False,
        "errors": [],
    }
    try:
        from server import user_capture
        cap_summary = user_capture.capture_managed_user_tokens(
            server_id,
            force=force_capture,
            only_if_missing=capture_only_if_missing,
            logger=log,
        )
        if cap_summary.get("throttled"):
            log.debug(
                "User-token capture throttled for server %r (default 4/hour/server)",
                server_id,
            )
        elif cap_summary.get("captured"):
            log.info(
                "Captured %d per-user auth token(s) for server %r",
                cap_summary["captured"], server_id,
            )
        for err in (cap_summary.get("errors") or []):
            log.debug("user_capture %r: %s", server_id, err)
    except Exception as exc:  # pragma: no cover (defensive)
        log.exception(
            "Auto-capture user tokens failed unexpectedly for server %r",
            server_id,
        )
        cap_summary["errors"].append(f"unexpected: {exc}")
    return cap_summary


# ── PR-12: preflight acknowledgement re-mapping ─────────────────────────────

def _apply_preflight_ack(params: Dict[str, Any]) -> None:
    """
    Map the public ``pin_preflight_acknowledged`` / ``pin_preflight_at_risk``
    fields a job-submit body may carry onto the underscore-prefixed
    synthetic params the engine expects on a JobRecord. Called by every
    job endpoint after ``body.model_dump`` so the convention is uniform.

    No-op when the end user never saw the modal: the flags default to
    False/None on the model, get stripped by ``exclude_none=True``,
    and we drop the False ack defensively.
    """
    ack = bool(params.pop("pin_preflight_acknowledged", False))
    at_risk_raw = params.pop("pin_preflight_at_risk", None)
    if not ack:
        return
    params["_pin_preflight_acknowledged"] = True
    if isinstance(at_risk_raw, list):
        params["_pin_preflight_at_risk"] = [
            str(x) for x in at_risk_raw if isinstance(x, str)
        ]
    else:
        params["_pin_preflight_at_risk"] = []


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
        ``debug_mode`` mirrors the PLEXMIGRATE_DEBUG_MODE env var; the
        frontend uses it to gate the Developer tab.
        """
        from server import debug_mode
        return {
            "ok": True,
            "api_version": SERVER_API_VERSION,
            "app_version": state.VERSION,
            "debug_mode": debug_mode.is_enabled(),
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

        Surfaces in the future Database tab so end users can answer
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

    # ── Run timings (Feature 1 phase 1.5) ────────────────────────────
    #
    # The dashboard's "Runtime breakdown" panel reads these two
    # endpoints. The first lists recent runs (one row per run_id, with
    # wall-clock totals); the second drills into one run's individual
    # timing entries grouped by scope.

    @app.get("/api/run-timings/runs")
    def list_runtime_runs(limit: int = 25) -> Dict[str, Any]:
        """
        Return summary rows for the most recent ``limit`` runs (default
        25). Each row carries ``run_id``, ``started_at``, ``ended_at``,
        ``duration_seconds``, ``entry_count``, and
        ``total_items_processed``.

        Clamped at 1-200 to bound the response size; 0 / negative
        values default to the spec value of 25.
        """
        from server import run_timings_db
        try:
            clamped = max(1, min(int(limit) or 25, 200))
        except (TypeError, ValueError):
            clamped = 25
        return {"runs": run_timings_db.list_recent_runs(limit=clamped)}

    @app.get("/api/run-timings/runs/{run_id}")
    def get_runtime_run_detail(run_id: str) -> Dict[str, Any]:
        """
        Return every timing entry for one run, ordered by ``started_at``
        ascending. Each entry includes the full ``extra`` annotation
        dict so the frontend can render context (strategy chosen,
        bulk_used flag, http_calls, etc.) inline next to durations.
        """
        from server import run_timings_db
        entries = run_timings_db.get_run_entries(run_id)
        if not entries:
            # Empty list is a valid response: the run may genuinely
            # have no entries, or the run_id was unknown. 404 would
            # force the frontend to handle two empty cases (existing-
            # but-empty vs missing); returning {entries: []} keeps the
            # contract uniform.
            return {"run_id": run_id, "entries": []}
        return {"run_id": run_id, "entries": entries}

    # ── Developer tool: unit test runner (Feature 3 phase 3.3) ───────
    #
    # Two endpoints, both gated behind the debug_mode flag. Production
    # deployments leave PLEXMIGRATE_DEBUG_MODE unset; calls to these
    # endpoints return 403 in that case. Developer / dev-container
    # deployments set the env var and the Developer tab in the
    # frontend becomes visible.
    #
    # Concurrency: only one test run at a time. A second POST while a
    # previous run is in-flight returns 409. The lock is module-level
    # so it survives across requests but does NOT survive a process
    # restart (intentional: a crashed run shouldn't deadlock the next
    # one forever).

    import threading as _threading_for_dev
    _dev_run_lock = _threading_for_dev.Lock()

    @app.post("/api/dev/run-tests")
    def run_dev_tests(body: DevTestRunIn) -> Dict[str, Any]:
        """
        Fire a unit-test run in the chosen mode and return the parsed
        summary. The .log + .summary.json artefacts are written to
        ``server_data/logs/test_runs/`` for the frontend's run-history
        list to consume.

        Debug-mode-gated. Live mode requires ``confirm_live=True``;
        the frontend collects the end user's explicit confirmation
        before submitting. The pytest filter is sanitised at the
        boundary; an invalid filter returns 400.

        A run in flight blocks concurrent submissions with 409
        Conflict so two end users cannot stomp on each other's log
        artefacts in the same second.
        """
        from server import debug_mode
        debug_mode.require_enabled()

        if body.mode == "live" and not body.confirm_live:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Live mode requires explicit confirm_live=true. "
                    "The frontend should surface a typed confirmation "
                    "before submitting."
                ),
            )

        from server import dev_test_harness
        if not _dev_run_lock.acquire(blocking=False):
            raise HTTPException(
                status_code=409,
                detail="A test run is already in flight; wait for it to finish.",
            )
        try:
            try:
                result = dev_test_harness.run_tests(
                    mode=body.mode,
                    pytest_filter=body.pytest_filter,
                    operator=None,  # auth identity wiring TBD
                )
            except ValueError as exc:
                # Bad mode / bad filter / missing test dir.
                raise HTTPException(status_code=400, detail=str(exc))
        finally:
            _dev_run_lock.release()

        return result.to_summary_dict()

    # ── Recent run history (Phase 4 of the dashboard / log reorg) ───
    #
    # Higher-level than /api/run-timings/runs: one row per RUN with
    # job_type, server, libraries, users_affected, duration, plus
    # boolean flags for has_settings_log / has_restoration_log so the
    # frontend can deep-link to those files. Drives the Servers >
    # Recent Runtimes sub-tab.

    @app.get("/api/runs/recent")
    def list_recent_run_history_endpoint(
        limit: int = 200,
        server_id: Optional[str] = None,
        library: Optional[str] = None,
        _user: Dict[str, Any] = Depends(_auth_router_module.require_role("viewer")),
    ) -> Dict[str, Any]:
        """
        Return the most recent ``limit`` run-history rows.

        Optional filters:
        * ``server_id`` - narrow to one registered server's runs.
        * ``library`` - narrow to runs that touched a specific library.

        Limit is clamped to 1-500 to bound the response size.
        viewer+ role gate because the rows surface server names and
        library lists; an anonymous caller has no business reading them.
        """
        from server import run_timings_db
        try:
            clamped = max(1, min(int(limit) or 200, 500))
        except (TypeError, ValueError):
            clamped = 200
        rows = run_timings_db.list_recent_run_history(
            limit=clamped,
            server_id=server_id or None,
            library=library or None,
        )
        return {"runs": rows}

    @app.get("/api/runs/recent/{run_id}")
    def get_recent_run_detail(
        run_id: str,
        _user: Dict[str, Any] = Depends(_auth_router_module.require_role("viewer")),
    ) -> Dict[str, Any]:
        """Return one run-history row by ``run_id``. 404 when absent."""
        from server import run_timings_db
        row = run_timings_db.get_run_history(run_id)
        if row is None:
            raise HTTPException(status_code=404, detail="run_id not found")
        return row

    # ── Adaptive ETA prediction (Plan[ETA-TRAINING] PR-D) ─────────────

    @app.post("/api/eta/predict")
    def predict_eta(
        body: EtaPredictIn,
        _user: Dict[str, Any] = Depends(_auth_router_module.require_role("viewer")),
    ) -> Dict[str, Any]:
        """
        Return an adaptive ETA prediction for the supplied job shape.

        The predictor walks the per-library, per-metric bucket store
        in services.eta_training and rolls the per-step estimates
        into a whole-job estimate. Cold-start fallback (the 4-tier
        cascade) gives a sensible answer from the very first call;
        the more runs the end user does, the tighter the confidence
        interval becomes.

        Returns: ``{mode, point, low, high, std, samples, tier,
        confidence_z, display, per_library: [...]}``. ``display`` is
        a pre-rendered string (e.g. "~12m (typically 8-15m)") for the
        UI to surface inline.
        """
        from services import eta_training
        trainer = eta_training.get_trainer()
        libs = [
            {
                "name": L.name,
                "library_type": L.library_type,
                "items_count": L.items_count,
            }
            for L in (body.libraries or [])
        ]
        # Best-effort: read the source server's last ping reading so
        # the trainer can apply its asymmetric latency-offset
        # multiplier. None means no offset (offset is a no-op when
        # either current or trained ping is unavailable).
        current_ping_ms: Optional[float] = None
        try:
            row = server_registry.get_server_by_id(
                str(body.source_server_id or ""),
                include_token=False,
            )
            if row is not None:
                raw_ping = row.get("last_response_ms")
                if isinstance(raw_ping, (int, float)) and raw_ping > 0:
                    current_ping_ms = float(raw_ping)
        except Exception:
            log.debug("ETA predict: ping lookup failed", exc_info=True)
        try:
            return trainer.predict_for_job(
                mode=str(body.mode or "snapshot"),
                source_server_id=str(body.source_server_id or ""),
                libraries=libs,
                metrics_enabled=dict(body.metrics_enabled or {}),
                user_count=int(body.user_count or 1),
                workers=int(body.workers or 1),
                bulk_strategy=str(body.bulk_strategy or "smart"),
                current_ping_ms=current_ping_ms,
            )
        except Exception as e:
            # Best-effort: a predictor failure must not block job
            # submission. Return a tier-5 "still learning" shape so
            # the UI shows a neutral copy.
            log.exception("ETA predict failed: %s", e)
            return {
                "mode": body.mode,
                "point": 0.0,
                "low": 0.0,
                "high": 0.0,
                "std": 0.0,
                "samples": 0,
                "tier": 5,
                "confidence_z": 1.0,
                "latency_multiplier": 1.0,
                "display": "(estimate unavailable)",
                "per_library": [],
            }

    # ── Ad-hoc cross-backend user copy (Plan[RUN-JOB-UI] follow-up) ──

    @app.post("/api/users/copy_to_destination")
    def copy_user_to_destination(
        body: UserCopyIn,
        _user: Dict[str, Any] = Depends(_auth_router_module.require_role("admin")),
    ) -> Dict[str, Any]:
        """
        Create a single user on a Jellyfin / Emby destination via
        the same ``services/user_creation.create_users_for_job``
        path the Run Job D-OWNER modal uses, but as a synchronous
        one-off (no queued job, no run log entry).

        Used by the User Management panel's per-user "Copy user to
        destination" inline expansion. The follow-up data transfer
        is the end user's choice and lands as a separate
        ``/api/job/direct`` request orchestrated by the frontend.

        Failure modes:
          * target_server_id resolves to a Plex backend -> 400
            (Plex cannot create users via API).
          * source or target server id unknown -> 404.
          * adapter.create_user raises / returns None -> 500 with a
            ``UserCreationError`` detail; same two-phase rollback as
            the batch path (single-spec rollback is a no-op but the
            error shape is consistent).

        Returns ``{backend_user_id, target_username,
        source_user_handle}`` on success.
        """
        from server.server_registry import (
            connect_registered_server, get_server_by_id,
        )
        from server.models import UserCreateSpec
        from services.user_creation import (
            UserCreationError, create_users_for_job,
        )

        src_row = get_server_by_id(body.source_server_id, include_token=False)
        if src_row is None:
            raise HTTPException(
                status_code=404,
                detail=f"source server {body.source_server_id!r} not found",
            )
        dst_row = get_server_by_id(body.target_server_id, include_token=False)
        if dst_row is None:
            raise HTTPException(
                status_code=404,
                detail=f"target server {body.target_server_id!r} not found",
            )

        dst_service = (dst_row.get("service_type") or "plex").lower()
        if dst_service == "plex":
            raise HTTPException(
                status_code=400,
                detail=(
                    "Plex destinations cannot create users via API; pick "
                    "a Jellyfin or Emby server as the target."
                ),
            )

        # Connect to the target so we have a live adapter.
        try:
            dst_connection = connect_registered_server(
                str(body.target_server_id), log,
            )
        except Exception as exc:
            log.exception("copy_user_to_destination: connect failed: %s", exc)
            raise HTTPException(
                status_code=502,
                detail=f"could not connect to target server: {exc}",
            )

        spec = UserCreateSpec(
            source_user_handle=body.source_user_handle,
            target_username=body.target_username,
            temp_password=body.temp_password,
            target_user_policy=body.target_user_policy,
        )

        try:
            results = create_users_for_job(
                adapter=dst_connection.adapter,
                specs=[spec],
                dest_server_id=str(body.target_server_id),
                logger=log,
            )
        except UserCreationError as exc:
            log.error(
                "copy_user_to_destination: user_creation failed: %s",
                exc,
            )
            raise HTTPException(
                status_code=500,
                detail=f"user creation failed: {exc}",
            )

        if not results:
            raise HTTPException(
                status_code=500,
                detail="user creation returned no results",
            )
        r = results[0]
        if r.status != "created":
            raise HTTPException(
                status_code=500,
                detail=f"user creation status was {r.status!r}: {r.error}",
            )

        # Auto-write the identity mapping. Best-effort: failure here
        # does not undo the user creation (the user does exist on
        # destination; the end user can manually add the mapping
        # later via the standalone mapping panel).
        try:
            from server.media_db import add_identity_map
            add_identity_map(
                server_a_id=body.source_server_id,
                user_a_handle=body.source_user_handle,
                server_b_id=body.target_server_id,
                user_b_handle=r.target_username,
                source="auto_copy",
            )
        except Exception:
            log.exception(
                "copy_user_to_destination: auto-mapping write failed "
                "(user created successfully; mapping skipped)"
            )

        return {
            "backend_user_id": r.backend_user_id,
            "target_username": r.target_username,
            "source_user_handle": r.source_user_handle,
        }

    # ── User identity mapping (cross-server) ─────────────────────────

    @app.post("/api/users/identity_map")
    def add_user_identity_map(
        body: UserIdentityMapIn,
        _user: Dict[str, Any] = Depends(_auth_router_module.require_role("admin")),
    ) -> Dict[str, Any]:
        """Add one (server_a, handle_a) <-> (server_b, handle_b)
        link. Duplicate pairs return ``{id: null, duplicate: true}``;
        new pairs return the inserted row id."""
        from server.media_db import add_identity_map
        try:
            new_id = add_identity_map(
                server_a_id=body.server_a_id,
                user_a_handle=body.user_a_handle,
                server_b_id=body.server_b_id,
                user_b_handle=body.user_b_handle,
                source=body.source,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {"id": new_id, "duplicate": new_id is None}

    @app.get("/api/users/identity_map")
    def list_user_identity_maps(
        _user: Dict[str, Any] = Depends(_auth_router_module.require_role("viewer")),
    ) -> Dict[str, Any]:
        """Return every identity-map row in insertion order. Viewer+
        gated because the mapping is operationally interesting but
        carries no credentials."""
        from server.media_db import list_identity_maps
        return {"maps": list_identity_maps()}

    @app.delete("/api/users/identity_map/{map_id}")
    def remove_user_identity_map(
        map_id: int,
        _user: Dict[str, Any] = Depends(_auth_router_module.require_role("admin")),
    ) -> Dict[str, Any]:
        """Delete one identity-map row by id. Returns 404 when the
        row did not exist."""
        from server.media_db import delete_identity_map
        ok = delete_identity_map(map_id)
        if not ok:
            raise HTTPException(status_code=404, detail="identity-map row not found")
        return {"removed": True, "id": map_id}

    @app.post("/api/users/identity_map/rerun-auto-link")
    def rerun_auto_link_identity_map(
        _user: Dict[str, Any] = Depends(_auth_router_module.require_role("admin")),
    ) -> Dict[str, Any]:
        """USER-MGMT-IDENTITY-AUDIT follow-on: explicit end user
        trigger for the backend_user_id auto-link helper.

        The helper runs automatically after every managed-users sync
        (services/media_db.sync_managed_users_from_live wires it in),
        so the normal operational path already covers it. This
        endpoint lets an end user force a rerun without waiting for
        the next sync - useful right after manually adding a new
        identity_map row, or to confirm cross-server detection picked
        up a just-registered server.

        Returns the helper's summary dict
        ``{pairs_written, pairs_skipped_duplicate, groups_seen}``.
        Best-effort: helper failures bubble up as 500 with the
        exception text so the end user can see what went wrong."""
        from server.media_db import auto_link_identity_map_by_backend_user_id
        try:
            summary = auto_link_identity_map_by_backend_user_id()
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))
        return summary

    @app.get("/api/eta/training-status")
    def get_eta_training_status(
        server_id: Optional[str] = None,
        _user: Dict[str, Any] = Depends(_auth_router_module.require_role("viewer")),
    ) -> Dict[str, Any]:
        """Return a snapshot of the trainer's bucket store grouped by
        server. Drives the Run Job form's 'Training progress'
        disclosure so the end user can confirm each bucket is
        accumulating samples after every job.

        Optional ``server_id`` filter scopes the response to one
        server. Without the filter, every server with at least one
        trained bucket is returned.

        Returns ``{by_server: [{server_id, buckets: [...], summary:
        {total_buckets, tier_one_count, anchor_count,
        min_samples_for_tier_one}}]}``."""
        from services import eta_training
        trainer = eta_training.get_trainer()
        return trainer.training_status(
            server_id=(server_id or None),
        )

    @app.post("/api/eta/backfill")
    def backfill_eta_weights(
        body: EtaBackfillIn,
        _user: Dict[str, Any] = Depends(_auth_router_module.require_role("admin")),
    ) -> Dict[str, Any]:
        """
        Warm-start the ETA engine from the historical ``run_timings``
        table. Gap-A of the post-cutover review: PlexBackUp has been
        recording per-operation timings since developer's Feature 1
        shipped (200-run retention), but the new ETA bucket store
        was just created and starts empty. This endpoint replays
        every existing ``run_timings`` row through the trainer's
        EMA + variance formula in chronological order so the
        predictor's tier-1 cells light up immediately.

        ``reset_first=True`` wipes the bucket store before replaying.
        Default False is safe to re-run; the EMA is additive.

        Returns the count of entries read + buckets touched.
        """
        from services import eta_training
        trainer = eta_training.get_trainer()
        result = trainer.backfill_from_history(reset_first=bool(body.reset_first))
        return result

    @app.post("/api/eta/reset")
    def reset_eta_weights(
        body: EtaResetIn,
        _user: Dict[str, Any] = Depends(_auth_router_module.require_role("admin")),
    ) -> Dict[str, Any]:
        """
        Reset every learned ETA weight for one server. Wired to the
        end user's D-RESET escape hatch: "I just upgraded this
        server's storage; the old timings are wrong, learn again
        from scratch."

        Body carries the ``server_id`` plus a typed confirmation
        string the UI prompts for. The backend refuses unless
        ``confirm == "RESET"``.
        """
        if (body.confirm or "").strip() != "RESET":
            raise HTTPException(
                status_code=400,
                detail='confirm must equal the literal string "RESET"',
            )
        if not (body.server_id or "").strip():
            raise HTTPException(
                status_code=400,
                detail="server_id is required",
            )
        from services import eta_training
        trainer = eta_training.get_trainer()
        removed = trainer.reset_server(body.server_id.strip())
        return {"server_id": body.server_id, "removed": removed}

    @app.get("/api/database/tables")
    def list_database_tables(
        _user: Dict[str, Any] = Depends(_auth_router_module.require_role("viewer")),
    ) -> Dict[str, Any]:
        """List every operational table available for export, with
        live row counts. Drives the Settings Run History database
        export/import panel."""
        from server import db_export
        return {"tables": db_export.list_tables()}

    @app.get("/api/database/export/{table_id}")
    def export_database_table(
        table_id: str,
        _user: Dict[str, Any] = Depends(_auth_router_module.require_role("admin")),
    ) -> Dict[str, Any]:
        """Dump one operational table as JSON. The frontend triggers
        a file download by reading the response body and creating a
        blob; this endpoint returns the JSON payload directly so the
        client can save it under any filename it likes."""
        from server import db_export
        try:
            return db_export.export_table(table_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"unknown table_id: {table_id}")
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc))

    @app.get("/api/database/export-all")
    def export_database_archive(
        _user: Dict[str, Any] = Depends(_auth_router_module.require_role("admin")),
    ) -> Dict[str, Any]:
        """Dump every operational table into a single archive
        document. End user stores this as a recovery checkpoint."""
        from server import db_export
        return db_export.export_all()

    @app.post("/api/database/import/{table_id}")
    def import_database_table(
        table_id: str,
        body: DbImportIn,
        _user: Dict[str, Any] = Depends(_auth_router_module.require_role("admin")),
    ) -> Dict[str, Any]:
        """Restore one operational table from a previously-exported
        JSON document. Replace-only: the table is wiped before the
        supplied rows are inserted. Behind a typed REPLACE
        confirmation; misspellings reject without touching data."""
        if (body.confirm or "").strip() != "REPLACE":
            raise HTTPException(
                status_code=400,
                detail='confirm must equal the literal string "REPLACE"',
            )
        # Force the URL's table_id to win over any value the end user
        # might have left in the JSON payload from a different export.
        # Better to reject mismatched dumps than to silently restore
        # the wrong table.
        payload = dict(body.payload or {})
        if payload.get("table_id") and payload["table_id"] != table_id:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"payload table_id ({payload.get('table_id')!r}) "
                    f"does not match URL table_id ({table_id!r})"
                ),
            )
        payload["table_id"] = table_id
        from server import db_export
        try:
            return db_export.restore_table(payload)
        except (ValueError, KeyError) as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc))

    @app.get("/api/database/per-server/{server_id}")
    def list_database_per_server(
        server_id: str,
        _user: Dict[str, Any] = Depends(_auth_router_module.require_role("viewer")),
    ) -> Dict[str, Any]:
        """Return per-table row counts scoped to one server. Drives
        the Settings Run History per-server identity-DB pivot where
        the end user inspects one Plex / Jellyfin / Emby server's
        rows in isolation (registry entry, known users, library
        sections, watch history, ratings, identity links, learned
        ETA buckets, snapshot registry, etc.)."""
        from server import db_export
        return {
            "server_id": server_id,
            "tables": db_export.list_per_server_view(server_id),
        }

    @app.get("/api/database/export-per-server/{server_id}")
    def export_database_per_server(
        server_id: str,
        _user: Dict[str, Any] = Depends(_auth_router_module.require_role("admin")),
    ) -> Dict[str, Any]:
        """Dump every per-server table filtered to one server into
        an archive document. Useful for one-server backups or for
        migrating identity state between installs."""
        from server import db_export
        try:
            return db_export.export_per_server(server_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.post("/api/database/import-archive")
    def import_database_archive(
        body: DbImportIn,
        _user: Dict[str, Any] = Depends(_auth_router_module.require_role("admin")),
    ) -> Dict[str, Any]:
        """Restore every table from a previously-exported archive.
        Each table is replace-only and processed independently;
        a failure on one does not abort the others. Returns
        per-table results + aggregate counts + any errors."""
        if (body.confirm or "").strip() != "REPLACE":
            raise HTTPException(
                status_code=400,
                detail='confirm must equal the literal string "REPLACE"',
            )
        from server import db_export
        try:
            return db_export.restore_archive(dict(body.payload or {}))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.post("/api/eta/repair-server-ids")
    def repair_eta_server_ids(
        _user: Dict[str, Any] = Depends(_auth_router_module.require_role("admin")),
    ) -> Dict[str, Any]:
        """Backfill missing ``server_id`` on legacy ``run_timings``
        entries by joining against ``run_history``. Fixes the
        snapshot_library bug where server_id was never threaded
        through the per-library dispatch, leaving ~99% of legacy
        timing rows orphaned and unusable by the trainer.

        After repair, the end user should POST /api/eta/backfill
        with ``reset_first=true`` to rebuild eta_buckets from the
        now-complete history. Both buttons are wired in the Settings
        Run History tab."""
        from server.run_timings_db import repair_missing_server_ids
        return repair_missing_server_ids()

    @app.post("/api/eta/flush")
    def flush_eta_training(
        body: EtaFlushIn,
        _user: Dict[str, Any] = Depends(_auth_router_module.require_role("admin")),
    ) -> Dict[str, Any]:
        """End user's 'Flush all training data' escape hatch from the
        Settings ETA Training panel. Clears every bucket in memory and
        on disk; when ``include_run_timings`` is True the underlying
        run_timings table is also wiped so the auto-backfill cannot
        repopulate from history.

        Behind a typed FLUSH confirmation - misspellings are rejected
        rather than silently wiping training data."""
        if (body.confirm or "").strip() != "FLUSH":
            raise HTTPException(
                status_code=400,
                detail='confirm must equal the literal string "FLUSH"',
            )
        from services import eta_training
        trainer = eta_training.get_trainer()
        return trainer.flush_all(include_run_timings=bool(body.include_run_timings))

    @app.get("/api/dev/test-runs")
    def list_dev_test_runs(limit: int = 25) -> Dict[str, Any]:
        """
        Return parsed summary JSON for the most recent ``limit`` dev
        tool runs, newest first by file mtime. The frontend's run-
        history list reads this and renders the per-run status chip +
        a deep link into the raw .log.
        """
        from server import debug_mode
        debug_mode.require_enabled()
        from server import dev_test_harness
        try:
            clamped = max(1, min(int(limit) or 25, 200))
        except (TypeError, ValueError):
            clamped = 25
        return {"runs": dev_test_harness.list_recent_runs(limit=clamped)}

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
        before the end user commits to the destructive call.

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
        # ``audit_log_enabled`` is read-only via this endpoint - it
        # only flips through the db_admin-gated audit-log-toggle below.
        # Silently strip it from the patch so a stray PATCH from the
        # Settings UI can never disable the audit trail by accident.
        patch.pop("audit_log_enabled", None)
        # v0.9.5: refuse Windows host paths early so the end user gets
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

    @app.post("/api/settings/audit-log-toggle")
    def toggle_audit_log(
        body: Dict[str, Any],
        _admin: Dict[str, Any] = Depends(_auth_router_module.require_role("admin")),
    ) -> Dict[str, Any]:
        """
        Enable / disable the db-access audit log.

        Two-factor gated (matches the destructive-endpoint pattern):
        admin/root_admin JWT plus the separate db_admin credential in
        the request body. Body: ``{db_admin_username, db_admin_password,
        enabled: bool}``.

        Self-documenting transition: when disabling, the final audit
        line records who switched it off; when re-enabling, the first
        new line records who switched it back on. The audit trail
        always shows that disablement was an explicit, attributable
        act - that's the C-design compromise for letting the audit
        log itself be end user-toggleable.
        """
        from server import auth_db
        from services import db_access_log
        un = str(body.get("db_admin_username") or "")
        pw = str(body.get("db_admin_password") or "")
        if not un or not pw:
            raise HTTPException(
                status_code=401,
                detail="Database admin credentials are required.",
            )
        verified = auth_db.verify_password(un, pw)
        if verified is None or verified.get("role") != "db_admin":
            raise HTTPException(
                status_code=401,
                detail="Database admin credentials are invalid.",
            )
        new_enabled = bool(body.get("enabled"))
        currently_enabled = db_access_log.is_enabled()
        if currently_enabled and not new_enabled:
            # Going OFF: write the final entry BEFORE flipping the flag
            # so the line lands. Then persist + cache.
            db_access_log.log_audit_disabled(un)
            db_access_log.set_enabled(False)
            persistence.save_settings({"audit_log_enabled": False})
        elif (not currently_enabled) and new_enabled:
            # Going ON: flip first so the entry actually writes, then
            # log the resumption.
            db_access_log.set_enabled(True)
            db_access_log.log_audit_enabled(un)
            persistence.save_settings({"audit_log_enabled": True})
        # Else: no-op transition (already in requested state).
        return {"audit_log_enabled": db_access_log.is_enabled()}

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
        the only reliable signal that the end user typed the right
        URL for the server they meant.

        PR-11: also kicks off a managed-users sync for the newly
        registered server so the User Management panel and the
        JobFormPanel picker (which now reads from the DB) are warm
        immediately. Failure is non-fatal - the server is still
        registered and the end user can run a manual sync later.
        """
        try:
            row = server_registry.add_server(
                body.name,
                body.url,
                body.token,
                logger=log,
                use_fallback_from_server_id=body.use_fallback_from_server_id,
                service_type=body.service_type,
            )
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
          * ``name_mismatch``     - true when the end user's typed
            friendly name doesn't match what the server reports for
            itself. Surfaced so the UI can show a warning before save.
          * ``duplicate_of``      - friendly name of an existing
            registered server that shares this server's
            ``machine_identifier``. Null when there's no collision.
        """
        probe = server_registry.probe_unsaved(
            body.url, body.token, log, service_type=body.service_type,
        )
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
        refreshed. Used by the Refresh / Test button in the Servers tab.

        PR-11: re-test is the "reconnected" trigger from the spec, so
        we also re-sync managed users best-effort. Failure is logged
        but doesn't break the test response - status info is the
        primary contract of this endpoint.

        Item 2 (admin-management plan, 2026-05-15): the Refresh button
        is an explicit end user action, so we bypass the per-server
        throttle on the token-capture sweep (``force_capture=True``)
        and the sweep is additive-only by default
        (``capture_only_if_missing=True``) so existing stored tokens
        are never overwritten. The capture summary is surfaced on the
        response under ``token_capture`` so the frontend can show a
        toast like "Captured N new user tokens".
        """
        try:
            out = server_registry.test_connection(server_id, log)
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e))
        cap_summary = _auto_sync_managed_users(
            server_id,
            force_capture=True,
            capture_only_if_missing=True,
        )
        if isinstance(out, dict):
            out["token_capture"] = cap_summary
        return out

    @app.post("/api/servers/{server_id}/retry-pending-token")
    def retry_pending_token(
        server_id: str,
        _admin: Dict[str, Any] = Depends(_auth_router_module.require_role("admin")),
    ) -> Dict[str, Any]:
        """
        Re-probe the server's stashed pending token (the end user's
        original typed token that failed with 401 at Add Server time).
        On success: swap pending into active token, clear pending
        fields, return the new row. On failure: bump
        pending_token_last_probed_at and return the failure detail
        so the UI can grey the chip and surface a "last tried" hint.

        Admin-gated because rewriting the stored token requires the
        same authority as create/update. The previously-active token
        is replaced (not preserved); end users who want a rollback
        path should test the pending token in a separate Add-form
        attempt before retrying.
        """
        try:
            out = server_registry.retry_pending_token(server_id, logger=log)
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e))
        # On a successful swap, also nudge the managed-users sync so
        # the user picker stays warm (mirrors what /test does).
        if out.get("swapped"):
            try:
                _auto_sync_managed_users(
                    server_id,
                    force_capture=True,
                    capture_only_if_missing=True,
                )
            except Exception:  # pragma: no cover (defensive)
                log.exception(
                    "Auto-sync after retry-pending-token failed for %r; "
                    "swap still committed.", server_id,
                )
        return out

    @app.get("/api/servers/{server_id}/pin-migration-suggestions")
    def get_pin_migration_suggestions(
        server_id: str,
        _admin: Dict[str, Any] = Depends(_auth_router_module.require_role("admin")),
    ) -> Dict[str, Any]:
        """
        Item 3 (admin-management plan, 2026-05-15): cross-server PIN
        migration suggestions.

        Lists managed users on this server that don't have a stored
        Plex Home PIN, and for whom a managed user with the same
        Plex user ID (or username, when the
        ``pin_migration_allow_username_fallback`` tunable is on) on a
        different registered server DOES have a stored PIN.

        Read-only. Admin role is sufficient to view the list because
        no credential material is leaked - only usernames + which
        server each side belongs to. Applying a migration requires
        elevation; see ``apply_pin_migrations`` below.
        """
        try:
            from server import pin_migration
            from server.persistence import load_settings
            fallback = bool((load_settings() or {}).get(
                "pin_migration_allow_username_fallback", False,
            ))
            return pin_migration.compute_pin_migration_suggestions(
                server_id, allow_username_fallback=fallback,
            )
        except Exception as exc:  # pragma: no cover (defensive)
            log.exception("pin-migration-suggestions failed for %r", server_id)
            raise HTTPException(status_code=500, detail=str(exc))

    @app.post("/api/servers/{server_id}/managed-users/{username}/rotate-token")
    def rotate_one_managed_user_token(
        server_id: str,
        username: str,
        user: Dict[str, Any] = Depends(_auth_router_module.require_elevation()),
    ) -> Dict[str, Any]:
        """
        Item 5 (admin-management plan, 2026-05-15): per-user "Rotate
        token" button. Forces a token re-capture for ONE managed user
        on ``server_id``, bypassing the per-server throttle AND the
        additive-only contract that protects the Refresh sweep.

        Use this when a managed user's Plex token has rotated on the
        Plex side (they re-signed in, were re-invited, etc.) and the
        stored token is now stale. The Refresh button does NOT do
        this by default; the end user opts into per-user rotation
        here so accidental clicks can't burn the auth caches of a
        whole Plex Home in one go.

        Requires sudo-style elevation. Returns the same shape as the
        bulk capture (``captured``, ``skipped_existing``, ``throttled``,
        ``errors``) so the frontend can render a consistent toast.
        """
        from server import user_capture
        result = user_capture.capture_managed_user_tokens(
            server_id, force=True, only_if_missing=False, logger=log,
        )
        log.info(
            "Manual token rotation: server=%r user=%r actor=%r captured=%d errors=%d",
            server_id, username, user["username"],
            result.get("captured", 0),
            len(result.get("errors") or []),
        )
        return result

    @app.post("/api/servers/{server_id}/pin-migration/apply")
    def apply_pin_migration(
        server_id: str,
        body: PinMigrationApplyIn,
        user: Dict[str, Any] = Depends(_auth_router_module.require_elevation()),
    ) -> Dict[str, Any]:
        """
        Item 3 apply path. Each entry in ``body.suggestions`` carries
        the (target_username, source_server_id, source_username)
        triple from the suggestions list. Requires sudo-style
        elevation: the caller's session must have a fresh password
        re-confirm because PIN material is auth-equivalent.

        Returns ``{applied, skipped, errors}`` (see
        ``pin_migration.apply_pin_migrations`` for the exact shape).
        """
        from server import pin_migration
        try:
            confirmed = [s.model_dump() for s in body.suggestions]
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"bad body: {exc}")
        result = pin_migration.apply_pin_migrations(
            server_id, confirmed, actor=user["username"], logger=log,
        )
        log.info(
            "PIN migration apply: server=%r actor=%r applied=%d skipped=%d errors=%d",
            server_id, user["username"],
            len(result.get("applied") or []),
            len(result.get("skipped") or []),
            len(result.get("errors") or []),
        )
        return result

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
        end user's chosen display name from the server's
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

    @app.post("/api/job/preflight-pin-check")
    def post_job_preflight_pin_check(body: PinPreflightIn) -> Dict[str, Any]:
        """
        PR-12 preflight. Returns the list of managed users in the
        about-to-submit job's scope who have neither a stored auth
        token nor a stored Plex Home PIN in media.db. The frontend
        renders a warning modal when the response contains at-risk
        users and submits with ``pin_preflight_acknowledged=true`` if
        the end user clicks Continue anyway.

        Read-only: no side effects, no mutation of any store. Any
        logged-in end user can call it.
        """
        from server import preflight
        return preflight.compute_pin_preflight(
            mode=body.mode,
            source_server_name=body.source_server_name,
            dest_server_names=body.dest_server_names,
            user_filter=body.user_filter,
        )

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
        _apply_preflight_ack(params)
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
        _apply_preflight_ack(params)
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
        consumes JSON; we just resolve the path here so the end user
        never has to type one in.
        """
        from server import snapshot_registry
        row = snapshot_registry.get(body.snapshot_id)
        if row is None:
            raise HTTPException(
                status_code=404,
                detail=f"No snapshot with id {body.snapshot_id!r}.",
            )

        # Feature 2: pre-restore snapshot validation. Gated by
        # settings.validate_snapshot_before_restore (default OFF per
        # D4/D5). Runs the validator against a temp copy of the
        # snapshot.db; errors abort the restore submission with
        # 422 Unprocessable Entity so the end user sees the specific
        # invariant violation rather than a generic engine failure
        # mid-run.
        from services import snapshot_validator
        if snapshot_validator.is_before_restore_enabled():
            db_file = row.get("file_path")
            if db_file:
                report = snapshot_validator.validate_snapshot(Path(db_file))
                if not report.ok:
                    err_lines = [
                        f"[{i.code}] {i.message}" for i in report.errors
                    ]
                    raise HTTPException(
                        status_code=422,
                        detail=(
                            "Pre-restore snapshot validation failed. "
                            "Disable validate_snapshot_before_restore in "
                            "Settings to bypass, or re-capture the "
                            "snapshot. Errors: "
                            + "; ".join(err_lines)
                        ),
                    )
                # Warnings don't abort; surface in the log so the
                # end user can investigate after the restore lands.
                for issue in report.warnings:
                    logging.getLogger("plexmigrate.server.app").warning(
                        "pre-restore validation warning [%s]: %s",
                        issue.code, issue.message,
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
        _apply_preflight_ack(params)
        rec = get_queue().submit_restore(params)
        return {"job_id": rec.job_id, "state": rec.state, "mode": rec.mode}

    @app.post("/api/job/direct")
    def post_job_direct(body: DirectTransferIn) -> Dict[str, Any]:
        """
        Enqueue a direct server-to-server transfer job. Both
        ``source_server_name`` and ``dest_server_name`` are required
        and must resolve to different registered servers.
        """
        params = body.model_dump(exclude_none=True)
        _apply_preflight_ack(params)
        rec = get_queue().submit_direct(params)
        return {"job_id": rec.job_id, "state": rec.state, "mode": rec.mode}

    # ── Cross-platform preflight (Plan[CROSS-PLATFORM-PREFLIGHT] step 4) ──
    #
    # Four routes wire the preflight UX into the engine's dry-run
    # resolver. The modal calls preflight before submit; the user
    # creates missing destination users inline; schedules persist the
    # end user's decisions so fires reuse them deterministically.
    # See Plan[CROSS-PLATFORM-PREFLIGHT]-2026-05-16.md + the UI Plan
    # for the wire contract.

    def _run_preflight_for_dest(
        payload: Dict[str, Any], dest_name: str,
        body: CrossPlatformPreflightJobIn,
    ) -> CrossPlatformPreflightReport:
        """Build the destination adapter, call dry_run_resolve_users,
        marshal the dataclass into the Pydantic wire shape."""
        from server import server_registry
        from services.restorer_adapter import dry_run_resolve_users
        try:
            connection = server_registry.connect_registered_server(
                dest_name, log,
            )
        except ValueError:
            raise HTTPException(
                status_code=404,
                detail=f"Destination server {dest_name!r} is not registered.",
            )
        except ConnectionError as exc:
            raise HTTPException(
                status_code=502,
                detail=f"Could not connect to destination {dest_name!r}: {exc}",
            )
        report_dc = dry_run_resolve_users(
            payload,
            connection.adapter,
            dest_server_id=str(connection.row.get("id") or ""),
            include_watch_history=body.include_watch_history,
            include_ratings=body.include_ratings,
            include_playlists=body.include_playlists,
            include_collections=body.include_collections,
            include_managed_users=body.include_managed_users,
            user_filter=body.user_filter,
            logger=log,
        )
        return CrossPlatformPreflightReport(
            source_kind=report_dc.source_kind,
            dest_kind=report_dc.dest_kind,
            source_server_id=report_dc.source_server_id,
            dest_server_id=report_dc.dest_server_id,
            is_cross_platform=report_dc.is_cross_platform,
            source_admin_count=report_dc.source_admin_count,
            dest_admin_count=report_dc.dest_admin_count,
            resolutions=[
                UserResolutionOut(
                    source_username=r.source_username,
                    source_role=r.source_role if r.source_role in ("owner", "admin", "managed") else "managed",
                    source_row_counts=UserRowCountsOut(**{
                        "watch_history": r.source_row_counts.watch_history,
                        "ratings": r.source_row_counts.ratings,
                        "playlists": r.source_row_counts.playlists,
                        "collections": r.source_row_counts.collections,
                    }),
                    proposed_resolution=r.proposed_resolution,
                    proposed_dest_user_id=r.proposed_dest_user_id,
                    proposed_dest_username=r.proposed_dest_username,
                    proposed_dest_role=r.proposed_dest_role,
                    needs_ack=r.needs_ack,
                    blocks_submit=r.blocks_submit,
                    warnings=list(r.warnings),
                    available_dest_users=[
                        DestUserOptionOut(
                            backend_user_id=opt.backend_user_id,
                            username=opt.username,
                            role=opt.role,
                            is_tombstoned=opt.is_tombstoned,
                        )
                        for opt in r.available_dest_users
                    ],
                )
                for r in report_dc.resolutions
            ],
            smart_playlists_skipped=report_dc.smart_playlists_skipped,
            smart_playlist_names=list(report_dc.smart_playlist_names),
            library_type_notes=[
                LibraryTypeNoteOut(
                    source_library=n.source_library,
                    source_type=n.source_type,
                    dest_type_used=n.dest_type_used,
                    message=n.message,
                )
                for n in report_dc.library_type_notes
            ],
            tombstoned_users_excluded=[
                TombstoneNoteOut(
                    dest_username=t.dest_username,
                    reason=t.reason,
                )
                for t in report_dc.tombstoned_users_excluded
            ],
            zero_row_skipped=[
                ZeroRowSkipOut(
                    source_username=z.source_username,
                    empty_signals=list(z.empty_signals),
                    filter_flags_in_effect=list(z.filter_flags_in_effect),
                    message=z.message,
                )
                for z in report_dc.zero_row_skipped
            ],
            overall_verdict=report_dc.overall_verdict,
            blocking_reasons=list(report_dc.blocking_reasons),
        )

    def _load_preflight_payload(
        body: CrossPlatformPreflightJobIn,
    ) -> Dict[str, Any]:
        """Resolve the preflight body to a single snapshot payload dict.

        snapshot_id: read from the snapshot registry + materialise via
        the existing sidecar mechanism. input_files: load the first
        file (multi-file restores all share the same per-server shape
        for preflight purposes - users are aggregated across libraries
        in a single file anyway). Either path raises HTTPException."""
        from server.jobs import _load_snapshot_payload as _load
        if body.snapshot_id:
            from server import snapshot_registry
            row = snapshot_registry.get(body.snapshot_id)
            if row is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"No snapshot with id {body.snapshot_id!r}.",
                )
            db_file = row.get("file_path")
            if not db_file:
                raise HTTPException(
                    status_code=422,
                    detail=f"Snapshot {body.snapshot_id!r} has no file_path.",
                )
            # Forward-compat wrap: post-strip _load_snapshot_payload's
            # .json branch raises ValueError instead of returning a
            # wrapped dict. Surface as a clean 415 to the end user.
            try:
                payload = _load(str(db_file), log)
            except ValueError as _exc:
                raise HTTPException(
                    status_code=415,
                    detail=(
                        f"Snapshot {body.snapshot_id!r} at {db_file!r} "
                        f"uses an unsupported file shape: {_exc}"
                    ),
                )
            if payload is None:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"Could not load payload from snapshot "
                        f"{body.snapshot_id!r} at {db_file!r}."
                    ),
                )
            return payload
        # File-based path: load the first input_file. For multi-file
        # restores the preflight focuses on the first file's user
        # roster; the engine still iterates all files at submit time.
        first = (body.input_files or [None])[0]
        if not first:
            raise HTTPException(
                status_code=400,
                detail="input_files is empty.",
            )
        # Resolve via the same three-attempt search the engine does.
        from pathlib import Path as _Path
        output_dir = "./snapshots"
        candidates = [
            _Path(first),
            _Path(output_dir) / first,
            _Path(output_dir) / "legacy" / first,
        ]
        for cand in candidates:
            if cand.is_file():
                # Same forward-compat wrap as the snapshot_id path.
                try:
                    payload = _load(str(cand), log)
                except ValueError as _exc:
                    raise HTTPException(
                        status_code=415,
                        detail=(
                            f"Input file {first!r} uses an unsupported "
                            f"file shape: {_exc}"
                        ),
                    )
                if payload is not None:
                    return payload
        raise HTTPException(
            status_code=404,
            detail=(
                f"Input file {first!r} not found (tried {candidates})."
            ),
        )

    @app.post(
        "/api/jobs/cross-platform-preflight",
        response_model=PreflightResponse,
    )
    def post_jobs_cross_platform_preflight(
        body: CrossPlatformPreflightJobIn,
    ) -> PreflightResponse:
        """Compute per-destination cross-platform preflight reports
        for the submit body. Returns a PreflightResponse wrapping one
        report per destination_server_id. Read-only: no side effects,
        no engine writes."""
        payload = _load_preflight_payload(body)
        dest_names = body.resolved_destinations()
        reports: Dict[str, CrossPlatformPreflightReport] = {}
        aggregate = "ok"
        rank = {"ok": 0, "ack_required": 1, "blocked": 2}
        for dest_name in dest_names:
            report = _run_preflight_for_dest(payload, dest_name, body)
            reports[report.dest_server_id or dest_name] = report
            if rank[report.overall_verdict] > rank[aggregate]:
                aggregate = report.overall_verdict
        return PreflightResponse(
            reports=reports,
            aggregate_verdict=aggregate,
        )

    @app.post(
        "/api/schedules/cross-platform-preflight",
        response_model=PreflightResponse,
    )
    def post_schedules_cross_platform_preflight(
        body: ScheduleIn,
    ) -> PreflightResponse:
        """Schedule-time preflight. Same shape as the jobs endpoint
        but takes a ScheduleIn body. Resolves the schedule's
        snapshot / input files + destinations and returns one report
        per destination_server_id."""
        # Map the schedule shape onto the preflight job body so the
        # implementation stays the same. Field names mostly overlap.
        body_dict = body.model_dump(exclude_none=True)
        preflight_body = CrossPlatformPreflightJobIn(
            snapshot_id=body_dict.get("snapshot_id"),
            input_files=body_dict.get("input_files") or [],
            dest_server_name=body_dict.get("dest_server_name"),
            dest_server_names=body_dict.get("dest_server_names"),
            include_watch_history=bool(body_dict.get("include_watch_history", True)),
            include_ratings=bool(body_dict.get("include_ratings", True)),
            include_playlists=bool(body_dict.get("include_playlists", True)),
            include_collections=bool(body_dict.get("include_collections", True)),
            include_managed_users=bool(body_dict.get("include_managed_users", True)),
            user_filter=body_dict.get("user_filter"),
        )
        return post_jobs_cross_platform_preflight(preflight_body)

    @app.post(
        "/api/jobs/inline-create-user",
        response_model=InlineCreateUserResponse,
    )
    def post_jobs_inline_create_user(
        body: InlineCreateUserIn,
    ) -> InlineCreateUserResponse:
        """Create a destination user inline from the preflight modal.

        Idempotent on (destination_server_id, username) collision:
        returns the existing user with ``was_newly_created=false``.
        Admin creation requires ``acknowledgement=true`` (validated
        on the input model).

        Plex destinations are blocked at the picker but enforced here
        too: returns 400 with a pointer to the Plex Home invite flow."""
        from server import server_registry
        from services.adapters import UserPolicy
        from services.restorer_adapter import _normalise_dest_role
        try:
            connection = server_registry.connect_registered_server(
                body.destination_server_id, log,
            )
        except ValueError:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"Destination server {body.destination_server_id!r} "
                    f"is not registered."
                ),
            )
        except ConnectionError as exc:
            raise HTTPException(
                status_code=502,
                detail=(
                    f"Could not connect to destination "
                    f"{body.destination_server_id!r}: {exc}"
                ),
            )
        if (connection.service_type or "").lower() == "plex":
            raise HTTPException(
                status_code=400,
                detail=(
                    "Plex destinations require the Plex Home invite "
                    "flow for user creation. Open plex.tv > Manage "
                    "Library Access and invite the user, then re-run "
                    "the preflight."
                ),
            )
        # Idempotency check: look up by username in the destination's
        # current roster.
        try:
            existing = connection.adapter.list_users() or []
        except Exception:
            existing = []
        normalised = body.username.strip().lower()
        for u in existing:
            if (u.username or "").strip().lower() == normalised:
                return InlineCreateUserResponse(
                    user=DestUserOptionOut(
                        backend_user_id=u.backend_user_id or "",
                        username=u.username,
                        role=_normalise_dest_role(u, connection.service_type),
                        is_tombstoned=False,
                    ),
                    was_newly_created=False,
                )
        # Create via the adapter. UserPolicy maps the end user's
        # role pick + acknowledgement into the backend's user-policy
        # shape; adapters that don't expose create_user surface as 501.
        policy = UserPolicy(is_administrator=(body.role == "admin"))
        try:
            spec = connection.adapter.create_user(
                username=body.username.strip(),
                password=body.initial_password or "",
                is_admin=(body.role == "admin"),
                policy=policy,
            )
        except NotImplementedError:
            raise HTTPException(
                status_code=501,
                detail=(
                    f"Destination backend {connection.service_type!r} "
                    f"does not support inline user creation."
                ),
            )
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail=(
                    f"Destination user creation failed: {exc}. Check "
                    f"the destination's user-creation rules (allowed "
                    f"characters, password policy) and try again."
                ),
            )
        if spec is None:
            raise HTTPException(
                status_code=502,
                detail="Destination user creation returned no UserSpec.",
            )
        return InlineCreateUserResponse(
            user=DestUserOptionOut(
                backend_user_id=spec.backend_user_id or "",
                username=spec.username,
                role=_normalise_dest_role(spec, connection.service_type),
                is_tombstoned=False,
            ),
            was_newly_created=True,
        )

    @app.patch("/api/schedules/{schedule_id}/resolutions")
    def patch_schedule_resolutions(
        schedule_id: str, body: ScheduleResolutionsPatchIn,
    ) -> Dict[str, Any]:
        """Update one schedule's stored cross-platform resolutions
        without touching any other schedule field. End user uses this
        from the schedule-row Resolution Editor surface.

        Returns the updated schedule row. 404 if the schedule
        doesn't exist."""
        existing = next(
            (s for s in persistence.load_schedules() if s.get("id") == schedule_id),
            None,
        )
        if existing is None:
            raise HTTPException(
                status_code=404,
                detail=f"No schedule with id {schedule_id!r}.",
            )
        existing["cross_platform_resolutions"] = {
            dest_id: ack.model_dump()
            for dest_id, ack in body.resolutions.items()
        }
        return persistence.upsert_schedule(existing)

    # ── Playlist Management (Plan[PLAYLIST-MANAGEMENT]-2026-05-16) ──
    #
    # Seven endpoints under /api/playlist-mgmt/* back the Run Jobs
    # > Playlist Management sub-tab. Read paths consult the cache when
    # fresh; copy + refresh paths always hit live and write through.
    # All server_id values carry the prefixed UID format from
    # Plan[SERVER-UID-IDENTITY]; Pydantic validators reject malformed
    # values at the API boundary.

    @app.get("/api/playlist-mgmt/users")
    def get_playlist_mgmt_users(
        server_id: str,
        force_refresh: bool = False,
    ) -> Dict[str, Any]:
        """Return the user roster for one server. Used by both column
        pickers (source + destination) in the Playlist Management UI.

        2026-05-16 rewrite (end user request): cache-first. Reads from
        the local ``managed_users`` table by default and applies the
        same share-state filter the Run Job picker uses at
        ``frontend/src/components/JobFormPanel.tsx:96`` — owners always
        shown; managed users only when ``active_share != False``. Hits
        Plex live only when (a) the end user passed ``force_refresh=true``
        or (b) the DB is empty (cold-start recovery).

        Why cache-first: the prior live-every-call path made the
        Playlist Management panel hit Plex's user-list on every open +
        every server pick. The local DB already has everything the UI
        needs (username, role/kind, has_token, app_user_uuid,
        active_share) and is populated by the same
        ``sync_managed_users_from_live`` helper the Servers tab runs on
        Refresh / Add. So the panel can be near-instant on every load
        as long as the end user has refreshed the server at least once.
        """
        from server import server_registry, media_db
        server_row = server_registry.get_server_by_id(
            server_id, include_token=False,
        )
        if server_row is None:
            raise HTTPException(
                status_code=404,
                detail=f"Server {server_id!r} not registered.",
            )
        service_type = (server_row.get("service_type") or "plex").strip().lower()

        # Force-refresh: pull live + persist before the read. Errors are
        # caught + logged rather than raised, so the panel can still
        # render with whatever cached rows survive.
        if force_refresh:
            try:
                media_db.sync_managed_users_from_live(server_id, log)
            except Exception:
                log.exception(
                    "playlist-mgmt force-refresh sync failed for %s",
                    server_id,
                )

        rows = media_db.list_managed_users(server_id, include_hidden=False)

        # Cold-start recovery (matches JobFormPanel.fetchPickerUsers): if
        # the DB has no rows yet (e.g. the end user just added this
        # server and hasn't hit Refresh), fire one sync + re-read.
        if not rows and not force_refresh:
            try:
                media_db.sync_managed_users_from_live(server_id, log)
                rows = media_db.list_managed_users(
                    server_id, include_hidden=False,
                )
            except Exception:
                log.exception(
                    "playlist-mgmt cold-start sync failed for %s",
                    server_id,
                )

        # Share-state filter — owners are always retained (admin token
        # covers them regardless of the per-user share check); managed
        # users are dropped when active_share is explicitly False (the
        # end user un-shared them on plex.tv). active_share is None on
        # rows the share-state refresher hasn't touched yet; we keep
        # those visible rather than hide on a missing signal.
        transferable = [
            r for r in rows
            if (r.get("kind") or "").lower() == "owner"
            or r.get("active_share") is not False
        ]

        return {
            "server_id": server_id,
            "service_type": service_type,
            "users": [
                {
                    "backend_user_id": r.get("backend_user_id") or "",
                    "username": r.get("username") or "",
                    "role": _playlist_mgmt_role_from_kind(
                        r.get("kind"), service_type,
                    ),
                    "has_token": bool(r.get("has_token")),
                    "app_user_uuid": r.get("app_user_uuid"),
                }
                for r in transferable
            ],
        }

    @app.get("/api/playlist-mgmt/playlists")
    def get_playlist_mgmt_playlists(
        server_id: str,
        user_id: str,
        force_refresh: bool = False,
    ) -> Dict[str, Any]:
        """List one user's playlists. Cache-aware unless
        ``force_refresh=true``."""
        from services import playlist_copy
        try:
            result = playlist_copy.list_user_playlists(
                server_id=server_id,
                user_id=user_id,
                force_refresh=bool(force_refresh),
            )
        except playlist_copy.PlaylistCopyError as exc:
            raise HTTPException(
                status_code=exc.http_status,
                detail={"code": exc.code, "message": str(exc)},
            )
        return {
            "server_id": server_id,
            "user_id": user_id,
            "from_cache": result.from_cache,
            "fetched_at": result.fetched_at,
            "playlists": [
                PlaylistSpecOut(
                    playlist_id=str(r["playlist_id"]),
                    name=str(r["name"]),
                    item_count=int(r.get("item_count") or 0),
                    is_smart=bool(r.get("is_smart") or False),
                ).model_dump()
                for r in result.playlists
            ],
        }

    @app.get(
        "/api/playlist-mgmt/playlist-detail",
        response_model=PlaylistDetail,
    )
    def get_playlist_mgmt_playlist_detail(
        server_id: str,
        user_id: str,
        playlist_id: str,
        force_refresh: bool = False,
    ) -> PlaylistDetail:
        """Return one playlist's full item list. Cache-aware unless
        ``force_refresh=true``."""
        from services import playlist_copy
        try:
            data = playlist_copy.get_playlist_detail(
                server_id=server_id,
                user_id=user_id,
                playlist_id=playlist_id,
                force_refresh=bool(force_refresh),
            )
        except playlist_copy.PlaylistCopyError as exc:
            raise HTTPException(
                status_code=exc.http_status,
                detail={"code": exc.code, "message": str(exc)},
            )
        return PlaylistDetail(
            playlist_id=str(data["playlist_id"]),
            name=str(data["name"]),
            is_smart=bool(data.get("is_smart") or False),
            items=[
                PlaylistItemOut(
                    title=str(it.get("title") or ""),
                    guids=list(it.get("guids") or []),
                    type=str(it.get("type") or ""),
                    duration_ms=it.get("duration_ms"),
                )
                for it in (data.get("items") or [])
            ],
            fetched_at=float(data.get("fetched_at") or 0.0),
            from_cache=bool(data.get("from_cache") or False),
        )

    @app.post(
        "/api/playlist-mgmt/copy",
        response_model=PlaylistCopyResult,
    )
    def post_playlist_mgmt_copy(body: PlaylistCopyIn) -> PlaylistCopyResult:
        """Copy one playlist from source -> destination.

        NOTE: synchronous path. Kept for back-compat / scripted callers
        who want the result in the response body. The UI now uses the
        async ``/copy-job`` route below so deploys survive page reload
        and stack on the active-deploys panel."""
        from services import playlist_copy
        try:
            result = playlist_copy.copy_playlist(
                source_server_id=body.source_server_id,
                source_user_id=body.source_user_id,
                source_playlist_id=body.source_playlist_id,
                dest_server_id=body.dest_server_id,
                dest_user_id=body.dest_user_id,
                dest_playlist_name=body.dest_playlist_name,
            )
        except playlist_copy.PlaylistCopyError as exc:
            raise HTTPException(
                status_code=exc.http_status,
                detail={"code": exc.code, "message": str(exc)},
            )
        return PlaylistCopyResult(
            success=bool(result.get("success")),
            new_playlist_id=result.get("new_playlist_id"),
            items_written=int(result.get("items_written") or 0),
            items_skipped_no_match=int(result.get("items_skipped_no_match") or 0),
            items_failed=int(result.get("items_failed") or 0),
            errors=list(result.get("errors") or []),
            elapsed_seconds=float(result.get("elapsed_seconds") or 0.0),
        )

    @app.post("/api/playlist-mgmt/copy-job")
    def post_playlist_mgmt_copy_job(body: PlaylistCopyIn) -> Dict[str, Any]:
        """Async path: enqueue the playlist copy as a job and return
        the ``job_id`` immediately. 2026-05-17 end user request — Deploy
        on the Playlist Management surface should:
          * Persist across page reload (job state lives on the server).
          * Support multi-deploy via N parallel queue entries.
          * Surface progress / result via the existing job-status API.
          * Allow clone-deploy by submitting the same body again.

        Body shape matches the synchronous /copy route. Response is
        ``{job_id: str, state: str}`` — the panel then polls
        /api/playlist-mgmt/copy-jobs (or subscribes via the existing WS)
        for progress + result."""
        rec = get_queue().submit_playlist_copy({
            "source_server_id": body.source_server_id,
            "source_user_id": body.source_user_id,
            "source_playlist_id": body.source_playlist_id,
            "dest_server_id": body.dest_server_id,
            "dest_user_id": body.dest_user_id,
            "dest_playlist_name": body.dest_playlist_name,
        })
        return {"job_id": rec.job_id, "state": rec.state}

    @app.get("/api/playlist-mgmt/copy-jobs")
    def get_playlist_mgmt_copy_jobs() -> Dict[str, Any]:
        """List every playlist_copy job currently known to the queue
        (active + queued + recent history). Drives the Playlist Mgmt
        active-deploys panel: on mount the frontend fetches this to
        repopulate the list across page reload.

        Returns ``{jobs: [{job_id, state, queued_at, started_at,
        finished_at, error, summary, params}]}`` — params let the
        frontend render "Clone deploy" without keeping local memory of
        what was originally submitted."""
        q = get_queue()
        out: List[Dict[str, Any]] = []
        cur = q.current()
        if cur is not None and cur.mode == "playlist_copy":
            out.append(_playlist_copy_job_to_dict(cur))
        for rec in q.pending():
            if rec.mode == "playlist_copy" and (cur is None or rec.job_id != cur.job_id):
                out.append(_playlist_copy_job_to_dict(rec))
        for rec in q.history():
            if rec.mode == "playlist_copy":
                out.append(_playlist_copy_job_to_dict(rec))
        # Dedupe by job_id (current can appear in both current() and
        # history() during the transition window). Newer-first ordering
        # comes from the iteration order above (active → queued →
        # history) since history is appended chronologically.
        seen: set = set()
        deduped: List[Dict[str, Any]] = []
        for j in out:
            jid = j.get("job_id")
            if jid in seen:
                continue
            seen.add(jid)
            deduped.append(j)
        return {"jobs": deduped}

    @app.post(
        "/api/playlist-mgmt/cache/refresh",
        response_model=PlaylistCacheRefreshResult,
    )
    def post_playlist_mgmt_cache_refresh(
        body: PlaylistCacheRefreshIn,
    ) -> PlaylistCacheRefreshResult:
        """Force-refresh one user's playlist cache (per-user path).
        ``user_id`` must be set; the bulk path uses /refresh-server."""
        from services import playlist_copy
        if not (body.user_id or "").strip():
            raise HTTPException(
                status_code=422,
                detail=(
                    "user_id is required for /cache/refresh; "
                    "use /cache/refresh-server for the per-server bulk path."
                ),
            )
        row = playlist_copy.refresh_user_cache(
            server_id=body.server_id, user_id=body.user_id,
        )
        return PlaylistCacheRefreshResult(
            server_id=str(row["server_id"]),
            user_id=row.get("user_id"),
            refreshed_at=float(row.get("refreshed_at") or 0.0),
            playlists_count=int(row.get("playlists_count") or 0),
            items_count=int(row.get("items_count") or 0),
            duration_ms=int(row.get("duration_ms") or 0),
            error=row.get("error"),
        )

    @app.post("/api/playlist-mgmt/cache/refresh-server")
    def post_playlist_mgmt_cache_refresh_server(
        body: PlaylistCacheRefreshIn,
        source: str = "api",
    ) -> Dict[str, Any]:
        """Per-server bulk refresh: re-fetch every user's playlists on
        the given server. Returns a list of per-user rows plus a final
        aggregate row with ``user_id=None``.

        ``source`` is an optional free-form tag (query param) the caller
        can set to attribute the trigger surface in the audit log.
        Defaults to ``"api"`` for raw-call attribution; the UI passes
        ``"servers-refresh"`` or ``"playlist-mgmt-panel"`` so the
        end user can grep ``playlist_cache.log`` by trigger."""
        from services import playlist_copy
        rows = playlist_copy.refresh_server_cache(
            server_id=body.server_id, source=source,
        )
        return {
            "server_id": body.server_id,
            "results": [
                PlaylistCacheRefreshResult(
                    server_id=str(r["server_id"]),
                    user_id=r.get("user_id"),
                    refreshed_at=float(r.get("refreshed_at") or 0.0),
                    playlists_count=int(r.get("playlists_count") or 0),
                    items_count=int(r.get("items_count") or 0),
                    duration_ms=int(r.get("duration_ms") or 0),
                    error=r.get("error"),
                ).model_dump()
                for r in rows
            ],
        }

    @app.get("/api/playlist-mgmt/cache/status")
    def get_playlist_mgmt_cache_status(server_id: str) -> Dict[str, Any]:
        """Return per-user freshness rows for every user on a server.
        Drives the "fresh / stale / never refreshed" badges in the
        Playlist Management user picker."""
        from server import playlist_cache_db, server_registry
        from services.tunables import (
            playlist_cache_snapshot_threshold_seconds,
        )
        try:
            conn = server_registry.connect_registered_server(server_id, log)
        except ValueError:
            raise HTTPException(
                status_code=404, detail=f"Server {server_id!r} not registered.",
            )
        except ConnectionError as exc:
            raise HTTPException(
                status_code=502, detail=f"Server {server_id!r} unreachable: {exc}",
            )
        try:
            users = conn.adapter.list_users() or []
        except Exception as exc:
            raise HTTPException(
                status_code=502, detail=f"list_users failed: {exc}",
            )
        threshold = float(playlist_cache_snapshot_threshold_seconds())
        now = time.time()
        rows: List[Dict[str, Any]] = []
        for u in users:
            uid = u.backend_user_id or u.username
            if not uid:
                continue
            marker = playlist_cache_db.get_refresh_marker(server_id, uid)
            cached_count = len(
                playlist_cache_db.list_cached_playlists(server_id, uid)
            )
            if marker is None:
                rows.append(CacheStatus(
                    server_id=server_id,
                    user_id=uid,
                    last_refreshed_at=0.0,
                    playlists_count=cached_count,
                    age_seconds=0.0,
                    is_stale=True,
                ).model_dump())
                continue
            age = now - float(marker["last_refreshed_at"])
            rows.append(CacheStatus(
                server_id=server_id,
                user_id=uid,
                last_refreshed_at=float(marker["last_refreshed_at"]),
                playlists_count=cached_count,
                age_seconds=float(age),
                is_stale=bool(age > threshold or marker.get("error")),
            ).model_dump())
        return {"server_id": server_id, "rows": rows}

    @app.post("/api/job/stop")
    def post_job_stop(hard: bool = False) -> Dict[str, Any]:
        """
        Ask the running job to wind down. Returns 409 if no job is
        currently running.

        Query params:
          * ``hard=true`` (v0.12.1) - force the engine off by tearing
            down the shared HTTP session in addition to setting the
            stop flag. Use when the soft Stop has been pending too
            long and the end user just wants the worker free.
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

    # ── Application-level log surfaces (Settings > Application Logs) ───
    #
    # The per-run job logs surfaced by /api/logs above are now grouped
    # per-server under Servers > Logs. Settings > Logs keeps the
    # application-level audit trail: db access, future auth events,
    # future network events. Today only db_access.log writes outside
    # of a run; the other categories are scaffolded so the UI is in
    # place when their log writers ship.

    @app.get("/api/logs/app/{category}")
    def read_app_log(
        category: str,
        tail_bytes: int = 0,
        since: int = 0,
        backup: str = "",
        _admin: Dict[str, Any] = Depends(_auth_router_module.require_role("admin")),
    ) -> Dict[str, Any]:
        """
        Read the application-level log file for ``category``. Two
        modes, selected by the presence of ``since``:

        * ``since=0`` (default): return the last ``tail_bytes`` of the
          file. Used by the panel's initial render and by Reload.
        * ``since=<offset>``: return bytes from ``offset`` to the
          file's current end. Used by the panel's 2-second poll for
          incremental live tail.

        Optional ``backup`` query parameter switches the read target
        from the active file to a specific rotated backup
        (``db_access.log.1`` etc.). When set, polling is meaningless
        (the file does not grow); the endpoint always reads the tail
        and ignores ``since``.

        Either mode returns:

        * ``content``        - the bytes read (UTF-8 decoded)
        * ``size_bytes``     - total size of the file on disk
        * ``next_offset``    - cursor for the next poll
        * ``head_omitted``   - True when the initial-tail path
                               returned only the last ``tail_bytes``
                               of a larger file. Older bytes are
                               still in the same file; bump
                               ``tail_bytes`` to fetch more. Distinct
                               from ``rotated_during_poll``: this is
                               normal-for-large-file, not a discontinuity.
        * ``rotated_during_poll`` - True when the polling cursor
                               (``since > 0``) landed past the file's
                               current end. Means logrotate fired
                               between polls; older bytes are GONE
                               from this file (look in the
                               ``backups`` list).
        * ``backups``        - list of rotated backup files for this
                               category, newest first. Each entry
                               carries ``filename`` and ``size_bytes``.
                               Empty when no rotation has fired yet.

        Categories:

        * ``db-access``: ``server_data/db_access.log`` (when no run is
          active; otherwise that file may be empty and the audit lines
          live inside the active run's log dir).
        * ``auth`` / ``network`` / ``debug``: scaffolded but no
          backing log writer ships today.

        Admin-gated. ``tail_bytes=0`` (default) resolves to the
        end user-tuned ``log_read_max_bytes`` setting (default 16 MB),
        so small log files load fully without the UI flashing a
        misleading "tail-only" banner. Explicit non-zero values
        clamp to [1024, log_read_max_bytes].
        """
        from pathlib import Path
        from server.persistence import get_data_dir, load_settings

        settings = load_settings() or {}
        # End user-tuned read cap (also used by the per-run log
        # viewer; default 16 MB). Same knob, same behaviour across
        # log surfaces.
        try:
            max_cap = max(1024, int(settings.get("log_read_max_bytes") or 16 * 1024 * 1024))
        except (TypeError, ValueError):
            max_cap = 16 * 1024 * 1024
        try:
            requested = int(tail_bytes) if tail_bytes else max_cap
            clamped = max(1024, min(requested, max_cap))
        except (TypeError, ValueError):
            clamped = max_cap
        try:
            since_offset = max(0, int(since))
        except (TypeError, ValueError):
            since_offset = 0

        category_to_filename: Dict[str, str] = {
            "db-access": "db_access.log",
            # 2026-05-16 (end user request): playlist-cache refresh
            # audit trail. Written by services.playlist_cache_log on
            # every refresh attempt (per-user + bulk-server).
            "playlist-cache": "playlist_cache.log",
        }
        placeholder_categories = ("auth", "network", "debug")

        if category in placeholder_categories:
            return {
                "category": category,
                "path": "",
                "size_bytes": 0,
                "next_offset": 0,
                "head_omitted": False,
                "rotated_during_poll": False,
                "content": "",
                "backups": [],
                "note": (
                    f"The '{category}' application-log category is scaffolded "
                    "but no writer ships in this build. Operators who need "
                    "this surface filed should request the matching log "
                    "writer in a follow-up scope."
                ),
            }

        filename = category_to_filename.get(category)
        if filename is None:
            raise HTTPException(
                status_code=404,
                detail=f"Unknown application-log category {category!r}.",
            )

        # Build the rotated-backup list. RotatingFileHandler names them
        # ``<base>.1``, ``<base>.2``, ... with .1 being the most recent
        # rotation. Sort by the numeric suffix so the picker shows
        # newest first.
        data_dir = get_data_dir()
        backups_list: List[Dict[str, Any]] = []
        try:
            for child in data_dir.iterdir():
                if not child.is_file():
                    continue
                name = child.name
                if not name.startswith(filename + "."):
                    continue
                suffix = name[len(filename) + 1:]
                if not suffix.isdigit():
                    continue
                try:
                    sz = child.stat().st_size
                except OSError:
                    sz = 0
                backups_list.append({
                    "filename": name,
                    "size_bytes": int(sz),
                    "suffix": int(suffix),
                })
        except OSError:
            pass
        backups_list.sort(key=lambda b: b["suffix"])
        # Strip the internal sort key before returning.
        backups_response = [
            {"filename": b["filename"], "size_bytes": b["size_bytes"]}
            for b in backups_list
        ]

        # Resolve the read target. If ``backup`` is set, point at that
        # specific rotated file; otherwise the active log. Bounds-check
        # the backup name against the allowlist we just built so the
        # end user can't slip a path traversal through the parameter.
        if backup:
            allowed = {b["filename"] for b in backups_list}
            if backup not in allowed:
                raise HTTPException(
                    status_code=404,
                    detail=(
                        f"Backup {backup!r} not found for category "
                        f"{category!r}."
                    ),
                )
            path = data_dir / backup
            # Polling is meaningless on an inactive backup; reset
            # since_offset so the read always returns the tail.
            since_offset = 0
        else:
            path = data_dir / filename

        if not path.is_file():
            return {
                "category": category,
                "path": str(path),
                "size_bytes": 0,
                "next_offset": 0,
                "head_omitted": False,
                "rotated_during_poll": False,
                "content": "",
                "backups": backups_response,
                "note": "Log file has not been written yet on this host.",
            }
        try:
            size = path.stat().st_size
        except OSError:
            size = 0

        head_omitted = False
        rotated_during_poll = False
        try:
            with path.open("rb") as fh:
                if since_offset > 0:
                    if since_offset > size:
                        # File shrank between polls = rotation fired.
                        # Restart from the tail and flag the
                        # discontinuity so the UI shows the right
                        # banner.
                        rotated_during_poll = True
                        start = max(0, size - clamped)
                        fh.seek(start)
                        if start > 0:
                            fh.readline()
                    else:
                        fh.seek(since_offset)
                else:
                    # Initial / Reload path. Show only the tail when
                    # the file is bigger than the read cap; flag
                    # head_omitted so the UI can label what the
                    # end user is seeing without conflating it with
                    # an actual rotation.
                    if size > clamped:
                        fh.seek(size - clamped)
                        fh.readline()
                        head_omitted = True
                content = fh.read().decode("utf-8", errors="replace")
        except OSError as exc:
            raise HTTPException(
                status_code=500,
                detail=f"Could not read application log: {exc}",
            )
        return {
            "category": category,
            "path": str(path),
            "size_bytes": int(size),
            "next_offset": int(size),
            "head_omitted": head_omitted,
            "rotated_during_poll": rotated_during_poll,
            "content": content,
            "backups": backups_response,
            "note": "",
        }

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
        in-browser viewer's 16 MB live-tail cap so an end user can
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
        sweep; the end user can retry to mop up anything that was
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
        in ``.plexbackup.json``) are left alone - the end user may
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

    @app.get("/api/snapshots/{snapshot_id}/users")
    def list_snapshot_users(snapshot_id: str) -> Dict[str, Any]:
        """
        Return the user list captured inside a snapshot ``.db`` file.

        Reads the ``snapshot_users`` table from the .db at the registry
        row's ``file_path``. Each row is shaped to mirror the
        ``ServerUser`` payload the destination's ``/api/servers/{id}/users``
        endpoint returns so the frontend can intersect both lists without
        a translation step:

            { "users": [ { "kind", "plex_id", "raw_name", "display_name" }, ... ] }

        ``kind`` is derived from the table's ``is_owner`` flag.
        ``plex_id`` mirrors ``user_handle`` (the canonical join key -
        owner email for owner rows, managed-user username for the
        rest). The Restore form uses this to render the intersection
        picker (snapshot ∩ destination) with a "no destination user"
        badge for rows that only exist on one side.

        Returns 404 / 410 for missing row / missing .db file (same
        contract as the download endpoints below) and 500 with a
        sanitised detail on any other failure.
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
                        "still exists but the file behind it cannot be found."
                    ),
                )
            import sqlite3
            uri = f"file:{db_path}?mode=ro"
            conn = sqlite3.connect(uri, uri=True, timeout=10.0)
            conn.row_factory = sqlite3.Row
            try:
                try:
                    rows = conn.execute(
                        "SELECT user_handle, display_name, is_owner "
                        "FROM snapshot_users ORDER BY is_owner DESC, user_handle ASC"
                    ).fetchall()
                except sqlite3.OperationalError:
                    # Older snapshot files (pre-snapshot_users) - no
                    # table to read. Return an empty list rather than
                    # raise; the UI handles the empty case as
                    # "user filter unavailable for this snapshot."
                    rows = []
            finally:
                conn.close()
            users: List[Dict[str, Any]] = []
            for r in rows:
                handle = str(r["user_handle"] or "")
                display = str(r["display_name"] or "")
                is_owner = bool(r["is_owner"])
                users.append({
                    "kind": "owner" if is_owner else "managed",
                    "plex_id": handle,
                    "raw_name": handle,
                    "display_name": display or handle,
                })
            return {"users": users, "error": None}
        except HTTPException:
            raise
        except Exception as exc:
            logging.getLogger("plexmigrate.server.jobs").exception(
                "list_snapshot_users failed for %r", snapshot_id,
            )
            raise HTTPException(
                status_code=500,
                detail="Could not read snapshot users - see the run log for details.",
            )

    @app.get("/api/snapshots/{snapshot_id}/download-db")
    def download_snapshot_db(snapshot_id: str) -> FileResponse:
        """
        Stream the per-snapshot ``.db`` file directly. No rendering,
        no caching - the .db lives on disk as the canonical artifact
        and the download is a flat FileResponse over the binary.

        Companion to ``/download`` (which renders + streams JSON).
        End users get both: ``.db`` for restore-into-another-install,
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
            # the run log above for the end user to inspect.
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
        live in-memory render so the end user still gets their file.

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

            # Sidecar materialisation failed but the end user still wants
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

    @app.post("/api/snapshots/server/{server_id}/merge-orphans")
    def merge_orphan_snapshots(
        server_id: str,
        _admin: Dict[str, Any] = Depends(_auth_router_module.require_role("admin")),
    ) -> Dict[str, Any]:
        """
        Reassign orphan snapshot rows (rows whose stored ``server_name``
        matches this server but whose ``server_id`` points at a
        no-longer-registered server) into this server. The Exports
        panel already merges them at display time; this endpoint
        rewrites the underlying rows so the migration is permanent.

        Admin-gated (no db_admin step) because the operation is
        non-destructive: no files are deleted, only the foreign-key
        column is rewritten. The Exports panel state ends up the same
        either way.
        """
        from server import server_registry, snapshot_registry
        row = server_registry.get_server_by_id(server_id, include_token=False)
        if row is None:
            raise HTTPException(
                status_code=404,
                detail=f"No server with id {server_id!r}.",
            )
        return snapshot_registry.reassign_orphan_snapshots(
            target_server_id=server_id,
            target_server_name=row.get("name") or "",
        )

    # ── Databases viewer (Plan[DATABASES-VIEWER]-2026-05-16) ─────────
    #
    # Root-admin only at the route layer. The engine in
    # server.db_browser opens every connection in true read-only mode
    # (file:<path>?mode=ro) + sets PRAGMA query_only=1 as belt-and-
    # braces, so even a logic bug in a future call site can't fire an
    # UPDATE. Encrypted columns, bcrypt hashes, and refresh tokens are
    # substituted with redacted placeholders by db_browser._format_cell
    # before any row reaches the wire.
    #
    # Every successful fetch logs to db_access.log with caller +
    # db_type + instance_id + table + row_count for forensic recovery.

    @app.get("/api/db-browser/databases")
    def db_browser_list_databases(
        _admin: Dict[str, Any] = Depends(
            _auth_router_module.require_role("root_admin"),
        ),
    ) -> Dict[str, Any]:
        """Return the catalogue of database types the viewer knows
        about. Each entry carries display_name, description,
        cardinality (single|many), and sensitivity (high|medium|low).
        """
        from server import db_browser
        return {"databases": db_browser.list_database_types()}

    @app.get("/api/db-browser/{db_type}/instances")
    def db_browser_list_instances(
        db_type: str,
        _admin: Dict[str, Any] = Depends(
            _auth_router_module.require_role("root_admin"),
        ),
    ) -> Dict[str, Any]:
        """List instances for one database type. Single-cardinality
        types return one element when the file exists, zero when it
        does not. Many-cardinality returns every registered snapshot
        file."""
        from server import db_browser
        try:
            return {"instances": db_browser.list_instances(db_type)}
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc))

    @app.get("/api/db-browser/{db_type}/{instance_id}/metadata")
    def db_browser_metadata(
        db_type: str,
        instance_id: str,
        _admin: Dict[str, Any] = Depends(
            _auth_router_module.require_role("root_admin"),
        ),
    ) -> Dict[str, Any]:
        """File path, size, schema_version (when present), modified
        timestamp, WAL-sidecar presence flag."""
        from server import db_browser
        try:
            return db_browser.get_database_metadata(db_type, instance_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc))

    @app.get("/api/db-browser/{db_type}/{instance_id}/schema")
    def db_browser_schema(
        db_type: str,
        instance_id: str,
        _admin: Dict[str, Any] = Depends(
            _auth_router_module.require_role("root_admin"),
        ),
    ) -> Dict[str, Any]:
        """Full table catalogue: per-table row count, columns, indexes,
        and the literal CREATE TABLE DDL from sqlite_master."""
        from server import db_browser
        try:
            schema = db_browser.get_schema(db_type, instance_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        # Audit logging - best-effort, never blocks the read.
        try:
            from services import db_access_log
            db_access_log.log_event(
                "DB_BROWSER_SCHEMA caller=%r db_type=%r instance_id=%r "
                "tables=%d",
                (_admin.get("username") or "?"),
                db_type, instance_id, len(schema.get("tables") or []),
            )
        except Exception:
            pass
        return schema

    @app.get("/api/db-browser/{db_type}/{instance_id}/tables/{table}/rows")
    def db_browser_rows(
        db_type: str,
        instance_id: str,
        table: str,
        limit: int = 50,
        offset: int = 0,
        filter_column: Optional[str] = None,
        filter_value: Optional[str] = None,
        _admin: Dict[str, Any] = Depends(
            _auth_router_module.require_role("root_admin"),
        ),
    ) -> Dict[str, Any]:
        """Paginated table read with security-aware cell formatting.
        Limits are clamped server-side; filter_column is validated
        against the live PRAGMA table_info; filter_value is bound as
        a parameter."""
        from server import db_browser
        try:
            page = db_browser.get_rows(
                db_type, instance_id, table,
                limit=limit, offset=offset,
                filter_column=filter_column,
                filter_value=filter_value,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        try:
            from services import db_access_log
            db_access_log.log_event(
                "DB_BROWSER_ROWS caller=%r db_type=%r instance_id=%r "
                "table=%r limit=%d offset=%d returned=%d total=%d",
                (_admin.get("username") or "?"),
                db_type, instance_id, table,
                page["limit"], page["offset"],
                len(page["rows"]), page["total_rows"],
            )
        except Exception:
            pass
        return page

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
