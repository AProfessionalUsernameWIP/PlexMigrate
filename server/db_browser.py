"""
Read-only database browser surface for the Settings > Databases sub-tab.

Catalogues every SQLite database the application creates, exposes a
schema + paginated row reader for each, and applies security-aware
cell formatting so sensitive columns (Fernet ciphertext, bcrypt
hashes, refresh-token ids) never reach the end user UI in raw form.

Root-admin only at the route layer (see ``server/app.py``); this
module is the engine the routes call into. Every connection is
opened in true read-only mode (``file:<path>?mode=ro``) AND has
``PRAGMA query_only=1`` set as belt-and-braces, so even a logic bug
in a future call site cannot fire an UPDATE.

Database catalogue (six categories, one of which has many instances):

  * ``auth``           - auth.db (single instance)
  * ``media``          - media.db (single instance)
  * ``snapshots``      - snapshots.db (the registry, single instance)
  * ``run_timings``    - run_timings.db (single instance)
  * ``playlist_cache`` - playlist_cache.db (single instance)
  * ``snapshot_file``  - per-capture snapshot .db files (N instances)

The catalogue is the only authoritative source the REST layer
consults; routes never accept arbitrary file paths. Instance ids
for ``snapshot_file`` are the snapshot registry's row ids (small
integers), validated against the live registry list on every call.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional


log = logging.getLogger("plexmigrate.server.db_browser")


# ── Tunables ────────────────────────────────────────────────────────────────

# Hard ceiling on rows-per-page. The REST layer accepts a caller-
# supplied limit; this caps the request server-side so an end user
# with a million-row ``items`` table can never dump the whole thing in
# one request. 500 fits comfortably in a single JSON payload + keeps
# the UI responsive.
_MAX_ROWS_PER_REQUEST = 500

# Schema-pane row count ceiling. The bounded-count query stops
# scanning at this number; tables larger than this return row_count =
# ROW_COUNT_THRESHOLD with the convention that the frontend renders
# it as ">= 10000". Avoids 100ms+ COUNT(*) scans on million-row
# tables. Exact counts can still be obtained by paginating the data
# browser, which uses a full COUNT but only after the end user has
# picked a specific table to look at.
ROW_COUNT_THRESHOLD = 10000

# Cell formatting threshold: TEXT columns longer than this get
# truncated to ``[N] chars`` + an expand affordance. BLOBs of this
# size or smaller render as a hex string; bigger BLOBs render as
# ``[binary, N bytes]``.
_TEXT_TRUNCATE_AT = 200
_BLOB_HEX_PREVIEW_MAX = 256

# Column-name suffix / literal heuristics for the security-aware
# cell formatter. Matched case-insensitively against each column
# name; first match wins. The substitutions are applied AT THE
# CELL formatter level (not by excluding columns from SELECT) so
# end users still see the column EXISTS and its size + structure.
_ENC_SUFFIXES = ("_enc",)
_BCRYPT_LITERAL = "password_hash"
_REFRESH_TOKEN_TABLE = "refresh_tokens"


# ── Catalogue ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DatabaseType:
    """One entry in the database catalogue.

    * ``key``: stable string used by the REST layer (URL slug).
    * ``display_name``: human-readable label for the UI tab.
    * ``description``: short summary for the empty-state and tooltip.
    * ``cardinality``: ``'single'`` (one fixed file) or ``'many'``
      (multiple instances, e.g. per-capture snapshot files).
    * ``sensitivity``: ``'high'`` (auth.db, snapshot files with
      encrypted tokens), ``'medium'`` (media.db, playlist_cache.db),
      ``'low'`` (run_timings.db).
    * ``single_path``: resolver for cardinality='single'. Returns
      ``None`` if the file doesn't exist yet (e.g. first boot, the
      backfill hasn't created it). Ignored for cardinality='many'.
    * ``many_lister``: instance lister for cardinality='many'.
      Returns a list of dicts with keys ``instance_id``, ``label``,
      ``file_path``, ``size_bytes``, plus type-specific extras.
    """
    key: str
    display_name: str
    description: str
    cardinality: str
    sensitivity: str
    single_path: Optional[Callable[[], Optional[Path]]] = None
    many_lister: Optional[Callable[[], List[Dict[str, Any]]]] = None


def _single_path_or_none(resolver: Callable[[], Path]) -> Optional[Path]:
    """Adapter for the per-module ``_db_path`` helpers that always
    return a Path; we return ``None`` when the file doesn't actually
    exist on disk so the UI can render an empty state instead of an
    error."""
    try:
        path = resolver()
        if path.exists():
            return path
        return None
    except Exception:
        return None


def _auth_path() -> Optional[Path]:
    from server import auth_db
    return _single_path_or_none(auth_db._db_path)


def _media_path() -> Optional[Path]:
    from server import media_db
    return _single_path_or_none(media_db._db_path)


def _snapshots_registry_path() -> Optional[Path]:
    from server import snapshot_registry
    return _single_path_or_none(snapshot_registry._db_path)


def _run_timings_path() -> Optional[Path]:
    from server import run_timings_db
    return _single_path_or_none(run_timings_db._db_path)


def _playlist_cache_path() -> Optional[Path]:
    from server import playlist_cache_db
    return _single_path_or_none(playlist_cache_db._db_path)


def _collection_cache_path() -> Optional[Path]:
    from server import collection_cache_db
    return _single_path_or_none(collection_cache_db._db_path)


def _server_mirror_path() -> Optional[Path]:
    from server import server_mirror_db
    return _single_path_or_none(server_mirror_db._db_path)


def _snapshot_files() -> List[Dict[str, Any]]:
    """Walk the snapshot registry; return one dict per captured .db
    file with a stable instance_id (the registry row id), a label
    (server name + snapshot name + capture date), and the file size.

    Order matches the registry default (newest captures first) so the
    UI picker doesn't need to re-sort."""
    try:
        from server import snapshot_registry
        rows = snapshot_registry.list_snapshots() or []
    except Exception:
        log.exception("snapshot_files: list_snapshots failed; returning empty.")
        return []
    out: List[Dict[str, Any]] = []
    for r in rows:
        file_path = r.get("file_path")
        if not file_path:
            continue
        path = Path(file_path)
        try:
            size = path.stat().st_size if path.exists() else 0
        except OSError:
            size = 0
        out.append({
            "instance_id":   str(r.get("id")),
            "label":         _snapshot_label(r),
            "file_path":     str(path),
            "size_bytes":    size,
            "server_id":     r.get("server_id"),
            "server_name":   r.get("server_name"),
            "service_type":  r.get("service_type"),
            "snapshot_name": r.get("snapshot_name"),
            "captured_at":   r.get("captured_at"),
            "exists":        path.exists(),
        })
    return out


