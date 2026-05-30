"""
Run timings persistence (Feature 1 phase 1.3).

Stores per-operation timing entries produced by
:mod:`services.run_logs.run_timer` so the ETR rework (phase 1.4) can train on
historical data and the end user can audit past runs from the
dashboard.

This is a **separate** SQLite database (``server_data/run_timings.db``)
from ``media.db``. Mixing operational telemetry into the content store
would muddy the schema-migration story, bloat any media.db backup with
data the end user probably doesn't want preserved long-term, and
introduce a write-amplification path on the cumulative store every
time the engine fires.

Schema:

``run_timings`` (one row per :class:`services.run_logs.run_timer.TimingEntry`)
    - ``id``                INTEGER PRIMARY KEY AUTOINCREMENT
    - ``run_id``            TEXT NOT NULL
    - ``scope``             TEXT NOT NULL (see run_timer.VALID_SCOPES)
    - ``label``             TEXT NOT NULL
    - ``server_id``         TEXT
    - ``library``           TEXT
    - ``user_handle``       TEXT
    - ``started_at``        REAL NOT NULL (epoch seconds)
    - ``ended_at``          REAL NOT NULL (epoch seconds)
    - ``duration_seconds``  REAL NOT NULL (perf_counter delta)
    - ``items_processed``   INTEGER
    - ``etr_at_start``      REAL
    - ``extra_json``        TEXT (free-form annotations as JSON)
    - ``recorded_at``       REAL NOT NULL (epoch seconds at insert time)

Indexes:
    - ``idx_run_timings_run`` on (run_id)
    - ``idx_run_timings_label`` on (label) for the ETR per-label lookup
    - ``idx_run_timings_recorded`` on (recorded_at) for retention pruning

Retention:
    The :func:`enforce_retention` helper keeps only the most recent
    N distinct ``run_id`` values, where N comes from
    ``settings.run_timings_retention_count`` (default 200). It runs
    automatically at the end of :func:`persist_entries`. A retention
    of 0 means "unlimited" (end user override; default cap is 200).

Concurrency:
    Fresh connection per call (matches snapshots.db / auth_db). Tiny
    DB, low concurrency. WAL journal mode lets dashboard reads happen
    without blocking the persistence write. :func:`init_db` is
    idempotent and runs inside :data:`_init_lock`; subsequent calls
    short-circuit.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from server.persistence import get_data_dir
from server._db_connect import apply_additive_columns, open_db


log = logging.getLogger("plexmigrate.server.run_timings_db")


_DB_NAME = "run_timings.db"
_init_lock = threading.Lock()
_initialised = False


# Default retention when no settings.json value is present or readable.
# Matches the value documented in CLAUDE.md for
# ``run_timings_retention_count``.
DEFAULT_RETENTION_COUNT = 200


def _db_path() -> Path:
    return get_data_dir() / _DB_NAME


def _connect() -> sqlite3.Connection:
    """Fresh connection per call. The persistence path is short-lived
    (write a batch, close); long-lived connections would add no benefit
    and complicate tests that swap the data dir between cases."""
    return open_db(
        _db_path(),
        label="run_timings.db",
        check_same_thread=True,
        foreign_keys=False,
        synchronous_normal=True,
        chmod_sidecars=False,
    )


_SCHEMA = """
-- Retired: the adaptive-ETA predictor was removed. Its legacy
-- eta_weights store and the eta_buckets store are dropped on
-- startup if a pre-retirement install still has them.
DROP TABLE IF EXISTS eta_weights;
DROP TABLE IF EXISTS eta_buckets;

