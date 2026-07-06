"""
services/dev_console.py - command service for the root-admin
"Server Commands" developer console.

Reads come from the per-server mirror database
(``server/dev_console_db.py``), kept fresh by the background sync
worker (``server/dev_console_sync.py``). The panel therefore browses
without ever calling a media-server API.

Writes have two modes:

* **Immediate** - the command is sent live to the server through the
  backend adapter, then the mirror row is updated write-through.
* **Staged** - the command is recorded as a pending row in the
  mirror's ``staged_changes`` table. Nothing touches the live server.
  The panel overlays staged rows so the operator previews the result.
  :func:`send_staged` later applies every pending change live;
  :func:`discard_staged` throws them away. While a server has pending
  staged changes the sync worker freezes its mirror.

``_do_connect`` is the single point that calls
``connect_registered_server``; every live connect is wrapped with a
hard timeout so a dead server can never hang the panel.
"""

from __future__ import annotations

import concurrent.futures
import logging
from typing import Any, Dict, List, Optional

from services.adapters import ItemRef, UserContext

log = logging.getLogger("plexmigrate.services.admin.dev_console")


# ── Errors ───────────────────────────────────────────────────────────────────

class DevConsoleError(Exception):
    """Service-layer failure carrying the HTTP status the router should
    surface (400 bad input, 404 missing, 502 connect failed, 504
    connect timed out)."""

    def __init__(self, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


# ── Live connection (timeout-bounded) ────────────────────────────────────────

_CONNECT_POOL = concurrent.futures.ThreadPoolExecutor(
    max_workers=4, thread_name_prefix="devconsole-connect",
)


def _do_connect(server_id: str):
    """Direct call into the registry connect helper. Wrapped by
    :func:`_connect` with a timeout."""
    from server.server_registry import connect_registered_server
    return connect_registered_server(server_id, log)


def _connect(server_id: str):
    """Connect to a registered server, bounded by the
    ``dev_console_connect_timeout_seconds`` tunable so a dead server
    fails fast (504) instead of hanging the request."""
    from services import tunables
    timeout = tunables.dev_console_connect_timeout_seconds()
    fut = _CONNECT_POOL.submit(_do_connect, server_id)
    try:
        return fut.result(timeout=timeout)
    except concurrent.futures.TimeoutError as exc:
        raise DevConsoleError(
            f"connecting to server {server_id!r} timed out after "
            f"{timeout}s; it may be offline", status=504,
        ) from exc
    except ValueError as exc:
        raise DevConsoleError(str(exc), status=404) from exc
    except ConnectionError as exc:
        raise DevConsoleError(
            f"could not connect to server {server_id!r}: {exc}", status=502,
        ) from exc


# ── User context ─────────────────────────────────────────────────────────────

def _owner_spec(adapter) -> Any:
    try:
        for u in adapter.list_users() or []:
            if (getattr(u, "role", "") or "") == "owner":
                return u
    except Exception:
        log.debug("dev_console: list_users failed while resolving owner")
    return None


def _build_user_context(conn, username: Optional[str]) -> UserContext:
    """Build a :class:`UserContext` for ``username`` (None = owner).

    Plex needs a per-user token; it is pulled from
    ``managed_users.auth_token_enc``. Jellyfin / Emby admin tokens
    write on behalf of any user via the URL ``UserId``, with a stored
    per-user token preferred for accurate reads."""
    adapter = conn.adapter
    service_type = (conn.service_type or "plex").lower()

    if not username:
        owner = _owner_spec(adapter)
        return UserContext(
            backend_user_id=getattr(owner, "backend_user_id", "") if owner else "",
            username=getattr(owner, "username", "") if owner else "owner",
            auth_token=conn.token,
            is_admin=True,
        )

    spec = None
    for u in adapter.list_users() or []:
        if (getattr(u, "username", "") or "") == username:
            spec = u
            break
    if spec is None:
        raise DevConsoleError(
            f"user {username!r} is not known to this server", status=404,
        )

    backend_user_id = getattr(spec, "backend_user_id", "") or ""
    if (getattr(spec, "role", "") or "") == "owner":
        return UserContext(
            backend_user_id=backend_user_id, username=username,
            auth_token=conn.token, is_admin=True,
        )

    sid = str(conn.row.get("id") or "")
    per_user_token = ""
    try:
        from server import media_db
        per_user_token = media_db.get_managed_user_credential(
            sid, username, kind="auth_token",
        ) or ""
    except Exception:
        log.debug("dev_console: per-user token lookup failed for %s", username)

    # Plex's admin token only sees the OWNER's playlists / item state;
    # reading a managed user's data needs that user's own token. When
    # none is saved, derive one from a stored Plex Home PIN - the same
    # fallback chain playlist_copy uses (saved token -> PIN sign-in ->
    # admin). Without this the panel mirrors an empty set for every
    # managed Plex user and the "By playlist" view shows nothing.
    if not per_user_token and service_type == "plex":
        try:
            from services.playlist_copy import _obtain_per_user_token_via_pin
            per_user_token = _obtain_per_user_token_via_pin(
                conn, sid, username,
            ) or ""
        except Exception:
            log.debug(
                "dev_console: PIN-derived token unavailable for %s", username,
            )

    return UserContext(
        backend_user_id=backend_user_id, username=username,
        auth_token=per_user_token or conn.token,
        is_admin=not per_user_token,
    )


# ── Server discovery ─────────────────────────────────────────────────────────

def list_servers() -> List[Dict[str, Any]]:
    """Every registered server as a slim row for the subtab strip."""
    from server import server_registry
    out: List[Dict[str, Any]] = []
    for row in server_registry.list_servers(include_tokens=False):
        out.append({
            "server_id": row.get("id"),
            "name": row.get("name") or "",
            "service_type": (row.get("service_type") or "plex").lower(),
            "url": row.get("url") or "",
            "last_status": row.get("last_status") or "unknown",
        })
    return out


def _registry_row(server_id: str) -> Dict[str, Any]:
    from server import server_registry
    row = server_registry.get_server_by_id(server_id, include_token=False)
    if row is None:
        raise DevConsoleError(
            f"no server registered with id {server_id!r}", status=404,
        )
    return row


def server_detail(server_id: str) -> Dict[str, Any]:
    """Stats + library/user lists for one server, read entirely from
    the mirror database. ``synced`` is False when the mirror has never
    completed a full sync - the panel then requests one."""
    from server import dev_console_db
    row = _registry_row(server_id)
    status = dev_console_db.sync_status(server_id)
    libraries: List[Dict[str, Any]] = []
    users: List[Dict[str, Any]] = []
    last_error = ""
    if dev_console_db.mirror_exists(server_id):
        libraries = dev_console_db.list_libraries(server_id)
        users = dev_console_db.list_users(server_id)
        last_error = dev_console_db.get_meta(server_id, "last_sync_error") or ""

    return {
        "server_id": server_id,
        "name": row.get("name") or "",
        "service_type": (row.get("service_type") or "plex").lower(),
        "url": row.get("url") or "",
        "last_status": row.get("last_status") or "unknown",
        "synced": bool(status.get("synced")),
        "last_full_sync_at": status.get("last_full_sync_at"),
        "last_sync_error": last_error,
        "pending_staged": status.get("pending_staged", 0),
        "libraries": libraries,
        "users": users,
        "library_count": len(libraries),
        "user_count": len(users),
    }


def list_libraries(server_id: str) -> List[Dict[str, Any]]:
    from server import dev_console_db
    if not dev_console_db.mirror_exists(server_id):
        return []
    return dev_console_db.list_libraries(server_id)


def list_server_users(server_id: str) -> List[Dict[str, Any]]:
    from server import dev_console_db
    if not dev_console_db.mirror_exists(server_id):
        return []
    return dev_console_db.list_users(server_id)


def sync_status(server_id: str) -> Dict[str, Any]:
    from server import dev_console_db
    st = dev_console_db.sync_status(server_id)
    st["server_id"] = server_id
    st["last_sync_error"] = (
        dev_console_db.get_meta(server_id, "last_sync_error") or ""
        if dev_console_db.mirror_exists(server_id) else ""
    )
    return st


def _trigger_sync(
    server_id: str, *, users: Optional[List[str]] = None,
) -> None:
    """Queue a mirror sync without validating the registry. Used by the
    internal post-write refresh path where the server is already
    known-good."""
    try:
        from server import dev_console_sync
        dev_console_sync.request_sync(server_id, users=users)
    except Exception:
        log.debug("dev_console: sync trigger failed", exc_info=True)


def request_server_sync(
    server_id: str, *, users: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Queue an on-demand mirror sync. Returns immediately; the panel
    learns of completion via the WebSocket ``sync`` event."""
    # Validate the server exists before queueing (router entry point).
    _registry_row(server_id)
    _trigger_sync(server_id, users=users)
    return {"server_id": server_id, "queued": True, "users": users or []}


def reset_mirror(server_id: str) -> Dict[str, Any]:
    """Delete this server's mirror cache and queue a fresh full sync.
    For when a schema change or a partial sync has left the mirror in
    a bad state - the next sync rebuilds it from scratch. Any pending
    staged changes are discarded with the cache."""
    from server import dev_console_db
    # Validate the server exists before touching anything (router entry).
    _registry_row(server_id)
    cleared = dev_console_db.reset_mirror(server_id)
    _trigger_sync(server_id)
    return {"server_id": server_id, "cleared": bool(cleared), "queued": True}


def sync_user_playlists(
    server_id: str, username: Optional[str] = None,
) -> Dict[str, Any]:
    """Mirror one user's playlists immediately - the fast path behind
    the "By playlist" view's user switch. Runs synchronously (no full
    library walk) so the panel can re-read the mirror right away."""
    from server import dev_console_sync
    _registry_row(server_id)
    return dev_console_sync.sync_user_playlists(server_id, username)


# ── Item explorer (reads the mirror) ─────────────────────────────────────────

def explore_items(
    server_id: str,
    library_id: str,
    *,
    username: Optional[str] = None,
    all_users: bool = False,
    categories: Optional[List[str]] = None,
    min_plays: Optional[int] = None,
    has_rating: bool = False,
    in_playlist: Optional[str] = None,
    in_collection: Optional[str] = None,
    artist: Optional[str] = None,
    album: Optional[str] = None,
    show_title: Optional[str] = None,
    season_index: Optional[int] = None,
    search: Optional[str] = None,
    sort: str = "name_asc",
    offset: int = 0,
    page_size: int = 20,
) -> Dict[str, Any]:
    """Paginated item query against the mirror for one library.

    ``categories`` narrows the result set to items matching ANY listed
    explorer category (watch / ratings / favorites / resume /
    playlists); empty means the whole library. ``all_users`` True
    returns every item with a ``per_user`` list so the panel can render
    the cross-user view. ``artist`` / ``album`` / ``show_title`` /
    ``season_index`` scope the query to one node of the hierarchy
    viewer."""
    from server import dev_console_db
    page_size = max(1, min(int(page_size), 50))
    offset = max(0, int(offset))
    if not dev_console_db.mirror_exists(server_id):
        return {
            "server_id": server_id, "library_id": library_id,
            "user": username or "", "all_users": all_users,
            "items": [], "total": 0, "offset": 0, "page_size": page_size,
            "user_synced": False, "mirror_cold": True,
        }

    resolved_username = ""
    if not all_users:
        user_row = dev_console_db.resolve_user(server_id, username)
        resolved_username = user_row.get("username", "")

    page = dev_console_db.query_items(
        server_id, library_id,
        username=resolved_username,
        categories=categories, all_users=all_users,
        min_plays=min_plays, has_rating=has_rating,
        in_playlist=in_playlist, in_collection=in_collection,
        artist=artist, album=album, show_title=show_title,
        season_index=season_index,
        search=search, sort=sort, offset=offset, page_size=page_size,
    )

    synced_users = set(dev_console_db.synced_usernames(server_id))
    if all_users:
        breakdown = dev_console_db.item_user_breakdown(
            server_id, [i["backend_item_id"] for i in page["items"]],
        )
        for it in page["items"]:
            it["per_user"] = breakdown.get(it["backend_item_id"], [])
        # Every user must be synced for the cross-user view to be
        # complete.
        all_usernames = {
            u["username"] for u in dev_console_db.list_users(server_id)
            if u.get("username")
        }
        user_synced = all_usernames.issubset(synced_users) if all_usernames else True
    else:
        user_synced = (not username) or (resolved_username in synced_users)

    return {
        "server_id": server_id,
        "library_id": library_id,
        "user": username or "",
        "all_users": all_users,
        "items": page["items"],
        "total": page["total"],
        "offset": offset,
        "page_size": page_size,
        "user_synced": user_synced,
        "mirror_cold": False,
    }


def list_groups(
    server_id: str,
    library_id: str,
    *,
    level: str,
    parent: str = "",
    username: Optional[str] = None,
    all_users: bool = False,
    categories: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Hierarchy-viewer node lists for the mini media browser:
    ``level`` artist/album/show/season/playlist. ``parent`` is the
    artist (for albums) or show (for seasons). When ``categories`` is
    given the tree is pruned to groups that contain a matching leaf
    item, viewed as ``username`` (or across all users).

    ``user_synced`` is meaningful for the ``playlist`` level: playlists
    are per-user, and a managed user's are only in the mirror after
    that user has been synced. When False the panel requests a sync."""
    from server import dev_console_db
    groups: List[Dict[str, Any]] = []
    user_synced = True
    if dev_console_db.mirror_exists(server_id):
        resolved_username = ""
        if not all_users:
            resolved_username = dev_console_db.resolve_user(
                server_id, username,
            ).get("username", "")
        groups = dev_console_db.list_groups(
            server_id, library_id, level=level, parent=parent,
            categories=categories, username=resolved_username,
            all_users=all_users,
        )
        if level == "playlist" and not all_users and username:
            # A managed user's playlists are mirrored on demand. Check
            # the playlist-synced set specifically - NOT the item-state
            # synced set, since a user synced before per-user playlists
            # existed has item state but no mirrored playlists. Keyed
            # by username: backend_user_id is blank on Plex.
            synced = set(dev_console_db.playlist_synced_usernames(server_id))
            user_synced = resolved_username in synced
    else:
        user_synced = False
    return {
        "server_id": server_id, "library_id": library_id,
        "level": level, "parent": parent, "groups": groups,
        "user_synced": user_synced,
    }


def item_detail(
    server_id: str,
    item_id: str,
    *,
    username: Optional[str] = None,
) -> Dict[str, Any]:
    """One item's full mirror state plus the playlists / collections it
    belongs to. Backs the drill-in detail view."""
    from server import dev_console_db
    if not dev_console_db.mirror_exists(server_id):
        raise DevConsoleError("server mirror not synced yet", status=409)
    user_row = dev_console_db.resolve_user(server_id, username)
    item = dev_console_db.get_item(
        server_id, item_id, user_row.get("username", ""),
    )
    if item is None:
        raise DevConsoleError(
            f"item {item_id!r} not in the mirror", status=404,
        )
    rels = dev_console_db.item_relationships(server_id, item_id)
    return {
        "server_id": server_id,
        "user": username or "",
        "item": item,
        "playlists": rels["playlists"],
        "collections": rels["collections"],
    }


def list_playlists(server_id: str) -> List[Dict[str, Any]]:
    from server import dev_console_db
    if not dev_console_db.mirror_exists(server_id):
        return []
    return dev_console_db.list_playlists(server_id)


def list_collections(server_id: str) -> List[Dict[str, Any]]:
    from server import dev_console_db
    if not dev_console_db.mirror_exists(server_id):
        return []
    return dev_console_db.list_collections(server_id)


# ── Per-item commands (immediate or staged) ──────────────────────────────────

# Op -> (adapter method name, the mirror state fields it writes).
_OPS = ("set_watch", "set_rating", "set_favorite", "set_resume")


def _ref(library_id: str, item_id: str) -> ItemRef:
    return ItemRef(backend_item_id=item_id, library_id=library_id)


def _stage(
    server_id: str, op: str, library_id: str, item_id: str,
    username: Optional[str], payload: Dict[str, Any],
) -> Dict[str, Any]:
    """Record a command as a pending staged change. Touches only the
    mirror DB - the live server is untouched until Send."""
    from server import dev_console_db
    if not dev_console_db.mirror_exists(server_id):
        raise DevConsoleError(
            "cannot stage against a server with no mirror yet; sync first",
            status=409,
        )
    user_row = dev_console_db.resolve_user(server_id, username)
    resolved_username = user_row.get("username", "")
    mirror_item = dev_console_db.get_item(
        server_id, item_id, resolved_username,
    )
    staged_id = dev_console_db.add_staged_change(
        server_id,
        library_id=library_id or (mirror_item or {}).get("library_id", ""),
        backend_item_id=item_id,
        item_title=(mirror_item or {}).get("title", ""),
        backend_user_id=user_row.get("backend_user_id", ""),
        username=resolved_username,
        op=op,
        payload=payload,
    )
    return {
        "op": op, "server_id": server_id, "item_id": item_id,
        "user": username or "", "staged": True, "staged_id": staged_id,
        "success": True, "detail": "staged",
    }


def _apply_live(
    server_id: str, op: str, library_id: str, item_id: str,
    username: Optional[str], payload: Dict[str, Any],
) -> Dict[str, Any]:
    """Send one command to the live server and write the result
    through to the mirror."""
    from server import dev_console_db
    conn = _connect(server_id)
    adapter = conn.adapter
    uctx = _build_user_context(conn, username)
    ref = _ref(library_id, item_id)
    sid = str(conn.row.get("id") or server_id)

    result, new_state = _dispatch_adapter(adapter, op, ref, uctx, payload)
    out = {
        "op": op, "server_id": server_id, "item_id": item_id,
        "user": username or "", "staged": False,
        "success": bool(result.success),
        "unsupported": bool(getattr(result, "unsupported", False)),
        "detail": result.detail or "",
        "new_state": new_state if result.success else {},
    }
    if result.success and new_state:
        try:
            dev_console_db.apply_state_to_mirror(
                sid, item_id, uctx.username, new_state,
            )
        except Exception:
            log.debug("dev_console: write-through to mirror failed",
                      exc_info=True)
    return out


def _dispatch_adapter(adapter, op, ref, uctx, payload):
    """Call the adapter for ``op`` and return ``(WriteResult,
    new_state_dict)``."""
    if op == "set_watch":
        target = max(0, int(payload.get("view_count") or 0))
        last_played = payload.get("last_played")
        current = None
        try:
            current = adapter.get_current_view_count(ref, user_context=uctx)
        except Exception:
            current = None
        result = adapter.set_watched(
            ref, view_count=target, last_viewed_at=last_played,
            user_context=uctx, current_view_count=current,
        )
        return result, {"view_count": target, "last_viewed_at": last_played}
    # POLICY EXCEPTION (audit anchor): the dev console performs DIRECT
    # adapter affinity writes that BYPASS services.translation.
    # backend_translation.translate_affinity. This is the ONLY code
    # path in the engine that should write affinity without going
    # through the chokepoint. The dev console is by design a raw write
    # surface (root-admin only, debug-tunable gated). Any feature
    # request that wants to write rating/favorite outside the dev
    # console MUST route through translate_affinity so the favorite
    # face vs numeric rating translation tunables (favorite_threshold,
    # favorite_as_rating_value) stay authoritative.
    if op == "set_rating":
        rating = float(payload.get("rating") or 0.0)
        result = adapter.set_rating(ref, rating, user_context=uctx)
        return result, {"user_rating": rating}
    if op == "set_favorite":
        fav = bool(payload.get("favorite"))
        result = adapter.set_favorite(ref, fav, user_context=uctx)
        return result, {"is_favorite": fav}
    if op == "set_resume":
        offset = max(0, int(payload.get("offset_ms") or 0))
        result = adapter.set_resume_position(ref, offset, user_context=uctx)
        return result, {"view_offset_ms": offset}
    raise DevConsoleError(f"unknown command op {op!r}")


def run_command(
    server_id: str,
    op: str,
    library_id: str,
    item_id: str,
    *,
    username: Optional[str] = None,
    payload: Optional[Dict[str, Any]] = None,
    stage: bool = True,
) -> Dict[str, Any]:
    """Per-item command entry point. ``stage`` True records a pending
    staged change (default - matches the panel's checked-by-default
    box); False sends it live immediately."""
    if op not in _OPS:
        raise DevConsoleError(
            f"unknown op {op!r}; expected one of {', '.join(_OPS)}",
        )
    payload = dict(payload or {})
    if stage:
        return _stage(server_id, op, library_id, item_id, username, payload)
    return _apply_live(server_id, op, library_id, item_id, username, payload)


# ── Staged change management ─────────────────────────────────────────────────

def list_staged(server_id: str) -> Dict[str, Any]:
    from server import dev_console_db
    return {
        "server_id": server_id,
        "pending": dev_console_db.list_staged(server_id, status="pending"),
        "history": (
            dev_console_db.list_staged(server_id, status="sent")
            + dev_console_db.list_staged(server_id, status="failed")
        ),
    }


def discard_staged(
    server_id: str, staged_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Drop pending staged changes and refresh the mirror so it shows
    true server state again."""
    from server import dev_console_db
    removed = dev_console_db.discard_staged(server_id, staged_id)
    if removed and not dev_console_db.has_pending_staged(server_id):
        _trigger_sync(server_id)
    return {"server_id": server_id, "discarded": removed}


def send_staged(server_id: str) -> Dict[str, Any]:
    """Apply every pending staged change to the live server, then
    refresh the mirror. Each change is marked sent or failed."""
    from server import dev_console_db
    pending = dev_console_db.list_staged(server_id, status="pending")
    if not pending:
        return {"server_id": server_id, "sent": 0, "failed": 0, "results": []}

    conn = _connect(server_id)
    adapter = conn.adapter
    sid = str(conn.row.get("id") or server_id)
    # One UserContext per distinct username in the batch.
    ctx_cache: Dict[str, UserContext] = {}
    results: List[Dict[str, Any]] = []
    sent = failed = 0
    for change in pending:
        uname = change.get("username") or ""
        if uname not in ctx_cache:
            try:
                ctx_cache[uname] = _build_user_context(conn, uname or None)
            except DevConsoleError as exc:
                dev_console_db.mark_staged(
                    server_id, change["id"], "failed", exc.message,
                )
                results.append({"id": change["id"], "ok": False,
                                 "detail": exc.message})
                failed += 1
                continue
        uctx = ctx_cache[uname]
        ref = _ref(change.get("library_id", ""), change["backend_item_id"])
        try:
            result, new_state = _dispatch_adapter(
                adapter, change["op"], ref, uctx, change.get("payload", {}),
            )
            if result.success:
                dev_console_db.mark_staged(
                    server_id, change["id"], "sent", result.detail or "ok",
                )
                # Write the new state through to the mirror right away,
                # the same as immediate-mode does, so the panel reflects
                # the sent value without waiting a full sync cycle.
                if new_state:
                    try:
                        dev_console_db.apply_state_to_mirror(
                            sid, change["backend_item_id"],
                            uctx.username, new_state,
                        )
                    except Exception:
                        log.debug(
                            "dev_console: write-through to mirror failed",
                            exc_info=True,
                        )
                sent += 1
                results.append({"id": change["id"], "ok": True,
                                 "detail": result.detail or "ok"})
            else:
                dev_console_db.mark_staged(
                    server_id, change["id"], "failed",
                    result.detail or "failed",
                )
                failed += 1
                results.append({"id": change["id"], "ok": False,
                                 "detail": result.detail or "failed"})
        except Exception as exc:  # pragma: no cover (defensive)
            dev_console_db.mark_staged(
                server_id, change["id"], "failed", str(exc),
            )
            failed += 1
            results.append({"id": change["id"], "ok": False,
                             "detail": str(exc)})

    # Staged rows are now sent/failed (no longer pending) so the mirror
    # is unfrozen; refresh it to reflect true post-write state.
    _trigger_sync(server_id)
    return {"server_id": server_id, "sent": sent, "failed": failed,
            "results": results}


# ── Raw per-call API passthrough (always live) ───────────────────────────────

_RAW_OPS_PLEX = ("scrobble", "unscrobble", "rate", "progress")
_RAW_OPS_JF = ("set_userdata", "mark_played", "delete_played")


def raw_api_call(
    server_id: str,
    op: str,
    *,
    item_id: str,
    username: Optional[str] = None,
    params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Run one discrete, named raw backend API call. Always live - raw
    ops are a testing primitive and are not staged."""
    conn = _connect(server_id)
    service_type = (conn.service_type or "plex").lower()
    params = dict(params or {})
    if service_type == "plex":
        return _raw_plex(conn, op, item_id, username, params)
    if service_type in ("jellyfin", "emby"):
        return _raw_jellyfin(conn, op, item_id, username, params)
    raise DevConsoleError(
        f"raw ops not supported for service_type {service_type!r}",
    )


def _raw_plex(conn, op, item_id, username, params):
    if op not in _RAW_OPS_PLEX:
        raise DevConsoleError(
            f"unknown Plex raw op {op!r}; expected one of "
            f"{', '.join(_RAW_OPS_PLEX)}",
        )
    import services.state as state
    uctx = _build_user_context(conn, username)
    token = uctx.auth_token or conn.token
    base = (conn.url or "").rstrip("/")
    headers = {"X-Plex-Token": token}
    query: Dict[str, Any] = {
        "key": item_id, "identifier": "com.plexapp.plugins.library",
    }
    try:
        if op == "scrobble":
            resp = state._session.get(
                f"{base}/:/scrobble", params=query, headers=headers, timeout=5,
            )
        elif op == "unscrobble":
            resp = state._session.get(
                f"{base}/:/unscrobble", params=query, headers=headers, timeout=5,
            )
        elif op == "rate":
            query["rating"] = float(params.get("rating", 0.0))
            resp = state._session.put(
                f"{base}/:/rate", params=query, headers=headers, timeout=10,
            )
        else:  # progress
            query["time"] = int(params.get("time", 0))
            query["state"] = str(params.get("state", "stopped"))
            query["hasMDE"] = 1
            resp = state._session.get(
                f"{base}/:/progress", params=query, headers=headers, timeout=5,
            )
    except Exception as exc:
        raise DevConsoleError(f"raw {op} failed: {exc}", status=502) from exc
    return {
        "op": op, "backend": "plex", "server_id": conn.row.get("id"),
        "item_id": item_id, "user": username or "",
        "http_status": resp.status_code, "ok": bool(resp.ok),
        "request_params": dict(query),
    }


def _raw_jellyfin(conn, op, item_id, username, params):
    if op not in _RAW_OPS_JF:
        raise DevConsoleError(
            f"unknown Jellyfin/Emby raw op {op!r}; expected one of "
            f"{', '.join(_RAW_OPS_JF)}",
        )
    adapter = conn.adapter
    uctx = _build_user_context(conn, username)
    uid = uctx.backend_user_id
    if not uid:
        raise DevConsoleError("no backend user id resolved for raw call")
    try:
        if op == "set_userdata":
            body = params.get("body")
            if not isinstance(body, dict):
                raise DevConsoleError(
                    "set_userdata requires a 'body' object in params",
                )
            adapter._post_json(
                f"/Users/{uid}/Items/{item_id}/UserData", json_body=body,
            )
        elif op == "mark_played":
            adapter._post_json(f"/Users/{uid}/PlayedItems/{item_id}")
        else:  # delete_played
            adapter._delete(f"/Users/{uid}/PlayedItems/{item_id}")
    except DevConsoleError:
        raise
    except Exception as exc:
        raise DevConsoleError(f"raw {op} failed: {exc}", status=502) from exc
    return {
        "op": op, "backend": (conn.service_type or "jellyfin").lower(),
        "server_id": conn.row.get("id"), "item_id": item_id,
        "user": username or "", "user_id": uid, "ok": True,
    }


# ── Playlist + collection membership (always live) ───────────────────────────

def create_playlist(
    server_id: str,
    name: str,
    *,
    item_ids: Optional[List[str]] = None,
    library_id: Optional[str] = None,
    username: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a new playlist on the live server, seeded with the given
    items, and refresh the mirror. Always live - playlist creation is
    structural, not a staged per-item edit.

    At least one item is required: Plex (and the engine's adapter
    contract) cannot create an empty playlist, so an empty
    ``item_ids`` is rejected up front with a clear 400 rather than
    surfacing the backend's opaque failure as a 502."""
    name = (name or "").strip()
    if not name:
        raise DevConsoleError("playlist name is required")
    item_ids = [i for i in (item_ids or []) if i]
    if not item_ids:
        raise DevConsoleError(
            "a new playlist needs at least one item - add items to it "
            "from the explorer list before creating it",
            status=400,
        )
    conn = _connect(server_id)
    adapter = conn.adapter
    uctx = _build_user_context(conn, username)
    refs = [_ref(library_id or "", i) for i in item_ids]
    try:
        new_id = adapter.create_playlist(name, refs, user_context=uctx)
    except Exception as exc:
        raise DevConsoleError(
            f"could not create playlist: {exc}", status=502,
        ) from exc
    if not new_id:
        raise DevConsoleError("backend did not return a playlist id", status=502)
    _trigger_sync(server_id)
    return {
        "op": "create_playlist", "server_id": server_id,
        "playlist_id": new_id, "name": name,
        "item_count": len(refs), "user": username or "", "success": True,
    }


def _resolve_library_section(
    server_id: str, library_id: str,
) -> "tuple[str, str]":
    """``(section_name, section_type)`` for a mirrored library - the
    pair the smart-playlist adapter calls key on. Raises 404 when the
    library is not in the mirror yet."""
    from server import dev_console_db
    for lib in dev_console_db.list_libraries(server_id):
        if lib.get("library_id") == library_id:
            return lib.get("name") or "", lib.get("type") or ""
    raise DevConsoleError(
        f"library {library_id!r} is not in the mirror; sync first",
        status=404,
    )


def smart_playlist_fields(
    server_id: str,
    library_id: str,
    *,
    libtype: Optional[str] = None,
    username: Optional[str] = None,
) -> Dict[str, Any]:
    """Enumerate the Plex smart-playlist filter vocabulary for one
    library + libtype: every filterable field, its value type, and the
    operators Plex accepts. Backs the dev console smart-playlist
    builder. Smart playlists are Plex-only; other backends return an
    empty field list."""
    conn = _connect(server_id)
    section_name, section_type = _resolve_library_section(
        str(conn.row.get("id") or server_id), library_id,
    )
    lt = libtype or section_type
    adapter = conn.adapter
    fields: List[Dict[str, Any]] = []
    if (conn.service_type or "").lower() == "plex" \
            and hasattr(adapter, "list_smart_filter_fields"):
        uctx = _build_user_context(conn, username)
        try:
            fields = adapter.list_smart_filter_fields(
                section_name=section_name, section_type=section_type,
                libtype=lt, user_context=uctx,
            ) or []
        except Exception as exc:
            raise DevConsoleError(
                f"could not read smart-playlist filters: {exc}", status=502,
            ) from exc
    return {
        "server_id": server_id, "library_id": library_id,
        "libtype": lt, "fields": fields,
    }


def create_smart_playlist(
    server_id: str,
    name: str,
    *,
    library_id: str,
    libtype: Optional[str] = None,
    match: str = "and",
    rows: Optional[List[Dict[str, Any]]] = None,
    sort: Optional[List[str]] = None,
    limit: Optional[int] = None,
    username: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a smart (filter-defined) playlist on a Plex library.
    ``rows`` is the operator-built filter list: each
    ``{field, op, value}`` becomes a ``field+op`` clause and the
    clauses are joined with ``match`` ('and' = match all, 'or' = match
    any). Always live, then the mirror is refreshed."""
    name = (name or "").strip()
    if not name:
        raise DevConsoleError("playlist name is required")
    clauses = [
        r for r in (rows or [])
        if isinstance(r, dict) and (r.get("field") or "").strip()
    ]
    if not clauses:
        raise DevConsoleError(
            "a smart playlist needs at least one filter", status=400,
        )
    conn = _connect(server_id)
    if (conn.service_type or "").lower() != "plex":
        raise DevConsoleError(
            "smart playlists are only supported on Plex", status=400,
        )
    adapter = conn.adapter
    if not hasattr(adapter, "create_smart_playlist"):
        raise DevConsoleError(
            "this server cannot create smart playlists", status=400,
        )
    section_name, section_type = _resolve_library_section(
        str(conn.row.get("id") or server_id), library_id,
    )
    # plexapi advanced-filter dict: each row is a {field+operator:
    # value} leaf; more than one leaf is wrapped in an and / or group.
    leaves = [
        {f"{r['field']}{(r.get('op') or '')}": r.get("value", "")}
        for r in clauses
    ]
    if len(leaves) == 1:
        filters: Dict[str, Any] = leaves[0]
    else:
        group = "or" if str(match).lower() == "or" else "and"
        filters = {group: leaves}
    uctx = _build_user_context(conn, username)
    try:
        rating_key = adapter.create_smart_playlist(
            title=name, section_name=section_name,
            section_type=section_type, libtype=libtype or section_type,
            filters=filters, sort=sort or None, limit=limit,
            user_context=uctx,
        )
    except Exception as exc:
        raise DevConsoleError(
            f"could not create smart playlist: {exc}", status=502,
        ) from exc
    _trigger_sync(server_id)
    return {
        "op": "create_smart_playlist", "server_id": server_id,
        "playlist_id": str(rating_key or ""), "name": name,
        "smart": True, "user": username or "",
        "success": bool(rating_key),
    }


def modify_playlist_members(
    server_id: str,
    playlist_id: str,
    *,
    add_item_ids: Optional[List[str]] = None,
    remove_item_ids: Optional[List[str]] = None,
    library_id: Optional[str] = None,
    username: Optional[str] = None,
) -> Dict[str, Any]:
    """Add / remove playlist members on the live server, then refresh
    the mirror. Removal rebuilds the playlist (its id changes) - no
    backend exposes a stable remove primitive."""
    conn = _connect(server_id)
    adapter = conn.adapter
    uctx = _build_user_context(conn, username)
    add_ids = list(add_item_ids or [])
    remove_ids = set(remove_item_ids or [])

    added = 0
    if add_ids:
        added = adapter.add_to_playlist(
            playlist_id, [_ref(library_id or "", i) for i in add_ids],
            user_context=uctx,
        )
    removed = 0
    new_playlist_id = playlist_id
    if remove_ids:
        spec = None
        for p in adapter.list_playlists(uctx) or []:
            if p.playlist_id == playlist_id:
                spec = p
                break
        if spec is None:
            raise DevConsoleError(
                f"playlist {playlist_id!r} not found", status=404,
            )
        survivors = [r for r in spec.items
                     if r.backend_item_id not in remove_ids]
        removed = len(spec.items) - len(survivors)
        if removed:
            del_result = adapter.delete_playlist(playlist_id, user_context=uctx)
            if not del_result.success:
                raise DevConsoleError(
                    f"could not rebuild playlist for member removal: "
                    f"{del_result.detail}", status=502,
                )
            new_playlist_id = adapter.create_playlist(
                spec.name, list(survivors), user_context=uctx,
            )
    _trigger_sync(server_id)
    return {
        "op": "playlist_members", "server_id": server_id,
        "playlist_id": new_playlist_id, "previous_playlist_id": playlist_id,
        "added": added, "removed": removed,
        "rebuilt": new_playlist_id != playlist_id, "success": True,
    }


def modify_collection_members(
    server_id: str,
    collection_id: str,
    *,
    add_item_ids: Optional[List[str]] = None,
    remove_item_ids: Optional[List[str]] = None,
    library_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Add / remove collection members on the live server, then refresh
    the mirror. Removal rebuilds the collection (its id changes)."""
    conn = _connect(server_id)
    adapter = conn.adapter
    add_ids = list(add_item_ids or [])
    remove_ids = set(remove_item_ids or [])

    added = 0
    if add_ids:
        added = adapter.add_to_collection(
            collection_id, [_ref(library_id or "", i) for i in add_ids],
        )
    removed = 0
    new_collection_id = collection_id
    if remove_ids:
        spec = None
        for c in adapter.list_collections(library_id) or []:
            if c.collection_id == collection_id:
                spec = c
                break
        if spec is None:
            raise DevConsoleError(
                f"collection {collection_id!r} not found", status=404,
            )
        survivors = [r for r in spec.items
                     if r.backend_item_id not in remove_ids]
        removed = len(spec.items) - len(survivors)
        if removed:
            del_result = adapter.delete_collection(collection_id)
            if not del_result.success:
                raise DevConsoleError(
                    f"could not rebuild collection for member removal: "
                    f"{del_result.detail}", status=502,
                )
            new_collection_id = adapter.create_collection(
                spec.name, list(survivors),
                library_id=spec.library_id or library_id,
            )
    _trigger_sync(server_id)
    return {
        "op": "collection_members", "server_id": server_id,
        "collection_id": new_collection_id,
        "previous_collection_id": collection_id,
        "added": added, "removed": removed,
        "rebuilt": new_collection_id != collection_id, "success": True,
    }
