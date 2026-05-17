"""
PR-13 - Snapshot registry. The metadata layer over the per-snapshot
``.db`` files that the capture pipeline writes.

Lifecycle
---------
* :func:`init_registry` creates ``server_data/snapshots.db`` on first
  use. Idempotent.
* :func:`register` inserts one row per captured snapshot and runs
  :func:`enforce_retention` against the affected server.
* :func:`list_snapshots` powers the Snapshots panel's list view.
* :func:`get` is the lookup the on-demand download path uses to
  resolve ``{id}`` -> snapshot file.
* :func:`delete` removes a snapshot: row from this DB, the
  per-snapshot ``.db`` file from disk, and any pre-built JSON
  sidecar. Best-effort on the file deletes; the row deletion is
  authoritative.
* :func:`enforce_retention` is called after every register and from
  the Settings UI when the end user edits retention values.

Storage
-------
The registry lives in ``server_data/snapshots.db``. It does NOT store
any media payload - only the catalogue. The actual per-server
snapshot ``.db`` files live under the configured ``output_dir``
(default ``snapshots/``) and are referenced by absolute path from
the registry rows.

Retention
---------
Two settings drive retention:

* ``snapshot_retention_global`` (default 30) - global ceiling.
* ``snapshot_retention_per_server`` ({server_id: int}) - per-server
  override that ONLY applies when strictly lower than the global.
  Global is a ceiling, never a floor.

Resolution: ``effective_retention_for(server_id)`` returns
``min(per_server_override, global)`` when an override exists, else
``global``. Enforcement deletes the oldest rows past the keep
window and best-effort deletes the corresponding files.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from server.persistence import get_data_dir, load_settings


log = logging.getLogger("plexmigrate.server.snapshot_registry")


# ── Display / filename composition ──────────────────────────────────────────
#
# Snapshots are referenced by humans (UI display, download filenames)
# and by the filesystem (on-disk .db basename, snapshot_name field in
# the registry). The two need slightly different shapes:
#
#   * Display    - "{server} / {libs} / {YYYY-MM-DD HH:MM}". Slashes
#                  and colons are end user-friendly separators.
#   * Filename   - "{server} - {libs} - {YYYY-MM-DD HH-MM}". Slashes
#                  and colons are filesystem-unsafe (Windows in
#                  particular reserves both); replaced with " - " and
#                  "-" respectively.
#
# Both helpers take the same field set so a snapshot's display and
# filename stay 1:1 derivable from each other.

_UNSAFE_FILENAME_CHARS = '/\\:*?"<>|'


def _sanitise_filename_segment(s: str) -> str:
    """Replace filesystem-unsafe chars with underscore. Trim whitespace."""
    for c in _UNSAFE_FILENAME_CHARS:
        s = s.replace(c, "_")
    return s.strip()


def format_snapshot_display(
    *,
    server_name: str,
    libraries: List[str],
    captured_at: float,
) -> str:
    """
    Human-facing display string for a snapshot. Format:
    ``{server_name} / {lib, lib, lib} / {YYYY-MM-DD HH:MM}``.
    Used by the Backups panel's Name column.
    """
    from datetime import datetime
    libs_str = ", ".join(libraries) if libraries else "no libraries"
    try:
        dt = datetime.fromtimestamp(float(captured_at)).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OSError):
        dt = "unknown time"
    return f"{server_name} / {libs_str} / {dt}"


def format_snapshot_filename(
    *,
    server_name: str,
    libraries: List[str],
    captured_at: float,
) -> str:
    """
    Filesystem-safe variant of :func:`format_snapshot_display`. No
    extension - callers append ``.db`` / ``.plexbackup.json``. Long
    library lists past 120 chars get truncated with " +N more" so the
    overall filename stays inside the 255-byte ext4 limit and the 260-
    char Windows path limit (with reasonable directory depth).
    """
    from datetime import datetime
    libs_raw = ", ".join(libraries) if libraries else "no libraries"
    if len(libs_raw) > 120:
        # Truncate at the last comma boundary that fits, append a
        # count of remaining libraries.
        cut = libs_raw[:120].rsplit(", ", 1)[0]
        kept = cut.count(",") + 1
        remaining = len(libraries) - kept
        libs_raw = f"{cut} +{remaining} more" if remaining > 0 else cut
    try:
        dt = datetime.fromtimestamp(float(captured_at)).strftime("%Y-%m-%d %H-%M")
    except (TypeError, ValueError, OSError):
        dt = "unknown-time"
    return f"{_sanitise_filename_segment(server_name)} - {_sanitise_filename_segment(libs_raw)} - {dt}"


# ── File location + connection ──────────────────────────────────────────────

_DB_NAME = "snapshots.db"
_init_lock = threading.Lock()
_initialised = False


def _db_path() -> Path:
    return get_data_dir() / _DB_NAME


def _connect() -> sqlite3.Connection:
    """Fresh connection per call. Tiny DB, low concurrency; matches auth_db's pattern."""
    conn = sqlite3.connect(str(_db_path()), timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# ── Schema ─────────────────────────────────────────────────────────────────

_SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    id                   TEXT PRIMARY KEY,
    server_id            TEXT NOT NULL,
    server_name          TEXT NOT NULL,
    snapshot_name        TEXT NOT NULL,
    file_path            TEXT NOT NULL,
    captured_at          REAL NOT NULL,
    libraries_json       TEXT,
    -- Phase E (2026-05-16): ``user_count`` is now the full roster
    -- captured (owner + every managed user the engine attempted to
    -- gather, regardless of whether they had data). The companion
    -- ``user_count_with_data`` column counts the subset that
    -- actually contributed at least one row to any metric table.
    -- Older snapshots have user_count populated under the legacy
    -- "with-data only, excluding owner" semantics; the listing UI
    -- treats user_count_with_data=NULL as "fall back to user_count".
    user_count           INTEGER,
    user_count_with_data INTEGER,
    row_counts_json      TEXT,
    file_size            INTEGER,
    prebuilt_json_path   TEXT,
    -- captured_types_json: JSON list of which data types this run
    -- *actually* gathered (subset of "watch_history" / "ratings" /
    -- "playlists" / "collections"). The import UI gates the
    -- include_* toggles on this rather than ``row_counts_json``,
    -- because row_counts reflects the cumulative state of media.db
    -- (every prior run's data for that server is still in the .db)
    -- whereas captured_types reflects what THIS run touched. NULL
    -- for rows captured before this column existed - the import UI
    -- falls back to row_counts in that case.
    captured_types_json  TEXT,
    -- Phase D (admin-management follow-up, 2026-05-15): a one-line
    -- human-readable summary stamped at capture time. Lists the
    -- server, library set, metric set, and user count. NULL for
    -- rows captured before this column existed - the listing UI
    -- renders an em-dash in that case.
    description          TEXT
);
CREATE INDEX IF NOT EXISTS idx_snapshots_server_id
    ON snapshots(server_id);
CREATE INDEX IF NOT EXISTS idx_snapshots_captured_at
    ON snapshots(captured_at);
"""


def init_registry() -> None:
    """Create ``snapshots.db`` and apply schema. Idempotent.

    Also runs an in-place ALTER for the ``captured_types_json``
    column so existing installs don't lose rows when this column was
    added. ``CREATE TABLE IF NOT EXISTS`` won't add columns to an
    already-present table; a separate ``ALTER TABLE ADD COLUMN``
    handles that case. Idempotent: SQLite raises OperationalError
    when the column already exists, which we catch.
    """
    global _initialised
    with _init_lock:
        if _initialised:
            return
        _db_path().parent.mkdir(parents=True, exist_ok=True)
        conn = _connect()
        try:
            conn.executescript(_SCHEMA)
            # In-place migration for installs that pre-date the
            # captured_types_json column. Existing rows keep NULL;
            # the import UI handles that as "fall back to row_counts".
            try:
                conn.execute("ALTER TABLE snapshots ADD COLUMN captured_types_json TEXT")
                log.info("snapshots.db: added captured_types_json column")
            except sqlite3.OperationalError:
                pass
            # Phase D migration: same pattern for the description column.
            try:
                conn.execute("ALTER TABLE snapshots ADD COLUMN description TEXT")
                log.info("snapshots.db: added description column")
            except sqlite3.OperationalError:
                # Column already exists. Expected on every boot after
                # the first; not an error.
                pass
            # Phase E migration: user_count_with_data column. The
            # legacy user_count column was populated under
            # "with-data only, excluding owner" semantics; the new
            # column carries the with-data subset while user_count
            # now means the full roster.
            try:
                conn.execute("ALTER TABLE snapshots ADD COLUMN user_count_with_data INTEGER")
                log.info("snapshots.db: added user_count_with_data column")
            except sqlite3.OperationalError:
                pass
        finally:
            conn.close()
        _initialised = True
        log.info("snapshots.db initialised at %s", _db_path())


# ── Retention resolution ────────────────────────────────────────────────────

DEFAULT_GLOBAL_RETENTION = 30


def effective_retention_for(server_id: str) -> int:
    """
    Resolve the effective keep-count for one server.

    Rule (locked in the planning manifest): per-server override applies
    only if STRICTLY LOWER than global. Global is a ceiling, never a
    floor.

    Examples (global=30):
      * server A override=10 -> 10
      * server B override=50 -> 30 (global wins)
      * server C no override -> 30
    """
    settings = load_settings()
    try:
        global_count = int(settings.get("snapshot_retention_global", DEFAULT_GLOBAL_RETENTION))
    except (TypeError, ValueError):
        global_count = DEFAULT_GLOBAL_RETENTION
    if global_count < 1:
        global_count = 1
    overrides = settings.get("snapshot_retention_per_server", {}) or {}
    raw = overrides.get(server_id) if isinstance(overrides, dict) else None
    if raw is None:
        return global_count
    try:
        override = int(raw)
    except (TypeError, ValueError):
        return global_count
    if override < 1:
        override = 1
    return min(override, global_count)


# ── Public API ──────────────────────────────────────────────────────────────

def register(
    *,
    server_id: str,
    server_name: str,
    snapshot_name: str,
    file_path: str,
    captured_at: Optional[float] = None,
    libraries: Optional[List[str]] = None,
    user_count: Optional[int] = None,
    user_count_with_data: Optional[int] = None,
    row_counts: Optional[Dict[str, int]] = None,
    file_size: Optional[int] = None,
    prebuilt_json_path: Optional[str] = None,
    # Subset of {"watch_history", "ratings", "playlists",
    # "collections"} that this run gathered. The import UI gates its
    # include_* toggles on this list. Distinct from ``row_counts``
    # which reflects the cumulative state of media.db (includes data
    # left over from previous runs for the same server). Pass
    # ``None`` for rows where the capture flags aren't known - the
    # frontend falls back to ``row_counts`` heuristics.
    captured_types: Optional[List[str]] = None,
    # Pre-generated snapshot id. When the caller wants the snapshot
    # .db file's internal ``snapshot_meta.snapshot_id`` to match the
    # registry row's id (which it should, post-isolation-fix), it
    # generates the uuid once and passes it both to
    # ``snapshot_capture.create_snapshot_db`` and to here. Falls back
    # to a fresh uuid when None - legacy callers and orphan recovery.
    snapshot_id: Optional[str] = None,
    # Phase D (admin-management follow-up, 2026-05-15): a short
    # human-readable summary of the snapshot's contents (server,
    # users, libraries, metrics). Persisted on the registry row so
    # the Exports listing can render it without reaching into each
    # .db. None on legacy registrations - the listing UI renders
    # an em-dash placeholder.
    description: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Insert a registry row for one newly-captured snapshot. Runs
    retention enforcement against ``server_id`` after the insert.
    Returns the newly-inserted row (public shape).
    """
    init_registry()
    if not server_id or not snapshot_name or not file_path:
        raise ValueError("server_id, snapshot_name and file_path are required")
    import json
    if not snapshot_id:
        snapshot_id = uuid.uuid4().hex
    ts = float(captured_at if captured_at is not None else time.time())
    libs_json = json.dumps(list(libraries or []))
    rows_json = json.dumps(dict(row_counts or {}))
    types_json = json.dumps(list(captured_types)) if captured_types is not None else None
    conn = _connect()
    try:
        try:
            conn.execute(
                """
                INSERT INTO snapshots (
                    id, server_id, server_name, snapshot_name, file_path,
                    captured_at, libraries_json, user_count,
                    user_count_with_data, row_counts_json,
                    file_size, prebuilt_json_path, captured_types_json,
                    description
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot_id, server_id, server_name, snapshot_name,
                    file_path, ts, libs_json, user_count,
                    user_count_with_data, rows_json,
                    file_size, prebuilt_json_path, types_json,
                    description,
                ),
            )
        except sqlite3.OperationalError:
            # Pre-migration registry: description / user_count_with_data
            # column might not exist yet (init_registry runs the ALTER
            # but a race against another process could in theory beat it).
            # Retry with the pre-Phase-E shape.
            conn.execute(
                """
                INSERT INTO snapshots (
                    id, server_id, server_name, snapshot_name, file_path,
                    captured_at, libraries_json, user_count, row_counts_json,
                    file_size, prebuilt_json_path, captured_types_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot_id, server_id, server_name, snapshot_name,
                    file_path, ts, libs_json, user_count, rows_json,
                    file_size, prebuilt_json_path, types_json,
                ),
            )
    finally:
        conn.close()
    # Audit trail: registry insert is the third half of the snapshot
    # write triple (media.db ingest → snapshot.db file → snapshots.db
    # row). The first two are already audited in their respective
    # writers; this closes the loop.
    try:
        from services import db_access_log
        db_access_log.log_write(
            table="snapshots.db:snapshots",
            where={"id": snapshot_id, "server_id": server_id},
            affected_rows=1,
            intent=(
                f"register snapshot {snapshot_name!r} "
                f"(libraries={len(libraries or [])}, "
                f"file_size={file_size or 0} bytes)"
            ),
        )
    except Exception:
        pass
    # Best-effort retention sweep. Failures don't fail the register.
    try:
        enforce_retention(server_id)
    except Exception:  # pragma: no cover (defensive)
        log.exception("retention enforcement failed for server %r", server_id)
    out = get(snapshot_id)
    assert out is not None
    return out


def list_snapshots(
    server_id: Optional[str] = None,
    order: str = "captured_at DESC",
) -> List[Dict[str, Any]]:
    """
    Return registry rows. Filter by ``server_id`` when provided;
    otherwise return everything. Order is newest-first by default.

    The ``order`` argument is a literal substring rather than a
    parameter binding because SQLite doesn't allow placeholders in
    ORDER BY clauses. We accept only a small whitelist so the
    caller can't inject SQL.

    Each row's ``service_type`` is joined from the server registry
    at call time (TODO-AGENT-2-4 from Finding[BACKEND-FILTER-AUDIT]-2026-05-16.md);
    snapshot rows themselves don't carry the backend type so the
    frontend's backend-tier filter would otherwise need a per-row
    registry join. We build the map once per call to keep it O(N+M)
    instead of O(N*M).
    """
    init_registry()
    if order not in ("captured_at DESC", "captured_at ASC"):
        order = "captured_at DESC"
    conn = _connect()
    try:
        if server_id:
            rows = conn.execute(
                f"SELECT * FROM snapshots WHERE server_id = ? ORDER BY {order}",
                (server_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                f"SELECT * FROM snapshots ORDER BY {order}"
            ).fetchall()
    finally:
        conn.close()
    service_map = _build_service_type_map()
    return [_row_to_dict(r, service_map=service_map) for r in rows]


def _build_service_type_map() -> Dict[str, str]:
    """Build ``{server_id: service_type}`` for every registered server.

    Used by :func:`list_snapshots` to attach ``service_type`` to each
    snapshot row without a per-row registry lookup. Empty dict on
    registry failure - rows fall back to ``service_type=None`` which
    the frontend treats as the legacy Plex bucket."""
    try:
        from server.server_registry import list_servers
        return {
            (sv.get("id") or ""): (sv.get("service_type") or "plex").lower()
            for sv in list_servers(include_tokens=False) or []
            if sv.get("id")
        }
    except Exception:
        return {}


def get(snapshot_id: str) -> Optional[Dict[str, Any]]:
    """Single-row lookup by id. Returns ``None`` if absent."""
    init_registry()
    if not snapshot_id:
        return None
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM snapshots WHERE id = ?", (snapshot_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    # Single-row path also gets service_type via the same registry
    # join (TODO-AGENT-2-4); the map is cheap to build for one row.
    return _row_to_dict(row, service_map=_build_service_type_map())


def reconcile_orphaned_snapshots(output_dir: str) -> Dict[str, int]:
    """
    Scan ``output_dir`` for snapshot ``.db`` files that have no
    corresponding registry row and register them after reading their
    metadata. Bridges the crash-recovery gap between
    :func:`server.snapshot_capture.create_snapshot_db` (step 4 - file
    on disk) and :func:`register` (step 5 - registry row inserted).
    If the process is killed between those two steps, the file
    survives but nothing in the UI knows about it; this scan picks
    it up on next boot.

    Returns ``{"recovered": N, "skipped": M, "errors": K}``. Best-
    effort: corrupt or unreadable ``.db`` files are logged at ERROR
    and skipped - never auto-deleted. The end user decides what to
    keep.

    Naming conventions accepted:

    * **Current** (v0.12+) - ``<server> - <libraries> - YYYY-MM-DD HH-MM.db``,
      as produced by :func:`format_snapshot_filename`. The
      ``<libraries>`` segment may contain commas, hyphens, and spaces.
    * **Legacy** (pre-v0.12) - ``<slug>_YYYYMMDD_HHMMSS.db``.

    Either pattern is recovered; anything else (``media.db``,
    ``auth.db``, ``snapshots.db``, ``.db-wal`` / ``.db-shm`` sidecars,
    end user-dropped files) is left alone.

    When the orphan file carries a ``snapshot_meta`` table (every
    snapshot built by the post-Rule-1 pipeline does), the recovered
    registry row reuses the snapshot_id, libraries, captured_at, and
    metrics list recorded there - so a recovered row is as rich as a
    freshly-captured one. Older orphans without ``snapshot_meta``
    fall back to filename-derived metadata.
    """
    import re
    init_registry()
    out = {"recovered": 0, "skipped": 0, "errors": 0}
    base = Path(output_dir).resolve()
    if not base.is_dir():
        # Nothing to scan. Fresh install, missing dir, or end user
        # pointed output_dir somewhere unexpected. Either way, no
        # orphans here.
        return out

    # Two name patterns: current (server - libs - date time) and legacy
    # (slug_date_time). The current pattern is intentionally permissive
    # on the library segment so any sanitised library name passes; the
    # date+time tail anchors the regex tightly enough that non-snapshot
    # .db files (media.db, auth.db, snapshots.db) don't match.
    current_re = re.compile(
        r"^(?P<server>.+?) - (?P<libs>.+) - "
        r"(?P<date>\d{4}-\d{2}-\d{2}) (?P<hh>\d{2})-(?P<mm>\d{2})\.db$"
    )
    legacy_re = re.compile(r"^(?P<slug>.+)_(?P<date>\d{8})_(?P<time>\d{6})\.db$")

    # Index the registered file_paths once so the per-file loop
    # below doesn't run N queries. We compare against BOTH the
    # resolved absolute path (best when the running process sees the
    # same FS layout the row was written under) AND the basename
    # alone, so a registry row written inside a container with paths
    # like ``/app/snapshots/foo.db`` still dedups against the same
    # file accessed via a host-side absolute path under a bind mount.
    # Basename collisions across directories are not a risk: the
    # output_dir is the only directory we scan.
    registered: set = set()
    registered_basenames: set = set()
    registered_ids: set = set()
    conn = _connect()
    try:
        for r in conn.execute(
            "SELECT id, file_path FROM snapshots"
        ).fetchall():
            sid = (r["id"] or "").strip()
            if sid:
                registered_ids.add(sid)
            fp = (r["file_path"] or "").strip()
            if fp:
                try:
                    registered.add(str(Path(fp).resolve()))
                except OSError:
                    registered.add(fp)
                # basename is the bind-mount-tolerant key.
                registered_basenames.add(Path(fp).name)
    finally:
        conn.close()

    candidates = sorted(base.glob("*.db"))
    for candidate in candidates:
        # Skip the legacy/ subdirectory's contents - those are JSON,
        # not .db, but be defensive.
        abs_path = str(candidate.resolve())
        if abs_path in registered or candidate.name in registered_basenames:
            out["skipped"] += 1
            continue
        match = current_re.match(candidate.name)
        kind = "current"
        if match is None:
            match = legacy_re.match(candidate.name)
            kind = "legacy"
        if match is None:
            # Doesn't look like one of our snapshots - don't touch it.
            out["skipped"] += 1
            continue
        # Skip zero-byte files - they're stale artefacts from the
        # pre-Rule-1 WAL-only capture path that left the .db at 0
        # bytes. Opening them as sqlite raises DatabaseError; pre-
        # checking the size keeps the error log scannable.
        try:
            if candidate.stat().st_size == 0:
                log.info(
                    "Orphan reconcile: %s is 0 bytes; skipping.",
                    candidate.name,
                )
                out["skipped"] += 1
                continue
        except OSError:
            pass
        try:
            recovered_row = _ingest_orphan(candidate, match, kind)
        except sqlite3.DatabaseError as exc:
            log.error(
                "Orphan reconcile: %s is unreadable (%s: %s); leaving alone.",
                candidate.name, type(exc).__name__, exc,
            )
            out["errors"] += 1
            continue
        except Exception:  # pragma: no cover (defensive)
            log.exception("Orphan reconcile failed for %s; leaving alone.", candidate.name)
            out["errors"] += 1
            continue
        if recovered_row is None:
            out["skipped"] += 1
            continue
        log.warning(
            "Recovered orphan snapshot %r (server=%r, captured_at=%s)",
            candidate.name, recovered_row.get("server_name") or "(unknown)",
            recovered_row.get("captured_at"),
        )
        out["recovered"] += 1
    return out


def _ingest_orphan(
    path: Path,
    match: "re.Match[str]",
    kind: str = "current",
) -> Optional[Dict[str, Any]]:
    """
    Open one orphan ``.db`` read-only and build a registry row.
    Returns the inserted row or ``None`` if the file's ``servers``
    table is empty (so we can't attribute it to any server).

    ``kind`` is ``"current"`` for files matching the new
    ``<server> - <libs> - YYYY-MM-DD HH-MM.db`` shape and
    ``"legacy"`` for pre-v0.12 ``<slug>_YYYYMMDD_HHMMSS.db`` files.
    The two differ only in how we derive ``captured_at`` from the
    filename when ``snapshot_meta`` isn't available.

    Rich provenance: snapshot files written by the post-Rule-1
    pipeline carry a ``snapshot_meta`` table (snapshot_id,
    captured_at, libraries, metrics, created_by) and a
    ``snapshot_users`` table. When present, the recovered row uses
    those values directly so a recovered snapshot is indistinguishable
    from a freshly-captured one in the Backups panel. Older files
    fall back to filename-derived metadata.
    """
    import json
    uri = f"file:{path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=10.0)
    conn.row_factory = sqlite3.Row

    meta_snapshot_id: Optional[str] = None
    meta_server_name: Optional[str] = None
    meta_captured_at: Optional[float] = None
    meta_libraries: Optional[List[str]] = None
    meta_metrics: Optional[List[str]] = None
    meta_description: Optional[str] = None
    user_count: Optional[int] = None

    try:
        srv = conn.execute("SELECT id, name FROM servers LIMIT 1").fetchone()
        if srv is None:
            log.warning(
                "Orphan reconcile: %s has no servers row; can't attribute, skipping.",
                path.name,
            )
            return None
        server_id = srv["id"]
        server_name = srv["name"] or ""

        # Read the rich provenance table when present. Best-effort -
        # an older .db without it falls through to filename-derived
        # values below.
        try:
            # Phase D: also pull ``description`` when the column exists.
            # Older snapshot files predating Phase D don't have the
            # column; the LEFT JOIN to PRAGMA isn't worth the
            # complexity, so we just try the wider SELECT and fall
            # back to the narrower one if it errors.
            try:
                meta_row = conn.execute(
                    "SELECT snapshot_id, server_name, captured_at, "
                    "libraries_json, metrics_json, description FROM snapshot_meta LIMIT 1"
                ).fetchone()
            except sqlite3.OperationalError:
                meta_row = conn.execute(
                    "SELECT snapshot_id, server_name, captured_at, "
                    "libraries_json, metrics_json FROM snapshot_meta LIMIT 1"
                ).fetchone()
            if meta_row is not None:
                meta_snapshot_id = (meta_row["snapshot_id"] or None)
                meta_server_name = (meta_row["server_name"] or None)
                try:
                    meta_captured_at = float(meta_row["captured_at"])
                except (TypeError, ValueError):
                    meta_captured_at = None
                try:
                    libs_raw = json.loads(meta_row["libraries_json"] or "[]")
                    meta_libraries = [str(x) for x in libs_raw] if isinstance(libs_raw, list) else None
                except Exception:
                    meta_libraries = None
                try:
                    metrics_raw = json.loads(meta_row["metrics_json"] or "[]")
                    meta_metrics = [str(x) for x in metrics_raw] if isinstance(metrics_raw, list) else None
                except Exception:
                    meta_metrics = None
                # Phase D: description column only present on snapshots
                # captured by post-Phase-D engine builds. sqlite3.Row
                # raises IndexError on missing keys, so the wrapping
                # try shields legacy rows.
                try:
                    meta_description = meta_row["description"] or None
                except Exception:
                    meta_description = None
        except sqlite3.OperationalError:
            # Older schema without snapshot_meta - fall through.
            pass

        # Phase E: read both the roster total and the with-data subset.
        # Older snapshots without the had_data column fall back to
        # roster-total only (with_data == total under pre-Phase-E
        # semantics where only-with-data rows were ever written).
        user_count: Optional[int] = None
        user_count_with_data: Optional[int] = None
        try:
            row = conn.execute(
                "SELECT "
                "  COUNT(*) AS n_total, "
                "  SUM(CASE WHEN had_data = 1 THEN 1 ELSE 0 END) AS n_with_data "
                "FROM snapshot_users"
            ).fetchone()
            if row is not None:
                user_count = int(row["n_total"] or 0) or None
                ucwd = row["n_with_data"]
                user_count_with_data = int(ucwd) if ucwd is not None else None
        except sqlite3.OperationalError:
            try:
                row = conn.execute(
                    "SELECT COUNT(*) AS n FROM snapshot_users"
                ).fetchone()
                if row is not None:
                    user_count = int(row["n"]) if row else None
                    user_count_with_data = user_count
            except sqlite3.OperationalError:
                pass

        # Per-table row counts so the registry row mirrors the shape
        # of a fresh capture's ``row_counts_json``.
        counters: Dict[str, int] = {}
        for table in (
            "servers", "items", "server_items", "watch_events",
            "ratings", "playlists", "collections",
        ):
            try:
                n = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
                counters[table] = int(n["n"]) if n else 0
            except sqlite3.OperationalError:
                counters[table] = 0
    finally:
        conn.close()

    # Resolve fields, preferring rich snapshot_meta values when present.
    if meta_server_name:
        server_name = meta_server_name

    captured_at: float
    if meta_captured_at is not None:
        captured_at = meta_captured_at
    else:
        try:
            from datetime import datetime
            if kind == "current":
                ts_str = f"{match['date']} {match['hh']}-{match['mm']}"
                captured_at = datetime.strptime(ts_str, "%Y-%m-%d %H-%M").timestamp()
            else:
                ts_str = f"{match['date']}_{match['time']}"
                captured_at = datetime.strptime(ts_str, "%Y%m%d_%H%M%S").timestamp()
        except Exception:
            captured_at = path.stat().st_mtime

    libraries_for_row: List[str]
    if meta_libraries is not None:
        libraries_for_row = meta_libraries
    elif kind == "current":
        # The ``<libs>`` segment from the filename is comma-joined when
        # there were multiple libraries (see format_snapshot_filename).
        # Split it back out so the Backups panel shows the correct
        # library chips.
        raw = (match["libs"] or "").strip()
        if raw and raw != "no libraries":
            libraries_for_row = [s.strip() for s in raw.split(",") if s.strip()]
        else:
            libraries_for_row = []
    else:
        libraries_for_row = []

    file_size = path.stat().st_size
    snapshot_id = meta_snapshot_id or uuid.uuid4().hex
    snapshot_name = f"{path.stem} (recovered)"

    # Insert directly so the snapshot_name carries the "(recovered)"
    # marker AND we can keep captured_at from snapshot_meta /
    # filename rather than time.time(). Retention is intentionally
    # not enforced for recovered rows on first insert.
    #
    # IntegrityError handling: if the snapshot_meta.snapshot_id is
    # already in the registry, the .db has a registry row under a
    # different file_path (e.g. the basename-only check above missed
    # because of a path-shape mismatch the new bind-mount-tolerant
    # check should have caught - but defence in depth). Treat as
    # "already registered, skip" by returning None.
    conn = _connect()
    try:
        try:
            conn.execute(
                """
                INSERT INTO snapshots (
                    id, server_id, server_name, snapshot_name, file_path,
                    captured_at, libraries_json, user_count,
                    user_count_with_data, row_counts_json,
                    file_size, prebuilt_json_path, captured_types_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot_id, server_id, server_name, snapshot_name,
                    str(path.resolve()), captured_at,
                    json.dumps(libraries_for_row), user_count,
                    user_count_with_data,
                    json.dumps(counters), file_size, None,
                    json.dumps(meta_metrics) if meta_metrics is not None else None,
                ),
            )
        except sqlite3.IntegrityError:
            log.info(
                "Orphan reconcile: %s carries snapshot_id %r which is "
                "already registered; skipping.",
                path.name, snapshot_id,
            )
            return None
        except sqlite3.OperationalError:
            # captured_types_json column may not exist on a very old
            # registry schema. Retry without it.
            try:
                conn.execute(
                    """
                    INSERT INTO snapshots (
                        id, server_id, server_name, snapshot_name, file_path,
                        captured_at, libraries_json, user_count, row_counts_json,
                        file_size, prebuilt_json_path
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        snapshot_id, server_id, server_name, snapshot_name,
                        str(path.resolve()), captured_at,
                        json.dumps(libraries_for_row), user_count,
                        json.dumps(counters), file_size, None,
                    ),
                )
            except sqlite3.IntegrityError:
                log.info(
                    "Orphan reconcile: %s carries snapshot_id %r which is "
                    "already registered; skipping.",
                    path.name, snapshot_id,
                )
                return None
    finally:
        conn.close()
    # Audit trail: orphan reconcile writes a fresh registry row by
    # bypassing register(); record the equivalent write so the audit
    # log doesn't have a gap.
    try:
        from services import db_access_log
        db_access_log.log_write(
            table="snapshots.db:snapshots",
            where={"id": snapshot_id, "server_id": server_id},
            affected_rows=1,
            intent=(
                f"recover orphan snapshot {path.name!r} "
                f"(kind={kind}, file_size={file_size} bytes)"
            ),
        )
    except Exception:
        pass
    return get(snapshot_id)


def materialise_sidecar(snapshot_id: str) -> Optional[str]:
    """
    Render this snapshot's ``.plexbackup.json`` sidecar from the
    underlying ``.db`` file and stamp the registry row's
    ``prebuilt_json_path`` so subsequent downloads stream the cached
    file instead of re-rendering.

    Returns the absolute sidecar path on success, ``None`` if the
    snapshot row is missing, the backing ``.db`` is gone, or the
    render fails. Idempotent: if a sidecar already exists on disk and
    the row already points at it, the existing path is returned
    unchanged.

    Used by:
      * ``_capture_snapshot_after_run`` when the end user opted in to
        ``prebuild_json_sidecar`` on the snapshot job / schedule.
      * The ``GET /api/snapshots/{id}/download`` endpoint on its first
        cache miss, so the second download is instant.
    """
    init_registry()
    row = get(snapshot_id)
    if row is None:
        return None

    # Already cached and the file still exists? Nothing to do.
    pre = row.get("prebuilt_json_path") or ""
    if pre and Path(pre).is_file():
        return pre

    db_path = Path(row.get("file_path") or "")
    if not db_path.is_file():
        log.warning(
            "materialise_sidecar: snapshot %s .db missing on disk (%s)",
            snapshot_id, db_path,
        )
        return None

    # Late import: the serializer is server-side only and we want this
    # module importable in CLI-only checkouts too.
    from server import snapshot_serializer
    try:
        payload = snapshot_serializer.build_payload_from_db(
            db_path,
            server_name=row.get("server_name") or "",
            server_id=row.get("server_id") or "",
            libraries=row.get("libraries") or [],
            captured_at_ts=row.get("captured_at"),
        )
    except Exception:
        log.exception("materialise_sidecar: render failed for %s", snapshot_id)
        return None

    import json
    # New sidecars use .plexexport.json. The browser / loader accept
    # both .plexexport.json and the legacy .plexbackup.json so existing
    # archives keep working without a migration.
    sidecar_path = db_path.with_suffix(".plexexport.json")
    try:
        sidecar_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError:
        log.exception(
            "materialise_sidecar: failed writing sidecar %s", sidecar_path,
        )
        return None

    # Update the registry row in place so the next download is instant.
    conn = _connect()
    try:
        conn.execute(
            "UPDATE snapshots SET prebuilt_json_path = ? WHERE id = ?",
            (str(sidecar_path), snapshot_id),
        )
    finally:
        conn.close()
    # Audit trail: stamping prebuilt_json_path is a mutating write on
    # the registry row that should appear in db_access.log.
    try:
        from services import db_access_log
        db_access_log.log_write(
            table="snapshots.db:snapshots",
            where={"id": snapshot_id, "column": "prebuilt_json_path"},
            affected_rows=1,
            intent=f"materialise sidecar {sidecar_path.name!r}",
        )
    except Exception:
        pass
    return str(sidecar_path)


def reap_stale_sidecars(*, ttl_seconds: Optional[int] = None, now: Optional[float] = None) -> Dict[str, int]:
    """
    v0.13.x: sweep the registry for ``prebuilt_json_path`` sidecars
    older than the configured TTL and delete them from disk, clearing
    the column on the matching row.

    Generated sidecars are re-materialisable from the snapshot ``.db``
    at any time (the download endpoint regenerates on first miss),
    so reaping a stale one is non-destructive - the end user's next
    Download click rebuilds it. The point of the TTL is to avoid
    holding the rendered JSON on disk indefinitely; pre-built or
    just-downloaded sidecars are intentionally short-lived caches.

    Args:
        ttl_seconds: seconds-since-mtime threshold for reaping. When
            ``None`` (the default), reads the live tunable. ``0``
            disables the sweep entirely (returns immediately with
            zeroed counters).
        now: epoch-seconds reference for "is this file old?" Defaults
            to ``time.time()``; the parameter exists so tests can pin
            the clock without monkey-patching ``time``.

    Returns counter dict:
        ``{"reaped": N, "missing": N, "skipped": N, "errors": N}``
        where ``reaped`` is the count of sidecar files actually
        deleted, ``missing`` is rows whose file was already gone (we
        still clear the column), ``skipped`` is rows whose file is
        younger than the TTL, ``errors`` is rows where deletion or
        the UPDATE failed.
    """
    counters: Dict[str, int] = {"reaped": 0, "missing": 0, "skipped": 0, "errors": 0}

    if ttl_seconds is None:
        try:
            from services import tunables
            ttl_seconds = tunables.snapshot_sidecar_ttl_seconds()
        except Exception:
            # Defence in depth: if the tunable can't be read for any
            # reason, default to the project-wide default (5 min)
            # rather than disabling the sweep silently.
            ttl_seconds = 300
    if ttl_seconds <= 0:
        # End user-disabled. Caller may still want a structured
        # response so the loop can log "sweep disabled" once.
        return counters

    init_registry()
    now_ts = float(now) if now is not None else time.time()
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT id, prebuilt_json_path FROM snapshots "
            "WHERE prebuilt_json_path IS NOT NULL AND prebuilt_json_path != ''"
        ).fetchall()
    finally:
        conn.close()

    for r in rows:
        sid = r["id"]
        path_str = r["prebuilt_json_path"] or ""
        if not path_str:
            continue
        path = Path(path_str)
        # File-gone branch: clear the column so the next download
        # round-trips through ``materialise_sidecar`` cleanly instead
        # of returning the stale path.
        if not path.is_file():
            try:
                conn = _connect()
                try:
                    conn.execute(
                        "UPDATE snapshots SET prebuilt_json_path = NULL WHERE id = ?",
                        (sid,),
                    )
                finally:
                    conn.close()
                counters["missing"] += 1
            except Exception:
                log.exception("reap: failed clearing column for snapshot %s", sid)
                counters["errors"] += 1
            continue

        try:
            age = now_ts - path.stat().st_mtime
        except OSError:
            counters["errors"] += 1
            continue

        if age < float(ttl_seconds):
            counters["skipped"] += 1
            continue

        # Stale: delete the file, then clear the column. Order matters
        # only for crash-safety: if we delete first and the UPDATE
        # fails, the next sweep finds the now-orphaned column and
        # routes to the file-gone branch above. The inverse order
        # would leave a stale sidecar pointing at a column we just
        # nulled.
        try:
            path.unlink()
            counters["reaped"] += 1
        except OSError:
            log.exception(
                "reap: could not delete stale sidecar %s for snapshot %s",
                path, sid,
            )
            counters["errors"] += 1
            continue

        try:
            conn = _connect()
            try:
                conn.execute(
                    "UPDATE snapshots SET prebuilt_json_path = NULL WHERE id = ?",
                    (sid,),
                )
            finally:
                conn.close()
        except Exception:
            log.exception("reap: failed clearing column after delete for %s", sid)
            counters["errors"] += 1

    # Audit trail: a sweep that actually reaped files is a noteworthy
    # write surface. Skip when nothing happened to avoid log noise.
    if counters["reaped"] or counters["missing"]:
        try:
            from services import db_access_log
            db_access_log.log_write(
                table="snapshots.db:snapshots",
                where={"column": "prebuilt_json_path", "reason": "sidecar_ttl_sweep"},
                affected_rows=counters["reaped"] + counters["missing"],
                intent=(
                    f"reap stale sidecars (ttl={ttl_seconds}s): "
                    f"reaped={counters['reaped']} "
                    f"missing={counters['missing']} "
                    f"skipped={counters['skipped']} "
                    f"errors={counters['errors']}"
                ),
            )
        except Exception:
            pass

    return counters


def delete(snapshot_id: str, *, keep_json: bool = False) -> Dict[str, Any]:
    """
    Remove the registry row and the on-disk ``.db``. By default the
    cached JSON sidecar is removed too; pass ``keep_json=True`` to
    preserve it as a portable JSON archive instead.

    Keep-JSON flow:
      1. If no cached sidecar exists yet, materialise one from the
         ``.db`` (so deletion never silently drops capturable data
         the end user wanted to preserve).
      2. Move the sidecar into ``<output_dir>/legacy/`` - the same
         directory that backs the JSON-archive listing endpoint
         (``GET /api/snapshots/legacy``) and the JobForm's "From
         JSON archive" import source. From this point on the file
         is a standalone JSON archive, not bound to the registry.
      3. Delete the .db and drop the registry row.

    Returns ``{deleted, file_removed, json_removed, json_archived,
    archived_path, errors}``. Raises ``ValueError`` on unknown id.
    """
    init_registry()
    row = get(snapshot_id)
    if row is None:
        raise ValueError(f"No snapshot with id {snapshot_id!r}.")
    errors: List[str] = []
    file_removed = False
    json_removed = False
    json_archived = False
    archived_path: Optional[str] = None

    # Step 1: materialise the sidecar if the end user wants to keep
    # JSON but no cache exists yet. Best-effort - if render fails we
    # still proceed with .db deletion; the end user loses the JSON
    # but the explicit failure surfaces in ``errors``.
    if keep_json:
        try:
            rendered = materialise_sidecar(snapshot_id)
            if rendered:
                # Re-read the row to pick up the freshly-stamped
                # prebuilt_json_path.
                row = get(snapshot_id) or row
        except Exception as exc:  # pragma: no cover (defensive)
            errors.append(
                f"materialise_sidecar failed: {type(exc).__name__}: {exc}"
            )

    pre = row.get("prebuilt_json_path") or ""
    if keep_json and pre:
        # Move the sidecar to the legacy/ archive directory before
        # the .db deletion, so a half-failed move never leaves an
        # orphan registry row with prebuilt_json_path pointing at a
        # vanished file.
        try:
            import shutil
            src = Path(pre)
            if src.is_file():
                # Archive dir lives next to the .db, i.e. inside the
                # snapshot output_dir's ``legacy/`` subdirectory.
                # Resolve from the registry row's file_path so we
                # don't have to re-read settings.
                file_path = row.get("file_path") or ""
                if file_path:
                    archive_dir = Path(file_path).parent / "legacy"
                else:
                    archive_dir = src.parent / "legacy"
                archive_dir.mkdir(parents=True, exist_ok=True)
                target = archive_dir / src.name
                # Avoid clobber: if a same-named file already lives
                # in legacy/, suffix the target with a numeric.
                n = 1
                while target.exists():
                    target = archive_dir / f"{src.stem}_{n}{src.suffix}"
                    n += 1
                shutil.move(str(src), str(target))
                json_archived = True
                archived_path = str(target)
        except OSError as e:
            errors.append(f"archive move: {type(e).__name__}: {e}")

    # Step 2: remove the .db file.
    file_path = row.get("file_path") or ""
    if file_path:
        try:
            p = Path(file_path)
            if p.is_file():
                p.unlink()
                file_removed = True
        except OSError as e:
            errors.append(f"{Path(file_path).name}: {type(e).__name__}: {e}")

    # Step 3: remove the cached JSON sidecar - but only when we
    # weren't asked to keep it. The archive move above already
    # relocated it; this branch handles the regular delete-everything
    # case where the sidecar is still at its registered path.
    if not keep_json and pre:
        try:
            p = Path(pre)
            if p.is_file():
                p.unlink()
                json_removed = True
        except OSError as e:
            errors.append(f"{Path(pre).name}: {type(e).__name__}: {e}")

    # Step 4: drop the registry row. Authoritative - any failures
    # above still result in the row going away because the registry
    # should never reference half-deleted state.
    conn = _connect()
    try:
        conn.execute("DELETE FROM snapshots WHERE id = ?", (snapshot_id,))
    finally:
        conn.close()
    # Audit trail: the row drop and any side-effects (.db unlink, JSON
    # archive move, sidecar delete) are end user-visible state changes
    # that belong in db_access.log.
    try:
        from services import db_access_log
        db_access_log.log_write(
            table="snapshots.db:snapshots",
            where={
                "id": snapshot_id,
                "server_id": row.get("server_id") or "",
            },
            affected_rows=1,
            intent=(
                f"delete snapshot {row.get('snapshot_name') or snapshot_id!r} "
                f"(file_removed={file_removed}, json_removed={json_removed}, "
                f"json_archived={json_archived}, keep_json={keep_json})"
            ),
        )
    except Exception:
        pass
    return {
        "deleted": snapshot_id,
        "file_removed": file_removed,
        "json_removed": json_removed,
        "json_archived": json_archived,
        "archived_path": archived_path,
        "errors": errors,
    }


def enforce_retention(server_id: str) -> Dict[str, int]:
    """
    Delete the oldest registry rows + files past the effective keep
    count for ``server_id``. Returns ``{deleted, errors}``.
    """
    keep = effective_retention_for(server_id)
    rows = list_snapshots(server_id=server_id, order="captured_at DESC")
    if len(rows) <= keep:
        return {"deleted": 0, "errors": 0}
    victims = rows[keep:]  # everything past the keep window
    deleted = 0
    errors = 0
    for v in victims:
        try:
            r = delete(v["id"])
            deleted += 1
            errors += len(r.get("errors") or [])
        except Exception:
            errors += 1
            log.exception("retention delete failed for %r", v.get("id"))
    if deleted:
        log.info("Retention: removed %d snapshot(s) for server %r (keep=%d)",
                 deleted, server_id, keep)
    return {"deleted": deleted, "errors": errors}


def reassign_orphan_snapshots(
    *,
    target_server_id: str,
    target_server_name: str,
) -> Dict[str, int]:
    """
    Find every snapshot whose stored ``server_name`` matches
    ``target_server_name`` but whose ``server_id`` no longer points at
    any registered server, and rewrite their ``server_id`` to
    ``target_server_id``.

    Used by the "Merge orphan snapshots" admin action surfaced under
    Tunables. The Exports panel already merges orphans into one tab
    at display time, so the end user-facing UX is the same either
    way; this helper exists to MIGRATE the underlying rows when an
    end user wants to clean up the registry once and for all.

    Returns counters: ``{checked, reassigned, no_op}``. ``no_op`` is
    the count of rows whose server_id was already correct (defensive
    in case the helper is called twice in a row).
    """
    from server import server_registry as _registry
    # Build the live id set so we know which snapshots are orphaned.
    live_ids = {s["id"] for s in _registry.list_servers()}

    counters = {"checked": 0, "reassigned": 0, "no_op": 0}
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT id, server_id FROM snapshots WHERE server_name = ?",
            (target_server_name,),
        ).fetchall()
        for r in rows:
            counters["checked"] += 1
            current_id = r["server_id"]
            if current_id == target_server_id:
                counters["no_op"] += 1
                continue
            if current_id in live_ids:
                # The current id is a DIFFERENT live server; do NOT
                # rewrite. The end user would be quietly merging
                # snapshots from a genuinely-separate registered
                # server. Counts under no_op because the row is
                # already correctly attached to a live server.
                counters["no_op"] += 1
                continue
            conn.execute(
                "UPDATE snapshots SET server_id = ? WHERE id = ?",
                (target_server_id, r["id"]),
            )
            counters["reassigned"] += 1
    finally:
        conn.close()
    log.info(
        "reassign_orphan_snapshots target=%r (id=%r): %s",
        target_server_name, target_server_id, counters,
    )
    return counters


