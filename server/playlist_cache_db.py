"""
Playlist cache database (Plan[PLAYLIST-MANAGEMENT]-2026-05-16, section 3.4).

Backs the Playlist Management sub-tab. Holds a TTL-bounded cache of
per-user playlist rosters + items so the end user's "show user X's
playlists" view does not have to hit the live backend on every
refresh, and so the snapshot path can opt to read from cache when
fresh (per ``playlist_cache_snapshot_threshold_seconds`` tunable).

Lives in its own SQLite DB (``server_data/playlist_cache.db``), not
in media.db, for the same reasons run_timings.db is separated: it's
operational telemetry / cache with its own retention story, and
mixing it into the content store would complicate the
schema-migration story.

Schema
------
Three tables:

* ``playlist_cache`` (one row per cached playlist):
    PRIMARY KEY (server_id, user_id, playlist_id).

* ``playlist_cache_items`` (one row per playlist item; ordered by
    position):
    PRIMARY KEY (server_id, user_id, playlist_id, position) with
    FOREIGN KEY back to ``playlist_cache`` and ON DELETE CASCADE so
    a re-upsert can clear the prior item rows in one step.

* ``playlist_cache_refresh`` (one row per (server_id, user_id) pair):
    Tracks the last refresh time + duration + last error per user
    so the UI can show "fresh / stale / failed" badges and the
    snapshot path can decide live-vs-cache without scanning the
    item rows.

``server_id`` carries the prefixed UID per
Plan[SERVER-UID-IDENTITY] (`<service_type>_<uuid_hex>`). The boot
UID-migration helpers in ``server/server_registry.py`` call
:func:`rewrite_server_ids_in_playlist_cache` so this DB stays in
sync after a bare-UUID upgrade.

Concurrency
-----------
One shared connection per process with ``check_same_thread=False``,
matching ``server/media_db.py``'s pattern. Writers serialise on
``_DB_LOCK``; readers go straight to the connection (WAL handles
read isolation). :func:`init_playlist_cache_db` is idempotent and
serialised by ``_init_lock``.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from server.persistence import get_data_dir


log = logging.getLogger("plexmigrate.server.playlist_cache_db")


_DB_NAME = "playlist_cache.db"


def _db_path() -> Path:
    return get_data_dir() / _DB_NAME


_DB_LOCK = threading.Lock()
_conn: Optional[sqlite3.Connection] = None
_init_lock = threading.Lock()
_initialised = False


_SCHEMA = """
CREATE TABLE IF NOT EXISTS playlist_cache (
    server_id          TEXT NOT NULL,
    user_id            TEXT NOT NULL,
    playlist_id        TEXT NOT NULL,
    name               TEXT NOT NULL,
    is_smart           INTEGER NOT NULL DEFAULT 0,
    item_count         INTEGER NOT NULL DEFAULT 0,
    fetched_at         REAL NOT NULL,
    -- 2026-05-16 (end user request): identity-link columns. Cache rows
    -- now carry the canonical app_user_uuid (from managed_users) plus
    -- the auth context that was used at fetch time. Lookups can match
    -- either user_id (legacy) or app_user_uuid (canonical). auth_kind
    -- + role_flags help debug stale-token / mismatched-role bugs.
    app_user_uuid      TEXT,
    auth_kind          TEXT,
    role_flags         INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (server_id, user_id, playlist_id)
);

