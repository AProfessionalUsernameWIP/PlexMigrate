"""
server/dev_console_db.py - per-server mirror database for the
"Server Commands" developer console.

Why a dedicated mirror
----------------------
The console panel browses a server's whole library: every item, its
per-user watch / rating / favorite / resume state, and playlist /
collection membership. Reading that live on every panel interaction
would hammer the media-server API and risk real rate limiting.

Instead each server gets its own SQLite mirror, modeled on the
snapshot schema. A background worker (``server/dev_console_sync.py``)
refreshes the mirror on a tunable cadence; the panel reads the mirror,
so normal browsing makes zero media-server API calls. Writes go to the
live server, then update the mirror row, then broadcast.

Staging
-------
Commands can be STAGED rather than sent live. A staged command is a
row in ``staged_changes``; the panel overlays pending staged rows on
the synced base when it lists items, so the operator sees the state
the mirror WILL have once they hit Send. While a server has pending
staged changes the sync worker freezes that server's mirror, so a
background refresh can never revert the operator's staged edits.

Storage
-------
One file per server: ``server_data/server_commands/sc_<server_id>.db``.
Created lazily - only when the console actually touches a server - so
mirrors never exist for installs that do not use the console.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from server.persistence import get_data_dir
from server._db_connect import open_db

log = logging.getLogger("plexmigrate.server.dev_console_db")

SCHEMA_VERSION = 4
_SUBDIR = "server_commands"
_ALL_TABLES = (
    "meta", "libraries", "users", "items", "item_user_state",
    "playlists", "playlist_items", "collections", "collection_items",
    "staged_changes",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS libraries (
    library_id     TEXT PRIMARY KEY,
    name           TEXT NOT NULL DEFAULT '',
    type           TEXT NOT NULL DEFAULT '',
    item_count     INTEGER,
    last_synced_at REAL
);

-- PRIMARY KEY is ``username`` (unique per server and the field the
-- panel selects by), not ``backend_user_id``: a backend can surface
-- the same account via more than one path, so its user ids are not
-- reliably unique. ``backend_user_id`` stays as a plain column.
CREATE TABLE IF NOT EXISTS users (
    username        TEXT PRIMARY KEY,
    backend_user_id TEXT NOT NULL DEFAULT '',
    display_name    TEXT NOT NULL DEFAULT '',
    role            TEXT NOT NULL DEFAULT 'managed',
    is_admin        INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS items (
    backend_item_id TEXT PRIMARY KEY,
    library_id      TEXT NOT NULL DEFAULT '',
    title           TEXT NOT NULL DEFAULT '',
    type            TEXT NOT NULL DEFAULT '',
    year            INTEGER,
    guids_json      TEXT NOT NULL DEFAULT '[]',
    show_title      TEXT NOT NULL DEFAULT '',
    season_index    INTEGER,
    episode_index   INTEGER,
    artist          TEXT NOT NULL DEFAULT '',
    album           TEXT NOT NULL DEFAULT '',
    file_path       TEXT NOT NULL DEFAULT '',
    sort_title      TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_items_library ON items(library_id);

-- Per-user item state, keyed by ``username`` - NOT backend_user_id,
-- which Plex leaves blank for every account. Keying by the blank id
-- would collapse every Plex user's state into one row; username is
-- the reliable key.
CREATE TABLE IF NOT EXISTS item_user_state (
    backend_item_id TEXT NOT NULL,
    username        TEXT NOT NULL,
    view_count      INTEGER NOT NULL DEFAULT 0,
    last_viewed_at  REAL,
    view_offset_ms  INTEGER NOT NULL DEFAULT 0,
    user_rating     REAL,
    is_favorite     INTEGER NOT NULL DEFAULT 0,
    synced_at       REAL,
    PRIMARY KEY (backend_item_id, username)
);

-- Playlists are per-user on every backend. The mirror keys them by
-- ``owner_username`` - the username, NOT backend_user_id: Plex leaves
-- backend_user_id blank for every account, so the user table is keyed
-- by username and playlists must follow or every user's playlists
-- collapse together.
CREATE TABLE IF NOT EXISTS playlists (
    playlist_id    TEXT PRIMARY KEY,
    name           TEXT NOT NULL DEFAULT '',
    is_smart       INTEGER NOT NULL DEFAULT 0,
    playlist_type  TEXT NOT NULL DEFAULT '',
    owner_username TEXT NOT NULL DEFAULT '',
    item_count     INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS playlist_items (
    playlist_id     TEXT NOT NULL,
    backend_item_id TEXT NOT NULL,
    position        INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (playlist_id, backend_item_id)
);
CREATE INDEX IF NOT EXISTS idx_playlist_items_item
    ON playlist_items(backend_item_id);

CREATE TABLE IF NOT EXISTS collections (
    collection_id TEXT PRIMARY KEY,
    name          TEXT NOT NULL DEFAULT '',
    library_id    TEXT,
    item_count    INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS collection_items (
    collection_id   TEXT NOT NULL,
    backend_item_id TEXT NOT NULL,
    PRIMARY KEY (collection_id, backend_item_id)
);
CREATE INDEX IF NOT EXISTS idx_collection_items_item
    ON collection_items(backend_item_id);

CREATE TABLE IF NOT EXISTS staged_changes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at      REAL NOT NULL,
    library_id      TEXT NOT NULL DEFAULT '',
    backend_item_id TEXT NOT NULL,
    item_title      TEXT NOT NULL DEFAULT '',
    backend_user_id TEXT NOT NULL DEFAULT '',
    username        TEXT NOT NULL DEFAULT '',
    op              TEXT NOT NULL,
    payload_json    TEXT NOT NULL DEFAULT '{}',
    status          TEXT NOT NULL DEFAULT 'pending',
    result_detail   TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_staged_status ON staged_changes(status);
"""

