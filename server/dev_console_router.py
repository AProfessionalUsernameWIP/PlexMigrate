"""
server/dev_console_router.py - REST surface for the root-admin
"Server Commands" developer console.

Every endpoint is double-gated:

1. ``Depends(_require_root)`` - the caller must hold the ``root_admin``
   role. ``_require_root`` is a single module-level dependency so the
   wiring is introspectable (the test suite asserts every route
   carries it).
2. :func:`_guard` - the ``dev_console_enabled`` tunable must be on.
   When off every endpoint 404s.

Reads come from the per-server mirror database; writes are immediate
or staged depending on the request's ``stage`` flag. Successful
mutations publish an event to the dev-console WebSocket.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from server.auth_router import require_role
from server.dev_console_ws import get_dev_console_manager
from services.admin import dev_console
from services.admin.dev_console import DevConsoleError

log = logging.getLogger("plexmigrate.server.dev_console_router")

router = APIRouter(prefix="/api/dev-console", tags=["dev-console"])

# Single shared dependency instance; reused on every route.
_require_root = require_role("root_admin")


# ── Gates + error mapping ────────────────────────────────────────────────────

def _guard() -> None:
    """Raise 404 unless the dev console tunable is enabled."""
    from services import tunables
    if not tunables.dev_console_enabled():
        raise HTTPException(status_code=404, detail="Not Found")


def _call(fn, *args, **kwargs):
    """Invoke a dev_console service function and translate failures
    into HTTP responses.

    Every dev_console.* entry point takes ``server_id`` as its first
    positional argument, so ``args[0]`` identifies which per-server
    mirror to reset when a corrupt mirror file is detected.

    Error mapping:
      * ``DevConsoleError``  -> its own status + message.
      * a corrupt mirror DB  -> reset that mirror (it is a disposable
        cache) + return 503 telling the operator to re-sync.
      * anything else        -> a 500 whose detail names the error and
        whose traceback is logged, so an unexpected failure is never
        an opaque blank 500 again.
    """
    try:
        return fn(*args, **kwargs)
    except DevConsoleError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.message) from exc
    except sqlite3.DatabaseError as exc:
        from server import dev_console_db
        if dev_console_db.is_corruption_error(exc) and args:
            # A per-server mirror SQLite file is unreadable - a known
            # hazard for WAL-mode SQLite on a bind-mounted volume. The
            # mirror is a disposable cache, so drop it; the next access
            # rebuilds a fresh one and a sync repopulates it.
            log.warning(
                "dev_console: mirror for %s is corrupt (%s); resetting it",
                args[0], exc,
            )
            try:
                dev_console_db.reset_mirror(str(args[0]))
            except Exception:
                log.exception("dev_console: mirror reset after corruption failed")
            raise HTTPException(
                status_code=503,
                detail=(
                    "This server's dev console mirror was corrupt and has "
                    "been reset. Reload the panel and run a sync to rebuild "
                    "it."
                ),
            ) from exc
        log.exception(
            "dev_console: database error in %s", getattr(fn, "__name__", fn),
        )
        raise HTTPException(
            status_code=500, detail=f"dev console database error: {exc}",
        ) from exc
    except Exception as exc:
        log.exception(
            "dev_console: unexpected error in %s", getattr(fn, "__name__", fn),
        )
        raise HTTPException(
            status_code=500,
            detail=f"dev console internal error: {type(exc).__name__}: {exc}",
        ) from exc


def _publish(event: Dict[str, Any]) -> None:
    try:
        get_dev_console_manager().publish(event)
    except Exception:
        log.debug("dev_console_router: WS publish failed", exc_info=True)


# ── Request bodies ───────────────────────────────────────────────────────────

class CommandIn(BaseModel):
    op: str = Field(description="set_watch | set_rating | set_favorite | set_resume")
    user: Optional[str] = Field(default=None, description="Username; omit for owner.")
    stage: bool = Field(default=True, description="Stage the change instead of sending it live.")
    payload: Dict[str, Any] = Field(default_factory=dict)


class RawCallIn(BaseModel):
    op: str
    user: Optional[str] = None
    params: Optional[Dict[str, Any]] = None


class MembersIn(BaseModel):
    add_item_ids: Optional[List[str]] = None
    remove_item_ids: Optional[List[str]] = None
    library_id: Optional[str] = None
    user: Optional[str] = None


class SyncIn(BaseModel):
    users: Optional[List[str]] = None


class UserPlaylistSyncIn(BaseModel):
    user: Optional[str] = Field(default=None, description="Username; omit for owner.")


class DiscardIn(BaseModel):
    staged_id: Optional[int] = None


class CreatePlaylistIn(BaseModel):
    name: str = Field(description="Name for the new playlist.")
    item_ids: Optional[List[str]] = None
    library_id: Optional[str] = None
    user: Optional[str] = None


class SmartFilterRowIn(BaseModel):
    field: str = Field(description="Plex filter field key, e.g. 'genre'.")
    op: str = Field(default="", description="Operator suffix, e.g. '>>'.")
    value: Any = Field(default="", description="Filter value.")


class CreateSmartPlaylistIn(BaseModel):
    name: str = Field(description="Name for the new smart playlist.")
    library_id: str
    libtype: Optional[str] = Field(default=None, description="track / album / artist.")
    match: str = Field(default="and", description="'and' = match all, 'or' = match any.")
    rows: List[SmartFilterRowIn] = Field(default_factory=list)
    sort: Optional[List[str]] = None
    limit: Optional[int] = None
    user: Optional[str] = None


# ── Discovery ────────────────────────────────────────────────────────────────

@router.get("/servers")
def dc_list_servers(_user: Dict[str, Any] = Depends(_require_root)):
    _guard()
    return {"servers": dev_console.list_servers()}


@router.get("/servers/{server_id}")
def dc_server_detail(
    server_id: str, _user: Dict[str, Any] = Depends(_require_root),
):
    _guard()
    return _call(dev_console.server_detail, server_id)


@router.get("/servers/{server_id}/libraries")
def dc_list_libraries(
    server_id: str, _user: Dict[str, Any] = Depends(_require_root),
):
    _guard()
    return {"libraries": _call(dev_console.list_libraries, server_id)}


@router.get("/servers/{server_id}/users")
def dc_list_users(
    server_id: str, _user: Dict[str, Any] = Depends(_require_root),
):
    _guard()
    return {"users": _call(dev_console.list_server_users, server_id)}


@router.get("/servers/{server_id}/sync-status")
def dc_sync_status(
    server_id: str, _user: Dict[str, Any] = Depends(_require_root),
):
    _guard()
    return _call(dev_console.sync_status, server_id)


@router.post("/servers/{server_id}/sync")
def dc_request_sync(
    server_id: str,
    body: SyncIn,
    _user: Dict[str, Any] = Depends(_require_root),
):
    _guard()
    result = _call(dev_console.request_server_sync, server_id, users=body.users)
    _publish({"type": "sync", "server_id": server_id, "phase": "requested"})
    return result


@router.post("/servers/{server_id}/reset")
def dc_reset_mirror(
    server_id: str, _user: Dict[str, Any] = Depends(_require_root),
):
    """Delete the server's mirror cache and queue a fresh full sync."""
    _guard()
    result = _call(dev_console.reset_mirror, server_id)
    _publish({"type": "sync", "server_id": server_id, "phase": "requested"})
    return result