def delete_all_for_server(server_id: str) -> Dict[str, int]:
    """
    End user-driven 'clear all snapshots for this server' path from
    the Backups panel. Removes every snapshot row + file for the
    given server. db_admin gating happens in the route layer.
    """
    rows = list_snapshots(server_id=server_id)
    deleted = 0
    errors = 0
    for r in rows:
        try:
            d = delete(r["id"])
            deleted += 1
            errors += len(d.get("errors") or [])
        except Exception:
            errors += 1
            log.exception("delete failed for snapshot %r", r.get("id"))
    return {"deleted": deleted, "errors": errors}


# ── Helpers ────────────────────────────────────────────────────────────────

def _row_to_dict(
    row: sqlite3.Row,
    *,
    service_map: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    import json
    file_path = row["file_path"] or ""
    prebuilt = row["prebuilt_json_path"] or ""
    has_db = bool(file_path) and os.path.exists(file_path)
    has_sidecar = bool(prebuilt) and os.path.exists(prebuilt)
    # ``sidecar_size`` is fresh-stat'd rather than stored in the DB
    # because the sidecar may be rendered, deleted, or re-rendered
    # between registry writes. Returning None when no cache exists
    # lets the UI label the row "(generates when downloaded)" instead
    # of "0 B". Cheap - one stat() per row, on a tiny per-snapshot
    # table.
    sidecar_size: Optional[int] = None
    if has_sidecar:
        try:
            sidecar_size = int(os.path.getsize(prebuilt))
        except OSError:
            sidecar_size = None
    # ``available`` lets the UI render dead-row state without a
    # separate liveness call per row. Cheap - one os.path.exists per
    # row. ``file_path`` is absolute (Fix 3) so the check doesn't
    # depend on the FastAPI process's CWD at call time.
    #
    # ``has_cached_sidecar`` lets the Backups panel pick the right
    # button label - "Generate" when no .plexbackup.json exists yet
    # for this snapshot, "Download" when a cached file is on disk.
    # captured_types: NULL on pre-fix rows (column added later). The
    # frontend distinguishes ``null`` from ``[]`` - null means "fall
    # back to row_counts gating", empty list means "the run captured
    # nothing" (every include_* toggle should be greyed).
    types_raw = None
    try:
        # _row_to_dict is called after ALTER TABLE so the column
        # exists; sqlite3.Row.keys() varies by SQLite version but
        # accessing a missing key raises IndexError. Defensive.
        types_raw = row["captured_types_json"]
    except (IndexError, KeyError):
        types_raw = None
    captured_types = _safe_json_list(types_raw) if types_raw else None
    # Phase D: description is None on pre-migration rows. The
    # frontend renders an em-dash placeholder when None.
    description: Optional[str] = None
    try:
        description = row["description"] or None
    except (IndexError, KeyError):
        description = None
    server_id = row["server_id"]
    # TODO-AGENT-2-4: service_type joined from the server registry.
    # ``None`` when the server is no longer registered (orphan
    # snapshot from a since-deleted server); the frontend treats
    # null as the legacy Plex bucket. ``service_map`` is the
    # pre-built lookup from :func:`_build_service_type_map`; absent
    # for legacy callers that haven't migrated yet.
    service_type: Optional[str] = None
    if service_map is not None and server_id:
        service_type = service_map.get(server_id)
    # Phase E: user_count is the roster total; user_count_with_data
    # is the subset with at least one row of metric data. Legacy
    # rows have NULL user_count_with_data; the frontend treats NULL
    # as "fall back to user_count for both numbers".
    user_count_with_data: Optional[int] = None
    try:
        ucwd = row["user_count_with_data"]
        if ucwd is not None:
            user_count_with_data = int(ucwd)
    except (IndexError, KeyError):
        pass
    return {
        "id": row["id"],
        "server_id": server_id,
        "server_name": row["server_name"],
        "snapshot_name": row["snapshot_name"],
        "file_path": file_path,
        "captured_at": row["captured_at"],
        "libraries": _safe_json_list(row["libraries_json"]),
        "user_count": row["user_count"],
        "user_count_with_data": user_count_with_data,
        "row_counts": _safe_json_dict(row["row_counts_json"]),
        "file_size": row["file_size"],
        "prebuilt_json_path": prebuilt,
        "available": has_db,
        "has_cached_sidecar": has_sidecar,
        "sidecar_size": sidecar_size,
        "captured_types": captured_types,
        "description": description,
        "service_type": service_type,
    }


def _safe_json_list(raw: Optional[str]) -> List[str]:
    import json
    if not raw:
        return []
    try:
        v = json.loads(raw)
        return list(v) if isinstance(v, list) else []
    except Exception:
        return []


def _safe_json_dict(raw: Optional[str]) -> Dict[str, int]:
    import json
    if not raw:
        return {}
    try:
        v = json.loads(raw)
        return {str(k): int(val) for k, val in v.items()} if isinstance(v, dict) else {}
    except Exception:
        return {}


# ── Server-UID boot migration helper (Plan[SERVER-UID-IDENTITY]) ─────────────
#
# Companion to ``server/server_registry.migrate_server_ids_add_backend_prefix``.
# When the boot upgrade rewrites bare-UUID server rows to the prefixed
# form, snapshots.db's ``snapshots.server_id`` column (and the
# ``snapshot_retention_per_server`` settings map) also needs the
# rewrite so registered snapshots keep resolving to the right
# server. Best-effort: failures are caught + logged + non-fatal.

def rewrite_server_ids_in_snapshots(
    old_to_new: Dict[str, str],
) -> int:
    """Bulk-update ``snapshots.server_id`` rows whose value appears
    in ``old_to_new``. Also rewrites the
    ``snapshot_retention_per_server`` settings map (JSON-encoded
    dict keyed by server_id) so per-server retention overrides
    survive the upgrade.

    Returns the total number of rows touched (snapshot rows updated
    plus 1 if the retention settings map was rewritten)."""
    if not old_to_new:
        return 0
    n = 0
    init_registry()
    conn = _connect()
    try:
        # Snapshot rows.
        for old_id, new_id in old_to_new.items():
            try:
                cur = conn.execute(
                    "UPDATE snapshots SET server_id = ? "
                    "WHERE server_id = ?", (new_id, old_id),
                )
                n += cur.rowcount or 0
            except Exception:
                log.exception(
                    "rewrite_server_ids_in_snapshots: pair "
                    "(%r -> %r) update failed; continuing.",
                    old_id, new_id,
                )
        # Per-server retention settings map (stored as JSON in the
        # snapshot_settings k/v table). Rewrite keys in-place.
        try:
            row = conn.execute(
                "SELECT value FROM snapshot_settings "
                "WHERE key = 'snapshot_retention_per_server'",
            ).fetchone()
            if row is not None and row[0]:
                import json as _json
                try:
                    current = _json.loads(row[0])
                except Exception:
                    current = None
                if isinstance(current, dict):
                    rewritten = {
                        old_to_new.get(k, k): v
                        for k, v in current.items()
                    }
                    if rewritten != current:
                        conn.execute(
                            "UPDATE snapshot_settings SET value = ? "
                            "WHERE key = 'snapshot_retention_per_server'",
                            (_json.dumps(rewritten),),
                        )
                        n += 1
        except Exception:
            log.exception(
                "rewrite_server_ids_in_snapshots: per-server retention "
                "rewrite failed; continuing.",
            )
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return n
