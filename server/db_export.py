"""
Operational database export / import.

Provides a generic JSON dump+restore over a curated list of operational
SQLite tables (run_timings, snapshots, media). The end user-facing
Settings > Run History tab uses this to:

  * download per-table JSON for backup / interop with external tooling
  * download a full-archive JSON bundling every operational table
  * restore from a previously-downloaded JSON (replace-only semantics
    behind a typed REPLACE confirmation - import never merges)

Out of scope (intentionally):

  * ``auth.db`` and ``.keyfile`` - exporting these would leak
    credentials. The Settings UI never offers a download button for
    auth state.
  * Per-snapshot ``.db`` / ``.plexexport.json`` files in
    ``snapshots/`` - those are large, immutable artefacts. The
    ``snapshots`` table here is the REGISTRY only (one row per
    captured file); the files themselves live next to it.

Format:

  Single table dump::

      {
        "format": "plexbackup.dbexport.v1",
        "table_id": "run_timings",
        "exported_at": 1747416000.0,
        "schema": ["col1", "col2", ...],
        "rows": [{"col1": v, "col2": v, ...}, ...]
      }

  Full archive::

      {
        "format": "plexbackup.archive.v1",
        "exported_at": 1747416000.0,
        "tables": {
          "<table_id>": { ...single-table dump shape... },
          ...
        }
      }
"""

from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from server._db_connect import open_db
from server.persistence import get_data_dir


log = logging.getLogger("plexmigrate.server.db_export")


# ── Table catalog ───────────────────────────────────────────────────────────
#
# Curated list of operational tables this module is willing to dump or
# restore. Each entry maps the end user-facing ``table_id`` to the
# (db_file, table_name) tuple. Schema is discovered via PRAGMA at
# runtime so adding a column to one of these tables doesn't require
# an update here.

_TABLE_CATALOG: Dict[str, Tuple[str, str]] = {
    # run_timings.db - per-job summaries + per-operation timings
    "run_history":         ("run_timings.db", "run_history"),
    "run_timings":         ("run_timings.db", "run_timings"),
    # snapshots.db - snapshot registry (the .db files themselves are
    # on disk in snapshots/; this is the INDEX of them)
    "snapshots_registry":  ("snapshots.db", "snapshots"),
    # media.db - cumulative media + activity + identity state
    "media_items":         ("media.db", "items"),
    "media_server_items":  ("media.db", "server_items"),
    "media_watch_events":  ("media.db", "watch_events"),
    "media_ratings":       ("media.db", "ratings"),
    "media_playlists":     ("media.db", "playlists"),
    "media_collections":   ("media.db", "collections"),
    "media_library_sections": ("media.db", "library_sections"),
    "media_managed_users": ("media.db", "managed_users"),
    "media_servers":       ("media.db", "servers"),
    "media_server_users":  ("media.db", "server_users"),
    "media_user_identity_map": ("media.db", "user_identity_map"),
    "media_library_walks": ("media.db", "library_walks"),
    "media_global_tombstones": ("media.db", "global_tombstones"),
    "media_schema_version": ("media.db", "schema_version"),
}


# Tables that contain a ``server_id`` column. The Settings panel uses
# this list to pivot the table view by server when the end user wants
# to inspect identity data scoped to one Plex/Jellyfin/Emby instance.
# Order in the list matches what's most useful when investigating one
# server's identity state: server registry row -> known users on it
# -> media items -> cross-server identity links.

_PER_SERVER_TABLES: List[str] = [
    "media_servers",
    "media_server_users",
    "media_managed_users",
    "media_server_items",
    "media_watch_events",
    "media_ratings",
    "media_playlists",
    "media_collections",
    "media_library_sections",
    "media_user_identity_map",
    "media_library_walks",
    "run_history",
    "snapshots_registry",
]


def list_tables() -> List[Dict[str, Any]]:
    """Return the catalog with live row counts + db file paths.
    Driven by the Settings > Run History tab to list what is
    available for export."""
    out: List[Dict[str, Any]] = []
    for table_id, (db_file, table_name) in _TABLE_CATALOG.items():
        path = get_data_dir() / db_file
        row_count: Optional[int] = None
        if path.exists():
            try:
                conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
                try:
                    r = conn.execute(
                        f"SELECT COUNT(*) FROM {table_name}"
                    ).fetchone()
                    row_count = int(r[0] if r else 0)
                except sqlite3.OperationalError:
                    row_count = None
                finally:
                    conn.close()
            except sqlite3.OperationalError:
                row_count = None
        out.append({
            "table_id": table_id,
            "db_file": db_file,
            "table_name": table_name,
            "row_count": row_count,
            "available": path.exists(),
            "per_server": table_id in _PER_SERVER_TABLES,
        })
    return out