CREATE TABLE IF NOT EXISTS playlist_cache_items (
    server_id          TEXT NOT NULL,
    user_id            TEXT NOT NULL,
    playlist_id        TEXT NOT NULL,
    position           INTEGER NOT NULL,
    title              TEXT NOT NULL,
    guids_json         TEXT NOT NULL,
    type               TEXT,
    duration_ms        INTEGER,
    app_user_uuid      TEXT,
    PRIMARY KEY (server_id, user_id, playlist_id, position),
    FOREIGN KEY (server_id, user_id, playlist_id)
        REFERENCES playlist_cache(server_id, user_id, playlist_id)
        ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS playlist_cache_refresh (
    server_id          TEXT NOT NULL,
    user_id            TEXT NOT NULL,
    last_refreshed_at  REAL NOT NULL,
    last_refresh_ms    INTEGER NOT NULL,
    error              TEXT,
    app_user_uuid      TEXT,
    auth_kind          TEXT,
    role_flags         INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (server_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_playlist_cache_fetched_at
    ON playlist_cache(fetched_at);
CREATE INDEX IF NOT EXISTS idx_playlist_cache_refresh_age
    ON playlist_cache_refresh(last_refreshed_at);
CREATE INDEX IF NOT EXISTS idx_playlist_cache_app_user_uuid
    ON playlist_cache(server_id, app_user_uuid)
    WHERE app_user_uuid IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_playlist_cache_refresh_app_user_uuid
    ON playlist_cache_refresh(server_id, app_user_uuid)
    WHERE app_user_uuid IS NOT NULL;
"""


# Bitmask values for the ``role_flags`` column. Combined OR-style so a
# user that's both admin AND owner (J/E permits this) carries 0b11.
ROLE_FLAG_ADMIN = 1
ROLE_FLAG_OWNER = 2


def _role_flags_from_user(is_admin: bool, role: Optional[str]) -> int:
    """Pack ``(is_admin, role)`` into the ``role_flags`` integer column.

    Mirrors the UserSpec semantics: Plex servers have a single ``owner``
    who is also admin; Jellyfin / Emby allow ``admin`` and ``owner``
    flags to be set independently per user. Storing both bits lets the
    cache row record the resolved identity unambiguously at fetch time.
    """
    flags = 0
    if is_admin:
        flags |= ROLE_FLAG_ADMIN
    if (role or "").lower() == "owner":
        flags |= ROLE_FLAG_OWNER
    return flags


# Additive schema migrations. ``init_playlist_cache_db`` runs the base
# DDL above (CREATE TABLE IF NOT EXISTS preserves existing rows) and
# then walks this list to ADD COLUMN any new columns onto pre-existing
# tables. Each entry is (table_name, column_definition); SQLite ignores
# duplicate ADD COLUMN attempts cleanly because we catch the "duplicate
# column name" exception.
_ADDITIVE_COLUMN_MIGRATIONS: List[Tuple[str, str]] = [
    ("playlist_cache", "app_user_uuid TEXT"),
    ("playlist_cache", "auth_kind TEXT"),
    ("playlist_cache", "role_flags INTEGER NOT NULL DEFAULT 0"),
    ("playlist_cache_items", "app_user_uuid TEXT"),
    ("playlist_cache_refresh", "app_user_uuid TEXT"),
    ("playlist_cache_refresh", "auth_kind TEXT"),
    ("playlist_cache_refresh", "role_flags INTEGER NOT NULL DEFAULT 0"),
]


def init_playlist_cache_db() -> None:
    """Open the shared connection, enable WAL + FK enforcement, create
    schema. Idempotent: safe to call from FastAPI startup even when a
    test has already initialised the DB."""
    global _conn, _initialised
    with _init_lock:
        if _initialised:
            return
        path = _db_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            str(path),
            timeout=30.0,
            isolation_level=None,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        # Owner-only on POSIX. Same defensive chmod as media_db.
        for _p in (path, path.with_name(path.name + "-wal"),
                   path.with_name(path.name + "-shm")):
            try:
                if _p.exists():
                    os.chmod(_p, 0o600)
            except OSError:
                pass
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        # FK enforcement matters for the ON DELETE CASCADE on
        # playlist_cache_items; SQLite defaults to off.
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(_SCHEMA)
        # Additive migrations for pre-existing databases. CREATE TABLE
        # IF NOT EXISTS above is a no-op when the tables already exist,
        # so any NEW columns we tacked onto the schema definition need
        # to be applied by hand to existing rows. Each ADD COLUMN is
        # wrapped in its own try/except so a partial migration history
        # doesn't block startup.
        for table, column_def in _ADDITIVE_COLUMN_MIGRATIONS:
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column_def}")
            except sqlite3.OperationalError as exc:
                msg = str(exc).lower()
                if "duplicate column name" in msg:
                    continue
                log.warning(
                    "playlist_cache_db: ADD COLUMN %s on %s failed: %s",
                    column_def, table, exc,
                )
        _conn = conn
        _initialised = True
        log.info("playlist_cache.db initialised at %s", path)


def _require_conn() -> sqlite3.Connection:
    if _conn is None:
        raise RuntimeError(
            "playlist_cache_db.init_playlist_cache_db() must be called "
            "before any other public API in this module."
        )
    return _conn


def _close_for_tests() -> None:
    """Close the shared connection so a test that swaps the data dir
    can recreate the DB from scratch. Not called by production code."""
    global _conn, _initialised
    with _init_lock:
        if _conn is not None:
            try:
                _conn.close()
            except Exception:
                pass
        _conn = None
        _initialised = False


# ── Cache write API ─────────────────────────────────────────────────────────

def upsert_playlist(
    *,
    server_id: str,
    user_id: str,
    playlist_id: str,
    name: str,
    is_smart: bool,
    items: List[Dict[str, Any]],
    fetched_at: Optional[float] = None,
    app_user_uuid: Optional[str] = None,
    auth_kind: Optional[str] = None,
    role_flags: int = 0,
) -> None:
    """Replace a cached playlist + its items in one transaction.

    ``items`` is a list of dicts with keys ``title`` (str), ``guids``
    (list[str]), and optionally ``type`` (str) + ``duration_ms`` (int).
    Position is assigned by enumeration order.

    The (server_id, user_id, playlist_id) row is upserted first; then
    the item rows for that key are deleted (CASCADE-style would also
    trigger from a DELETE on the parent but we want to preserve the
    parent row across re-fetches) and re-inserted in one transaction.
    """
    if not server_id or not user_id or not playlist_id:
        raise ValueError("server_id, user_id, playlist_id are all required")
    if fetched_at is None:
        fetched_at = time.time()
    conn = _require_conn()
    with _DB_LOCK:
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT INTO playlist_cache
                    (server_id, user_id, playlist_id, name, is_smart,
                     item_count, fetched_at,
                     app_user_uuid, auth_kind, role_flags)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(server_id, user_id, playlist_id) DO UPDATE SET
                    name          = excluded.name,
                    is_smart      = excluded.is_smart,
                    item_count    = excluded.item_count,
                    fetched_at    = excluded.fetched_at,
                    -- COALESCE so a re-fetch that didn't resolve the
                    -- uuid (e.g. live-only fallback) doesn't blow
                    -- away an existing tag.
                    app_user_uuid = COALESCE(excluded.app_user_uuid, playlist_cache.app_user_uuid),
                    auth_kind     = COALESCE(excluded.auth_kind, playlist_cache.auth_kind),
                    role_flags    = excluded.role_flags
                """,
                (
                    server_id, user_id, playlist_id,
                    name, 1 if is_smart else 0,
                    len(items), float(fetched_at),
                    app_user_uuid, auth_kind, int(role_flags or 0),
                ),
            )
            conn.execute(
                """
                DELETE FROM playlist_cache_items
                WHERE server_id = ? AND user_id = ? AND playlist_id = ?
                """,
                (server_id, user_id, playlist_id),
            )
            rows: List[Tuple[Any, ...]] = []
            for pos, it in enumerate(items):
                title = str(it.get("title") or "")
                guids = it.get("guids") or []
                if not isinstance(guids, (list, tuple)):
                    guids = []
                rows.append((
                    server_id, user_id, playlist_id, pos,
                    title,
                    json.dumps(list(guids), separators=(",", ":")),
                    it.get("type"),
                    int(it["duration_ms"]) if it.get("duration_ms") is not None else None,
                    app_user_uuid,
                ))
            if rows:
                conn.executemany(
                    """
                    INSERT INTO playlist_cache_items
                        (server_id, user_id, playlist_id, position,
                         title, guids_json, type, duration_ms,
                         app_user_uuid)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    rows,
                )
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise


def record_refresh(
    *,
    server_id: str,
    user_id: str,
    last_refreshed_at: Optional[float] = None,
    last_refresh_ms: int = 0,
    error: Optional[str] = None,
    app_user_uuid: Optional[str] = None,
    auth_kind: Optional[str] = None,
    role_flags: int = 0,
) -> None:
    """Upsert the per-user refresh marker. Call after every live
    fetch (success or failure) so the staleness check has a single
    source of truth.

    ``app_user_uuid`` / ``auth_kind`` / ``role_flags`` tag the row with
    the canonical identity + auth context that was active at fetch
    time. Used by :func:`get_refresh_marker_by_uuid` for UUID-based
    cache lookup."""
    if not server_id or not user_id:
        raise ValueError("server_id and user_id required")
    if last_refreshed_at is None:
        last_refreshed_at = time.time()
    conn = _require_conn()
    with _DB_LOCK:
        conn.execute(
            """
            INSERT INTO playlist_cache_refresh
                (server_id, user_id, last_refreshed_at, last_refresh_ms, error,
                 app_user_uuid, auth_kind, role_flags)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(server_id, user_id) DO UPDATE SET
                last_refreshed_at = excluded.last_refreshed_at,
                last_refresh_ms   = excluded.last_refresh_ms,
                app_user_uuid     = COALESCE(excluded.app_user_uuid, playlist_cache_refresh.app_user_uuid),
                auth_kind         = COALESCE(excluded.auth_kind, playlist_cache_refresh.auth_kind),
                role_flags        = excluded.role_flags,
                error             = excluded.error
            """,
            (
                server_id, user_id,
                float(last_refreshed_at),
                int(last_refresh_ms),
                error,
                app_user_uuid, auth_kind, int(role_flags or 0),
            ),
        )


# ── Cache read API ─────────────────────────────────────────────────────────

def get_refresh_marker(
    server_id: str,
    user_id: str,
    *,
    app_user_uuid: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Return the per-user refresh marker dict or None if absent.

    Same strict app_user_uuid resolution as
    :func:`list_cached_playlists` (2026-05-17 fix): when a uuid is
    known, match by uuid OR by user_id with NULL uuid (legacy rows).
    Never falls back to a generic user_id match against rows tagged
    with a DIFFERENT uuid — that was the cross-user contamination path.
    """
    if not server_id:
        return None
    conn = _require_conn()
    if app_user_uuid:
        row = conn.execute(
            """
            SELECT last_refreshed_at, last_refresh_ms, error,
                   app_user_uuid, auth_kind, role_flags
            FROM playlist_cache_refresh
            WHERE server_id = ?
              AND (
                  app_user_uuid = ?
                  OR (app_user_uuid IS NULL AND user_id = ?)
              )
            ORDER BY app_user_uuid IS NULL ASC
            LIMIT 1
            """,
            (server_id, app_user_uuid, user_id or ""),
        ).fetchone()
        if row is None:
            return None
        return _row_to_refresh_marker(row)
    if not user_id:
        return None
    row = conn.execute(
        """
        SELECT last_refreshed_at, last_refresh_ms, error,
               app_user_uuid, auth_kind, role_flags
        FROM playlist_cache_refresh
        WHERE server_id = ? AND user_id = ?
        """,
        (server_id, user_id),
    ).fetchone()
    if row is None:
        return None
    return _row_to_refresh_marker(row)


def _row_to_refresh_marker(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "last_refreshed_at": float(row["last_refreshed_at"]),
        "last_refresh_ms": int(row["last_refresh_ms"] or 0),
        "error": row["error"],
        "app_user_uuid": row["app_user_uuid"],
        "auth_kind": row["auth_kind"],
        "role_flags": int(row["role_flags"] or 0),
    }


def list_cached_playlists(
    server_id: str,
    user_id: str,
    *,
    app_user_uuid: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Return the cached playlists for one user as a list of dicts
    ``{playlist_id, name, is_smart, item_count, fetched_at, app_user_uuid,
       auth_kind, role_flags}``. Order is by ``name`` ASC.

    2026-05-17 fix (end user bug report — cross-user contamination):
    when ``app_user_uuid`` is provided, lookup is STRICT — we return
    rows tagged with that uuid OR rows whose user_id matches AND whose
    own app_user_uuid is NULL (legacy pre-schema-v2 rows that haven't
    been re-stamped yet). We DO NOT fall through to a generic user_id
    match against rows tagged with a DIFFERENT uuid — that's the path
    that surfaced one user's playlists under another user's card when
    cache_uid happened to alias across rows (e.g. owners cached under
    email key colliding with managed users whose backend_user_id alias
    matched).

    When ``app_user_uuid`` is None (caller hasn't resolved a uuid for
    this user, e.g. user not yet synced to managed_users), we fall
    back to pure user_id match — the legacy single-key path.
    """
    if not server_id:
        return []
    conn = _require_conn()
    if app_user_uuid:
        # Strict: rows matching THIS uuid, plus legacy untagged rows
        # whose user_id matches the caller's user_id. Excludes rows
        # whose own app_user_uuid is set to a DIFFERENT uuid — those
        # belong to another user.
        rows = conn.execute(
            """
            SELECT playlist_id, name, is_smart, item_count, fetched_at,
                   app_user_uuid, auth_kind, role_flags
            FROM playlist_cache
            WHERE server_id = ?
              AND (
                  app_user_uuid = ?
                  OR (app_user_uuid IS NULL AND user_id = ?)
              )
            ORDER BY name COLLATE NOCASE ASC
            """,
            (server_id, app_user_uuid, user_id or ""),
        ).fetchall()
        return [_row_to_cached_playlist(r) for r in rows]
    if not user_id:
        return []
    rows = conn.execute(
        """
        SELECT playlist_id, name, is_smart, item_count, fetched_at,
               app_user_uuid, auth_kind, role_flags
        FROM playlist_cache
        WHERE server_id = ? AND user_id = ?
        ORDER BY name COLLATE NOCASE ASC
        """,
        (server_id, user_id),
    ).fetchall()
    return [_row_to_cached_playlist(r) for r in rows]


def _row_to_cached_playlist(r: sqlite3.Row) -> Dict[str, Any]:
    return {
        "playlist_id": r["playlist_id"],
        "name": r["name"],
        "is_smart": bool(r["is_smart"]),
        "item_count": int(r["item_count"] or 0),
        "fetched_at": float(r["fetched_at"]),
        "app_user_uuid": r["app_user_uuid"],
        "auth_kind": r["auth_kind"],
        "role_flags": int(r["role_flags"] or 0),
    }


def get_cached_playlist_items(
    server_id: str,
    user_id: str,
    playlist_id: str,
) -> Optional[Dict[str, Any]]:
    """Return ``{name, is_smart, item_count, fetched_at, items: [...]}``
    or ``None`` if the playlist is not cached. ``items`` carries the
    full per-row payload sorted by position."""
    if not server_id or not user_id or not playlist_id:
        return None
    conn = _require_conn()
    head = conn.execute(
        """
        SELECT name, is_smart, item_count, fetched_at
        FROM playlist_cache
        WHERE server_id = ? AND user_id = ? AND playlist_id = ?
        """,
        (server_id, user_id, playlist_id),
    ).fetchone()
    if head is None:
        return None
    items_rows = conn.execute(
        """
        SELECT position, title, guids_json, type, duration_ms
        FROM playlist_cache_items
        WHERE server_id = ? AND user_id = ? AND playlist_id = ?
        ORDER BY position ASC
        """,
        (server_id, user_id, playlist_id),
    ).fetchall()
    items: List[Dict[str, Any]] = []
    for ir in items_rows:
        try:
            guids = json.loads(ir["guids_json"] or "[]")
        except (TypeError, ValueError):
            guids = []
        items.append({
            "position": int(ir["position"]),
            "title": ir["title"],
            "guids": guids,
            "type": ir["type"],
            "duration_ms": int(ir["duration_ms"]) if ir["duration_ms"] is not None else None,
        })
    return {
        "name": head["name"],
        "is_smart": bool(head["is_smart"]),
        "item_count": int(head["item_count"] or 0),
        "fetched_at": float(head["fetched_at"]),
        "items": items,
    }


def is_fresh(
    server_id: str,
    user_id: str,
    *,
    threshold_seconds: float,
    now: Optional[float] = None,
) -> bool:
    """Return True if the per-user refresh marker is within
    ``threshold_seconds`` of ``now``. False for missing marker, errored
    refresh, or stale marker. Used by the snapshot path to decide
    live-vs-cache without scanning the item rows."""
    if threshold_seconds <= 0:
        return False
    marker = get_refresh_marker(server_id, user_id)
    if marker is None:
        return False
    if marker.get("error"):
        return False
    if now is None:
        now = time.time()
    age = now - float(marker["last_refreshed_at"])
    return age >= 0 and age <= float(threshold_seconds)


def invalidate_older_than(max_age_seconds: float, now: Optional[float] = None) -> int:
    """Delete cache rows older than ``max_age_seconds``. Returns the
    number of (parent) playlist rows removed; item rows cascade via
    the FK. Refresh markers are independent and left untouched so the
    "last refresh failed N minutes ago" badge still has data."""
    if max_age_seconds <= 0:
        return 0
    if now is None:
        now = time.time()
    cutoff = now - float(max_age_seconds)
    conn = _require_conn()
    with _DB_LOCK:
        cur = conn.execute(
            "DELETE FROM playlist_cache WHERE fetched_at < ?",
            (cutoff,),
        )
        return cur.rowcount or 0


def clear_user_cache(
    server_id: str,
    user_id: Optional[str],
    *,
    app_user_uuid: Optional[str] = None,
) -> int:
    """Wipe every cached row for a single user before a re-insert.

    2026-05-17 (operator bug report): when a refresh discovers the
    user's playlist list has changed (or — the original bug — when
    pre-fix rows were written under the wrong user_id), the per-
    playlist upsert path only OVERWRITES matching playlist_ids. Stale
    rows with playlist_ids no longer in the live list survive forever.

    This helper deletes every (server_id, user_id) OR
    (server_id, app_user_uuid) row + their CASCADE-linked item rows +
    the refresh marker, so the next set of upserts starts from a
    clean slate for that user.

    Returns the total number of playlist rows deleted (items + marker
    not counted; CASCADE handles items, marker is a single row).
    Idempotent + best-effort: failures are swallowed and logged so the
    caller's live-fetch path proceeds even if cleanup mis-fires.
    """
    if not server_id:
        return 0
    conn = _require_conn()
    deleted = 0
    with _DB_LOCK:
        try:
            # Two WHERE clauses joined by OR: legacy rows (matched by
            # user_id only, app_user_uuid NULL) AND uuid-tagged rows
            # for this user (matched by app_user_uuid). Either branch
            # may be empty.
            params = [server_id]
            clauses = ["1=0"]
            if user_id:
                clauses.append("user_id = ?")
                params.append(user_id)
            if app_user_uuid:
                clauses.append("app_user_uuid = ?")
                params.append(app_user_uuid)
            where = " OR ".join(clauses)
            cur = conn.execute(
                f"DELETE FROM playlist_cache WHERE server_id = ? AND ({where})",
                params,
            )
            deleted = cur.rowcount or 0
            # The refresh marker should also be cleared so the next
            # cache-status check reflects the fresh state.
            conn.execute(
                f"DELETE FROM playlist_cache_refresh WHERE server_id = ? AND ({where})",
                params,
            )
        except Exception:
            log.exception(
                "clear_user_cache(%r, user_id=%r, uuid=%r) failed; "
                "stale rows may survive into the next read.",
                server_id, user_id, app_user_uuid,
            )
    return deleted


def invalidate_user(server_id: str, user_id: str) -> int:
    """Delete all cached playlists for (server_id, user_id). Returns
    the number of parent rows removed. Refresh marker is also cleared
    so a subsequent ``is_fresh`` returns False until the next live
    fetch records a new marker."""
    if not server_id or not user_id:
        return 0
    conn = _require_conn()
    with _DB_LOCK:
        cur = conn.execute(
            "DELETE FROM playlist_cache WHERE server_id = ? AND user_id = ?",
            (server_id, user_id),
        )
        conn.execute(
            "DELETE FROM playlist_cache_refresh WHERE server_id = ? AND user_id = ?",
            (server_id, user_id),
        )
        return cur.rowcount or 0


# ── Server-UID boot migration helper ────────────────────────────────────────

def rewrite_server_ids_in_playlist_cache(
    old_to_new: Dict[str, str],
) -> int:
    """Bulk-rewrite ``server_id`` references in this DB after the
    Plan[SERVER-UID-IDENTITY] boot migration upgrades bare-UUID server
    rows to the prefixed form. Mirrors
    ``server.media_db.rewrite_server_ids_in_identity_map``.

    Updates all three tables in one pass per pair, inside a single
    transaction. Returns the total number of rows updated across all
    three tables. Defensive: catches per-pair failures and continues
    so one stale id never blocks the rest of the migration."""
    if not old_to_new:
        return 0
    conn = _require_conn()
    n = 0
    with _DB_LOCK:
        for old_id, new_id in old_to_new.items():
            try:
                conn.execute("BEGIN IMMEDIATE")
                cur_a = conn.execute(
                    "UPDATE playlist_cache SET server_id = ? "
                    "WHERE server_id = ?", (new_id, old_id),
                )
                cur_b = conn.execute(
                    "UPDATE playlist_cache_items SET server_id = ? "
                    "WHERE server_id = ?", (new_id, old_id),
                )
                cur_c = conn.execute(
                    "UPDATE playlist_cache_refresh SET server_id = ? "
                    "WHERE server_id = ?", (new_id, old_id),
                )
                conn.commit()
                n += (
                    (cur_a.rowcount or 0)
                    + (cur_b.rowcount or 0)
                    + (cur_c.rowcount or 0)
                )
            except Exception:
                try:
                    conn.rollback()
                except Exception:
                    pass
                log.exception(
                    "rewrite_server_ids_in_playlist_cache: pair "
                    "(%r -> %r) update failed; continuing.",
                    old_id, new_id,
                )
    return n


# ── Stats / introspection ──────────────────────────────────────────────────

def get_stats() -> Dict[str, Any]:
    """Return a JSON-safe snapshot of cache health: row counts, size,
    oldest + newest fetched_at. Useful for the TunablesPanel /
    diagnostic surface."""
    conn = _require_conn()
    counts: Dict[str, int] = {}
    for table in ("playlist_cache", "playlist_cache_items", "playlist_cache_refresh"):
        try:
            row = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
            counts[table] = int(row["n"] or 0)
        except sqlite3.OperationalError:
            counts[table] = 0
    age_row = conn.execute(
        "SELECT MIN(fetched_at) AS oldest, MAX(fetched_at) AS newest "
        "FROM playlist_cache"
    ).fetchone()
    size_bytes = 0
    try:
        size_bytes = _db_path().stat().st_size
    except OSError:
        pass
    return {
        "row_counts": counts,
        "oldest_fetched_at": float(age_row["oldest"]) if age_row and age_row["oldest"] is not None else None,
        "newest_fetched_at": float(age_row["newest"]) if age_row and age_row["newest"] is not None else None,
        "size_bytes": size_bytes,
        "path": str(_db_path()),
    }
