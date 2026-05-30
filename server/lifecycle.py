"""Startup / shutdown lifecycle handlers for the FastAPI app.

Extracted from ``server.app`` as part of the Phase-3a organizational
decomposition. The handlers are plain async functions; ``create_app``
wires them in via ``add_event_handler``. Behaviour is preserved
byte-for-byte from the prior in-app.py implementations.

NOTE: this module intentionally does NOT migrate to the lifespan
context-manager API. The codebase has deprecation noise around
``on_event`` but the migration is a behaviour change (different
exception semantics around startup failures) and is out of scope for
this organizational split.
"""

from __future__ import annotations

import logging
from typing import Any

from server import persistence, server_registry
from server.dev_console_sync import get_sync_worker as get_dev_console_sync_worker
from server.dev_console_ws import get_dev_console_manager
from server.jobs import get_queue
from server.schedules import get_scheduler
from server.ws import get_manager


log = logging.getLogger("plexmigrate.server")


def _install_app_log_handler() -> None:
    """Attach a rotating file handler to the ``plexmigrate`` root logger
    pointing at ``server_data/app.log``. Every plexmigrate.* descendant
    logger (server, services, etc.) propagates to this handler, so the
    file ends up with the same lines that also land on stdout /
    uvicorn's terminal.

    Idempotent: if the handler is already attached (re-import,
    test-harness re-init, hot reload), the duplicate is skipped so log
    lines are not written N times.

    Sized 10 MB per file, 5 backups, UTF-8 encoded. Matches the
    rotation profile used by db_access.log / playlist_cache.log /
    sync.log so the existing read_app_log endpoint can surface the
    rotated backups without special-casing.
    """
    import logging
    import logging.handlers as _handlers
    from pathlib import Path

    from server.persistence import get_data_dir

    app_log_path = get_data_dir() / "app.log"
    root_logger = logging.getLogger("plexmigrate")

    for h in root_logger.handlers:
        attached = getattr(h, "_pm_app_log", False)
        if attached:
            return  # already installed

    handler: logging.Handler = _handlers.RotatingFileHandler(
        str(app_log_path),
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    handler.setLevel(logging.INFO)
    handler.setFormatter(logging.Formatter(
        fmt="[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    handler._pm_app_log = True  # type: ignore[attr-defined]
    root_logger.addHandler(handler)
    # Defensive: lift the root level if it was higher than INFO (a
    # default logging.getLogger returns level=0 = inherit, which is
    # what we want, but a prior call may have set it).
    if root_logger.level == 0 or root_logger.level > logging.INFO:
        root_logger.setLevel(logging.INFO)


async def on_startup() -> None:
    # Surface the app logger under Settings > Application Logs:
    # attach a rotating file handler to the "plexmigrate" root
    # logger so every plexmigrate.* record (including
    # plexmigrate.server.user_capture, the per-user-token capture
    # diagnostics, and every other plexmigrate.server.* module)
    # writes to ``server_data/app.log``. Without this the lines
    # only go to stdout / uvicorn's terminal and there is no way
    # to find the user-capture trace after a Refresh-users click.
    # Runs BEFORE the log scrubber install so the scrubber covers
    # the new handler too.
    try:
        _install_app_log_handler()
    except Exception:  # pragma: no cover (defensive)
        log.exception("App-log handler install failed; continuing.")

    # Install the X-Plex-Token scrubber on every existing log
    # handler BEFORE any token-touching code runs. Catches
    # uvicorn's access/error loggers (which can record request
    # URLs containing the token as a query param) plus anything
    # the legacy-migration path logs below.
    try:
        from server.log_scrubber import install_on_all_handlers
        install_on_all_handlers()
    except Exception:  # pragma: no cover (defensive)
        log.exception("Log scrubber install failed; continuing.")

    # Initialise the auth database eagerly so the first
    # ``/api/auth/...`` request doesn't pay the cold-start cost.
    # Always runs - auth is mandatory.
    try:
        from server import auth_db
        auth_db.init_auth_db()
    except Exception:  # pragma: no cover (defensive)
        log.exception("Auth database init failed; continuing.")

    # Rename the default ``plex_exports`` output_dir to
    # ``snapshots`` in settings.json. Idempotent; subsequent boots
    # silent-noop.
    try:
        persistence.migrate_output_dir_setting()
    except Exception:  # pragma: no cover (defensive)
        log.exception("Snapshot-dir migration failed; continuing.")

    # Initialise the snapshot registry (server_data/snapshots.db).
    # Cheap; just creates the file + schema on first run.
    try:
        from server import snapshot_registry
        snapshot_registry.init_registry()
    except Exception:  # pragma: no cover (defensive)
        log.exception("Snapshot registry init failed; continuing.")

    # v0.12.0 / v0.15: initialise media.db BEFORE any other code
    # that calls a media_db public API. Pre-fix this block lived
    # well below the server-backfill loop, so every backfill row
    # tripped the "init_media_db() must be called before any
    # other public API" guard and logged a noisy traceback. The
    # archive-old-schema check still runs first because it's
    # SCHEMA-LEVEL (file-rename, no DB connection); the
    # downstream init opens the new connection.
    try:
        from server.app import _archive_old_media_db_if_needed
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
        from server import snapshot_registry
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

    # Stale-sidecar housekeeping. Generated ``.plexexport.json``
    # sidecars live next to their snapshot ``.db`` and are
    # re-materialisable on demand, so we don't need to hold them
    # on disk after the download window. The TTL is end
    # user-tunable (default 5 min); ``0`` disables.
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


    # Playlist Management cache DB. Separate SQLite store from
    # media.db so the cache + its TTL retention live
    # independently. Idempotent + best-effort.
    try:
        from server import playlist_cache_db
        playlist_cache_db.init_playlist_cache_db()
    except Exception:  # pragma: no cover (defensive)
        log.exception(
            "playlist_cache_db init failed; Playlist Management "
            "will run without a cache (live API every call)."
        )

    # Smart Playlist Migration record DB. Holds one row per
    # migrated smart playlist. Idempotent + best-effort.
    try:
        from server import smart_playlist_db
        smart_playlist_db.init_smart_playlist_db()
    except Exception:  # pragma: no cover (defensive)
        log.exception(
            "smart_playlist_db init failed; Smart Playlist Migration "
            "results will not be persisted."
        )

    # Per-server metadata mirror DB. Separate SQLite file so a
    # corrupt mirror cannot poison media.db / playlist_cache.db.
    # An integrity_check failure raises; the catch below flips
    # engine into always-live mode for the rest of the session.
    try:
        from server import server_mirror_db
        server_mirror_db.init_server_mirror_db()
    except Exception:  # pragma: no cover (defensive)
        log.exception(
            "server_mirror_db init failed; engine will run in "
            "always-live mode (no mirror consulted). Operator can "
            "POST /api/server-mirror/invalidate to rebuild."
        )

    # Collection-children cache (Movies N+1 fix).
    # Per-server cache of collection.items() responses keyed on
    # the collection's updatedAt timestamp. First snapshot pays
    # the full /children fetch cost; subsequent snapshots skip
    # unchanged collections entirely. Cache miss is non-fatal
    # (snapshot falls back to live fetch + tries to cache again).
    try:
        from server import collection_cache_db
        collection_cache_db.init_collection_cache_db()
    except Exception:  # pragma: no cover (defensive)
        log.exception(
            "collection_cache_db init failed; snapshot_collections "
            "will run without the cache (slower per-snapshot)."
        )

    # Library auto-matcher. Persists per-(source_server,
    # source_library, dest_server) mappings so transfers work when
    # source's "Music" and dest's "Tunes" refer to the same
    # content. Auto rows are cleared on mirror sync;
    # operator-confirmed rows survive.
    try:
        from server import library_mapping_db
        library_mapping_db.init_library_mapping_db()
    except Exception:  # pragma: no cover (defensive)
        log.exception(
            "library_mapping_db init failed; the restorer will "
            "fall back to exact-name matching for libraries."
        )

    # Library-pair sync engine. Holds per-(library pair,
    # sync_type) subscriptions, observation log (per-poll snapshot
    # of view_count / rating / favorite per item per user per
    # server), and the per-write audit trail. The
    # worker reads subscriptions + observations on every poll cycle
    # and decides what to write under the configured conflict
    # policy.
    try:
        from server import sync_db
        sync_db.init_sync_db()
    except Exception:  # pragma: no cover (defensive)
        log.exception(
            "sync_db init failed; library-pair sync worker will "
            "not start until the next boot."
        )

    # Sync polling worker. Daemon thread; wakes every 30s and
    # processes any subscription whose poll interval has
    # elapsed. Defaults dry_run=True per subscription so a fresh
    # subscription declares intent + logs writes without firing
    # them; the operator confirms via the UI's "Enable real writes"
    # toggle after reviewing the dry-run log.
    try:
        from services.mirror_sync import sync_worker as sync_worker
        sync_worker.start()
    except Exception:  # pragma: no cover (defensive)
        log.exception(
            "sync_worker.start() failed; library-pair sync will "
            "not run this process."
        )

    # User-activity sweeper. Daemon thread; idempotent start. The
    # body of every tick short-circuits on the master tunable so
    # it costs nothing on installs where the operator hasn't
    # opted in.
    try:
        from services.user_management import activity_sweeper as user_activity_sweeper
        user_activity_sweeper.start()
    except Exception:  # pragma: no cover (defensive)
        log.exception(
            "user_activity_sweeper.start() failed; auto-tombstone "
            "sweeper will not run this process."
        )

    # Background playlist cache refresher. Default ON at 15min
    # cadence, tunable via
    # playlist_cache_background_refresh_enabled +
    # playlist_cache_background_refresh_interval_seconds. The
    # thread re-checks the enabled flag every iteration so live
    # toggling works without restart.
    try:
        from services.playlist_copy.adapter import cache_refresher as playlist_cache_refresher
        playlist_cache_refresher.start()
    except Exception:  # pragma: no cover (defensive)
        log.exception(
            "playlist_cache_refresher start failed; playlist cache "
            "will only refresh on operator action."
        )

    # Restore the persisted audit-log-enabled state into the
    # in-process cache. Default true on missing or read errors -
    # the audit trail is on by default; only an explicit
    # db_admin-gated disable via /api/settings/audit-log-toggle
    # turns it off.
    try:
        from services.run_logs import db_access as _dal
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

    # Sweep stale chained-transfer temp files
    # (``*.tmp.plexexport.json``) from the configured output dir.
    # A failed chained import deliberately leaves its payload on
    # disk for manual recovery; this clears ones old enough that
    # the end user has moved on, so sensitive payloads don't
    # linger indefinitely.
    try:
        from services.direct_transfer import engine as _dt
        _out_dir = (persistence.load_settings() or {}).get("output_dir") or "./snapshots"
        _dt.sweep_stale_tmp_exports(_out_dir)
    except Exception:  # pragma: no cover (defensive)
        log.exception("Stale tmp-export sweep failed; continuing.")

    # Migrate any legacy plex_url/plex_token in settings.json
    # into the registry as a server called "Default". Idempotent.
    try:
        server_registry.migrate_legacy_settings(log)
    except Exception:  # pragma: no cover (defensive)
        log.exception("Legacy settings migration failed; continuing.")

    # Force-migrate any plaintext rows in servers.json so the
    # file on disk is fully encrypted before uvicorn binds the
    # port. Without this the encryption pass would only run when
    # the first /api/servers request landed - leaving a brief
    # window where someone inspecting the volume could see
    # plaintext.
    try:
        count = server_registry.ensure_encrypted_at_rest()
        log.info("Registry at-rest encryption verified for %d row(s).", count)
    except Exception:  # pragma: no cover (defensive)
        log.exception("Registry encryption ensure step failed; continuing.")

    # Server-UID boot migration: upgrade any bare-UUID server
    # rows to the prefixed ``<service_type>_<uuid>`` form and
    # rewrite cross-table
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
    await get_dev_console_manager().start()
    get_dev_console_sync_worker().start()
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

    log.info("Hestia-MediaManager server started")


async def on_shutdown() -> None:
    get_scheduler().stop()
    await get_manager().stop()
    await get_dev_console_manager().stop()
    get_dev_console_sync_worker().stop()
    log.info("Hestia-MediaManager server stopped")
