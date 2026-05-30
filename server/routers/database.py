"""Database-health, export/import, and browser routes.

Combines ``/api/db/*``, ``/api/database/*``, and ``/api/db-browser/*``
handlers. Behaviour preserved verbatim.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException

from server import auth_router as _auth_router_module
from server.models import DbImportIn


log = logging.getLogger("plexmigrate.server")

router = APIRouter(tags=["database"])


# ── Database health (v0.12.0) ────────────────────────────────────


@router.get("/api/db/stats")
def db_stats(
    _user: Dict[str, Any] = Depends(_auth_router_module.require_role("viewer")),
) -> Dict[str, Any]:
    """
    Return media.db health: schema version, per-table row counts,
    most-recent ``updated_at`` per content table, on-disk size,
    and the file path inside the container.

    Surfaces in the future Database tab so end users can answer
    "is the DB warm" without opening sqlite3 on the host. Auth-
    protected when auth is enabled (the middleware sits in front
    of every route except the public-prefix list).
    """
    try:
        from server import media_db
        return media_db.get_stats()
    except RuntimeError as e:
        # init_media_db() was never called or failed at startup.
        raise HTTPException(status_code=503, detail=str(e))


# ── Operational-table export / import ────────────────────────────


@router.get("/api/database/tables")
def list_database_tables(
    _user: Dict[str, Any] = Depends(_auth_router_module.require_role("viewer")),
) -> Dict[str, Any]:
    """List every operational table available for export, with
    live row counts. Drives the Settings Run History database
    export/import panel."""
    from server import db_export
    return {"tables": db_export.list_tables()}


@router.get("/api/database/export/{table_id}")
def export_database_table(
    table_id: str,
    _user: Dict[str, Any] = Depends(_auth_router_module.require_role("admin")),
) -> Dict[str, Any]:
    """Dump one operational table as JSON. The frontend triggers
    a file download by reading the response body and creating a
    blob; this endpoint returns the JSON payload directly so the
    client can save it under any filename it likes."""
    from server import db_export
    try:
        return db_export.export_table(table_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"unknown table_id: {table_id}")
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.get("/api/database/export-all")
def export_database_archive(
    _user: Dict[str, Any] = Depends(_auth_router_module.require_role("admin")),
) -> Dict[str, Any]:
    """Dump every operational table into a single archive
    document. End user stores this as a recovery checkpoint."""
    from server import db_export
    return db_export.export_all()


@router.post("/api/database/import/{table_id}")
def import_database_table(
    table_id: str,
    body: DbImportIn,
    _user: Dict[str, Any] = Depends(_auth_router_module.require_role("admin")),
) -> Dict[str, Any]:
    """Restore one operational table from a previously-exported
    JSON document. Replace-only: the table is wiped before the
    supplied rows are inserted. Behind a typed REPLACE
    confirmation; misspellings reject without touching data."""
    if (body.confirm or "").strip() != "REPLACE":
        raise HTTPException(
            status_code=400,
            detail='confirm must equal the literal string "REPLACE"',
        )
    # Force the URL's table_id to win over any value the end user
    # might have left in the JSON payload from a different export.
    # Better to reject mismatched dumps than to silently restore
    # the wrong table.
    payload = dict(body.payload or {})
    if payload.get("table_id") and payload["table_id"] != table_id:
        raise HTTPException(
            status_code=400,
            detail=(
                f"payload table_id ({payload.get('table_id')!r}) "
                f"does not match URL table_id ({table_id!r})"
            ),
        )
    payload["table_id"] = table_id
    from server import db_export
    try:
        return db_export.restore_table(payload)
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.get("/api/database/per-server/{server_id}")
def list_database_per_server(
    server_id: str,
    _user: Dict[str, Any] = Depends(_auth_router_module.require_role("viewer")),
) -> Dict[str, Any]:
    """Return per-table row counts scoped to one server. Drives
    the Settings Run History per-server identity-DB pivot where
    the end user inspects one Plex / Jellyfin / Emby server's
    rows in isolation (registry entry, known users, library
    sections, watch history, ratings, identity links, learned
    ETA buckets, snapshot registry, etc.)."""
    from server import db_export
    return {
        "server_id": server_id,
        "tables": db_export.list_per_server_view(server_id),
    }


@router.get("/api/database/export-per-server/{server_id}")
def export_database_per_server(
    server_id: str,
    _user: Dict[str, Any] = Depends(_auth_router_module.require_role("admin")),
) -> Dict[str, Any]:
    """Dump every per-server table filtered to one server into
    an archive document. Useful for one-server backups or for
    migrating identity state between installs."""
    from server import db_export
    try:
        return db_export.export_per_server(server_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/api/database/import-archive")
def import_database_archive(
    body: DbImportIn,
    _user: Dict[str, Any] = Depends(_auth_router_module.require_role("admin")),
) -> Dict[str, Any]:
    """Restore every table from a previously-exported archive.
    Each table is replace-only and processed independently;
    a failure on one does not abort the others. Returns
    per-table results + aggregate counts + any errors."""
    if (body.confirm or "").strip() != "REPLACE":
        raise HTTPException(
            status_code=400,
            detail='confirm must equal the literal string "REPLACE"',
        )
    from server import db_export
    try:
        return db_export.restore_archive(dict(body.payload or {}))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


# ── Databases viewer ─────────────────────────────────────────────
#
# Root-admin only at the route layer. The engine in
# server.db_browser opens every connection in true read-only mode
# (file:<path>?mode=ro) + sets PRAGMA query_only=1 as belt-and-
# braces, so even a logic bug in a future call site can't fire an
# UPDATE. Encrypted columns, bcrypt hashes, and refresh tokens are
# substituted with redacted placeholders by db_browser._format_cell
# before any row reaches the wire.
#
# Every successful fetch logs to db_access.log with caller +
# db_type + instance_id + table + row_count for forensic recovery.


@router.get("/api/db-browser/databases")
def db_browser_list_databases(
    _admin: Dict[str, Any] = Depends(
        _auth_router_module.require_role("root_admin"),
    ),
) -> Dict[str, Any]:
    """Return the catalogue of database types the viewer knows
    about. Each entry carries display_name, description,
    cardinality (single|many), and sensitivity (high|medium|low).
    """
    from server import db_browser
    return {"databases": db_browser.list_database_types()}


@router.get("/api/db-browser/{db_type}/instances")
def db_browser_list_instances(
    db_type: str,
    _admin: Dict[str, Any] = Depends(
        _auth_router_module.require_role("root_admin"),
    ),
) -> Dict[str, Any]:
    """List instances for one database type. Single-cardinality
    types return one element when the file exists, zero when it
    does not. Many-cardinality returns every registered snapshot
    file."""
    from server import db_browser
    try:
        return {"instances": db_browser.list_instances(db_type)}
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.get("/api/db-browser/{db_type}/{instance_id}/metadata")
def db_browser_metadata(
    db_type: str,
    instance_id: str,
    _admin: Dict[str, Any] = Depends(
        _auth_router_module.require_role("root_admin"),
    ),
) -> Dict[str, Any]:
    """File path, size, schema_version (when present), modified
    timestamp, WAL-sidecar presence flag."""
    from server import db_browser
    try:
        return db_browser.get_database_metadata(db_type, instance_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.get("/api/db-browser/{db_type}/{instance_id}/schema")
def db_browser_schema(
    db_type: str,
    instance_id: str,
    _admin: Dict[str, Any] = Depends(
        _auth_router_module.require_role("root_admin"),
    ),
) -> Dict[str, Any]:
    """Full table catalogue: per-table row count, columns, indexes,
    and the literal CREATE TABLE DDL from sqlite_master."""
    from server import db_browser
    try:
        schema = db_browser.get_schema(db_type, instance_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    # Audit logging - best-effort, never blocks the read.
    try:
        from services.run_logs import db_access as db_access_log
        db_access_log.log_event(
            "DB_BROWSER_SCHEMA caller=%r db_type=%r instance_id=%r "
            "tables=%d",
            (_admin.get("username") or "?"),
            db_type, instance_id, len(schema.get("tables") or []),
        )
    except Exception:
        pass
    return schema


@router.get("/api/db-browser/{db_type}/{instance_id}/tables/{table}/rows")
def db_browser_rows(
    db_type: str,
    instance_id: str,
    table: str,
    limit: int = 50,
    offset: int = 0,
    filter_column: Optional[str] = None,
    filter_value: Optional[str] = None,
    _admin: Dict[str, Any] = Depends(
        _auth_router_module.require_role("root_admin"),
    ),
) -> Dict[str, Any]:
    """Paginated table read with security-aware cell formatting.
    Limits are clamped server-side; filter_column is validated
    against the live PRAGMA table_info; filter_value is bound as
    a parameter."""
    from server import db_browser
    try:
        page = db_browser.get_rows(
            db_type, instance_id, table,
            limit=limit, offset=offset,
            filter_column=filter_column,
            filter_value=filter_value,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    try:
        from services.run_logs import db_access as db_access_log
        db_access_log.log_event(
            "DB_BROWSER_ROWS caller=%r db_type=%r instance_id=%r "
            "table=%r limit=%d offset=%d returned=%d total=%d",
            (_admin.get("username") or "?"),
            db_type, instance_id, table,
            page["limit"], page["offset"],
            len(page["rows"]), page["total_rows"],
        )
    except Exception:
        pass
    return page