CREATE TABLE IF NOT EXISTS run_timings (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id            TEXT NOT NULL,
    scope             TEXT NOT NULL,
    label             TEXT NOT NULL,
    server_id         TEXT,
    library           TEXT,
    user_handle       TEXT,
    started_at        REAL NOT NULL,
    ended_at          REAL NOT NULL,
    duration_seconds  REAL NOT NULL,
    items_processed   INTEGER,
    etr_at_start      REAL,
    extra_json        TEXT,
    recorded_at       REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_run_timings_run
    ON run_timings(run_id);
CREATE INDEX IF NOT EXISTS idx_run_timings_label
    ON run_timings(label);
CREATE INDEX IF NOT EXISTS idx_run_timings_recorded
    ON run_timings(recorded_at);

-- One row per RUN (not per timing entry). Drives the Servers >
-- Recent Runtimes panel. Joined with run_timings at query time
-- when the end user drills into a specific row from the Recent
-- Runtimes table.
CREATE TABLE IF NOT EXISTS run_history (
    run_id              TEXT PRIMARY KEY,
    started_at          REAL NOT NULL,
    finished_at         REAL NOT NULL,
    job_type            TEXT NOT NULL,
    server_id           TEXT,
    server_name         TEXT,
    libraries           TEXT NOT NULL,
    users_affected      INTEGER NOT NULL DEFAULT 0,
    users_affected_list TEXT,
    state               TEXT NOT NULL,
    duration_ms         INTEGER NOT NULL,
    run_log_dir         TEXT,
    has_settings_log    INTEGER NOT NULL DEFAULT 0,
    has_restoration_log INTEGER NOT NULL DEFAULT 0,
    error_summary       TEXT,
    recorded_at         REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_run_history_recorded
    ON run_history(recorded_at);
CREATE INDEX IF NOT EXISTS idx_run_history_server
    ON run_history(server_id);
CREATE INDEX IF NOT EXISTS idx_run_history_started
    ON run_history(started_at);
"""


# Additive column migrations for tables created on a pre-release
# install before a column was added to CREATE TABLE. Each entry
# is (table_name, column_definition). Empty since the ETA buckets
# table was retired.
_ADDITIVE_COLUMN_MIGRATIONS: List[tuple] = []


def init_db() -> None:
    """Create ``run_timings.db`` and apply schema. Idempotent.

    Also runs additive column migrations for tables that may have
    been created on a pre-release install before columns were
    added to the CREATE TABLE statement. Each ADD COLUMN is
    wrapped in its own try/except so a partial migration history
    doesn't block startup.
    """
    global _initialised
    with _init_lock:
        if _initialised:
            return
        _db_path().parent.mkdir(parents=True, exist_ok=True)
        conn = _connect()
        try:
            conn.executescript(_SCHEMA)
            # Additive column migrations onto pre-existing installs.
            apply_additive_columns(conn, _ADDITIVE_COLUMN_MIGRATIONS)
        finally:
            conn.close()
        _initialised = True
        log.info("run_timings.db initialised at %s", _db_path())


def persist_entries(entries: Sequence[Any]) -> int:
    """
    Insert every entry in ``entries`` into ``run_timings`` and then
    enforce retention. Returns the number of rows inserted.

    Each entry is expected to be a :class:`services.run_logs.run_timer.TimingEntry`,
    but this helper duck-types on the fields so a future caller can
    pass dicts or a different record class without rewriting this.
    Missing optional fields land as NULL.

    Bulk insert in one transaction so a long buffer flush is one
    fsync rather than N.
    """
    init_db()
    if not entries:
        return 0

    now = time.time()
    rows: List[tuple] = []
    for e in entries:
        # Duck-type so dicts and dataclasses both work. dataclasses
        # expose attribute access; dicts expose .get(). Try attribute
        # first (fast path), fall back to dict-style.
        def _get(name: str, default: Any = None) -> Any:
            if hasattr(e, name):
                return getattr(e, name)
            if isinstance(e, dict):
                return e.get(name, default)
            return default

        extra = _get("extra")
        extra_json = json.dumps(extra) if extra else None
        rows.append((
            _get("run_id"),
            _get("scope"),
            _get("label"),
            _get("server_id"),
            _get("library"),
            _get("user_handle"),
            float(_get("started_at") or 0.0),
            float(_get("ended_at") or 0.0),
            float(_get("duration_seconds") or 0.0),
            _get("items_processed"),
            _get("etr_at_start"),
            extra_json,
            now,
        ))

    conn = _connect()
    try:
        conn.execute("BEGIN")
        try:
            conn.executemany(
                """
                INSERT INTO run_timings (
                    run_id, scope, label, server_id, library, user_handle,
                    started_at, ended_at, duration_seconds, items_processed,
                    etr_at_start, extra_json, recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()

    # Retention runs in its own brief transaction so a slow prune
    # doesn't hold the write lock during the next insert batch.
    try:
        enforce_retention(retention_count=_resolve_retention_count())
    except Exception:
        # Retention is best-effort; a failure here must not throw out
        # the rows we just inserted.
        log.exception("run_timings retention enforcement failed")

    return len(rows)


def enforce_retention(*, retention_count: int) -> int:
    """
    Trim the table so only the most recent ``retention_count`` distinct
    ``run_id`` values survive. Returns the number of rows deleted.

    ``retention_count <= 0`` is a no-op (end user escape hatch for
    "keep everything"). Negative or non-int values from settings.json
    fall through to the default at the caller; this function trusts
    its argument.

    The ranking key is the maximum ``recorded_at`` per run_id, so a
    run that wrote rows over a long wall-clock span counts as "newest"
    by its last entry. Ties on identical timestamps fall back to
    ``run_id`` lexicographic order, which is acceptable because the
    run-id generator embeds a wall-clock timestamp.
    """
    if retention_count <= 0:
        return 0
    init_db()
    conn = _connect()
    try:
        # Find run_ids to keep: the N with the highest max(recorded_at).
        keep_rows = conn.execute(
            """
            SELECT run_id
            FROM (
                SELECT run_id, MAX(recorded_at) AS last_seen
                FROM run_timings
                GROUP BY run_id
                ORDER BY last_seen DESC, run_id DESC
                LIMIT ?
            )
            """,
            (int(retention_count),),
        ).fetchall()
        keep_ids = [r["run_id"] for r in keep_rows]
        if not keep_ids:
            return 0
        # Delete everything not in the keep set. Parameterise the IN
        # list rather than f-string-injecting the run_ids.
        placeholders = ",".join("?" for _ in keep_ids)
        cur = conn.execute(
            f"DELETE FROM run_timings WHERE run_id NOT IN ({placeholders})",
            keep_ids,
        )
        return int(cur.rowcount or 0)
    finally:
        conn.close()


def _resolve_retention_count() -> int:
    """
    Read ``settings.run_timings_retention_count`` with a fallback to
    :data:`DEFAULT_RETENTION_COUNT`. A bad or missing value falls
    through to the default; we never raise on misconfiguration.
    """
    try:
        from server.persistence import load_settings
        settings = load_settings() or {}
        raw = settings.get("run_timings_retention_count")
        if isinstance(raw, int) and raw >= 0:
            return raw
        if isinstance(raw, str) and raw.isdigit():
            return int(raw)
    except Exception:
        log.debug("retention setting lookup failed", exc_info=True)
    return DEFAULT_RETENTION_COUNT


# ── Read helpers (for dashboard + ETR training) ─────────────────────────────

def list_recent_runs(*, limit: int = 25) -> List[Dict[str, Any]]:
    """
    Return summary rows for the most recent ``limit`` runs.

    Each row carries ``{run_id, started_at, ended_at, duration_seconds,
    entry_count, total_items_processed, job_type, state}``. Used by
    the dashboard's runtime-breakdown panel to render the recent-runs
    sidebar.

    Drives off ``run_history`` (the canonical one-row-per-job table
    populated by every job's central finally block via
    :func:`record_run_history`) and LEFT-JOINs the per-operation
    aggregates from run_timings for entry_count + total_items.
    Driving off run_timings instead would miss restore jobs that do
    not emit per-operation timings via ``services.run_logs.run_timer``: they
    produce zero rows in run_timings and would be silently invisible
    to the dashboard. Runs without per-operation timings still
    surface with their wall-clock duration + job_type + state; the
    dashboard's drilldown returns "No entries recorded for this run"
    honestly when expanded.
    """
    init_db()
    conn = _connect()
    try:
        rows = conn.execute(
            """
            SELECT
                h.run_id           AS run_id,
                h.started_at       AS started_at,
                h.finished_at      AS ended_at,
                h.duration_ms      AS duration_ms,
                h.job_type         AS job_type,
                h.state            AS state,
                COALESCE(t.entry_count, 0)  AS entry_count,
                COALESCE(t.total_items, 0)  AS total_items
            FROM run_history h
            LEFT JOIN (
                SELECT
                    run_id,
                    COUNT(*) AS entry_count,
                    SUM(COALESCE(items_processed, 0)) AS total_items
                FROM run_timings
                GROUP BY run_id
            ) t ON t.run_id = h.run_id
            ORDER BY h.recorded_at DESC
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
    finally:
        conn.close()
    out: List[Dict[str, Any]] = []
    for r in rows:
        dur_ms = r["duration_ms"]
        dur_s = float(dur_ms) / 1000.0 if dur_ms else 0.0
        out.append({
            "run_id": r["run_id"],
            "started_at": r["started_at"],
            "ended_at": r["ended_at"],
            "duration_seconds": dur_s,
            "entry_count": int(r["entry_count"] or 0),
            "total_items_processed": int(r["total_items"] or 0),
            "job_type": r["job_type"],
            "state": r["state"],
        })
    return out


def get_run_entries(run_id: str) -> List[Dict[str, Any]]:
    """Return every timing row for one ``run_id``, ordered by
    ``started_at`` ascending. Used by the dashboard drilldown and by
    any post-run report tooling."""
    init_db()
    conn = _connect()
    try:
        rows = conn.execute(
            """
            SELECT * FROM run_timings
            WHERE run_id = ?
            ORDER BY started_at ASC
            """,
            (run_id,),
        ).fetchall()
    finally:
        conn.close()
    out: List[Dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        # Parse extra_json into a dict so consumers don't need to
        # re-deserialize. Bad / missing values land as an empty dict.
        raw = d.pop("extra_json", None)
        try:
            d["extra"] = json.loads(raw) if raw else {}
        except Exception:
            d["extra"] = {}
        out.append(d)
    return out


def get_label_history(
    label: str, *, server_id: Optional[str] = None, limit: int = 100,
) -> List[Dict[str, Any]]:
    """
    Return recent timing rows matching a label (and optionally a
    server_id), newest first. The ETR rework consumes this to build a
    per-operation rate estimate from real history rather than from a
    pre-run guess.
    """
    init_db()
    conn = _connect()
    try:
        if server_id:
            rows = conn.execute(
                """
                SELECT duration_seconds, items_processed, started_at
                FROM run_timings
                WHERE label = ? AND server_id = ?
                ORDER BY recorded_at DESC
                LIMIT ?
                """,
                (label, server_id, int(limit)),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT duration_seconds, items_processed, started_at
                FROM run_timings
                WHERE label = ?
                ORDER BY recorded_at DESC
                LIMIT ?
                """,
                (label, int(limit)),
            ).fetchall()
    finally:
        conn.close()
    return [
        {
            "duration_seconds": float(r["duration_seconds"] or 0.0),
            "items_processed": (
                int(r["items_processed"]) if r["items_processed"] is not None else None
            ),
            "started_at": float(r["started_at"] or 0.0),
        }
        for r in rows
    ]


# ── run_history (per-run summary) ─────────────────────────────────────────────


_RUN_HISTORY_DEFAULT_RETENTION = 200


def record_run_history(
    *,
    run_id: str,
    started_at: float,
    finished_at: float,
    job_type: str,
    server_id: Optional[str],
    server_name: Optional[str],
    libraries: Sequence[str],
    users_affected: int,
    users_affected_list: Sequence[str],
    state: str,
    duration_ms: int,
    run_log_dir: Optional[str],
    has_settings_log: bool,
    has_restoration_log: bool,
    error_summary: Optional[str] = None,
) -> None:
    """
    Insert (or replace) one row in ``run_history``. Called by jobs.py
    at run finalisation for every job type. ``INSERT OR REPLACE``
    keyed by ``run_id`` so a retry on the same run_id stays a single
    row (no duplicate UI entries).

    Best-effort: any failure is logged and swallowed. The job
    finalisation path must never block on this diagnostic write.
    """
    if not run_id:
        log.warning("record_run_history: empty run_id; skipping")
        return
    try:
        init_db()
        libs_blob = json.dumps(list(libraries or []))
        users_blob = json.dumps(list(users_affected_list or []))
        now = time.time()
        conn = _connect()
        try:
            conn.execute(
                """
                INSERT OR REPLACE INTO run_history (
                    run_id, started_at, finished_at, job_type,
                    server_id, server_name, libraries,
                    users_affected, users_affected_list, state,
                    duration_ms, run_log_dir,
                    has_settings_log, has_restoration_log,
                    error_summary, recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(run_id),
                    float(started_at),
                    float(finished_at),
                    str(job_type),
                    server_id if server_id else None,
                    server_name if server_name else None,
                    libs_blob,
                    int(users_affected),
                    users_blob,
                    str(state),
                    int(duration_ms),
                    run_log_dir if run_log_dir else None,
                    1 if has_settings_log else 0,
                    1 if has_restoration_log else 0,
                    error_summary if error_summary else None,
                    now,
                ),
            )
        finally:
            conn.close()
        # Prune to the configured retention. Best-effort - failure to
        # prune doesn't fail the insert.
        try:
            enforce_run_history_retention()
        except Exception:
            log.exception("record_run_history: retention prune failed")
    except Exception:
        log.exception("record_run_history: insert failed for run_id=%r", run_id)


def _row_to_history_dict(row: sqlite3.Row) -> Dict[str, Any]:
    """Decode JSON columns + cast bools back to bool. Used by the
    list/get helpers below."""
    out = dict(row)
    libs_raw = out.pop("libraries", None) or "[]"
    users_raw = out.pop("users_affected_list", None) or "[]"
    try:
        out["libraries"] = json.loads(libs_raw)
    except Exception:
        out["libraries"] = []
    try:
        out["users_affected_list"] = json.loads(users_raw)
    except Exception:
        out["users_affected_list"] = []
    out["has_settings_log"] = bool(out.get("has_settings_log"))
    out["has_restoration_log"] = bool(out.get("has_restoration_log"))
    return out


def list_recent_run_history(
    *,
    limit: int = 200,
    server_id: Optional[str] = None,
    library: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Return the most recent N rows from ``run_history``, newest first.
    Optional filters: ``server_id`` matches the row's server_id;
    ``library`` matches when the libraries JSON array contains that
    name (case-sensitive).

    Used by ``GET /api/runs/recent`` to populate the Servers > Recent
    Runtimes panel.
    """
    init_db()
    conn = _connect()
    try:
        if server_id:
            rows = conn.execute(
                """
                SELECT * FROM run_history
                WHERE server_id = ?
                ORDER BY recorded_at DESC
                LIMIT ?
                """,
                (str(server_id), int(limit)),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT * FROM run_history
                ORDER BY recorded_at DESC
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
    finally:
        conn.close()
    decoded = [_row_to_history_dict(r) for r in rows]
    if library:
        # Library filter is applied in Python because SQLite doesn't
        # cleanly index "JSON array contains". With retention capped
        # at 200 the post-filter is negligible.
        decoded = [d for d in decoded if library in (d.get("libraries") or [])]
    return decoded


def get_run_history(run_id: str) -> Optional[Dict[str, Any]]:
    """Return one row by run_id or ``None`` when absent. Used by the
    drilldown path on Recent Runtimes."""
    init_db()
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM run_history WHERE run_id = ?",
            (str(run_id),),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return _row_to_history_dict(row)


def enforce_run_history_retention(
    *, retention_count: Optional[int] = None,
) -> int:
    """
    Keep at most ``retention_count`` rows in ``run_history``; delete
    the oldest beyond that. Returns the number of rows deleted. A
    value of 0 means "unlimited" (end user opt-out).

    When ``retention_count`` is None the helper reads
    ``settings.run_history_retention_count`` and falls back to the
    default (200). The retention setting is a simple int; values <0
    are treated as the default.
    """
    if retention_count is None:
        try:
            from server.persistence import load_settings
            settings = load_settings() or {}
            raw = settings.get("run_history_retention_count")
            if raw is None:
                retention_count = _RUN_HISTORY_DEFAULT_RETENTION
            else:
                retention_count = int(raw)
        except Exception:
            retention_count = _RUN_HISTORY_DEFAULT_RETENTION
    if retention_count <= 0:
        return 0
    init_db()
    conn = _connect()
    try:
        # Two-step: find the cutoff recorded_at value at position
        # ``retention_count`` (0-indexed), then delete everything
        # older. Single DELETE with subquery is cleaner; we use it.
        cur = conn.execute(
            """
            DELETE FROM run_history
            WHERE run_id IN (
                SELECT run_id FROM run_history
                ORDER BY recorded_at DESC
                LIMIT -1 OFFSET ?
            )
            """,
            (int(retention_count),),
        )
        return int(cur.rowcount or 0)
    finally:
        conn.close()


# ── Bulk reset ───────────────────────────────────────────────────────────────


def flush_all_run_timings() -> int:
    """Wipe every row in run_timings. Used when the end user wants a
    true zero-state reset of the run-timing history. Returns the count
    of rows deleted."""
    init_db()
    conn = _connect()
    try:
        cur = conn.execute("DELETE FROM run_timings")
        return int(cur.rowcount or 0)
    finally:
        conn.close()


def _close_for_tests() -> None:
    """Reset the init flag so a fresh data dir gets a fresh DB on the
    next call. Test-only; production paths must never call this
    (the connection lifecycle is per-call so there's nothing to
    actually close)."""
    global _initialised
    _initialised = False