# Op -> the item_user_state column its payload overlays.
_STAGE_OP_FIELD = {
    "set_watch": "view_count",
    "set_rating": "user_rating",
    "set_favorite": "is_favorite",
    "set_resume": "view_offset_ms",
}

# Predicate against the per-user state alias ``s`` (single-user view).
_CATEGORY_SINGLE = {
    "watch": "COALESCE(s.view_count, 0) > 0",
    "ratings": "(s.user_rating IS NOT NULL AND s.user_rating > 0)",
    "favorites": "COALESCE(s.is_favorite, 0) = 1",
    "resume": "COALESCE(s.view_offset_ms, 0) > 0",
}
# Per-state-column predicate used inside an EXISTS for the all-users
# view (an item qualifies when ANY user matches).
_CATEGORY_ANY_USER = {
    "watch": "st.view_count > 0",
    "ratings": "(st.user_rating IS NOT NULL AND st.user_rating > 0)",
    "favorites": "st.is_favorite = 1",
    "resume": "st.view_offset_ms > 0",
}
_PLAYLIST_MEMBER_SQL = (
    "i.backend_item_id IN (SELECT backend_item_id FROM playlist_items)"
)

# Whitelisted ORDER BY fragments for the flat item query. The key is
# the only operator-supplied input; the value is a literal, so the
# sort can never be an injection vector.
_ORDER_BY = {
    "name_asc": "i.sort_title, i.title",
    "name_desc": "i.sort_title DESC, i.title DESC",
}


def _category_filter_sql(
    categories: Optional[List[str]], all_users: bool,
) -> str:
    """Build the OR-joined WHERE fragment for the selected explorer
    categories, or '' when nothing is selected (whole library)."""
    if not categories:
        return ""
    conds: List[str] = []
    for cat in categories:
        if cat == "playlists":
            conds.append(_PLAYLIST_MEMBER_SQL)
        elif all_users:
            inner = _CATEGORY_ANY_USER.get(cat)
            if inner:
                conds.append(
                    "EXISTS(SELECT 1 FROM item_user_state st "
                    "WHERE st.backend_item_id = i.backend_item_id "
                    f"AND {inner})"
                )
        else:
            single = _CATEGORY_SINGLE.get(cat)
            if single:
                conds.append(single)
    return "(" + " OR ".join(conds) + ")" if conds else ""


# ── Connection registry (one connection per server) ──────────────────────────

_conns: Dict[str, sqlite3.Connection] = {}
_conns_lock = threading.RLock()


def _safe_id(server_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", str(server_id or "unknown"))


def _db_dir() -> Path:
    return get_data_dir() / _SUBDIR


def db_path(server_id: str) -> Path:
    return _db_dir() / f"sc_{_safe_id(server_id)}.db"


def _open(server_id: str) -> sqlite3.Connection:
    path = db_path(server_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = open_db(
        path,
        label=f"sc_{_safe_id(server_id)}.db",
        check_same_thread=False,
        foreign_keys=False,
        synchronous_normal=True,
        chmod_sidecars=True,
    )
    _migrate_if_needed(conn)
    conn.executescript(_SCHEMA)
    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )
    conn.execute(
        "INSERT OR IGNORE INTO meta(key, value) VALUES('server_id', ?)",
        (str(server_id),),
    )
    return conn


def _migrate_if_needed(conn: sqlite3.Connection) -> None:
    """The mirror is a disposable cache. On a schema-version bump just
    drop every table and let ``executescript`` + the next sync rebuild
    it; there is no in-place migration to maintain."""
    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'",
        ).fetchone()
    except sqlite3.OperationalError:
        return  # no meta table -> fresh file
    if row is None:
        return
    try:
        on_disk = int(row[0])
    except (TypeError, ValueError):
        on_disk = 0
    if on_disk == SCHEMA_VERSION:
        return
    log.info(
        "dev_console mirror schema %s -> %s: rebuilding cache",
        on_disk, SCHEMA_VERSION,
    )
    for tbl in _ALL_TABLES:
        conn.execute(f"DROP TABLE IF EXISTS {tbl}")


def _conn(server_id: str) -> sqlite3.Connection:
    """Lazily open (creating the file + schema on first touch) and
    return the cached connection for one server's mirror."""
    sid = str(server_id)
    with _conns_lock:
        c = _conns.get(sid)
        if c is None:
            c = _open(sid)
            _conns[sid] = c
            log.info("dev_console mirror opened: %s", db_path(sid))
        return c


def mirror_exists(server_id: str) -> bool:
    """True when a mirror file is already on disk for this server."""
    return db_path(server_id).exists()