def _snapshot_label(row: Dict[str, Any]) -> str:
    """Format a human-readable label for a snapshot file: server
    name + capture date in ISO form."""
    server_name = row.get("server_name") or row.get("server_id") or "?"
    snapshot_name = row.get("snapshot_name") or ""
    captured = row.get("captured_at")
    if isinstance(captured, (int, float)) and captured > 0:
        try:
            iso = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(captured))
        except (OSError, OverflowError, ValueError):
            iso = "?"
    else:
        iso = "?"
    return f"{server_name} - {snapshot_name} ({iso})"


def _dev_console_files() -> List[Dict[str, Any]]:
    """One dict per Server Commands per-server mirror .db file. The
    root-admin developer console keeps a disposable SQLite mirror of
    each server's library / playlist / per-user state under
    server_data/server_commands/. instance_id is the server id."""
    try:
        from server import dev_console_db
        server_ids = dev_console_db.mirror_server_ids()
    except Exception:
        log.exception(
            "dev_console_files: mirror_server_ids failed; returning empty.",
        )
        return []
    names: Dict[str, str] = {}
    try:
        from server import server_registry
        for row in server_registry.list_servers(include_tokens=False):
            sid = str(row.get("id") or "")
            if sid:
                names[sid] = row.get("name") or ""
    except Exception:
        log.debug("dev_console_files: server-name lookup failed")
    out: List[Dict[str, Any]] = []
    for sid in server_ids:
        path = dev_console_db.db_path(sid)
        try:
            size = path.stat().st_size if path.exists() else 0
        except OSError:
            size = 0
        name = names.get(str(sid)) or str(sid)
        out.append({
            "instance_id": str(sid),
            "label":       name,
            "file_path":   str(path),
            "size_bytes":  size,
            "server_id":   str(sid),
            "server_name": name,
            "exists":      path.exists(),
        })
    return out


