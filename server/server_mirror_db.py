"""
Server metadata mirror database.

Holds a per-server cached representation of every item the engine
might need to resolve against: rating_key, title, type, file_path,
GUIDs, parent rating-keys, library section. One row per item per
server. Queries replace the in-memory caches the playlist-transfer
path would otherwise accumulate.

The mirror is a CACHE, never a source of truth:
  - Snapshot reads stay 100% live API; the mirror is fed AS A SIDE
    EFFECT of the snapshot walk.
  - Restore / direct / fan-out / playlist transfer query the mirror
    after a cheap freshness probe; they fall through to live on miss
    or in always-live mode.
  - Drift events log when live state diverges from mirror state so
    operators can audit cache freshness.

Lives in its own SQLite DB (``server_data/server_mirror.db``), not
in media.db or playlist_cache.db, for isolation: a corrupt mirror
must not poison snapshot/restore/auth state.

Schema
------
Five tables:

* ``mirror_server_state`` (one row per registered server). Tracks
  lifecycle + mode + URL/token fingerprint.

* ``mirror_library_sections`` (one row per (server, section)).
  Drives freshness probe + delta-sync window.

* ``mirror_items`` (one row per item). The heart of the mirror.
  Indexed for GUID / full-path / path-tail / fuzzy-title lookups.

* ``mirror_item_guids`` (one row per (item, guid)). Materialized
  GUID index for fast scheme-wide lookups.

* ``drift_events`` (audit log). 30-day retention by default.

Concurrency
-----------
One shared connection per process with ``check_same_thread=False``,
matching media_db / playlist_cache_db's pattern. SQLite WAL allows
concurrent readers + a single writer; writers serialise on
``_DB_LOCK``. :func:`init_server_mirror_db` is idempotent.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
from pathlib import Path
from typing import Optional

from server.persistence import get_data_dir
from server._db_connect import apply_additive_columns, open_db


log = logging.getLogger("plexmigrate.server.server_mirror_db")


_DB_NAME = "server_mirror.db"


def _db_path() -> Path:
    return get_data_dir() / _DB_NAME


_DB_LOCK = threading.Lock()
_conn: Optional[sqlite3.Connection] = None
_init_lock = threading.Lock()
_initialised = False


_SCHEMA = """
CREATE TABLE IF NOT EXISTS mirror_server_state (
    server_id              TEXT PRIMARY KEY,
    backend                TEXT NOT NULL,
    first_sync_at          REAL,
    last_full_sync_at      REAL,
    last_drift_check_at    REAL,
    mode_override          TEXT,
    url_fingerprint        TEXT,
    token_hash             TEXT
);

CREATE TABLE IF NOT EXISTS mirror_library_sections (
    server_id              TEXT NOT NULL,
    section_id             TEXT NOT NULL,
    name                   TEXT NOT NULL,
    section_type           TEXT NOT NULL,
    live_total_size        INTEGER,
    live_updated_at        REAL,
    mirror_synced_at       REAL,
    PRIMARY KEY (server_id, section_id)
);

CREATE TABLE IF NOT EXISTS mirror_items (
    server_id              TEXT NOT NULL,
    section_id             TEXT NOT NULL,
    rating_key             TEXT NOT NULL,
    title                  TEXT NOT NULL,
    item_type              TEXT NOT NULL,
    file_path              TEXT,
    guids_json             TEXT NOT NULL,
    artist                 TEXT,
    album                  TEXT,
    show_title             TEXT,
    season_number          INTEGER,
    episode_number         INTEGER,
    parent_rating_key      TEXT,
    -- The parent item's cross-server GUID (series GUID for an
    -- episode, artist GUID for a track). Preferred over
    -- show_title/artist string matching by the resolver's hierarchy
    -- tier. NULL for movies and for any backend that did not expose
    -- a parent GUID.
    grandparent_guid       TEXT,
    live_updated_at        REAL,
    mirror_synced_at       REAL,
    PRIMARY KEY (server_id, rating_key)
);

CREATE INDEX IF NOT EXISTS idx_mirror_items_section
    ON mirror_items(server_id, section_id);

CREATE INDEX IF NOT EXISTS idx_mirror_items_file_path
    ON mirror_items(server_id, file_path)
    WHERE file_path IS NOT NULL AND file_path != '';

CREATE INDEX IF NOT EXISTS idx_mirror_items_type_title
    ON mirror_items(server_id, item_type, title COLLATE NOCASE);

CREATE INDEX IF NOT EXISTS idx_mirror_items_artist
    ON mirror_items(server_id, item_type, artist COLLATE NOCASE)
    WHERE artist IS NOT NULL AND artist != '';