def mirror_server_ids() -> List[str]:
    """Server ids that already have a mirror file. Used by the sync
    worker to know which mirrors to keep fresh."""
    out: List[str] = []
    d = _db_dir()
    if not d.exists():
        return out
    for f in d.glob("sc_*.db"):
        try:
            c = sqlite3.connect(str(f), timeout=5.0)
            try:
                row = c.execute(
                    "SELECT value FROM meta WHERE key='server_id'",
                ).fetchone()
            finally:
                c.close()
            if row and row[0]:
                out.append(str(row[0]))
        except sqlite3.Error:
            continue
    return out


def close_all_for_tests() -> None:
    """Test-only: drop every cached connection."""
    with _conns_lock:
        for c in _conns.values():
            try:
                c.close()
            except sqlite3.Error:
                pass
        _conns.clear()


def reset_mirror(server_id: str) -> bool:
    """Close and delete this server's mirror file (plus its WAL / SHM
    sidecars) so the next access builds a fresh one. The mirror is a
    disposable cache; this is the clean reset when a schema change or
    a partial sync has left it in a bad state. Returns True when a
    mirror file was actually removed."""
    sid = str(server_id)
    path = db_path(sid)
    removed = False
    with _conns_lock:
        c = _conns.pop(sid, None)
        if c is not None:
            try:
                c.close()
            except sqlite3.Error:
                pass
        for p in (path,
                  path.with_name(path.name + "-wal"),
                  path.with_name(path.name + "-shm")):
            try:
                if p.exists():
                    p.unlink()
                    if p == path:
                        removed = True
            except OSError as exc:
                log.warning(
                    "dev_console: could not remove mirror file %s: %s",
                    p, exc,
                )
    if removed:
        log.info("dev_console mirror reset: removed %s", path)
    return removed


def is_corruption_error(exc: BaseException) -> bool:
    """True when a SQLite exception means the mirror file itself is
    corrupt or unreadable, as opposed to a transient lock, a missing
    table, or a logic error.

    Corruption is the only case where :func:`reset_mirror` - deleting
    the file and rebuilding - is the correct recovery. A lock or a
    logic error must NOT trigger a destructive reset, so the caller
    gates the reset on this predicate. ``OperationalError`` (locks,
    missing tables) is a ``DatabaseError`` subclass, hence the
    message check rather than a bare ``isinstance``."""
    if not isinstance(exc, sqlite3.DatabaseError):
        return False
    msg = str(exc).lower()
    return (
        "malformed" in msg
        or "disk image" in msg
        or "not a database" in msg
        or "file is encrypted" in msg
    )


# ── Meta ─────────────────────────────────────────────────────────────────────

def set_meta(server_id: str, key: str, value: Any) -> None:
    _conn(server_id).execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, json.dumps(value)),
    )


def get_meta(server_id: str, key: str) -> Any:
    row = _conn(server_id).execute(
        "SELECT value FROM meta WHERE key=?", (key,),
    ).fetchone()
    if row is None or row[0] is None:
        return None
    try:
        return json.loads(row[0])
    except (TypeError, ValueError):
        return row[0]


# ── Sync writers (called by dev_console_sync) ────────────────────────────────

def replace_libraries(server_id: str, libraries: List[Dict[str, Any]]) -> None:
    c = _conn(server_id)
    # Dedupe on library_id (last wins) so a backend that lists a
    # library twice cannot trip the primary-key constraint.
    by_id: Dict[str, Dict[str, Any]] = {
        str(l.get("library_id")): l for l in libraries
    }
    with c:
        c.execute("DELETE FROM libraries")
        c.executemany(
            "INSERT OR REPLACE INTO libraries(library_id, name, type, "
            "item_count, last_synced_at) VALUES(?, ?, ?, ?, ?)",
            [
                (lid, l.get("name") or "", l.get("type") or "",
                 l.get("item_count"), l.get("last_synced_at"))
                for lid, l in by_id.items()
            ],
        )


def replace_users(server_id: str, users: List[Dict[str, Any]]) -> None:
    c = _conn(server_id)
    # A backend's user list can carry duplicate or blank
    # backend_user_ids (Plex surfaces the same account via more than
    # one path), so dedupe on username - unique per server - and
    # prefer the entry that actually carries a backend_user_id.
    by_username: Dict[str, Dict[str, Any]] = {}
    for u in users:
        uname = u.get("username") or ""
        existing = by_username.get(uname)
        if existing is None:
            by_username[uname] = u
        elif not (existing.get("backend_user_id") or "") \
                and (u.get("backend_user_id") or ""):
            by_username[uname] = u
    with c:
        c.execute("DELETE FROM users")
        c.executemany(
            "INSERT OR REPLACE INTO users(username, backend_user_id, "
            "display_name, role, is_admin) VALUES(?, ?, ?, ?, ?)",
            [
                (uname, u.get("backend_user_id") or "",
                 u.get("display_name") or "", u.get("role") or "managed",
                 1 if u.get("is_admin") else 0)
                for uname, u in by_username.items()
            ],
        )