_CATALOGUE: List[DatabaseType] = [
    DatabaseType(
        key="auth",
        display_name="auth.db",
        description=(
            "App user accounts (bcrypt-hashed passwords), refresh tokens, "
            "and per-user permission grants and revokes. Most sensitive "
            "file in server_data/."
        ),
        cardinality="single",
        sensitivity="high",
        single_path=_auth_path,
    ),
    DatabaseType(
        key="media",
        display_name="media.db",
        description=(
            "Items, watch events, ratings, playlists, collections, "
            "managed_users, server_users, user_identity_map, and "
            "library_sections. The live working store every snapshot "
            "is built from."
        ),
        cardinality="single",
        sensitivity="medium",
        single_path=_media_path,
    ),
    DatabaseType(
        key="snapshots",
        display_name="snapshots.db",
        description=(
            "Registry indexing every captured snapshot .db file on "
            "disk, with per-server retention metadata."
        ),
        cardinality="single",
        sensitivity="low",
        single_path=_snapshots_registry_path,
    ),
    DatabaseType(
        key="run_timings",
        display_name="run_timings.db",
        description=(
            "Run history rows, per-operation timings, adaptive-ETA "
            "training data. No sensitive material."
        ),
        cardinality="single",
        sensitivity="low",
        single_path=_run_timings_path,
    ),
    DatabaseType(
        key="playlist_cache",
        display_name="playlist_cache.db",
        description=(
            "TTL-bounded per-(server, user) playlist cache used by "
            "the Playlist Management surface to avoid hammering "
            "every backend on every UI read."
        ),
        cardinality="single",
        sensitivity="medium",
        single_path=_playlist_cache_path,
    ),
    DatabaseType(
        key="collection_cache",
        display_name="collection_cache.db",
        description=(
            "Per-collection cached children list keyed on Plex's "
            "collection.updatedAt. Each row also carries owner_user_id "
            "so library-wide and per-managed-user collections are "
            "separately attributable. The snapshotter's Collections "
            "phase reads this cache to skip the expensive "
            "/library/metadata/X/children fetch on unchanged "
            "collections; the Servers > Overview bulk-cache button "
            "is the operator-facing way to warm or clear it."
        ),
        cardinality="single",
        sensitivity="low",
        single_path=_collection_cache_path,
    ),
    DatabaseType(
        key="server_mirror",
        display_name="server_mirror.db",
        description=(
            "Per-server metadata mirror (item ids, GUIDs, full-paths, "
            "path-tails) used by the engine's resolver as Tier-0 of "
            "the resolution chain so cross-server transfers don't "
            "re-fetch metadata that has not changed since the last "
            "sync. WAL mode, integrity-check on init, drift events "
            "recorded for the Drift History sub-tab."
        ),
        cardinality="single",
        sensitivity="low",
        single_path=_server_mirror_path,
    ),
    DatabaseType(
        key="dev_console_mirror",
        display_name="Server Commands mirrors",
        description=(
            "Per-server disposable SQLite mirror behind the root-admin "
            "Server Commands console: library items, per-user watch / "
            "rating / favorite / resume state, playlists, collections, "
            "and staged (un-sent) commands. One file per server under "
            "server_data/server_commands/; the console's background "
            "sync worker rebuilds it and a schema bump drops it."
        ),
        cardinality="many",
        sensitivity="medium",
        many_lister=_dev_console_files,
    ),
    DatabaseType(
        key="snapshot_file",
        display_name="Snapshot files",
        description=(
            "Per-capture immutable .db files. Each carries the slice "
            "of media.db that was the live state when the snapshot "
            "ran. Schema v15+. Counts grow with capture cadence; the "
            "picker is paginated server-side."
        ),
        cardinality="many",
        sensitivity="high",
        many_lister=_snapshot_files,
    ),
]


