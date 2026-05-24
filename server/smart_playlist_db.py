"""Smart Playlist Migration record database.

Holds one row per migrated smart playlist so the Smart Playlist
Migration panel's results view can be revisited and the portable
filter definition re-exported. Operational record-keeping, not
content - lives in its own SQLite file (``server_data/smart_playlist.db``)
for the same reasons ``playlist_cache.db`` / ``run_timings.db`` are
separate from ``media.db``.

Concurrency follows ``server/playlist_cache_db.py``: one shared
connection (``check_same_thread=False``), writers serialise on
``_DB_LOCK``, ``init_smart_playlist_db`` is idempotent under
``_init_lock``.
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
from server._db_connect import open_db


log = logging.getLogger("plexmigrate.server.smart_playlist_db")

_DB_NAME = "smart_playlist.db"


def _db_path() -> Path:
    return get_data_dir() / _DB_NAME


_DB_LOCK = threading.Lock()
_conn: Optional[sqlite3.Connection] = None
_init_lock = threading.Lock()
_initialised = False


_SCHEMA = """
CREATE TABLE IF NOT EXISTS smart_playlist_migrations (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id               TEXT NOT NULL,
    migrated_at          REAL NOT NULL,
    source_server_id     TEXT NOT NULL,
    source_playlist_id   TEXT NOT NULL,
    source_playlist_name TEXT NOT NULL DEFAULT '',
    dest_server_id       TEXT NOT NULL,
    dest_backend         TEXT NOT NULL DEFAULT '',
    -- the created playlist's id on the destination; NULL on failure.
    dest_playlist_id     TEXT,
    -- 'smart'    : filter re-created as a true Plex smart playlist.
    -- 'static'   : filter migration to a Jellyfin / Emby dest, which
    --              has no smart concept, so the current contents were
    --              materialised as a static playlist.
    -- 'hard_copy': operator chose to transfer the current matched
    --              items as a static playlist (any destination).
    mode                 TEXT NOT NULL DEFAULT '',
    -- the PortableSmartFilter serialised as JSON (re-exportable).
    portable_filter_json TEXT,
    status               TEXT NOT NULL DEFAULT '',  -- success|partial|failed
    coverage             REAL,                       -- validation 0..1
    unresolved_json      TEXT,                       -- JSON list of strings
    warning              TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_smart_pl_mig_job
    ON smart_playlist_migrations(job_id);
CREATE INDEX IF NOT EXISTS idx_smart_pl_mig_at
    ON smart_playlist_migrations(migrated_at);
"""


def init_smart_playlist_db() -> None:
    """Open the shared connection, enable WAL, create the schema.
    Idempotent: safe to call from FastAPI startup."""
    global _conn, _initialised
    with _init_lock:
        if _initialised:
            return
        path = _db_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = open_db(
            path,
            label="smart_playlist.db",
            check_same_thread=False,
            foreign_keys=False,
            synchronous_normal=True,
            chmod_sidecars=True,
        )
        conn.executescript(_SCHEMA)
        _conn = conn
        _initialised = True
        log.info("smart_playlist.db initialised at %s", path)


def _require_conn() -> sqlite3.Connection:
    if _conn is None:
        raise RuntimeError(
            "smart_playlist_db.init_smart_playlist_db() must be called "
            "before any other public API in this module."
        )
    return _conn


def record_migration(
    *,
    job_id: str,
    source_server_id: str,
    source_playlist_id: str,
    source_playlist_name: str,
    dest_server_id: str,
    dest_backend: str,
    dest_playlist_id: Optional[str],
    mode: str,
    portable_filter: Optional[Dict[str, Any]],
    status: str,
    coverage: Optional[float],
    unresolved: Optional[List[str]],
    warning: str = "",
) -> None:
    """Record one migrated playlist. Best-effort: a DB hiccup is
    logged + swallowed so it never fails the migration itself."""
    conn = _require_conn()
    try:
        pf_json = (
            json.dumps(portable_filter, default=str)
            if portable_filter is not None else None
        )
        unres_json = json.dumps(list(unresolved or []))
        with _DB_LOCK:
            conn.execute(
                "INSERT INTO smart_playlist_migrations ("
                "job_id, migrated_at, source_server_id, source_playlist_id, "
                "source_playlist_name, dest_server_id, dest_backend, "
                "dest_playlist_id, mode, portable_filter_json, status, "
                "coverage, unresolved_json, warning"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(job_id), time.time(), str(source_server_id),
                    str(source_playlist_id), str(source_playlist_name or ""),
                    str(dest_server_id), str(dest_backend or ""),
                    (str(dest_playlist_id) if dest_playlist_id else None),
                    str(mode or ""), pf_json, str(status or ""),
                    (float(coverage) if coverage is not None else None),
                    unres_json, str(warning or ""),
                ),
            )
    except Exception as exc:
        log.warning("record_migration failed (job %s): %s", job_id, exc)


def list_migrations(
    *, job_id: Optional[str] = None, limit: int = 100,
) -> List[Dict[str, Any]]:
    """Recent migration records, newest first. Filtered to one
    ``job_id`` when given. Returns ``[]`` on any DB error."""
    conn = _require_conn()
    try:
        limit = max(1, min(int(limit), 1000))
        if job_id:
            rows = conn.execute(
                "SELECT * FROM smart_playlist_migrations WHERE job_id = ? "
                "ORDER BY migrated_at DESC, id DESC LIMIT ?",
                (str(job_id), limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM smart_playlist_migrations "
                "ORDER BY migrated_at DESC, id DESC LIMIT ?",
                (limit,),
            ).fetchall()
    except Exception as exc:
        log.warning("list_migrations failed: %s", exc)
        return []
    out: List[Dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        for jkey in ("portable_filter_json", "unresolved_json"):
            raw = d.get(jkey)
            if raw:
                try:
                    d[jkey[:-5]] = json.loads(raw)
                except (ValueError, TypeError):
                    d[jkey[:-5]] = None
            else:
                d[jkey[:-5]] = None
        out.append(d)
    return out


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