def replace_library_items(
    server_id: str, library_id: str, items: List[Dict[str, Any]],
) -> None:
    """Replace every item row for one library. Per-user state for
    items that vanished is dropped too so the mirror cannot leak
    stale rows for deleted items."""
    c = _conn(server_id)
    with c:
        old = [
            r[0] for r in c.execute(
                "SELECT backend_item_id FROM items WHERE library_id=?",
                (library_id,),
            ).fetchall()
        ]
        if old:
            c.executemany(
                "DELETE FROM item_user_state WHERE backend_item_id=?",
                [(i,) for i in old],
            )
        c.execute("DELETE FROM items WHERE library_id=?", (library_id,))
        c.executemany(
            "INSERT OR REPLACE INTO items(backend_item_id, library_id, "
            "title, type, year, guids_json, show_title, season_index, "
            "episode_index, artist, album, file_path, sort_title) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    it["backend_item_id"], library_id,
                    it.get("title") or "", it.get("type") or "",
                    it.get("year"),
                    json.dumps(list(it.get("guids") or ())),
                    it.get("show_title") or "", it.get("season_index"),
                    it.get("episode_index"), it.get("artist") or "",
                    it.get("album") or "", it.get("file_path") or "",
                    (it.get("title") or "").lower(),
                )
                for it in items
            ],
        )
        c.execute(
            "UPDATE libraries SET item_count=?, last_synced_at=? "
            "WHERE library_id=?",
            (len(items), time.time(), library_id),
        )


def upsert_item_states(
    server_id: str, username: str, states: List[Dict[str, Any]],
) -> None:
    """Write one user's per-item state, keyed by ``username``.
    ``states`` carries one dict per item the user has data for."""
    c = _conn(server_id)
    now = time.time()
    with c:
        c.executemany(
            "INSERT INTO item_user_state(backend_item_id, username, "
            "view_count, last_viewed_at, view_offset_ms, user_rating, "
            "is_favorite, synced_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(backend_item_id, username) DO UPDATE SET "
            "view_count=excluded.view_count, "
            "last_viewed_at=excluded.last_viewed_at, "
            "view_offset_ms=excluded.view_offset_ms, "
            "user_rating=excluded.user_rating, "
            "is_favorite=excluded.is_favorite, synced_at=excluded.synced_at",
            [
                (
                    s["backend_item_id"], username,
                    int(s.get("view_count") or 0), s.get("last_viewed_at"),
                    int(s.get("view_offset_ms") or 0), s.get("user_rating"),
                    1 if s.get("is_favorite") else 0, now,
                )
                for s in states
            ],
        )


def apply_state_to_mirror(
    server_id: str, backend_item_id: str, username: str,
    fields: Dict[str, Any],
) -> None:
    """Write-through update of one item's state after a live command
    (immediate mode), keyed by ``username``. ``fields`` carries any of
    view_count / last_viewed_at / view_offset_ms / user_rating /
    is_favorite."""
    c = _conn(server_id)
    cols = {
        "view_count", "last_viewed_at", "view_offset_ms",
        "user_rating", "is_favorite",
    }
    use = {k: v for k, v in fields.items() if k in cols}
    if not use:
        return
    with c:
        c.execute(
            "INSERT OR IGNORE INTO item_user_state(backend_item_id, "
            "username) VALUES(?, ?)",
            (backend_item_id, username),
        )
        sets = ", ".join(f"{k}=?" for k in use)
        c.execute(
            f"UPDATE item_user_state SET {sets}, synced_at=? "
            "WHERE backend_item_id=? AND username=?",
            [*use.values(), time.time(), backend_item_id, username],
        )


def replace_playlists(
    server_id: str, owner_username: str, playlists: List[Dict[str, Any]],
) -> None:
    """Replace ONE user's playlists. Playlists are per-user on every
    backend, so the mirror keys them by ``owner_username`` - the
    username, NOT backend_user_id, which Plex leaves blank for every
    account. This drops only the given user's playlist rows + their
    items and re-inserts. ``playlists`` dicts carry ``item_ids``
    (ordered)."""
    if not (owner_username or "").strip():
        # CONSOLE-11: an empty owner_username would DELETE then re-INSERT
        # under the "" key, collapsing every blank-username user's
        # mirrored playlists into one shared bucket. Fail loud rather
        # than silently corrupt the mirror.
        raise ValueError(
            "replace_playlists requires a non-empty owner_username"
        )
    c = _conn(server_id)
    with c:
        old = [
            r[0] for r in c.execute(
                "SELECT playlist_id FROM playlists WHERE owner_username=?",
                (owner_username,),
            ).fetchall()
        ]
        if old:
            c.executemany(
                "DELETE FROM playlist_items WHERE playlist_id=?",
                [(pid,) for pid in old],
            )
        c.execute(
            "DELETE FROM playlists WHERE owner_username=?", (owner_username,),
        )
        for p in playlists:
            c.execute(
                "INSERT OR REPLACE INTO playlists(playlist_id, name, "
                "is_smart, playlist_type, owner_username, item_count) "
                "VALUES(?, ?, ?, ?, ?, ?)",
                (p["playlist_id"], p.get("name") or "",
                 1 if p.get("is_smart") else 0,
                 p.get("playlist_type") or "", owner_username,
                 len(p.get("item_ids") or ())),
            )
            c.executemany(
                "INSERT OR REPLACE INTO playlist_items(playlist_id, "
                "backend_item_id, position) VALUES(?, ?, ?)",
                [
                    (p["playlist_id"], iid, pos)
                    for pos, iid in enumerate(p.get("item_ids") or ())
                ],
            )
    # Record that this user's playlists have been mirrored - even a
    # user with zero playlists counts as synced. The "By playlist"
    # view checks this dedicated set, not the item-state synced set,
    # so a user with item state but no mirrored playlists still gets
    # a playlist re-sync.
    if owner_username:
        cur = get_meta(server_id, "playlist_synced_users")
        users = set(cur) if isinstance(cur, list) else set()
        if owner_username not in users:
            users.add(owner_username)
            set_meta(server_id, "playlist_synced_users", sorted(users))


