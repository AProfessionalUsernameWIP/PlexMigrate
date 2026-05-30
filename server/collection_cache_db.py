"""
Collection-children cache database (Movies N+1 fix).

Background
----------
``snapshot_collections`` calls ``collection.items()`` once per
collection. Each call hits Plex's
``/library/metadata/{collection_id}/children`` endpoint. For a
316-collection Movies library this costs 290-376 seconds, and Plex
serializes these requests server-side, so client-side threading does
not help.

The fix: cache each collection's children list keyed on the
collection's own ``updatedAt`` timestamp (cheap, comes back in the
lightweight ``section.collections()`` response without an extra HTTP
call). On the next snapshot, if ``collection.updatedAt`` hasn't
advanced past the cached row, skip the ``.items()`` call entirely and
build the serialized dict from the cached payload. Movie collections
rarely change between snapshots, so steady-state cache hit-rate
is expected to be ~95%+, dropping the Movies collections phase from
~370s to ~10s.

Cache freshness
---------------
The cache row's ``updated_at`` is Plex's value at fetch time. On a
read, we compare against the LIVE collection's ``updatedAt``:

  cached.updated_at >= live.updatedAt  →  cache hit (use cached)
  cached.updated_at <  live.updatedAt  →  cache miss (refetch + rewrite)

There's no TTL. Plex's ``updatedAt`` is the source of truth; we
trust it. If the operator wants to force-invalidate the cache,
:func:`invalidate_collection_cache` clears per-server or globally.

Storage
-------
Separate file ``server_data/collection_cache.db`` so it can be
backed up / wiped independently of media.db, playlist_cache.db, and
server_mirror.db. WAL mode, owner-only chmod, matches the existing
SQLite stores' pattern.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from server.persistence import get_data_dir
from server._db_connect import apply_additive_columns, open_db


log = logging.getLogger("plexmigrate.server.collection_cache_db")


_DB_NAME = "collection_cache.db"


def _db_path() -> Path:
    return get_data_dir() / _DB_NAME


_DB_LOCK = threading.Lock()
_conn: Optional[sqlite3.Connection] = None
_init_lock = threading.Lock()
_initialised = False


_SCHEMA = """
CREATE TABLE IF NOT EXISTS collection_cache (
    server_id              TEXT NOT NULL,
    collection_rating_key  TEXT NOT NULL,
    name                   TEXT NOT NULL,
    section_id             TEXT,
    sort_order             INTEGER NOT NULL DEFAULT 0,
    item_count             INTEGER NOT NULL DEFAULT 0,
    -- Plex's collection.updatedAt at fetch time. The next snapshot
    -- compares live updatedAt against this value; if equal-or-older
    -- the cache is valid.
    updated_at             REAL NOT NULL,
    -- When WE cached this row. Used for observability + optional
    -- TTL-based eviction (not currently enforced; the updatedAt
    -- comparison handles invalidation).
    fetched_at             REAL NOT NULL,
    -- Ownership attribution: who owns this collection on
    -- Plex's side. ``"_owner"`` is the sentinel for library-wide /
    -- admin-owned (auto-generated server-wide) collections; for a
    -- personal collection owned by a managed user this is that
    -- user's ``librarySectionUserID`` rendered as str so two users
    -- whose IDs differ never collide on the label. Operator surface:
    -- the Servers > Overview cache panel breaks down per server +
    -- per owner_user_id so the operator can see which collections
    -- the owner caches vs which each managed user caches.
    owner_user_id          TEXT NOT NULL DEFAULT '_owner',
    PRIMARY KEY (server_id, collection_rating_key)
);

