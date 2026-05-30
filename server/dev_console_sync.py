"""
server/dev_console_sync.py - background mirror sync for the
"Server Commands" developer console.

Keeps each server's mirror database (``server/dev_console_db.py``)
fresh so the console panel can read the mirror instead of hammering
the media-server API.

* :func:`sync_server` does one full refresh of a server's mirror:
  libraries, items, the owner's per-item state, playlists, and
  collections. Pass ``users`` to also walk specific managed users'
  per-item state (the panel requests this on demand when the operator
  selects a user).
* :class:`DevConsoleSyncWorker` is a daemon thread that refreshes
  every existing mirror on the tunable cadence
  (``dev_console_mirror_sync_seconds``, per-server overridable) and
  also services explicit on-demand requests promptly.

Freeze rule: a server with pending staged changes is skipped. The
operator's staged edits live in the mirror until they hit Send; a
background refresh must never overwrite them.

The worker does nothing while ``dev_console_enabled`` is off, so
mirrors are only ever built when the console tab is live.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Deque, Dict, List, Optional, Tuple

from server import dev_console_db

log = logging.getLogger("plexmigrate.server.dev_console_sync")


def _console_enabled() -> bool:
    try:
        from services import tunables
        return tunables.dev_console_enabled()
    except Exception:
        return False


def _publish_sync(server_id: str, phase: str, detail: str = "") -> None:
    """Best-effort sync event over the dev-console WebSocket."""
    try:
        from server.dev_console_ws import get_dev_console_manager
        get_dev_console_manager().publish({
            "type": "sync",
            "server_id": server_id,
            "phase": phase,
            "detail": detail,
        })
    except Exception:
        log.debug("dev_console_sync: publish failed", exc_info=True)


def _snap_metadata(snap) -> Dict[str, Any]:
    return {
        "backend_item_id": snap.backend_item_id,
        "title": snap.title,
        "type": snap.type,
        "year": snap.year,
        "guids": list(snap.guids or ()),
        "show_title": snap.show_title or "",
        "season_index": snap.season_index,
        "episode_index": snap.episode_index,
        "artist": snap.artist or "",
        "album": snap.album or "",
        "file_path": snap.file_path or "",
    }


def _snap_state(snap) -> Dict[str, Any]:
    return {
        "backend_item_id": snap.backend_item_id,
        "view_count": int(snap.view_count or 0),
        "last_viewed_at": snap.last_viewed_at,
        "view_offset_ms": int(snap.view_offset_ms or 0),
        "user_rating": snap.user_rating,
        "is_favorite": bool(snap.is_favorite),
    }


def _sync_user_playlists(adapter, sid: str, uctx) -> None:
    """Sync one user's playlists into the mirror, keyed by that user's
    username. Playlists are per-user on every backend; the mirror keys
    them by username (NOT backend_user_id, which Plex leaves blank for
    every account) so each user gets their own set."""
    # CONSOLE-10 (known, accepted limitation): when the dev console is
    # enabled this does a live list_playlists fetch that overlaps the
    # one services/playlist_cache_refresher.py already performs for the
    # same (server, user). The two write to separate stores (the
    # dev-console mirror vs playlist_cache) so they are not trivially
    # dedupable. Accepted because the dev console ships disabled, so
    # the overlap only occurs when an operator opts in; a shared
    # roster-fetch layer is deferred unless the console becomes
    # default-on.
    username = getattr(uctx, "username", "") or ""
    if not username:
        return
    playlists: List[Dict[str, Any]] = []
    for spec in (adapter.list_playlists(uctx) or []):
        item_ids = [r.backend_item_id for r in (spec.items or ())]
        if not item_ids:
            try:
                item_ids = [
                    r.backend_item_id
                    for r in adapter.get_playlist_items(
                        spec.playlist_id, user_context=uctx,
                    )
                ]
            except Exception:
                item_ids = []
        playlists.append({
            "playlist_id": spec.playlist_id, "name": spec.name,
            "is_smart": spec.is_smart,
            "playlist_type": getattr(spec, "playlist_type", "") or "",
            "item_ids": item_ids,
        })
    dev_console_db.replace_playlists(sid, username, playlists)


def sync_server(
    server_id: str, *, users: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Refresh one server's mirror. Returns a result dict
    ``{server_id, ok, phase, detail}``.

    ``users`` is a list of usernames whose per-item state should also
    be walked (on top of the always-synced owner view)."""
    if dev_console_db.has_pending_staged(server_id):
        # Frozen: the operator has uncommitted staged edits.
        return {"server_id": server_id, "ok": False, "phase": "frozen",
                "detail": "server has pending staged changes; sync skipped"}

    _publish_sync(server_id, "start")
    from services.admin import dev_console as dc

    try:
        conn = dc._connect(server_id)
    except dc.DevConsoleError as exc:
        # Create the mirror file so the panel can still show the
        # server with the failure surfaced via sync_status.
        try:
            dev_console_db.set_meta(server_id, "last_sync_error", exc.message)
            dev_console_db.set_meta(server_id, "last_sync_error_at", time.time())
        except Exception:
            pass
        _publish_sync(server_id, "error", exc.message)
        return {"server_id": server_id, "ok": False, "phase": "error",
                "detail": exc.message}

    adapter = conn.adapter
    sid = str(conn.row.get("id") or server_id)
    try:
        # Emby / Jellyfin surface a server-wide "Collections" virtual
        # library (type 'boxsets'). It is NOT a real library - its
        # items already live in, and are walked under, the real source
        # libraries. Walking it too would re-fetch the same items and
        # double the API cost. Skip it; collections themselves are
        # still captured below via adapter.list_collections().
        libs = [
            l for l in (adapter.list_libraries() or [])
            if (l.type or "").lower() != "boxsets"
        ]
        dev_console_db.replace_libraries(sid, [
            {"library_id": l.library_id, "name": l.name, "type": l.type,
             "item_count": l.item_count}
            for l in libs
        ])
        user_specs = list(adapter.list_users() or [])
        dev_console_db.replace_users(sid, [
            {"backend_user_id": u.backend_user_id, "username": u.username,
             "display_name": u.display_name, "role": u.role,
             "is_admin": u.is_admin}
            for u in user_specs
        ])

        owner_ctx = dc._build_user_context(conn, None)
        for lib in libs:
            metadata: List[Dict[str, Any]] = []
            owner_states: List[Dict[str, Any]] = []
            for snap in adapter.iter_items(
                lib.library_id, user_context=owner_ctx,
            ):
                metadata.append(_snap_metadata(snap))
                owner_states.append(_snap_state(snap))
            dev_console_db.replace_library_items(sid, lib.library_id, metadata)
            # Per-item state keys by username (backend_user_id is blank
            # on Plex). The owner's admin context reads the owner's own
            # state, so it is always correct to mirror.
            dev_console_db.upsert_item_states(
                sid, owner_ctx.username, owner_states,
            )

        # Playlists are per-user on every backend. Sync the owner's
        # always; sync each requested user's so the panel's
        # "By playlist" view reflects whoever is selected.
        _sync_user_playlists(adapter, sid, owner_ctx)

        for uname in (users or []):
            try:
                uctx = dc._build_user_context(conn, uname)
            except dc.DevConsoleError:
                continue
            # Playlists + per-item state both key by username, which is
            # always present, so they sync even when the backend leaves
            # backend_user_id blank (Plex).
            _sync_user_playlists(adapter, sid, uctx)
            # Item state still needs a real per-user perspective: a
            # backend_user_id to scope by (Emby / Jellyfin) or a
            # per-user token (Plex, which sets is_admin False once a
            # token is found). With neither, iter_items would return
            # the admin's state mislabeled as this user's - skip it.
            if not uctx.backend_user_id and uctx.is_admin:
                # Warn so the operator knows this user was skipped and
                # needs a per-user token / PIN before the console can
                # verify their per-item state. ``explore_items`` reports
                # ``user_synced=False`` for this user, but a silent skip
                # gave the operator no way to know why.
                log.warning(
                    "dev_console sync: skipped per-item state for user "
                    "%r on server %s (no per-user token/PIN); the "
                    "console will report this user as not synced.",
                    uname, sid,
                )
                continue
            for lib in libs:
                states = [
                    _snap_state(snap)
                    for snap in adapter.iter_items(
                        lib.library_id, user_context=uctx,
                    )
                ]
                dev_console_db.upsert_item_states(
                    sid, uctx.username, states,
                )

        collections = [
            {"collection_id": c.collection_id, "name": c.name,
             "library_id": c.library_id,
             "item_ids": [r.backend_item_id for r in (c.items or ())]}
            for c in (adapter.list_collections() or [])
        ]
        dev_console_db.replace_collections(sid, collections)

        dev_console_db.set_meta(sid, "last_sync_error", "")
        dev_console_db.mark_full_sync(sid)
        _publish_sync(server_id, "complete")
        return {"server_id": server_id, "ok": True, "phase": "complete",
                "detail": f"{len(libs)} libraries synced"}
    except Exception as exc:  # pragma: no cover (defensive)
        log.exception("dev_console_sync: sync of %s failed", server_id)
        try:
            dev_console_db.set_meta(sid, "last_sync_error", str(exc))
            dev_console_db.set_meta(sid, "last_sync_error_at", time.time())
        except Exception:
            pass
        _publish_sync(server_id, "error", str(exc))
        return {"server_id": server_id, "ok": False, "phase": "error",
                "detail": str(exc)}