def list_database_types() -> List[Dict[str, Any]]:
    """Public catalogue read. Returns plain dicts (Pydantic-friendly)."""
    return [
        {
            "key":           t.key,
            "display_name":  t.display_name,
            "description":   t.description,
            "cardinality":   t.cardinality,
            "sensitivity":   t.sensitivity,
        }
        for t in _CATALOGUE
    ]


def _catalogue_entry(db_type: str) -> DatabaseType:
    """Look up the catalogue entry, raising ``ValueError`` on unknown
    keys. The REST layer hands the raw URL slug here; the catalogue
    is the allowlist."""
    for t in _CATALOGUE:
        if t.key == db_type:
            return t
    raise ValueError(f"Unknown database type: {db_type!r}")


def list_instances(db_type: str) -> List[Dict[str, Any]]:
    """List instances for the given database type. Always returns a
    list; single-instance types yield a one-element list when the
    file exists, an empty list when it doesn't."""
    entry = _catalogue_entry(db_type)
    if entry.cardinality == "single":
        path = entry.single_path() if entry.single_path else None
        if path is None:
            return []
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        return [{
            "instance_id": "_",
            "label":       entry.display_name,
            "file_path":   str(path),
            "size_bytes":  size,
            "exists":      True,
        }]
    # cardinality == "many"
    if entry.many_lister is None:
        return []
    return entry.many_lister()


def _resolve_instance_path(db_type: str, instance_id: str) -> Path:
    """Look up the on-disk path for ``(db_type, instance_id)``. Raises
    ``FileNotFoundError`` when the instance is unknown OR when the
    file doesn't exist on disk. The instance_id is validated against
    the live instance list on every call so a hand-crafted REST
    request can't address an arbitrary path."""
    for inst in list_instances(db_type):
        if inst["instance_id"] == instance_id:
            path = Path(inst["file_path"])
            if not path.exists():
                raise FileNotFoundError(
                    f"{db_type}/{instance_id}: file {path} does not exist"
                )
            return path
    raise FileNotFoundError(
        f"{db_type}/{instance_id}: no such instance"
    )


# ── Read-only connection ────────────────────────────────────────────────────


def _open_readonly(path: Path) -> sqlite3.Connection:
    """Open ``path`` in true read-only mode + set query_only as a
    second layer of protection. Caller is responsible for closing
    (use a try/finally)."""
    # mode=ro at the URI layer prevents the SQLite engine from
    # accepting any write. query_only=1 belt-and-braces this at the
    # statement parser. Both are documented as enforced at the
    # engine level (not just hints).
    uri = f"file:{path.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=1")
    return conn


# ── Metadata ────────────────────────────────────────────────────────────────


def get_database_metadata(db_type: str, instance_id: str) -> Dict[str, Any]:
    """Return file metadata + schema_version (when present) for one
    database instance. Cheap; safe to call on every UI mount."""
    path = _resolve_instance_path(db_type, instance_id)
    try:
        stat = path.stat()
    except OSError as exc:
        raise FileNotFoundError(str(exc))
    schema_version: Optional[int] = None
    has_wal = (
        path.with_suffix(path.suffix + "-wal").exists()
        or path.with_name(path.name + "-wal").exists()
    )
    conn = _open_readonly(path)
    try:
        try:
            row = conn.execute(
                "SELECT MAX(version) AS v FROM schema_version"
            ).fetchone()
            if row is not None and row["v"] is not None:
                schema_version = int(row["v"])
        except sqlite3.OperationalError:
            # No schema_version table (e.g. auth.db doesn't have one).
            pass
    finally:
        conn.close()
    return {
        "db_type":         db_type,
        "instance_id":     instance_id,
        "file_path":       str(path),
        "size_bytes":      stat.st_size,
        "modified_at":     stat.st_mtime,
        "schema_version":  schema_version,
        "wal_present":     has_wal,
    }