def list_per_server_view(server_id: str) -> List[Dict[str, Any]]:
    """Return per-table row counts SCOPED to ``server_id``. Drives
    the Settings Run History "filter by server" pivot where the
    end user inspects one Plex/Jellyfin/Emby server's identity-
    related rows in isolation (server registry entry, known users,
    library sections, watch history, ratings, identity links, etc.).

    For tables that don't carry a ``server_id`` column, the entry is
    silently skipped - the per-server view only exposes tables that
    are actually filterable that way."""
    if not server_id:
        return []
    out: List[Dict[str, Any]] = []
    for table_id in _PER_SERVER_TABLES:
        if table_id not in _TABLE_CATALOG:
            continue
        db_file, table_name = _TABLE_CATALOG[table_id]
        path = get_data_dir() / db_file
        if not path.exists():
            continue
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
        except sqlite3.OperationalError:
            continue
        try:
            # Detect the server-id column name. ``servers`` itself
            # uses ``id`` as PK; everything else uses ``server_id``.
            cols = [
                r[1] for r in conn.execute(
                    f"PRAGMA table_info({table_name})"
                ).fetchall()
            ]
            if "server_id" in cols:
                sid_col = "server_id"
            elif "id" in cols and table_name == "servers":
                sid_col = "id"
            else:
                continue  # not a per-server table after all
            try:
                row = conn.execute(
                    f"SELECT COUNT(*) FROM {table_name} WHERE {sid_col} = ?",
                    (server_id,),
                ).fetchone()
                row_count = int(row[0] if row else 0)
            except sqlite3.OperationalError:
                continue
        finally:
            conn.close()
        out.append({
            "table_id": table_id,
            "db_file": db_file,
            "table_name": table_name,
            "row_count": row_count,
        })
    return out


def export_per_server(server_id: str) -> Dict[str, Any]:
    """Dump every per-server table filtered to ``server_id`` into a
    single archive document. Useful for migrating one server's
    identity state to another install, or archiving before a
    server removal."""
    if not server_id:
        raise ValueError("server_id is required")
    archive_tables: Dict[str, Any] = {}
    for table_id in _PER_SERVER_TABLES:
        if table_id not in _TABLE_CATALOG:
            continue
        db_file, table_name = _TABLE_CATALOG[table_id]
        path = get_data_dir() / db_file
        if not path.exists():
            continue
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10.0)
            conn.row_factory = sqlite3.Row
        except sqlite3.OperationalError:
            continue
        try:
            cols = [
                r["name"] for r in conn.execute(
                    f"PRAGMA table_info({table_name})"
                ).fetchall()
            ]
            if "server_id" in cols:
                sid_col = "server_id"
            elif "id" in cols and table_name == "servers":
                sid_col = "id"
            else:
                continue
            cur = conn.execute(
                f"SELECT {', '.join(cols)} FROM {table_name} "
                f"WHERE {sid_col} = ?",
                (server_id,),
            )
            rows = [{col: r[col] for col in cols} for r in cur.fetchall()]
        finally:
            conn.close()
        archive_tables[table_id] = {
            "format": "plexbackup.dbexport.v1",
            "table_id": table_id,
            "db_file": db_file,
            "table_name": table_name,
            "exported_at": time.time(),
            "schema": cols,
            "rows": rows,
        }
    return {
        "format": "plexbackup.archive.v1",
        "scope": "per_server",
        "server_id": server_id,
        "exported_at": time.time(),
        "tables": archive_tables,
    }