@router.post("/servers/{server_id}/sync-user-playlists")
def dc_sync_user_playlists(
    server_id: str,
    body: UserPlaylistSyncIn,
    _user: Dict[str, Any] = Depends(_require_root),
):
    """Fast path: mirror one user's playlists synchronously, with no
    full library / item-state walk. Backs the By-playlist view's
    user switch."""
    _guard()
    return _call(dev_console.sync_user_playlists, server_id, body.user)


# ── Item explorer ────────────────────────────────────────────────────────────

@router.get("/servers/{server_id}/libraries/{library_id}/items")
def dc_explore_items(
    server_id: str,
    library_id: str,
    user: Optional[str] = None,
    all_users: bool = False,
    categories: Optional[str] = None,
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
    _user: Dict[str, Any] = Depends(_require_root),
):
    _guard()
    cats = [c.strip() for c in categories.split(",") if c.strip()] \
        if categories else None
    return _call(
        dev_console.explore_items, server_id, library_id,
        username=user, all_users=all_users, categories=cats,
        min_plays=min_plays, has_rating=has_rating,
        in_playlist=in_playlist, in_collection=in_collection,
        artist=artist, album=album, show_title=show_title,
        season_index=season_index,
        search=search, sort=sort, offset=offset, page_size=page_size,
    )


@router.get("/servers/{server_id}/libraries/{library_id}/groups")
def dc_list_groups(
    server_id: str,
    library_id: str,
    level: str,
    parent: str = "",
    user: Optional[str] = None,
    all_users: bool = False,
    categories: Optional[str] = None,
    _user: Dict[str, Any] = Depends(_require_root),
):
    _guard()
    cats = [c.strip() for c in categories.split(",") if c.strip()] \
        if categories else None
    return _call(
        dev_console.list_groups, server_id, library_id,
        level=level, parent=parent, username=user,
        all_users=all_users, categories=cats,
    )


@router.get("/servers/{server_id}/items/{item_id}")
def dc_item_detail(
    server_id: str,
    item_id: str,
    user: Optional[str] = None,
    _user: Dict[str, Any] = Depends(_require_root),
):
    _guard()
    return _call(dev_console.item_detail, server_id, item_id, username=user)


# ── Per-item command (immediate or staged) ───────────────────────────────────

@router.post("/servers/{server_id}/libraries/{library_id}/items/{item_id}/command")
def dc_command(
    server_id: str,
    library_id: str,
    item_id: str,
    body: CommandIn,
    _user: Dict[str, Any] = Depends(_require_root),
):
    _guard()
    result = _call(
        dev_console.run_command, server_id, body.op, library_id, item_id,
        username=body.user, payload=body.payload, stage=body.stage,
    )
    _publish({"type": "command", **result})
    return result


