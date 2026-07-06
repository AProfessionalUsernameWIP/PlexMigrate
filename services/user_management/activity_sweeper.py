"""
User-activity sweeper: background daemon that periodically probes each user's auth health
on registered servers and auto-tombstones users on N consecutive failures.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Optional

from services.user_management.activity_log import get_user_activity_logger

log = get_user_activity_logger()


# Module-global thread state. Same shape as services.mirror_sync.sync_worker.
_worker_thread: Optional[threading.Thread] = None
_worker_stop = threading.Event()
_worker_lock = threading.Lock()

# Default top-level cadence. Each server has its own
# ``user_activity_sweep_interval_hours`` and we honour that on a
# per-server cursor; this is just how often the daemon wakes up to
# check if anything is due.
_TICK_INTERVAL_SECONDS = 60


def start() -> None:
    """Start the sweeper thread. Idempotent — calling repeatedly is
    a no-op once running. Safe to call from FastAPI's startup hook
    regardless of whether the master tunable is on (the body
    short-circuits on each tick)."""
    global _worker_thread
    with _worker_lock:
        if _worker_thread is not None and _worker_thread.is_alive():
            return
        _worker_stop.clear()
        _worker_thread = threading.Thread(
            target=_run, name="user-activity-sweeper", daemon=True,
        )
        _worker_thread.start()
        log.info("user_activity_sweeper: thread started")


def stop() -> None:
    """Signal the sweeper to exit. Called on app shutdown."""
    _worker_stop.set()


def is_running() -> bool:
    """For tests + status surfaces. True iff the daemon thread is
    alive."""
    return _worker_thread is not None and _worker_thread.is_alive()


def _run() -> None:
    """Daemon main loop. Wakes every _TICK_INTERVAL_SECONDS, runs
    one tick, sleeps. Exceptions inside _tick are logged + swallowed
    so the thread keeps running across transient failures."""
    log.info("user_activity_sweeper: main loop entering")
    while not _worker_stop.is_set():
        try:
            _tick()
        except Exception:
            log.exception(
                "user_activity_sweeper: tick raised; continuing"
            )
        # Sleep in small increments so stop() takes effect quickly.
        for _ in range(_TICK_INTERVAL_SECONDS):
            if _worker_stop.is_set():
                break
            time.sleep(1)
    log.info("user_activity_sweeper: main loop exited")


def _tick() -> None:
    """Walk registered servers and run probe pass if sweep interval elapsed.
    Per-server cursor lives in server row's user_activity_last_sweep_at field."""
    try:
        from services.tunables import (
            user_activity_sweeper_enabled,
            user_activity_sweep_interval_hours,
        )
    except Exception:
        return

    if not user_activity_sweeper_enabled():
        return

    interval_seconds = max(1, int(user_activity_sweep_interval_hours()) * 3600)
    now = time.time()

    try:
        from server import server_registry
        servers = server_registry.list_servers() or []
    except Exception as exc:
        log.warning("server_registry.list_servers failed: %s", exc)
        return

    for srv in servers:
        server_id = srv.get("id") or ""
        if not server_id:
            continue
        last_sweep = srv.get("user_activity_last_sweep_at") or 0
        try:
            last_sweep = float(last_sweep or 0)
        except (TypeError, ValueError):
            last_sweep = 0
        if last_sweep and (now - last_sweep) < interval_seconds:
            continue
        try:
            _sweep_server(server_id, srv)
        except Exception:
            log.exception(
                "user_activity_sweeper: sweep for server=%s failed",
                server_id,
            )