CREATE TABLE IF NOT EXISTS collection_cache_items (
    server_id              TEXT NOT NULL,
    collection_rating_key  TEXT NOT NULL,
    position               INTEGER NOT NULL,
    title                  TEXT NOT NULL,
    type                   TEXT,
    rating_key             TEXT,
    guids_json             TEXT NOT NULL DEFAULT '[]',
    filepath               TEXT,
    year                   INTEGER,
    PRIMARY KEY (server_id, collection_rating_key, position),
    FOREIGN KEY (server_id, collection_rating_key)
        REFERENCES collection_cache(server_id, collection_rating_key)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_collection_cache_server
    ON collection_cache(server_id);
CREATE INDEX IF NOT EXISTS idx_collection_cache_fetched_at
    ON collection_cache(fetched_at);
CREATE INDEX IF NOT EXISTS idx_collection_cache_owner
    ON collection_cache(server_id, owner_user_id);
"""


def init_collection_cache_db() -> None:
    """Open the shared connection, enable WAL + FK, create schema.
    Idempotent."""
    global _conn, _initialised
    with _init_lock:
        if _initialised:
            return
        path = _db_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = open_db(
            path,
            label="collection_cache.db",
            check_same_thread=False,
            foreign_keys=True,
            synchronous_normal=True,
            chmod_sidecars=True,
        )
        # Additive migration for installs that pre-date the
        # owner_user_id column. MUST run before executescript(_SCHEMA):
        # _SCHEMA's ``CREATE INDEX ... ON collection_cache(server_id,
        # owner_user_id)`` references the column, so an older
        # collection_cache already on disk needs the column added
        # first. apply_additive_columns skips the table when it does
        # not exist yet (fresh install); the schema script below then
        # creates it complete.
        apply_additive_columns(conn, [
            ("collection_cache", "owner_user_id TEXT NOT NULL DEFAULT '_owner'"),
        ])
        conn.executescript(_SCHEMA)
        _conn = conn
        _initialised = True
        log.info("collection_cache.db initialised at %s", path)


def _require_conn() -> sqlite3.Connection:
    if _conn is None:
        raise RuntimeError(
            "collection_cache_db.init_collection_cache_db() must be "
            "called before any other public API in this module."
        )
    return _conn


def _close_for_tests() -> None:
    """Test-only: close the shared connection so a fresh test gets
    a fresh DB."""
    global _conn, _initialised
    with _init_lock:
        if _conn is not None:
            try:
                _conn.close()
            except sqlite3.Error:
                pass
        _conn = None
        _initialised = False


# ── Public API ──────────────────────────────────────────────────────────────


def lookup_cached_collection(
    server_id: str, collection_rating_key: str,
    *, live_updated_at: float,
) -> Optional[Dict[str, Any]]:
    """Return the cached collection's serialized dict if and only if
    the cache row's ``updated_at`` is >= ``live_updated_at``. On
    miss (no row, or stale), returns ``None`` and the caller must
    refetch via plexapi.

    The returned dict matches ``services.resolver.serialize_collection``'s
    shape so callers can append it directly to the snapshot result
    list. Fields: ``name``, ``rating_key``, ``sort_order``, ``items``
    (list of {title, type, guids, filepath, rating_key, year} dicts).

    Best-effort: any DB error returns None (treat as cache miss).
    """
    if not server_id or not collection_rating_key:
        return None
    try:
        conn = _require_conn()
    except RuntimeError:
        return None
    try:
        row = conn.execute(
            "SELECT name, sort_order, item_count, updated_at, "
            "       fetched_at, owner_user_id "
            "FROM collection_cache "
            "WHERE server_id = ? AND collection_rating_key = ?",
            (server_id, collection_rating_key),
        ).fetchone()
    except sqlite3.OperationalError as exc:
        log.debug("collection_cache lookup failed: %s", exc)
        return None
    if row is None:
        return None
    if (row["updated_at"] or 0.0) < float(live_updated_at or 0.0):
        # Stale: live collection has been edited since we cached it.
        return None
    try:
        item_rows = conn.execute(
            "SELECT title, type, rating_key, guids_json, filepath, year "
            "FROM collection_cache_items "
            "WHERE server_id = ? AND collection_rating_key = ? "
            "ORDER BY position ASC",
            (server_id, collection_rating_key),
        ).fetchall()
    except sqlite3.OperationalError as exc:
        log.debug("collection_cache items fetch failed: %s", exc)
        return None
    items: List[Dict[str, Any]] = []
    for r in item_rows:
        try:
            guids = json.loads(r["guids_json"] or "[]")
            if not isinstance(guids, list):
                guids = []
        except (TypeError, ValueError):
            guids = []
        items.append({
            "title": r["title"],
            "type": r["type"],
            "guids": guids,
            "filepath": r["filepath"],
            "rating_key": r["rating_key"],
            "year": r["year"],
        })
    return {
        "name": row["name"],
        "rating_key": collection_rating_key,
        "sort_order": row["sort_order"],
        "items": items,
        # Ownership attribution. Surfaced so the snapshotter
        # / warmer can verify the cached entry's owner before reusing
        # it; today's snapshotter treats it as observational only.
        "owner_user_id": row["owner_user_id"] or "_owner",
    }


def write_collection_cache(
    server_id: str, collection_rating_key: str,
    *,
    serialized: Dict[str, Any],
    live_updated_at: float,
    section_id: Optional[str] = None,
    owner_user_id: str = "_owner",
) -> bool:
    """Persist a freshly-fetched collection's serialized dict.

    Replaces any prior row + items for this (server_id, rating_key)
    in one transaction. Returns True on success, False on any DB
    error (logged at DEBUG).

    ``owner_user_id`` records who owns the collection on the Plex side
    (default ``"_owner"`` is the sentinel for library-wide /
    admin-owned). The warmer + per-user snapshotter call sites pass
    the actual ``librarySectionUserID`` so the cache reflects the same
    ownership model the operator sees in their snapshot.
    """
    if not server_id or not collection_rating_key:
        return False
    try:
        conn = _require_conn()
    except RuntimeError:
        return False
    items = serialized.get("items") or []
    name = serialized.get("name") or ""
    sort_order = int(serialized.get("sort_order") or 0)
    now = time.time()
    owner_label = (owner_user_id or "_owner").strip() or "_owner"
    with _DB_LOCK:
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT OR REPLACE INTO collection_cache("
                "server_id, collection_rating_key, name, section_id, "
                "sort_order, item_count, updated_at, fetched_at, "
                "owner_user_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (server_id, collection_rating_key, name, section_id,
                 sort_order, len(items), float(live_updated_at), now,
                 owner_label),
            )
            # Delete prior item rows for this collection (CASCADE
            # would also handle this but explicit is clearer).
            conn.execute(
                "DELETE FROM collection_cache_items "
                "WHERE server_id = ? AND collection_rating_key = ?",
                (server_id, collection_rating_key),
            )
            for pos, item in enumerate(items):
                guids = item.get("guids") or []
                conn.execute(
                    "INSERT INTO collection_cache_items("
                    "server_id, collection_rating_key, position, "
                    "title, type, rating_key, guids_json, filepath, year) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (server_id, collection_rating_key, pos,
                     item.get("title") or "",
                     item.get("type"),
                     str(item.get("rating_key")) if item.get("rating_key") is not None else None,
                     json.dumps(guids),
                     item.get("filepath"),
                     item.get("year")),
                )
            conn.execute("COMMIT")
            return True
        except sqlite3.OperationalError as exc:
            log.debug("collection_cache write failed: %s", exc)
            try:
                conn.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass
            return False


def invalidate_collection_cache(
    server_id: Optional[str] = None,
) -> int:
    """Drop cached rows. ``server_id=None`` clears every server.
    Returns count of cache rows deleted (item rows cascade)."""
    try:
        conn = _require_conn()
    except RuntimeError:
        return 0
    with _DB_LOCK:
        try:
            if server_id is None:
                cur = conn.execute("DELETE FROM collection_cache")
            else:
                cur = conn.execute(
                    "DELETE FROM collection_cache WHERE server_id = ?",
                    (server_id,),
                )
            return cur.rowcount or 0
        except sqlite3.OperationalError as exc:
            log.warning("collection_cache invalidate failed: %s", exc)
            return 0


def cache_stats() -> Dict[str, int]:
    """Diagnostic helper: returns total cached collection rows +
    item rows. Used by the dashboard debug surface."""
    try:
        conn = _require_conn()
    except RuntimeError:
        return {"collections": 0, "items": 0}
    try:
        coll_count = conn.execute(
            "SELECT COUNT(*) FROM collection_cache",
        ).fetchone()[0]
        item_count = conn.execute(
            "SELECT COUNT(*) FROM collection_cache_items",
        ).fetchone()[0]
        return {"collections": int(coll_count), "items": int(item_count)}
    except sqlite3.OperationalError as exc:
        # A COUNT(*) error means the cache tables are sick (missing /
        # corrupt / locked), not empty. Log so the zero is not read
        # as an empty cache.
        log.warning("cache_stats: collection_cache COUNT(*) failed: %s", exc)
        return {"collections": 0, "items": 0}


def count_per_section(server_id: str) -> Dict[str, int]:
    """Return ``{section_id: collection_count}`` for one server,
    indexed by stringified section_id. Empty dict on cache miss /
    DB hiccup so callers can fall through to a live query.

    Used by ``/api/library-mapping/sides`` to enrich the per-library
    leaf_counts payload with collection counts pulled from
    collection_cache.db when the cache has been warmed (operator
    triggered via Servers > Overview > bulk cache, or via the
    per-server cache buttons). Cache-first beats Plex live calls
    because the cache is comprehensive + already on disk."""
    try:
        conn = _require_conn()
    except RuntimeError:
        return {}
    try:
        rows = conn.execute(
            "SELECT section_id, COUNT(*) AS n "
            "FROM collection_cache "
            "WHERE server_id = ? AND section_id IS NOT NULL AND section_id <> '' "
            "GROUP BY section_id",
            (server_id,),
        ).fetchall()
    except sqlite3.OperationalError as exc:
        log.debug("collection_cache count_per_section failed: %s", exc)
        return {}
    return {
        str(r["section_id"]): int(r["n"] or 0)
        for r in rows
    }


def stats_per_server() -> List[Dict[str, Any]]:
    """One row per server with cached collections. Each row carries
    server_id, cached collection count, total item count across those
    collections, last_fetched_at (most recent fetch_at), and
    oldest_fetched_at. Operator-facing - drives the Servers panel
    cache status badge."""
    try:
        conn = _require_conn()
    except RuntimeError:
        return []
    try:
        rows = conn.execute(
            "SELECT cc.server_id AS server_id, "
            "COUNT(cc.collection_rating_key) AS collections, "
            "COALESCE(SUM(cc.item_count), 0) AS items, "
            "MAX(cc.fetched_at) AS last_fetched_at, "
            "MIN(cc.fetched_at) AS oldest_fetched_at "
            "FROM collection_cache cc "
            "GROUP BY cc.server_id "
            "ORDER BY cc.server_id"
        ).fetchall()
        return [
            {
                "server_id": r["server_id"],
                "collections": int(r["collections"] or 0),
                "items": int(r["items"] or 0),
                "last_fetched_at": float(r["last_fetched_at"] or 0.0),
                "oldest_fetched_at": float(r["oldest_fetched_at"] or 0.0),
            }
            for r in rows
        ]
    except sqlite3.OperationalError as exc:
        log.debug("collection_cache stats_per_server failed: %s", exc)
        return []


def stats_per_server_per_owner() -> List[Dict[str, Any]]:
    """Per-(server, owner_user_id) breakdown so the operator can see
    how many collections each owning user has cached on each server.
    ``owner_user_id == "_owner"`` is the library-wide / auto-generated
    bucket; everything else is a managed user's
    ``librarySectionUserID``.

    Each row::

        {server_id, owner_user_id, collections, items,
         last_fetched_at, oldest_fetched_at}

    Drives the Collection cache per-user breakdown surface on the
    Servers > Overview panel."""
    try:
        conn = _require_conn()
    except RuntimeError:
        return []
    try:
        rows = conn.execute(
            "SELECT cc.server_id AS server_id, "
            "cc.owner_user_id AS owner_user_id, "
            "COUNT(cc.collection_rating_key) AS collections, "
            "COALESCE(SUM(cc.item_count), 0) AS items, "
            "MAX(cc.fetched_at) AS last_fetched_at, "
            "MIN(cc.fetched_at) AS oldest_fetched_at "
            "FROM collection_cache cc "
            "GROUP BY cc.server_id, cc.owner_user_id "
            "ORDER BY cc.server_id, cc.owner_user_id"
        ).fetchall()
        return [
            {
                "server_id": r["server_id"],
                "owner_user_id": r["owner_user_id"] or "_owner",
                "collections": int(r["collections"] or 0),
                "items": int(r["items"] or 0),
                "last_fetched_at": float(r["last_fetched_at"] or 0.0),
                "oldest_fetched_at": float(r["oldest_fetched_at"] or 0.0),
            }
            for r in rows
        ]
    except sqlite3.OperationalError as exc:
        log.debug(
            "collection_cache stats_per_server_per_owner failed: %s", exc,
        )
        return []
