"""Background daemon that periodically refreshes playlist cache for all (server, user) pairs."""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from typing import List, Optional, Tuple

from server import playlist_cache_db
from services.tunables import (
    playlist_cache_background_refresh_enabled,
    playlist_cache_background_refresh_interval_seconds,
)


log = logging.getLogger("plexmigrate.services.playlist_copy.adapter.cache_refresher")


_THREAD: Optional[threading.Thread] = None
_STOP_EVENT = threading.Event()
_LOCK = threading.Lock()


def _list_cached_pairs() -> List[Tuple[str, str]]:
    """Return the distinct (server_id, user_id) pairs currently in the
    playlist_cache. Best-effort; an empty list on any error keeps the
    refresher loop quiet."""
    try:
        conn = playlist_cache_db._require_conn()
    except Exception:
        return []
    try:
        rows = conn.execute(
            """
            SELECT DISTINCT server_id, user_id
            FROM playlist_cache
            WHERE server_id IS NOT NULL AND user_id IS NOT NULL
              AND server_id != '' AND user_id != ''
            """
        ).fetchall()
    except sqlite3.Error:
        return []
    return [(str(r[0]), str(r[1])) for r in rows]


def _job_queue_busy() -> bool:
    """True when there's a running OR queued job. The refresher
    pauses in that case so its log chatter and HTTP load don't
    contaminate the active job's run log + dashboard."""
    try:
        from server import jobs as _jobs_mod
        q = _jobs_mod.get_queue()
        return bool(q.busy())
    except Exception:
        # If the queue isn't reachable, default to NOT busy so the
        # refresher keeps running (better than silently never
        # refreshing).
        return False


def _refresh_loop() -> None:
    """Daemon loop. Sleeps the cadence, then walks every (server, user)
    pair currently in the cache and re-fetches via the adapter. Errors
    are isolated per-pair so one bad server doesn't poison the loop.
    Pauses (skips the iteration) while a job is running so the
    refresher's HTTP load + log chatter don't contaminate the
    active job."""
    log.info(
        "playlist_cache_refresher: thread started "
        "(initial interval=%ds, enabled=%s)",
        playlist_cache_background_refresh_interval_seconds(),
        playlist_cache_background_refresh_enabled(),
    )
    while not _STOP_EVENT.is_set():
        # Re-read the tunables every iteration so operator toggles
        # take effect without restart.
        interval = playlist_cache_background_refresh_interval_seconds()
        enabled = playlist_cache_background_refresh_enabled()
        if not enabled:
            # Quiet wait when disabled; check back every minute so a
            # flip-back-on lands within ~60s.
            if _STOP_EVENT.wait(60):
                break
            continue

        # Re-check busy state before this pass. The refresher's
        # connections + list_playlists calls were leaking into
        # batch-job run logs + dashboards, making it look like the
        # batch was hitting unrelated servers + users.
        if _job_queue_busy():
            log.debug(
                "playlist_cache_refresher: skipping pass — job queue busy"
            )
            if _STOP_EVENT.wait(60):
                break
            continue

        pairs = _list_cached_pairs()
        if pairs:
            log.info(
                "playlist_cache_refresher: refreshing %d (server, user) "
                "pair(s)", len(pairs),
            )
        for sid, uid in pairs:
            if _STOP_EVENT.is_set():
                break
            # Re-check busy state between pairs too — a job submitted
            # mid-pass should stop further refresh work.
            if _job_queue_busy():
                log.debug(
                    "playlist_cache_refresher: aborting pass mid-flight "
                    "— job queue went busy"
                )
                break
            try:
                # Local import to avoid an import cycle at module load
                # (playlist_copy imports playlist_cache_db, which this
                # module also imports).
                from services import playlist_copy
                playlist_copy.refresh_user_cache(
                    server_id=sid, user_id=uid,
                )
            except Exception as exc:
                log.warning(
                    "playlist_cache_refresher: refresh failed for "
                    "(server=%r, user=%r): %s",
                    sid, uid, exc,
                )
        # Sleep for the cadence, but wake on _STOP_EVENT for prompt
        # shutdown. Use wait() with timeout so we don't ignore the
        # operator's tunable changes for too long; an interval bump
        # from 1h to 5min won't take effect until the NEXT iteration
        # starts, which is acceptable.
        if _STOP_EVENT.wait(interval):
            break
    log.info("playlist_cache_refresher: thread stopping")


def start() -> None:
    """Start the daemon thread if not already running. Idempotent so
    the app startup hook can call it without checking state."""
    global _THREAD
    with _LOCK:
        if _THREAD is not None and _THREAD.is_alive():
            return
        _STOP_EVENT.clear()
        _THREAD = threading.Thread(
            target=_refresh_loop,
            name="plexmigrate-playlist-cache-refresher",
            daemon=True,
        )
        _THREAD.start()


def stop(timeout: float = 5.0) -> None:
    """Signal the thread to stop + wait for it. Best-effort; daemon
    thread will be torn down on process exit anyway."""
    global _THREAD
    with _LOCK:
        if _THREAD is None:
            return
        _STOP_EVENT.set()
        try:
            _THREAD.join(timeout=timeout)
        except Exception:
            pass
        _THREAD = None


def is_running() -> bool:
    """Test helper: report whether the thread is currently alive."""
    t = _THREAD
    return t is not None and t.is_alive()


def trigger_one_pass_for_tests() -> None:
    """Test-only hook: run one pass of the refresh logic synchronously
    in the caller's thread. Avoids race conditions in tests that need
    to assert refresh side-effects."""
    pairs = _list_cached_pairs()
    for sid, uid in pairs:
        try:
            from services import playlist_copy
            playlist_copy.refresh_user_cache(
                server_id=sid, user_id=uid,
            )
        except Exception as exc:
            log.warning(
                "trigger_one_pass_for_tests: refresh failed for "
                "(server=%r, user=%r): %s", sid, uid, exc,
            )