@router.post("/servers/{server_id}/items/{item_id}/raw")
def dc_raw_call(
    server_id: str,
    item_id: str,
    body: RawCallIn,
    _user: Dict[str, Any] = Depends(_require_root),
):
    _guard()
    result = _call(
        dev_console.raw_api_call, server_id, body.op,
        item_id=item_id, username=body.user, params=body.params,
    )
    _publish({"type": "raw", **result})
    return result


# ── Staged changes ───────────────────────────────────────────────────────────

@router.get("/servers/{server_id}/staged")
def dc_list_staged(
    server_id: str, _user: Dict[str, Any] = Depends(_require_root),
):
    _guard()
    return _call(dev_console.list_staged, server_id)


@router.post("/servers/{server_id}/staged/send")
def dc_send_staged(
    server_id: str, _user: Dict[str, Any] = Depends(_require_root),
):
    _guard()
    result = _call(dev_console.send_staged, server_id)
    _publish({"type": "staged_sent", "server_id": server_id,
              "sent": result.get("sent", 0), "failed": result.get("failed", 0)})
    return result


@router.post("/servers/{server_id}/staged/discard")
def dc_discard_staged(
    server_id: str,
    body: DiscardIn,
    _user: Dict[str, Any] = Depends(_require_root),
):
    _guard()
    result = _call(dev_console.discard_staged, server_id, body.staged_id)
    _publish({"type": "staged_discarded", "server_id": server_id,
              "discarded": result.get("discarded", 0)})
    return result


# ── Playlists + collections ──────────────────────────────────────────────────

@router.get("/servers/{server_id}/playlists")
def dc_list_playlists(
    server_id: str, _user: Dict[str, Any] = Depends(_require_root),
):
    _guard()
    return {"playlists": _call(dev_console.list_playlists, server_id)}


@router.post("/servers/{server_id}/playlists")
def dc_create_playlist(
    server_id: str,
    body: CreatePlaylistIn,
    _user: Dict[str, Any] = Depends(_require_root),
):
    _guard()
    result = _call(
        dev_console.create_playlist, server_id, body.name,
        item_ids=body.item_ids, library_id=body.library_id,
        username=body.user,
    )
    _publish({"type": "command", **result})
    return result


@router.get(
    "/servers/{server_id}/libraries/{library_id}/smart-playlist-fields"
)
def dc_smart_playlist_fields(
    server_id: str,
    library_id: str,
    libtype: Optional[str] = None,
    user: Optional[str] = None,
    _user: Dict[str, Any] = Depends(_require_root),
):
    """Enumerate the Plex smart-playlist filter vocabulary for a
    library + libtype, so the builder offers only valid filters."""
    _guard()
    return _call(
        dev_console.smart_playlist_fields, server_id, library_id,
        libtype=libtype, username=user,
    )


@router.post("/servers/{server_id}/smart-playlists")
def dc_create_smart_playlist(
    server_id: str,
    body: CreateSmartPlaylistIn,
    _user: Dict[str, Any] = Depends(_require_root),
):
    """Create a smart (filter-defined) playlist from operator-built
    filter rows."""
    _guard()
    result = _call(
        dev_console.create_smart_playlist, server_id, body.name,
        library_id=body.library_id, libtype=body.libtype,
        match=body.match,
        rows=[{"field": r.field, "op": r.op, "value": r.value}
              for r in body.rows],
        sort=body.sort, limit=body.limit, username=body.user,
    )
    _publish({"type": "command", **result})
    return result


@router.get("/servers/{server_id}/collections")
def dc_list_collections(
    server_id: str, _user: Dict[str, Any] = Depends(_require_root),
):
    _guard()
    return {"collections": _call(dev_console.list_collections, server_id)}


@router.post("/servers/{server_id}/playlists/{playlist_id}/members")
def dc_playlist_members(
    server_id: str,
    playlist_id: str,
    body: MembersIn,
    _user: Dict[str, Any] = Depends(_require_root),
):
    _guard()
    result = _call(
        dev_console.modify_playlist_members, server_id, playlist_id,
        add_item_ids=body.add_item_ids, remove_item_ids=body.remove_item_ids,
        library_id=body.library_id, username=body.user,
    )
    _publish({"type": "command", **result})
    return result


@router.post("/servers/{server_id}/collections/{collection_id}/members")
def dc_collection_members(
    server_id: str,
    collection_id: str,
    body: MembersIn,
    _user: Dict[str, Any] = Depends(_require_root),
):
    _guard()
    result = _call(
        dev_console.modify_collection_members, server_id, collection_id,
        add_item_ids=body.add_item_ids, remove_item_ids=body.remove_item_ids,
        library_id=body.library_id,
    )
    _publish({"type": "command", **result})
    return result