def _sweep_server(server_id: str, server_row: Dict[str, Any]) -> None:
    """Probe every active user on one server. Record results +
    apply tombstones where the safety layers align."""
    started = time.time()
    name = server_row.get("name") or server_id

    # Pull every user this server knows about. bypass_health_filter=
    # True so the sweeper sees users currently flagged as failing
    # (we still want to keep probing them — a recovery would reset
    # the counter).
    try:
        from services.user_management.activity_filter import (
            list_active_users, record_auth_result, should_auto_tombstone,
        )
        users = list_active_users(
            server_id, bypass_health_filter=True,
        ) or []
    except Exception as exc:
        log.warning(
            "sweep server=%s name=%s: list_active_users failed: %s",
            server_id, name, exc,
        )
        return

    if not users:
        log.info(
            "sweep server=%s name=%s: no users to probe", server_id, name,
        )
        _stamp_last_sweep(server_id, server_row, started, 0, 0, 0)
        return

    # Connect once per sweep; the adapter handles its own caching.
    try:
        from server import server_registry
        conn = server_registry.connect_registered_server(server_id, log)
        adapter = conn.adapter
    except Exception as exc:
        # Server-side unreachable. Record one 'unreachable' signal per
        # user (the whole server is down; every user inherits that
        # status).
        log.warning(
            "sweep server=%s name=%s: connect failed: %s",
            server_id, name, exc,
        )
        for u in users:
            uname = u.get("username") or ""
            if not uname:
                continue
            try:
                record_auth_result(
                    server_id=server_id, username=uname,
                    result="unreachable",
                    detail=f"server connect failed: {exc}",
                )
            except Exception:
                pass
        _stamp_last_sweep(server_id, server_row, started, len(users), 0, len(users))
        return

    probed = 0
    tombstoned = 0
    errors = 0

    # Filter probe-eligible users once so the parallel pool only does
    # network work. Owner accounts (admin token = owner token) skip
    # the probe entirely; an owner-auth failure surfaces at the
    # server level via wh_servers.last_status.
    eligible = [
        u for u in users
        if (u.get("username") or "").strip()
        and (u.get("kind") or "").lower() != "owner"
    ]

    def _probe_one(u):
        if _worker_stop.is_set():
            return None
        uname = (u.get("username") or "").strip()
        try:
            return (uname, adapter.probe_user(uname), None)
        except Exception as exc:
            return (uname, "unknown", exc)

    # Probes are network round-trips with no shared state; run them
    # in parallel. Capped at 4 workers to avoid oversubscribing the
    # adapter's HTTP session pool.
    import concurrent.futures
    pool_size = max(1, min(4, len(eligible)))
    if pool_size > 1:
        with concurrent.futures.ThreadPoolExecutor(max_workers=pool_size) as pool:
            results = list(pool.map(_probe_one, eligible))
    else:
        results = [_probe_one(u) for u in eligible]

    # Sequential post-processing: record_auth_result + auto-tombstone
    # both write to the shared _DB_LOCK, so parallelizing them does
    # nothing for wall time and complicates the counters.
    for entry in results:
        if entry is None:
            log.info(
                "sweep server=%s name=%s: stop signal received "
                "mid-sweep; bailing", server_id, name,
            )
            break
        uname, result, exc = entry
        if exc is not None:
            log.warning(
                "sweep server=%s user=%s: probe raised: %s",
                server_id, uname, exc,
            )
            errors += 1
        probed += 1
        log.info(
            "sweep server=%s user=%s result=%s",
            server_id, uname, result,
        )
        try:
            record_auth_result(
                server_id=server_id, username=uname, result=result,
            )
        except Exception:
            log.exception(
                "sweep server=%s user=%s: record_auth_result failed",
                server_id, uname,
            )
            continue
        try:
            if should_auto_tombstone(server_id=server_id, username=uname):
                from server import media_db
                media_db.set_managed_user_tombstone(
                    server_id=server_id, username=uname, tombstoned=True,
                )
                tombstoned += 1
                log.warning(
                    "sweep server=%s user=%s: AUTO-TOMBSTONED "
                    "(consecutive failures crossed threshold; "
                    "matching per-trigger toggle on)",
                    server_id, uname,
                )
        except Exception:
            log.exception(
                "sweep server=%s user=%s: tombstone apply failed",
                server_id, uname,
            )

    _stamp_last_sweep(
        server_id, server_row, started, probed, tombstoned, errors,
    )


def _stamp_last_sweep(
    server_id: str,
    server_row: Dict[str, Any],
    started_at: float,
    probed: int,
    tombstoned: int,
    errors: int,
) -> None:
    """Persist the per-server cursor + summary on the registry row."""
    try:
        from server import server_registry
        finished = time.time()
        server_registry.update_server_settings(
            server_id,
            {
                "user_activity_last_sweep_at": finished,
                "user_activity_last_sweep_summary": {
                    "started_at": started_at,
                    "finished_at": finished,
                    "probed": probed,
                    "tombstoned": tombstoned,
                    "errors": errors,
                },
            },
        )
    except Exception:
        log.exception(
            "user_activity_sweeper: stamping last_sweep on %s failed",
            server_id,
        )
    log.info(
        "sweep server=%s done probed=%d tombstoned=%d errors=%d elapsed_s=%.1f",
        server_id, probed, tombstoned, errors,
        time.time() - started_at,
    )