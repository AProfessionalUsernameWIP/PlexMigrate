"""
User-activity sweeper.

Background daemon that periodically probes each user's auth health
on every registered server and persists the result via
``services.user_activity_filter.record_auth_result``. When the per-
server + per-trigger toggles align, N consecutive failures auto-
tombstone the user.

Five-layer safety check (every layer defaults OFF in production):

  1. Global tunable ``user_activity_sweeper_enabled`` is on?
  2. Per-server ``auto_tombstone_inactive_users_enabled`` is on?
     (the sweeper still probes + records signals even when this
     is off — it just doesn't apply tombstones. Recording the
     signal helps Phase B's filter even on hosts where
     auto-tombstone isn't wanted.)
  3. The user has reached the consecutive-failure threshold?
  4. The latest failure type has its per-trigger toggle on?
     (``auto_tombstone_on_auth_error`` /
      ``auto_tombstone_on_unreachable``)
  5. The probe just confirmed the failure (we don't tombstone on
     stale signals).

The actual gate composition lives in
``services.user_activity_filter.should_auto_tombstone`` so the
caller (this sweeper) doesn't need to re-check each layer.

Logger: dedicated namespace ``plexmigrate.user_activity`` with
``propagate=False`` (no bleed into job runtime.log). Lives in
``services.user_activity_log`` which Phase C ships alongside this
module.

Idempotent ``start()`` / ``stop()`` for app lifecycle hooks. The
sweeper sleeps in small increments so ``stop()`` takes effect
quickly during graceful shutdown.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Optional

from services.user_activity_log import get_user_activity_logger

log = get_user_activity_logger()


# Module-global thread state. Same shape as services.sync_worker.
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
    """Walk every registered server. For each one whose last sweep
    was at least the configured interval ago, run one probe pass.
    The per-server cursor lives in ``last_sweep_at`` on the registry
    row (added implicitly via the registry's flat-dict pattern; the
    field defaults to None on rows that have never been swept)."""
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
        from services.user_activity_filter import (
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

    for u in users:
        if _worker_stop.is_set():
            log.info(
                "sweep server=%s name=%s: stop signal received "
                "mid-sweep; bailing", server_id, name,
            )
            break
        uname = (u.get("username") or "").strip()
        if not uname:
            continue
        # Owner: skip the probe entirely. The owner-token IS the
        # admin token; an admin auth failure is a server-level
        # problem we already report via wh_servers.last_status.
        if (u.get("kind") or "").lower() == "owner":
            continue
        try:
            result = adapter.probe_user(uname)
        except Exception as exc:
            log.warning(
                "sweep server=%s user=%s: probe raised: %s",
                server_id, uname, exc,
            )
            result = "unknown"
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
        # Auto-tombstone check (consults every safety layer).
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
    """Persist the per-server cursor + summary on the registry row.
    Failure here is non-fatal (the next tick will simply pick it up
    again sooner than ideal).
    """
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
