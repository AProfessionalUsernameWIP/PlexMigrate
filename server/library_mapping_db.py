"""
Library-mapping persistence.

Stores per-(source_server, source_library, dest_server) → dest_library
mappings so the engine can resolve "my Music library" → "their Tunes
library" when transferring content between servers whose library names
don't match.

Mappings have two origins:
  * ``auto``     — computed by :mod:`services.library_mapper`. Cleared
                   on mirror sync invalidation (so a re-warm refreshes
                   the suggestions).
  * ``operator`` — operator confirmed via the UI or overrode an auto
                   suggestion. Survives mirror invalidations.

The table key is ``(source_server_id, source_library_id, dest_server_id)``:
each source library has exactly ONE destination on a given dest server.
A library can be unmapped on dest A but mapped on dest B.

Lookup contract: the restorer + library-filter UI ask "where does
source library X on source server S go when restoring to dest server D"
and get either an operator mapping (preferred), an auto mapping
(fallback), or None (the existing exact-name + type-fallback rules
in the restorer take over).
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from server.persistence import get_data_dir
from server._db_connect import apply_additive_columns, open_db


log = logging.getLogger("plexmigrate.server.library_mapping_db")


_DB_NAME = "library_mappings.db"


def _db_path() -> Path:
    return get_data_dir() / _DB_NAME


_DB_LOCK = threading.Lock()
_conn: Optional[sqlite3.Connection] = None
_init_lock = threading.Lock()
_initialised = False


_SCHEMA = """
CREATE TABLE IF NOT EXISTS library_mappings (
    source_server_id     TEXT NOT NULL,
    source_library_id    TEXT NOT NULL,
    -- Denormalised name (free of the dest server's adapter)
    -- so the restorer can match incoming snapshot entries
    -- (which carry library NAMES) without a live API call.
    -- Updated whenever the matcher / operator writes the row.
    source_library_name  TEXT NOT NULL DEFAULT '',
    dest_server_id       TEXT NOT NULL,
    -- The mapped destination library's id. Empty string means "no
    -- match" (operator explicitly declined a suggestion); the
    -- restorer treats this as "skip" instead of falling through to
    -- exact-name match. NULL is not used; rows are deleted instead.
    dest_library_id      TEXT NOT NULL,
    -- Denormalised dest name; same rationale as above.
    dest_library_name    TEXT NOT NULL DEFAULT '',
    -- 0.0 - 1.0. Auto-rows carry the matcher's confidence; operator
    -- rows are 1.0 by definition.
    confidence         REAL NOT NULL DEFAULT 0.0,
    -- 'auto' or 'operator'. Auto rows are eligible for invalidation
    -- on mirror sync; operator rows survive (the operator told us
    -- this is the truth - re-running automap doesn't change that).
    source             TEXT NOT NULL,
    -- ASCII tier label from the matcher (e.g. 'content', 'path',
    -- 'type', 'name'). Pure observability; helps explain WHY a
    -- given mapping was suggested. Empty for operator rows.
    tier               TEXT NOT NULL DEFAULT '',
    -- Epoch seconds. When auto: when the automap last ran. When
    -- operator: when the operator confirmed/overrode.
    last_computed_at   REAL NOT NULL,
    -- Non-null only on operator rows. The automap re-runner skips
    -- rows whose last_confirmed_at is set.
    last_confirmed_at  REAL,
    -- Free text annotation surfaced in the UI hover. Operator can
    -- leave notes ("matches via GUID overlap, verified 2026-05-18").
    notes              TEXT,
    -- Bidirectional flag. 1 means the equivalence runs BOTH
    -- directions - used by the sync worker to know that a play
    -- on dest should propagate back to source. The restorer still
    -- treats the row as directional (source -> dest); the sync
    -- subscription engine respects the flag for both-ways state
    -- reconciliation. Default 0 keeps directional semantics; the
    -- operator opts in per-pair via the UI toggle.
    bidirectional      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (source_server_id, source_library_id, dest_server_id)
);

CREATE INDEX IF NOT EXISTS idx_library_mappings_by_server_pair
    ON library_mappings(source_server_id, dest_server_id);
CREATE INDEX IF NOT EXISTS idx_library_mappings_by_source
    ON library_mappings(source);
"""


def init_library_mapping_db() -> None:
    """Open the shared connection + create schema. Idempotent."""
    global _conn, _initialised
    with _init_lock:
        if _initialised:
            return
        path = _db_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = open_db(
            path,
            label="library_mappings.db",
            check_same_thread=False,
            foreign_keys=True,
            synchronous_normal=True,
            chmod_sidecars=False,
        )
        conn.executescript(_SCHEMA)
        # Additive migrations for pre-existing installs; the schema
        # above already includes every current column.
        apply_additive_columns(conn, [
            ("library_mappings", "bidirectional INTEGER NOT NULL DEFAULT 0"),
        ])
        _conn = conn
        _initialised = True
        log.info("library_mappings.db initialised at %s", path)


def _require_conn() -> sqlite3.Connection:
    if _conn is None:
        raise RuntimeError(
            "library_mapping_db.init_library_mapping_db() must be "
            "called before any other public API in this module."
        )
    return _conn


def _close_for_tests() -> None:
    """Test-only: close the shared connection so a fresh test gets a
    fresh DB."""
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


def get_mapping(
    source_server_id: str,
    source_library_id: str,
    dest_server_id: str,
) -> Optional[Dict[str, Any]]:
    """Return the mapping row for one source library on one dest server,
    or None when no mapping exists.

    Operator rows take precedence over auto rows by virtue of the
    primary key — there's only ever one row per (src_server, src_lib,
    dst_server), and when an operator confirms a different mapping the
    save path overwrites the auto row.
    """
    if not source_server_id or not source_library_id or not dest_server_id:
        return None
    try:
        conn = _require_conn()
    except RuntimeError:
        return None
    try:
        row = conn.execute(
            "SELECT * FROM library_mappings "
            "WHERE source_server_id = ? "
            "  AND source_library_id = ? "
            "  AND dest_server_id = ?",
            (source_server_id, source_library_id, dest_server_id),
        ).fetchone()
    except sqlite3.OperationalError as exc:
        log.debug("library_mapping_db.get_mapping failed: %s", exc)
        return None
    return _row_to_dict(row) if row else None


def list_mappings_for_pair(
    source_server_id: str,
    dest_server_id: str,
) -> List[Dict[str, Any]]:
    """All mappings between one source server and one dest server.
    Ordered by source_library_id for stable rendering."""
    if not source_server_id or not dest_server_id:
        return []
    try:
        conn = _require_conn()
    except RuntimeError:
        return []
    try:
        rows = conn.execute(
            "SELECT * FROM library_mappings "
            "WHERE source_server_id = ? AND dest_server_id = ? "
            "ORDER BY source_library_id",
            (source_server_id, dest_server_id),
        ).fetchall()
    except sqlite3.OperationalError as exc:
        log.debug(
            "library_mapping_db.list_mappings_for_pair failed: %s", exc,
        )
        return []
    return [_row_to_dict(r) for r in rows]


def upsert_mapping(
    *,
    source_server_id: str,
    source_library_id: str,
    dest_server_id: str,
    dest_library_id: str,
    source: str,
    source_library_name: str = "",
    dest_library_name: str = "",
    confidence: float = 0.0,
    tier: str = "",
    notes: Optional[str] = None,
    bidirectional: bool = False,
) -> bool:
    """Insert or replace a mapping. ``source`` must be ``'auto'`` or
    ``'operator'``. The operator path stamps ``last_confirmed_at``;
    auto only stamps ``last_computed_at`` so the invalidator can tell
    them apart at row level.

    Returns True on success, False on any DB error.
    """
    if not source_server_id or not source_library_id or not dest_server_id:
        return False
    if source not in ("auto", "operator"):
        raise ValueError(
            f"source must be 'auto' or 'operator'; got {source!r}"
        )
    now = time.time()
    last_confirmed_at: Optional[float] = now if source == "operator" else None
    # Operator confirmations are by definition truth; clamp confidence.
    effective_confidence = 1.0 if source == "operator" else float(confidence)
    try:
        conn = _require_conn()
    except RuntimeError:
        return False
    with _DB_LOCK:
        try:
            conn.execute(
                "INSERT OR REPLACE INTO library_mappings ("
                "source_server_id, source_library_id, "
                "source_library_name, dest_server_id, dest_library_id, "
                "dest_library_name, confidence, source, tier, "
                "last_computed_at, last_confirmed_at, notes, "
                "bidirectional"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    source_server_id, source_library_id,
                    str(source_library_name or ""),
                    dest_server_id, str(dest_library_id or ""),
                    str(dest_library_name or ""),
                    effective_confidence,
                    source, str(tier or ""),
                    now, last_confirmed_at, notes,
                    1 if bidirectional else 0,
                ),
            )
            return True
        except sqlite3.OperationalError as exc:
            log.warning("library_mapping_db.upsert_mapping failed: %s", exc)
            return False


def delete_mapping(
    source_server_id: str,
    source_library_id: str,
    dest_server_id: str,
) -> int:
    """Drop one mapping row. Returns the number of rows deleted (0 or 1)."""
    if not source_server_id or not source_library_id or not dest_server_id:
        return 0
    try:
        conn = _require_conn()
    except RuntimeError:
        return 0
    with _DB_LOCK:
        try:
            cur = conn.execute(
                "DELETE FROM library_mappings "
                "WHERE source_server_id = ? "
                "  AND source_library_id = ? "
                "  AND dest_server_id = ?",
                (source_server_id, source_library_id, dest_server_id),
            )
            return int(cur.rowcount or 0)
        except sqlite3.OperationalError as exc:
            log.warning("library_mapping_db.delete_mapping failed: %s", exc)
            return 0


def invalidate_auto_mappings(
    *,
    source_server_id: Optional[str] = None,
    dest_server_id: Optional[str] = None,
) -> int:
    """Drop every ``source='auto'`` row that matches the filter.
    Operator-confirmed rows are PRESERVED — the operator already
    decided, re-automap doesn't override that.

    Filter modes:
      * Both ids None → wipe every auto row (used by manual "rebuild
        all mappings" controls).
      * Only source_server_id → wipe auto rows where source = that.
      * Only dest_server_id → wipe auto rows where dest = that.
      * Both → wipe auto rows for that exact pair (used by mirror-sync
        invalidation hooks).

    Returns the number of rows deleted.
    """
    try:
        conn = _require_conn()
    except RuntimeError:
        return 0
    where = ["source = 'auto'"]
    params: List[Any] = []
    if source_server_id:
        where.append("source_server_id = ?")
        params.append(source_server_id)
    if dest_server_id:
        where.append("dest_server_id = ?")
        params.append(dest_server_id)
    sql = f"DELETE FROM library_mappings WHERE {' AND '.join(where)}"
    with _DB_LOCK:
        try:
            cur = conn.execute(sql, params)
            n = int(cur.rowcount or 0)
            if n:
                log.info(
                    "library_mapping_db: invalidated %d auto row(s) "
                    "(source=%s, dest=%s)",
                    n, source_server_id or "*", dest_server_id or "*",
                )
            return n
        except sqlite3.OperationalError as exc:
            log.warning(
                "library_mapping_db.invalidate_auto_mappings failed: %s",
                exc,
            )
            return 0


def stats() -> Dict[str, int]:
    """Tiny diagnostic for the dashboard surface."""
    try:
        conn = _require_conn()
    except RuntimeError:
        return {"total": 0, "auto": 0, "operator": 0}
    try:
        total = conn.execute(
            "SELECT COUNT(*) FROM library_mappings",
        ).fetchone()[0]
        auto = conn.execute(
            "SELECT COUNT(*) FROM library_mappings WHERE source = 'auto'",
        ).fetchone()[0]
        op = conn.execute(
            "SELECT COUNT(*) FROM library_mappings WHERE source = 'operator'",
        ).fetchone()[0]
        return {"total": int(total), "auto": int(auto), "operator": int(op)}
    except sqlite3.OperationalError:
        return {"total": 0, "auto": 0, "operator": 0}


def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    # The name columns can be missing on rows from tests that swap
    # the schema mid-run. Tolerate that with _get.
    def _get(r: sqlite3.Row, key: str, default: Any = None) -> Any:
        try:
            return r[key]
        except (IndexError, KeyError):
            return default
    return {
        "source_server_id":     row["source_server_id"],
        "source_library_id":    row["source_library_id"],
        "source_library_name":  _get(row, "source_library_name", "") or "",
        "dest_server_id":       row["dest_server_id"],
        "dest_library_id":      row["dest_library_id"] or "",
        "dest_library_name":    _get(row, "dest_library_name", "") or "",
        "confidence":           float(row["confidence"] or 0.0),
        "source":               row["source"],
        "tier":                 row["tier"] or "",
        "last_computed_at":     float(row["last_computed_at"] or 0.0),
        "last_confirmed_at":    (
            float(row["last_confirmed_at"])
            if row["last_confirmed_at"] is not None else None
        ),
        "notes":                row["notes"],
        "bidirectional":        bool(_get(row, "bidirectional", 0)),
    }