def export_table(table_id: str) -> Dict[str, Any]:
    """Dump one table as a JSON-serialisable dict. Returns the
    single-table export format documented at module top."""
    if table_id not in _TABLE_CATALOG:
        raise KeyError(f"unknown table_id: {table_id!r}")
    db_file, table_name = _TABLE_CATALOG[table_id]
    path = get_data_dir() / db_file
    if not path.exists():
        raise FileNotFoundError(f"database file missing: {path}")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10.0)
    conn.row_factory = sqlite3.Row
    try:
        schema_rows = conn.execute(
            f"PRAGMA table_info({table_name})"
        ).fetchall()
        columns = [r["name"] for r in schema_rows]
        if not columns:
            raise RuntimeError(
                f"table {table_name!r} has no columns "
                f"(or does not exist in {db_file})"
            )
        cur = conn.execute(f"SELECT {', '.join(columns)} FROM {table_name}")
        rows = [
            {col: row[col] for col in columns}
            for row in cur.fetchall()
        ]
    finally:
        conn.close()
    return {
        "format": "plexbackup.dbexport.v1",
        "table_id": table_id,
        "db_file": db_file,
        "table_name": table_name,
        "exported_at": time.time(),
        "schema": columns,
        "rows": rows,
    }


def _validate_table_payload(
    payload: Dict[str, Any],
) -> Tuple[str, str, str, List[str], List[Dict[str, Any]]]:
    """Validate one single-table dump's shape and resolve it against
    the catalog. Returns ``(table_id, db_file, table_name, schema,
    rows)``; raises ``ValueError`` on any shape problem.

    Pure pre-flight validation - it never opens or touches a database.
    Shared by both restore paths so they reject a malformed payload
    identically, before a connection is ever opened.
    """
    fmt = payload.get("format")
    if fmt != "plexbackup.dbexport.v1":
        raise ValueError(
            f"unsupported export format: {fmt!r} "
            f"(expected plexbackup.dbexport.v1)"
        )
    table_id = payload.get("table_id")
    if table_id not in _TABLE_CATALOG:
        raise ValueError(f"unknown table_id in payload: {table_id!r}")
    db_file, table_name = _TABLE_CATALOG[table_id]
    schema = list(payload.get("schema") or [])
    rows = list(payload.get("rows") or [])
    if not schema:
        raise ValueError("payload schema must be a non-empty list of column names")
    return table_id, db_file, table_name, schema, rows