# ── Schema introspection ────────────────────────────────────────────────────


def get_schema(db_type: str, instance_id: str) -> Dict[str, Any]:
    """Return the table catalogue for one instance.

    Each table entry carries:

      * ``name``         - table name
      * ``row_count``    - exact count (cheap on typical sizes; the
                           REST layer can choose to skip this for
                           very large tables in a follow-up)
      * ``columns``      - list of {name, type, notnull, default,
                           is_primary_key} from PRAGMA table_info
      * ``indexes``      - list of {name, unique, columns} from
                           PRAGMA index_list + PRAGMA index_info
      * ``ddl``          - the literal CREATE TABLE SQL from
                           sqlite_master.sql (end users trying to
                           understand the schema benefit from seeing
                           CHECK constraints + defaults + FKs that
                           PRAGMA doesn't surface cleanly)
    """
    path = _resolve_instance_path(db_type, instance_id)
    conn = _open_readonly(path)
    try:
        # Pull all user tables. Exclude sqlite_* internal tables.
        table_rows = conn.execute(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
            "ORDER BY name"
        ).fetchall()
        tables: List[Dict[str, Any]] = []
        for tr in table_rows:
            table_name = tr["name"]
            # Bounded row count. A naive ``SELECT COUNT(*)`` is fast on
            # typical sizes but can take 100ms+ on a million-row
            # ``items`` table. The bounded form ``COUNT(*) FROM (SELECT
            # 1 FROM <tab> LIMIT N+1)`` stops scanning at N+1 rows: a
            # result equal to N+1 signals "at least N+1" and we expose
            # that as row_count = ROW_COUNT_THRESHOLD (the frontend
            # renders it as ">= N"). Below the threshold the count is
            # exact.
            try:
                bounded = conn.execute(
                    f"SELECT COUNT(*) AS n FROM "
                    f"(SELECT 1 FROM \"{table_name}\" LIMIT ?)",
                    (ROW_COUNT_THRESHOLD + 1,),
                ).fetchone()
                if bounded is None:
                    row_count = 0
                else:
                    n = int(bounded["n"])
                    row_count = (
                        ROW_COUNT_THRESHOLD if n > ROW_COUNT_THRESHOLD else n
                    )
            except sqlite3.OperationalError:
                row_count = -1  # signals "could not count"
            # Columns.
            col_rows = conn.execute(
                f"PRAGMA table_info(\"{table_name}\")"
            ).fetchall()
            columns = [
                {
                    "name":           c["name"],
                    "type":           c["type"] or "",
                    "notnull":        bool(c["notnull"]),
                    "default":        c["dflt_value"],
                    "is_primary_key": bool(c["pk"]),
                }
                for c in col_rows
            ]
            # Indexes.
            idx_rows = conn.execute(
                f"PRAGMA index_list(\"{table_name}\")"
            ).fetchall()
            indexes: List[Dict[str, Any]] = []
            for idx in idx_rows:
                idx_name = idx["name"]
                # Skip the auto-generated sqlite_autoindex_* indexes
                # backing UNIQUE / PRIMARY KEY constraints; they're
                # visible in the DDL already.
                if idx_name.startswith("sqlite_autoindex_"):
                    continue
                idx_cols = conn.execute(
                    f"PRAGMA index_info(\"{idx_name}\")"
                ).fetchall()
                indexes.append({
                    "name":    idx_name,
                    "unique":  bool(idx["unique"]),
                    "columns": [ic["name"] for ic in idx_cols],
                })
            tables.append({
                "name":      table_name,
                "row_count": row_count,
                "columns":   columns,
                "indexes":   indexes,
                "ddl":       (tr["sql"] or "").strip(),
            })
    finally:
        conn.close()
    return {
        "db_type":     db_type,
        "instance_id": instance_id,
        "tables":      tables,
    }