def playlist_synced_usernames(server_id: str) -> List[str]:
    """Usernames whose playlists have been mirrored at least once.
    Distinct from ``synced_usernames`` (per-item state)."""
    if not mirror_exists(server_id):
        return []
    cur = get_meta(server_id, "playlist_synced_users")
    return list(cur) if isinstance(cur, list) else []


def replace_collections(
    server_id: str, collections: List[Dict[str, Any]],
) -> None:
    c = _conn(server_id)
    with c:
        c.execute("DELETE FROM collections")
        c.execute("DELETE FROM collection_items")
        for col in collections:
            c.execute(
                "INSERT OR REPLACE INTO collections(collection_id, name, "
                "library_id, item_count) VALUES(?, ?, ?, ?)",
                (col["collection_id"], col.get("name") or "",
                 col.get("library_id"), len(col.get("item_ids") or ())),
            )
            c.executemany(
                "INSERT OR REPLACE INTO collection_items(collection_id, "
                "backend_item_id) VALUES(?, ?)",
                [(col["collection_id"], iid)
                 for iid in (col.get("item_ids") or ())],
            )


def mark_full_sync(server_id: str) -> None:
    set_meta(server_id, "last_full_sync_at", time.time())


def synced_usernames(server_id: str) -> List[str]:
    """Usernames that already have per-item state rows in the mirror.
    The sync worker re-syncs these so a user the operator has looked
    at stays fresh."""
    if not mirror_exists(server_id):
        return []
    rows = _conn(server_id).execute(
        "SELECT DISTINCT username FROM item_user_state",
    ).fetchall()
    return [r[0] for r in rows if r[0]]


# ── Readers (called by the service) ──────────────────────────────────────────

def list_libraries(server_id: str) -> List[Dict[str, Any]]:
    rows = _conn(server_id).execute(
        "SELECT library_id, name, type, item_count, last_synced_at "
        "FROM libraries ORDER BY name",
    ).fetchall()
    return [dict(r) for r in rows]


def list_users(server_id: str) -> List[Dict[str, Any]]:
    rows = _conn(server_id).execute(
        "SELECT backend_user_id, username, display_name, role, is_admin "
        "FROM users ORDER BY role DESC, username",
    ).fetchall()
    return [
        {**dict(r), "is_admin": bool(r["is_admin"])} for r in rows
    ]


def resolve_user(server_id: str, username: Optional[str]) -> Dict[str, Any]:
    """Resolve a username to the mirror's user row. ``username`` None
    or empty resolves to the owner. Returns ``{}`` when unresolvable."""
    c = _conn(server_id)
    if not username:
        row = c.execute(
            "SELECT * FROM users WHERE role='owner' LIMIT 1",
        ).fetchone()
    else:
        row = c.execute(
            "SELECT * FROM users WHERE username=? LIMIT 1", (username,),
        ).fetchone()
    return dict(row) if row else {}


def _staged_overlay(
    server_id: str, username: str,
) -> Dict[str, Dict[str, Any]]:
    """Return ``{item_id: {field: value}}`` for pending staged changes
    scoped to one user (by ``username``). Later staged rows win over
    earlier ones."""
    rows = _conn(server_id).execute(
        "SELECT backend_item_id, op, payload_json FROM staged_changes "
        "WHERE status='pending' AND username=? ORDER BY id",
        (username,),
    ).fetchall()
    overlay: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        field = _STAGE_OP_FIELD.get(r["op"])
        if not field:
            continue
        try:
            payload = json.loads(r["payload_json"])
        except (TypeError, ValueError):
            payload = {}
        slot = overlay.setdefault(r["backend_item_id"], {})
        if r["op"] == "set_watch":
            slot["view_count"] = int(payload.get("view_count") or 0)
            if payload.get("last_played") is not None:
                slot["last_viewed_at"] = payload.get("last_played")
        elif r["op"] == "set_rating":
            slot["user_rating"] = payload.get("rating")
        elif r["op"] == "set_favorite":
            slot["is_favorite"] = 1 if payload.get("favorite") else 0
        elif r["op"] == "set_resume":
            slot["view_offset_ms"] = int(payload.get("offset_ms") or 0)
    return overlay


def _item_row_to_dict(r: sqlite3.Row) -> Dict[str, Any]:
    try:
        guids = json.loads(r["guids_json"])
    except (TypeError, ValueError):
        guids = []
    return {
        "backend_item_id": r["backend_item_id"],
        "library_id": r["library_id"],
        "title": r["title"],
        "type": r["type"],
        "year": r["year"],
        "guids": guids,
        "show_title": r["show_title"] or "",
        "season_index": r["season_index"],
        "episode_index": r["episode_index"],
        "artist": r["artist"] or "",
        "album": r["album"] or "",
        "view_count": int(r["view_count"] or 0),
        "last_viewed_at": r["last_viewed_at"],
        "view_offset_ms": int(r["view_offset_ms"] or 0),
        "user_rating": r["user_rating"],
        "is_favorite": bool(r["is_favorite"]),
        "in_any_playlist": bool(r["in_any_playlist"]),
    }