def sync_user_playlists(
    server_id: str, username: Optional[str],
) -> Dict[str, Any]:
    """Fast path: mirror ONE user's playlists and nothing else - no
    library walk, no per-item-state walk. The "By playlist" view calls
    this when the operator selects a user, so switching users is cheap
    even on a large library. The full :func:`sync_server` re-walks
    every library item (per owner AND per requested user); on a big
    music library that is minutes of API calls, so a managed user's
    playlists effectively never surfaced through that path."""
    if dev_console_db.has_pending_staged(server_id):
        return {"server_id": server_id, "ok": False, "phase": "frozen",
                "detail": "server has pending staged changes; sync skipped"}
    from services.admin import dev_console as dc
    try:
        conn = dc._connect(server_id)
    except dc.DevConsoleError as exc:
        return {"server_id": server_id, "ok": False, "phase": "error",
                "detail": exc.message}
    sid = str(conn.row.get("id") or server_id)
    try:
        uctx = dc._build_user_context(conn, username or None)
        _sync_user_playlists(conn.adapter, sid, uctx)
        return {"server_id": server_id, "ok": True, "phase": "complete",
                "detail": f"playlists synced for {uctx.username or 'owner'}"}
    except dc.DevConsoleError as exc:
        return {"server_id": server_id, "ok": False, "phase": "error",
                "detail": exc.message}
    except Exception as exc:  # pragma: no cover (defensive)
        log.exception(
            "dev_console_sync: playlist sync of %s/%s failed",
            server_id, username,
        )
        return {"server_id": server_id, "ok": False, "phase": "error",
                "detail": str(exc)}