def _restore_table_body(
    conn: sqlite3.Connection,
    table_id: str,
    table_name: str,
    schema: List[str],
    rows: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Replace one table's contents on an ALREADY-OPEN connection,
    inside a transaction the caller owns. Runs the live-schema drift
    check, the DELETE, and the INSERT - but never opens the
    connection and never issues BEGIN / COMMIT / ROLLBACK itself.

    Leaving transaction control to the caller is what lets several
    tables that share one db_file restore atomically as a unit (see
    :func:`restore_archive`). Returns ``{table_id, deleted, inserted}``.
    """
    # Live-schema check: import-replace only works against the
    # exact column set captured at export time. A new column
    # added between export and import is a schema drift the
    # end user should resolve explicitly (re-export + re-import)
    # rather than silently dropping a column on restore.
    live_cols = [
        r[1] for r in conn.execute(
            f"PRAGMA table_info({table_name})"
        ).fetchall()
    ]
    if live_cols != schema:
        raise ValueError(
            "schema drift between export and live table: "
            f"export had {schema!r}, live table has {live_cols!r}"
        )

    placeholders = ", ".join("?" for _ in schema)
    col_list = ", ".join(schema)
    deleted_cur = conn.execute(f"DELETE FROM {table_name}")
    deleted = int(deleted_cur.rowcount or 0)
    inserted = 0
    if rows:
        values_list = [
            tuple(row.get(col) for col in schema)
            for row in rows
        ]
        conn.executemany(
            f"INSERT INTO {table_name} ({col_list}) "
            f"VALUES ({placeholders})",
            values_list,
        )
        inserted = len(values_list)
    return {
        "table_id": table_id,
        "deleted": deleted,
        "inserted": inserted,
    }


def restore_table(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Replace one table's contents with the supplied JSON dump.
    Operates in a single transaction: DELETE all existing rows then
    INSERT every row from ``rows``. Returns
    ``{table_id, deleted, inserted}``.

    Validates the payload shape before touching the DB:
      * ``format`` must match ``plexbackup.dbexport.v1``
      * ``table_id`` must be in the catalog
      * ``schema`` must match the live table's columns exactly (no
        renames, no adds, no removes)
    """
    table_id, db_file, table_name, schema, rows = _validate_table_payload(payload)

    path = get_data_dir() / db_file
    if not path.exists():
        raise FileNotFoundError(f"database file missing: {path}")

    # Open via the shared helper so the restore connection gets the
    # project's standard journal-mode + synchronous + row_factory
    # config instead of a bare connection. foreign_keys stays OFF: a
    # bulk DELETE-then-INSERT table replace must not trip FK
    # enforcement mid-restore.
    conn = open_db(path, label=db_file, foreign_keys=False)
    try:
        conn.execute("BEGIN")
        try:
            result = _restore_table_body(conn, table_id, table_name, schema, rows)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()
    return result


def export_all() -> Dict[str, Any]:
    """Dump every catalog table into a single archive document.
    Skipped silently for missing DBs (a fresh install that has yet
    to write run_timings.db, say); the archive carries only what
    actually exists on disk."""
    archive_tables: Dict[str, Any] = {}
    for table_id in _TABLE_CATALOG:
        try:
            archive_tables[table_id] = export_table(table_id)
        except FileNotFoundError:
            continue
        except Exception:
            log.exception("export_table failed for %r; skipping", table_id)
            continue
    return {
        "format": "plexbackup.archive.v1",
        "exported_at": time.time(),
        "tables": archive_tables,
    }


def restore_archive(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Restore every table in a previously-exported archive.

    Tables are grouped by their backing ``db_file`` and each file is
    restored in ONE transaction: if any table in a file fails (schema
    drift, a bad row) every table in that same file rolls back as a
    unit. This keeps intra-file foreign keys consistent - e.g.
    ``server_items`` is never left referencing ``items`` rows a
    half-applied restore deleted.

    A single SQLite transaction cannot span multiple database files,
    so atomicity is per-db_file, not whole-archive: a failure
    restoring media.db does not roll back an already-committed
    run_timings.db. The other files still restore. Returns per-table
    results plus an aggregate summary.
    """
    fmt = payload.get("format")
    if fmt != "plexbackup.archive.v1":
        raise ValueError(
            f"unsupported archive format: {fmt!r} "
            f"(expected plexbackup.archive.v1)"
        )
    tables = dict(payload.get("tables") or {})
    results: Dict[str, Any] = {}
    total_deleted = 0
    total_inserted = 0
    errors: List[str] = []

    # Pass 1: validate every table payload and bucket it by db_file.
    # A payload that fails validation is recorded as a per-table
    # error here and never reaches a transaction.
    by_db_file: Dict[str, List[Tuple[str, str, List[str], List[Dict[str, Any]]]]] = {}
    for table_id, sub in tables.items():
        try:
            v_table_id, db_file, table_name, schema, rows = _validate_table_payload(sub)
        except Exception as exc:
            results[table_id] = {"error": str(exc)}
            errors.append(f"{table_id}: {exc}")
            continue
        by_db_file.setdefault(db_file, []).append(
            (v_table_id, table_name, schema, rows)
        )

    # Pass 2: restore each db_file's tables in a single transaction.
    for db_file, entries in by_db_file.items():
        path = get_data_dir() / db_file
        if not path.exists():
            msg = f"database file missing: {path}"
            for (t_id, _table_name, _schema, _rows) in entries:
                results[t_id] = {"error": msg}
                errors.append(f"{t_id}: {msg}")
            continue

        file_results: List[Dict[str, Any]] = []
        file_error: Optional[str] = None
        conn = open_db(path, label=db_file, foreign_keys=False)
        try:
            conn.execute("BEGIN")
            try:
                for (t_id, table_name, schema, rows) in entries:
                    file_results.append(
                        _restore_table_body(conn, t_id, table_name, schema, rows)
                    )
                conn.execute("COMMIT")
            except Exception as exc:
                try:
                    conn.execute("ROLLBACK")
                except Exception:
                    pass
                file_error = str(exc)
        finally:
            conn.close()

        if file_error is not None:
            # Whole-file rollback: no table in this db_file was
            # restored. Report the failure against every table that
            # shared the file's transaction.
            for (t_id, _table_name, _schema, _rows) in entries:
                results[t_id] = {
                    "error": f"db_file {db_file} rolled back: {file_error}"
                }
                errors.append(f"{t_id}: {file_error}")
        else:
            for res in file_results:
                results[res["table_id"]] = res
                total_deleted += int(res.get("deleted") or 0)
                total_inserted += int(res.get("inserted") or 0)

    return {
        "per_table": results,
        "total_deleted": total_deleted,
        "total_inserted": total_inserted,
        "errors": errors,
    }