CREATE INDEX IF NOT EXISTS idx_mirror_items_show_season_episode
    ON mirror_items(server_id, show_title COLLATE NOCASE, season_number, episode_number)
    WHERE show_title IS NOT NULL AND show_title != '';

CREATE INDEX IF NOT EXISTS idx_mirror_items_grandparent_guid
    ON mirror_items(server_id, grandparent_guid)
    WHERE grandparent_guid IS NOT NULL AND grandparent_guid != '';

CREATE TABLE IF NOT EXISTS mirror_item_guids (
    server_id              TEXT NOT NULL,
    rating_key             TEXT NOT NULL,
    guid                   TEXT NOT NULL,
    PRIMARY KEY (server_id, guid, rating_key),
    FOREIGN KEY (server_id, rating_key)
        REFERENCES mirror_items(server_id, rating_key)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_mirror_item_guids_guid
    ON mirror_item_guids(guid);

CREATE TABLE IF NOT EXISTS drift_events (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    server_id              TEXT NOT NULL,
    section_id             TEXT NOT NULL,
    section_name           TEXT NOT NULL,
    detected_at            REAL NOT NULL,
    mirror_total_size      INTEGER,
    live_total_size        INTEGER,
    mirror_updated_at      REAL,
    live_updated_at        REAL,
    detected_by_job_id     TEXT
);

CREATE INDEX IF NOT EXISTS idx_drift_events_server_time
    ON drift_events(server_id, detected_at DESC);
"""


def init_server_mirror_db() -> None:
    """Open the shared connection, enable WAL + FK enforcement, create
    schema. Idempotent: safe to call from FastAPI startup even when a
    test has already initialised the DB.

    A corrupt-on-disk DB triggers an integrity_check failure; the
    caller (app.py startup hook) catches and logs; runtime falls
    back to always-live mode globally.
    """
    global _conn, _initialised
    with _init_lock:
        if _initialised:
            return
        path = _db_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = open_db(
            path,
            label="server_mirror.db",
            check_same_thread=False,
            foreign_keys=True,
            synchronous_normal=True,
            chmod_sidecars=True,
        )
        try:
            row = conn.execute("PRAGMA integrity_check").fetchone()
            verdict = (row[0] if row else "") or ""
        except sqlite3.DatabaseError as exc:
            raise sqlite3.DatabaseError(
                f"server_mirror.db integrity_check raised: {exc}"
            ) from exc
        if verdict.strip().lower() != "ok":
            raise sqlite3.DatabaseError(
                f"server_mirror.db integrity_check returned {verdict!r}; "
                "operator must invalidate the mirror via "
                "POST /api/server-mirror/invalidate to rebuild."
            )
        # Idempotent additive column on mirror_items. Runs BEFORE
        # executescript - _SCHEMA's ``CREATE INDEX ...
        # grandparent_guid`` would fail on a pre-existing mirror_items
        # that lacks the column.
        apply_additive_columns(conn, [
            ("mirror_items", "grandparent_guid TEXT"),
        ])
        conn.executescript(_SCHEMA)
        _conn = conn
        _initialised = True
        log.info("server_mirror.db initialised at %s", path)


def _require_conn() -> sqlite3.Connection:
    if _conn is None:
        raise RuntimeError(
            "server_mirror_db.init_server_mirror_db() must be called "
            "before any other public API in this module."
        )
    return _conn


def get_connection() -> sqlite3.Connection:
    """Public accessor for the shared connection.

    Callers in :mod:`services.server_mirror` use this to run their
    own transactions. The connection is configured WAL + FK-on; the
    sync layer is responsible for its own BEGIN / COMMIT scoping.
    """
    return _require_conn()


def get_db_lock() -> threading.Lock:
    """Public accessor for the write-side lock.

    Writers (sync layer + bootstrap helper) acquire this lock around
    multi-statement transactions so two writers do not interleave
    statements. Readers do not need it because WAL mode allows
    concurrent readers.
    """
    return _DB_LOCK


def _close_for_tests() -> None:
    """Close the shared connection so a test that swaps the data dir
    can recreate the DB from scratch. Not called by production code.
    """
    global _conn, _initialised
    with _init_lock:
        if _conn is not None:
            try:
                _conn.close()
            except sqlite3.Error:
                pass
        _conn = None
        _initialised = False


def db_size_bytes() -> int:
    """Return current on-disk size of the mirror DB plus its WAL/SHM
    sidecars. Used by the ``engine_mirror_db_size_mb_warning`` tunable
    surface to decide when to show the size-warning banner.

    Returns 0 if the DB has not been initialised or files are missing.
    """
    total = 0
    base = _db_path()
    for p in (base, base.with_name(base.name + "-wal"),
              base.with_name(base.name + "-shm")):
        try:
            total += p.stat().st_size
        except OSError:
            continue
    return total