def query_items(
    server_id: str,
    library_id: str,
    *,
    username: str,
    categories: Optional[List[str]] = None,
    all_users: bool = False,
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
    """Paginated item query against the mirror. ``categories`` narrows
    the result set to items matching ANY selected explorer category
    (watched / rated / favorited / has a resume point / in a playlist);
    empty means the whole library. Filters run on the synced base
    values; the returned page then has pending staged changes overlaid
    so the operator sees the effective state."""
    c = _conn(server_id)
    where = ["i.library_id = ?"]
    args: List[Any] = [library_id]
    join = (
        "FROM items i LEFT JOIN item_user_state s "
        "ON s.backend_item_id = i.backend_item_id "
        "AND s.username = ?"
    )
    state_arg = [username]

    cat_sql = _category_filter_sql(categories, all_users)
    if cat_sql:
        where.append(cat_sql)

    if min_plays is not None:
        where.append("COALESCE(s.view_count, 0) >= ?")
        args.append(int(min_plays))
    if has_rating:
        where.append("s.user_rating IS NOT NULL AND s.user_rating > 0")
    if in_playlist:
        where.append(
            "i.backend_item_id IN (SELECT backend_item_id "
            "FROM playlist_items WHERE playlist_id = ?)"
        )
        args.append(in_playlist)
    if in_collection:
        where.append(
            "i.backend_item_id IN (SELECT backend_item_id "
            "FROM collection_items WHERE collection_id = ?)"
        )
        args.append(in_collection)
    # Hierarchy-viewer node scoping.
    if artist is not None:
        where.append("i.artist = ?")
        args.append(artist)
    if album is not None:
        where.append("i.album = ?")
        args.append(album)
    if show_title is not None:
        where.append("i.show_title = ?")
        args.append(show_title)
    if season_index is not None:
        where.append("i.season_index = ?")
        args.append(int(season_index))
    if search:
        where.append("(i.title LIKE ? OR i.show_title LIKE ? "
                      "OR i.artist LIKE ?)")
        like = f"%{search}%"
        args.extend([like, like, like])

    where_sql = " AND ".join(where)
    total = c.execute(
        f"SELECT COUNT(*) {join} WHERE {where_sql}",
        [*state_arg, *args],
    ).fetchone()[0]

    rows = c.execute(
        "SELECT i.backend_item_id, i.library_id, i.title, i.type, i.year, "
        "i.guids_json, i.show_title, i.season_index, i.episode_index, "
        "i.artist, i.album, "
        "COALESCE(s.view_count, 0) AS view_count, s.last_viewed_at, "
        "COALESCE(s.view_offset_ms, 0) AS view_offset_ms, s.user_rating, "
        "COALESCE(s.is_favorite, 0) AS is_favorite, "
        "EXISTS(SELECT 1 FROM playlist_items pli "
        "WHERE pli.backend_item_id = i.backend_item_id) AS in_any_playlist "
        f"{join} WHERE {where_sql} "
        f"ORDER BY {_ORDER_BY.get(sort, _ORDER_BY['name_asc'])} "
        "LIMIT ? OFFSET ?",
        [*state_arg, *args, max(1, int(page_size)), max(0, int(offset))],
    ).fetchall()

    overlay = _staged_overlay(server_id, username)
    items: List[Dict[str, Any]] = []
    for r in rows:
        d = _item_row_to_dict(r)
        staged = overlay.get(r["backend_item_id"])
        if staged:
            for k, v in staged.items():
                d[k] = bool(v) if k == "is_favorite" else v
            d["staged"] = True
        else:
            d["staged"] = False
        items.append(d)

    return {"items": items, "total": int(total or 0)}


# Per-level grouping config: (group column, sub-group column or None).
_GROUP_LEVELS = {
    "artist": ("i.artist", "i.album", "i.artist != ''"),
    "album": ("i.album", None, "i.album != ''"),
    "show": ("i.show_title", "i.season_index", "i.show_title != ''"),
    "season": ("i.season_index", None, None),
}


def list_groups(
    server_id: str,
    library_id: str,
    *,
    level: str,
    parent: str = "",
    categories: Optional[List[str]] = None,
    username: str = "",
    all_users: bool = False,
) -> List[Dict[str, Any]]:
    """Hierarchy-viewer node lists. ``level`` is one of:

      * ``artist`` - distinct artists in a music library.
      * ``album``  - distinct albums for ``parent`` (an artist).
      * ``show``   - distinct shows in a TV library.
      * ``season`` - distinct seasons for ``parent`` (a show).
      * ``playlist`` - playlists that have at least one item IN this
        library; ``key`` is the playlist id, ``count`` the in-library
        member count. Scoped to the library so the operator sees the
        playlists relevant to what they are browsing.

    Each entry is ``{key, label, count, sub_count}``: ``count`` is the
    leaf item count and ``sub_count`` the sub-group count.

    When ``categories`` is given the tree is PRUNED: a group is
    returned only if it contains at least one leaf item matching ANY
    selected category, and the counts reflect only matching items. So
    an artist whose tracks have nothing the operator filtered for
    simply does not appear - no drilling into empty branches."""
    c0 = _conn(server_id)
    if level == "playlist":
        # Playlists are per-user; scope to the viewing user (by
        # username - backend_user_id is blank on Plex) unless the
        # all-users view asked for everyone's.
        where = "i.library_id = ?"
        pl_args: List[Any] = [library_id]
        if username and not all_users:
            where += " AND p.owner_username = ?"
            pl_args.append(username)
        rows = c0.execute(
            "SELECT pi.playlist_id AS k, p.name AS label, COUNT(*) AS n "
            "FROM playlist_items pi "
            "JOIN items i ON i.backend_item_id = pi.backend_item_id "
            "JOIN playlists p ON p.playlist_id = pi.playlist_id "
            f"WHERE {where} "
            "GROUP BY pi.playlist_id ORDER BY p.name",
            pl_args,
        ).fetchall()
        return [{"key": r["k"], "label": r["label"] or r["k"],
                 "count": r["n"], "sub_count": 0} for r in rows]

    cfg = _GROUP_LEVELS.get(level)
    if cfg is None:
        return []
    group_col, sub_col, type_where = cfg
    c = _conn(server_id)

    cat_sql = _category_filter_sql(categories, all_users)
    join = ""
    join_args: List[Any] = []
    if cat_sql and not all_users:
        join = ("LEFT JOIN item_user_state s "
                "ON s.backend_item_id = i.backend_item_id "
                "AND s.username = ?")
        join_args = [username]

    where = ["i.library_id = ?"]
    args: List[Any] = [library_id]
    if level == "album":
        where.append("i.artist = ?")
        args.append(parent)
    elif level == "season":
        where.append("i.show_title = ?")
        args.append(parent)
    if type_where:
        where.append(type_where)
    if cat_sql:
        where.append(cat_sql)

    sub_expr = f"COUNT(DISTINCT {sub_col})" if sub_col else "0"
    rows = c.execute(
        f"SELECT {group_col} AS k, COUNT(*) AS n, {sub_expr} AS subn "
        f"FROM items i {join} WHERE {' AND '.join(where)} "
        f"GROUP BY {group_col} ORDER BY {group_col}",
        [*join_args, *args],
    ).fetchall()

    if level == "season":
        out: List[Dict[str, Any]] = []
        for r in rows:
            idx = r["k"]
            out.append({
                "key": "" if idx is None else str(idx),
                "label": "Season ?" if idx is None else f"Season {idx}",
                "count": r["n"], "sub_count": 0,
            })
        return out
    return [{"key": r["k"], "label": r["k"], "count": r["n"],
             "sub_count": r["subn"]} for r in rows]


def item_user_breakdown(
    server_id: str, item_ids: List[str],
) -> Dict[str, List[Dict[str, Any]]]:
    """For the "All users" explorer view: return ``{item_id: [per-user
    state, ...]}`` listing only users that actually have non-default
    state on that item (watched / rated / favorited / has a resume
    point). Users whose row is all-zero are omitted so the column
    shows only the users who matter for that item."""
    if not item_ids:
        return {}
    c = _conn(server_id)
    placeholders = ",".join("?" * len(item_ids))
    rows = c.execute(
        "SELECT s.backend_item_id, s.username AS username, "
        "COALESCE(u.display_name, '') AS display_name, "
        "s.view_count, s.last_viewed_at, s.view_offset_ms, "
        "s.user_rating, s.is_favorite "
        "FROM item_user_state s "
        "LEFT JOIN users u ON u.username = s.username "
        f"WHERE s.backend_item_id IN ({placeholders}) "
        "AND (s.view_count > 0 OR s.view_offset_ms > 0 "
        "OR (s.user_rating IS NOT NULL AND s.user_rating > 0) "
        "OR s.is_favorite = 1) "
        "ORDER BY s.username",
        list(item_ids),
    ).fetchall()
    out: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        out.setdefault(r["backend_item_id"], []).append({
            "username": r["username"] or "",
            "display_name": r["display_name"] or "",
            "view_count": int(r["view_count"] or 0),
            "last_viewed_at": r["last_viewed_at"],
            "view_offset_ms": int(r["view_offset_ms"] or 0),
            "user_rating": r["user_rating"],
            "is_favorite": bool(r["is_favorite"]),
        })
    return out


def get_item(
    server_id: str, backend_item_id: str, username: str,
) -> Optional[Dict[str, Any]]:
    c = _conn(server_id)
    r = c.execute(
        "SELECT i.backend_item_id, i.library_id, i.title, i.type, i.year, "
        "i.guids_json, i.show_title, i.season_index, i.episode_index, "
        "i.artist, i.album, "
        "COALESCE(s.view_count, 0) AS view_count, s.last_viewed_at, "
        "COALESCE(s.view_offset_ms, 0) AS view_offset_ms, s.user_rating, "
        "COALESCE(s.is_favorite, 0) AS is_favorite, "
        "EXISTS(SELECT 1 FROM playlist_items pli "
        "WHERE pli.backend_item_id = i.backend_item_id) AS in_any_playlist "
        "FROM items i LEFT JOIN item_user_state s "
        "ON s.backend_item_id = i.backend_item_id "
        "AND s.username = ? WHERE i.backend_item_id = ?",
        (username, backend_item_id),
    ).fetchone()
    if r is None:
        return None
    d = _item_row_to_dict(r)
    staged = _staged_overlay(server_id, username).get(backend_item_id)
    if staged:
        for k, v in staged.items():
            d[k] = bool(v) if k == "is_favorite" else v
        d["staged"] = True
    else:
        d["staged"] = False
    return d


def item_relationships(
    server_id: str, backend_item_id: str,
) -> Dict[str, Any]:
    """Playlists + collections that contain this item."""
    c = _conn(server_id)
    pls = c.execute(
        "SELECT p.playlist_id, p.name FROM playlist_items pi "
        "JOIN playlists p ON p.playlist_id = pi.playlist_id "
        "WHERE pi.backend_item_id = ? ORDER BY p.name",
        (backend_item_id,),
    ).fetchall()
    cols = c.execute(
        "SELECT col.collection_id, col.name FROM collection_items ci "
        "JOIN collections col ON col.collection_id = ci.collection_id "
        "WHERE ci.backend_item_id = ? ORDER BY col.name",
        (backend_item_id,),
    ).fetchall()
    return {
        "playlists": [dict(r) for r in pls],
        "collections": [dict(r) for r in cols],
    }


def list_playlists(server_id: str) -> List[Dict[str, Any]]:
    rows = _conn(server_id).execute(
        "SELECT playlist_id, name, is_smart, playlist_type, owner_username, "
        "item_count FROM playlists ORDER BY name",
    ).fetchall()
    return [{**dict(r), "is_smart": bool(r["is_smart"])} for r in rows]


def list_collections(server_id: str) -> List[Dict[str, Any]]:
    rows = _conn(server_id).execute(
        "SELECT collection_id, name, library_id, item_count "
        "FROM collections ORDER BY name",
    ).fetchall()
    return [dict(r) for r in rows]


def sync_status(server_id: str) -> Dict[str, Any]:
    """Freshness summary for the panel: last full sync, per-library
    timestamps, which users have synced state, pending staged count."""
    if not mirror_exists(server_id):
        return {
            "synced": False, "last_full_sync_at": None,
            "libraries": [], "synced_usernames": [], "pending_staged": 0,
        }
    c = _conn(server_id)
    libs = [
        {"library_id": r["library_id"], "name": r["name"],
         "last_synced_at": r["last_synced_at"]}
        for r in c.execute(
            "SELECT library_id, name, last_synced_at FROM libraries",
        ).fetchall()
    ]
    last_full = get_meta(server_id, "last_full_sync_at")
    pending = c.execute(
        "SELECT COUNT(*) FROM staged_changes WHERE status='pending'",
    ).fetchone()[0]
    return {
        "synced": last_full is not None,
        "last_full_sync_at": last_full,
        "libraries": libs,
        "synced_usernames": synced_usernames(server_id),
        "pending_staged": int(pending or 0),
    }


# ── Staged changes ───────────────────────────────────────────────────────────

def add_staged_change(
    server_id: str,
    *,
    library_id: str,
    backend_item_id: str,
    item_title: str,
    backend_user_id: str,
    username: str,
    op: str,
    payload: Dict[str, Any],
) -> int:
    """Record one pending staged command. Returns the staged row id."""
    c = _conn(server_id)
    cur = c.execute(
        "INSERT INTO staged_changes(created_at, library_id, "
        "backend_item_id, item_title, backend_user_id, username, op, "
        "payload_json, status) VALUES(?, ?, ?, ?, ?, ?, ?, ?, 'pending')",
        (time.time(), library_id, backend_item_id, item_title or "",
         backend_user_id or "", username or "", op, json.dumps(payload)),
    )
    return int(cur.lastrowid)


def list_staged(
    server_id: str, *, status: str = "pending",
) -> List[Dict[str, Any]]:
    if not mirror_exists(server_id):
        return []
    rows = _conn(server_id).execute(
        "SELECT * FROM staged_changes WHERE status=? ORDER BY id",
        (status,),
    ).fetchall()
    out: List[Dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        try:
            d["payload"] = json.loads(d.pop("payload_json"))
        except (TypeError, ValueError):
            d["payload"] = {}
        out.append(d)
    return out


def pending_staged(server_id: str) -> List[Dict[str, Any]]:
    return list_staged(server_id, status="pending")


def has_pending_staged(server_id: str) -> bool:
    """True when this server has uncommitted staged changes. The sync
    worker treats such a server as frozen."""
    if not mirror_exists(server_id):
        return False
    n = _conn(server_id).execute(
        "SELECT COUNT(*) FROM staged_changes WHERE status='pending'",
    ).fetchone()[0]
    return bool(n)


def mark_staged(
    server_id: str, staged_id: int, status: str, detail: str = "",
) -> None:
    _conn(server_id).execute(
        "UPDATE staged_changes SET status=?, result_detail=? WHERE id=?",
        (status, detail or "", int(staged_id)),
    )


def discard_staged(server_id: str, staged_id: Optional[int] = None) -> int:
    """Delete pending staged changes. ``staged_id`` None drops them
    all. Returns the count removed."""
    c = _conn(server_id)
    with c:
        if staged_id is None:
            cur = c.execute(
                "DELETE FROM staged_changes WHERE status='pending'",
            )
        else:
            cur = c.execute(
                "DELETE FROM staged_changes WHERE id=? AND status='pending'",
                (int(staged_id),),
            )
    return cur.rowcount or 0