class DevConsoleSyncWorker:
    """Daemon thread: refreshes every mirror on the tunable cadence and
    services explicit on-demand sync requests promptly. CONSOLE-06:
    each server is synced one-at-a-time via a per-server lock so its
    own API load never multiplies, but different servers refresh
    concurrently (bounded pool) so one slow server cannot starve the
    rest."""

    def __init__(self) -> None:
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._requests: Deque[Tuple[str, Tuple[str, ...]]] = deque()
        self._req_lock = threading.Lock()
        self._next_due: Dict[str, float] = {}
        # CONSOLE-06: per-server locks (was a single global lock). A
        # given server is still synced one-at-a-time; distinct servers
        # are free to sync in parallel.
        self._server_locks: Dict[str, threading.Lock] = {}
        self._server_locks_meta = threading.Lock()

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="dev-console-sync", daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        t = self._thread
        if t is not None:
            t.join(timeout=5.0)
        self._thread = None

    def request_sync(
        self, server_id: str, *, users: Optional[List[str]] = None,
    ) -> None:
        """Queue an immediate sync of ``server_id`` and wake the loop.
        Safe to call from any thread (the request handler thread)."""
        with self._req_lock:
            self._requests.append((str(server_id), tuple(users or ())))
        self._wake.set()

    def _drain_requests(self) -> List[Tuple[str, Tuple[str, ...]]]:
        with self._req_lock:
            out = list(self._requests)
            self._requests.clear()
        return out

    def _lock_for(self, server_id: str) -> threading.Lock:
        """Per-server sync lock (CONSOLE-06), created on first use."""
        with self._server_locks_meta:
            lk = self._server_locks.get(server_id)
            if lk is None:
                lk = threading.Lock()
                self._server_locks[server_id] = lk
            return lk

    def run_one(
        self, server_id: str, *, users: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Synchronously run one sync under the worker's lock. Used by
        the on-demand path so the request can report its outcome."""
        with self._lock_for(server_id):
            return sync_server(server_id, users=list(users or []))

    def _scheduled_refresh(self, server_id: str) -> None:
        """One server's scheduled mirror refresh, then reschedule it.
        Run on a worker thread by _run's bounded pool (CONSOLE-06)."""
        if self._stop.is_set():
            return
        self.run_one(server_id)
        self._next_due[server_id] = (
            time.monotonic() + self._cadence(server_id)
        )

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                if not _console_enabled():
                    self._wake.wait(timeout=30.0)
                    self._wake.clear()
                    continue
                for sid, users in self._drain_requests():
                    if self._stop.is_set():
                        break
                    self.run_one(sid, users=list(users))
                    self._next_due[sid] = (
                        time.monotonic() + self._cadence(sid)
                    )
                now = time.monotonic()
                due = [
                    sid for sid in dev_console_db.mirror_server_ids()
                    if now >= self._next_due.get(sid, 0.0)
                ]
                if due and not self._stop.is_set():
                    # CONSOLE-06: refresh due servers concurrently
                    # (bounded) so a slow / large server no longer
                    # starves the others. Each server stays
                    # one-at-a-time via its per-server lock in run_one.
                    with ThreadPoolExecutor(
                        max_workers=min(4, len(due)),
                        thread_name_prefix="dc-sync",
                    ) as ex:
                        list(ex.map(self._scheduled_refresh, due))
            except Exception:  # pragma: no cover (defensive)
                log.exception("dev_console_sync worker tick failed")
            self._wake.wait(timeout=5.0)
            self._wake.clear()

    @staticmethod
    def _cadence(server_id: str) -> float:
        try:
            from services import tunables
            return float(tunables.dev_console_mirror_sync_seconds(server_id))
        except Exception:
            return 300.0


# ── Module-level singleton ───────────────────────────────────────────────────
_worker: Optional[DevConsoleSyncWorker] = None


def get_sync_worker() -> DevConsoleSyncWorker:
    global _worker
    if _worker is None:
        _worker = DevConsoleSyncWorker()
    return _worker


def request_sync(server_id: str, *, users: Optional[List[str]] = None) -> None:
    """Module-level convenience: queue an on-demand mirror sync."""
    get_sync_worker().request_sync(server_id, users=users)