# ── Cell formatter ──────────────────────────────────────────────────────────


def _looks_like_timestamp_column(name: str) -> bool:
    """Heuristic: column names ending in ``_at`` or matching common
    timestamp literals get their REAL values rendered as ISO 8601
    next to the raw value. False positives are harmless (the raw
    value is still surfaced); the heuristic just controls whether
    the auxiliary display string is added."""
    n = (name or "").lower()
    return (
        n.endswith("_at")
        or n in ("last_seen", "created_at", "updated_at",
                 "captured_at", "expires_at", "issued_at",
                 "applied_at", "started_at", "finished_at",
                 "tombstoned_at", "refreshed_at",
                 "shared_state_refreshed_at")
    )


def _looks_like_encrypted_column(name: str) -> bool:
    """Column names ending in ``_enc`` carry Fernet ciphertext."""
    n = (name or "").lower()
    return any(n.endswith(suf) for suf in _ENC_SUFFIXES)


def _format_cell(
    value: Any,
    column_name: str,
    table_name: str,
) -> Dict[str, Any]:
    """Return a JSON-safe dict describing how to render the cell.

    Always returns ``{"display": <string|null>}`` at minimum. May add:

      * ``raw``             - the underlying value (when safe to surface)
      * ``raw_present``     - True when the underlying value is held
                              back for security; the UI can offer an
                              "expand" affordance if you eventually
                              want to reveal it (out of scope for v1)
      * ``truncated``       - True for TEXT longer than 200 chars

    Security-aware substitutions:

      * ``*_enc`` columns               -> ``[encrypted, N bytes]``
      * ``password_hash`` (any table)   -> ``[bcrypt hash, N chars]``
      * ``refresh_tokens.id``           -> ``[redacted refresh token id]``
      * BLOB > 256 bytes                -> ``[binary, N bytes]``
      * BLOB <= 256 bytes               -> ``0x<hex>``
      * TEXT > 200 chars                -> first 200 chars + truncated flag
    """
    # NULL passes through.
    if value is None:
        return {"display": None}

    col = (column_name or "").lower()
    tbl = (table_name or "").lower()

    # Encrypted columns - never reveal plaintext.
    if _looks_like_encrypted_column(col):
        try:
            size = len(value) if isinstance(value, (str, bytes)) else len(str(value))
        except Exception:
            size = 0
        return {
            "display":     f"[encrypted, {size} bytes]",
            "raw_present": True,
        }

    # bcrypt hashes in any table.
    if col == _BCRYPT_LITERAL:
        try:
            size = len(value) if isinstance(value, str) else len(str(value))
        except Exception:
            size = 0
        return {
            "display":     f"[bcrypt hash, {size} chars]",
            "raw_present": True,
        }

    # Refresh token ids - the credential itself.
    if tbl == _REFRESH_TOKEN_TABLE and col == "id":
        return {
            "display":     "[redacted refresh token id]",
            "raw_present": True,
        }

    # BLOB / bytes.
    if isinstance(value, (bytes, bytearray, memoryview)):
        size = len(value)
        if size <= _BLOB_HEX_PREVIEW_MAX:
            return {
                "display": "0x" + bytes(value).hex(),
                "raw":     None,  # already in display form
            }
        return {
            "display":     f"[binary, {size} bytes]",
            "raw_present": True,
        }

    # REAL epoch timestamps - decode to ISO when the column name
    # suggests timestamp semantics.
    if isinstance(value, (int, float)) and _looks_like_timestamp_column(col):
        try:
            iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(value)))
            return {"display": iso, "raw": float(value)}
        except (OSError, OverflowError, ValueError):
            # Fall through to default numeric rendering.
            pass

    # Long TEXT.
    if isinstance(value, str) and len(value) > _TEXT_TRUNCATE_AT:
        return {
            "display":   value[:_TEXT_TRUNCATE_AT] + "...",
            "raw":       value,
            "truncated": True,
        }

    # Everything else: pass through.
    if isinstance(value, (str, int, float, bool)):
        return {"display": value}

    # Defensive: unknown sqlite type that survived the row_factory
    # (shouldn't happen with sqlite3.Row).
    return {"display": repr(value)}


# ── Row reader ──────────────────────────────────────────────────────────────


def get_rows(
    db_type: str,
    instance_id: str,
    table: str,
    *,
    limit: int = 50,
    offset: int = 0,
    filter_column: Optional[str] = None,
    filter_value: Optional[str] = None,
) -> Dict[str, Any]:
    """Return a paginated, formatted slice of one table.

    Pagination + filtering:

    * ``limit`` is clamped to [1, _MAX_ROWS_PER_REQUEST] server-side.
    * ``offset`` is clamped to >= 0.
    * ``filter_column`` is validated against the live ``PRAGMA
      table_info`` of the table; unknown columns raise ``ValueError``
      (no SQL injection via a hand-crafted column name).
    * ``filter_value`` is bound as a parameter; no string concat
      ever happens.

    Returns ``{db_type, instance_id, table, columns, rows,
    total_rows, limit, offset, has_more}``. Each row is a list of
    formatted cells matching the ``columns`` order.
    """
    path = _resolve_instance_path(db_type, instance_id)
    table_clean = (table or "").strip()
    if not table_clean:
        raise ValueError("table is required")
    limit_n = max(1, min(_MAX_ROWS_PER_REQUEST, int(limit)))
    offset_n = max(0, int(offset))
    conn = _open_readonly(path)
    try:
        # Validate the table exists (no quoting trickery; the catalogue
        # is the allowlist for db type but tables come from this
        # specific db so we look them up explicitly).
        table_exists = conn.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type = 'table' AND name = ? "
            "AND name NOT LIKE 'sqlite_%'",
            (table_clean,),
        ).fetchone()
        if table_exists is None:
            raise ValueError(f"table {table!r} not found in {db_type}/{instance_id}")
        # Columns.
        col_rows = conn.execute(
            f"PRAGMA table_info(\"{table_clean}\")"
        ).fetchall()
        column_names: List[str] = [c["name"] for c in col_rows]
        # Filter validation.
        filter_clause = ""
        bind: List[Any] = []
        if filter_column:
            fc = filter_column.strip()
            if fc not in column_names:
                raise ValueError(
                    f"filter_column {filter_column!r} not in {table!r}'s columns"
                )
            # Equality filter only. NULL handling: an empty filter_value
            # matches NULLs explicitly (end user can find unset rows
            # without typing "NULL" literal).
            if filter_value is None or filter_value == "":
                filter_clause = f" WHERE \"{fc}\" IS NULL"
            else:
                filter_clause = f" WHERE \"{fc}\" = ?"
                bind.append(filter_value)
        # Total rows (post-filter for correctness; pre-filter would
        # mislead the end user about pagination).
        count_sql = f"SELECT COUNT(*) AS n FROM \"{table_clean}\"" + filter_clause
        count_row = conn.execute(count_sql, bind).fetchone()
        total_rows = int(count_row["n"]) if count_row else 0
        # Page.
        page_sql = (
            f"SELECT * FROM \"{table_clean}\"" + filter_clause
            + " LIMIT ? OFFSET ?"
        )
        page = conn.execute(page_sql, bind + [limit_n, offset_n]).fetchall()
        formatted: List[List[Dict[str, Any]]] = []
        for row in page:
            formatted.append([
                _format_cell(row[name], name, table_clean)
                for name in column_names
            ])
    finally:
        conn.close()
    return {
        "db_type":     db_type,
        "instance_id": instance_id,
        "table":       table_clean,
        "columns":     column_names,
        "rows":        formatted,
        "total_rows":  total_rows,
        "limit":       limit_n,
        "offset":      offset_n,
        "has_more":    (offset_n + limit_n) < total_rows,
    }


# ── Constants exposed for the REST layer ────────────────────────────────────

MAX_ROWS_PER_REQUEST = _MAX_ROWS_PER_REQUEST
