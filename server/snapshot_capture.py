"""
Snapshot capture. Materialises a per-server SQLite snapshot
file from the live ``media.db`` filtered to one ``server_id``.

Why filtered, not full export?
------------------------------
SQLite's online export API (:meth:`sqlite3.Connection.export`) copies
the entire database byte-for-byte. For a multi-server install that
would put every server's data into every snapshot - wasteful and
breaks the per-server-snapshot model. Instead this module:

1. Reads the live ``media.db`` schema (DDL strings from sqlite_master).
2. Applies that schema to a freshly-created snapshot file.
3. Uses ``ATTACH DATABASE`` to bridge the two files, then
   ``INSERT ... SELECT`` from source to destination with
   ``WHERE server_id = ?`` filters on the per-server tables.
4. For tables that aren't server-scoped (``items``) it joins through
   ``server_items`` so only items referenced by this server's rows
   come along for the ride.

Tables included
---------------
* ``servers``       - one row (the server being captured)
* ``items``         - referenced by this server's ``server_items``
* ``server_items``  - rows where ``server_id = ?``
* ``watch_events``  - rows where ``server_id = ?``
* ``ratings``       - rows where ``server_id = ?``
* ``playlists``     - rows where ``server_id = ?``
* ``collections``   - rows where ``server_id = ?``

Tables explicitly skipped
-------------------------
* ``managed_users``      - per-server but holds Fernet-encrypted
  credentials tied to this host's ``.keyfile``. A snapshot moved to
  another host would have unreadable cells; safer to keep credentials
  out of the snapshot entirely. The managed-user sync re-populates
  after a restore.
* ``global_tombstones``  - end user-only UI state; not data.
* ``schema_version``     - copied implicitly via the schema DDL
  application above; needed so the snapshot file is openable by code
  that runs migrations.

Concurrency
-----------
The source connection opens ``media.db`` in read-only WAL mode so a
job actively writing to media.db won't block the snapshot read.
SQLite gives the reader a consistent point-in-time view of the
source through WAL.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


log = logging.getLogger("plexmigrate.server.snapshot_capture")


# Per-server tables: rows are filtered by server_id.
_PER_SERVER_TABLES = (
    "server_items",
    "watch_events",
    "ratings",
    "playlists",
    "collections",
)

# Mapping from the end user-facing metric label ("watch_history" etc.)
# to the per-server SQL table the metric lives in. Used to gate the
# per-table INSERT loop on the requested-metrics list. Tables NOT in
# this map (``server_items``, ``items``, ``servers``) are always
# copied - they're not metric-scoped (server_items is the resolver
# Tier-0 cache, items is the GUID-keyed pool, servers is the
# one-row server descriptor).
_METRIC_TO_TABLE: Dict[str, str] = {
    "watch_history": "watch_events",
    "ratings":       "ratings",
    "playlists":     "playlists",
    "collections":   "collections",
}
_METRIC_TABLES = frozenset(_METRIC_TO_TABLE.values())

# Tables explicitly skipped from snapshots (see module docstring).
_SKIP_TABLES = frozenset({
    "managed_users",
    "global_tombstones",
    "schema_version",  # repopulated via DDL when we re-run migrations
    "sqlite_sequence",  # SQLite internal
})


def create_snapshot_db(
    *,
    server_id: str,
    snapshot_path: Path,
    # Every parameter below is required for the snapshot to be
    # self-describing AND scope-correct. Defaults exist so existing
    # callers (orphan reconcile, ad-hoc tooling) keep working, but the
    # job runner supplies all of them.
    snapshot_id: Optional[str] = None,
    server_name: str = "",
    libraries: Optional[List[str]] = None,
    metrics: Optional[List[str]] = None,
    captured_at: Optional[float] = None,
    created_by: Optional[str] = None,
    user_display_names: Optional[Dict[str, str]] = None,
) -> Dict[str, int]:
    """
    Build a new per-server snapshot ``.db`` file at ``snapshot_path``.

    Returns a counter dict with one entry per copied table plus a
    ``file_size`` field reporting the on-disk byte size of the
    finished snapshot. Raises if media.db has no row matching
    ``server_id`` in the ``servers`` table (a snapshot of a
    nonexistent server would be empty and confusing).

    Scoping
    -------
    ``metrics`` is the end user-requested subset of
    ``{"watch_history", "ratings", "playlists", "collections"}``.
    When supplied, only the corresponding per-server tables receive
    rows; the rest are created with their full schema but left empty.
    When ``None``, every metric table is copied (legacy behaviour
    for the recovery / ad-hoc paths).

    Self-describing artefact
    ------------------------
    Two metadata tables are written into the snapshot file itself:

    * ``snapshot_meta`` - one row recording snapshot_id, server,
      libraries, metrics, captured_at, created_by. Lets a future
      reader (different install, lost registry, etc.) know what
      this file contains without consulting the registry.
    * ``snapshot_users`` - one row per user who *actually* has data
      in the populated metric tables. Computed after the metric
      tables are copied so zero-activity users never appear here.
    """
    from server import media_db

    src_path = media_db._db_path()
    if not src_path.is_file():
        raise RuntimeError(
            f"media.db not found at {src_path}; cannot build a snapshot until "
            "the engine has run at least once."
        )

    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    if snapshot_path.exists():
        # Clean re-create: an existing file at this path means a
        # previous capture failed mid-write or the end user picked
        # a colliding name. Either way, drop it.
        snapshot_path.unlink()

    # 1. Open source read-only via the URI form so a concurrent
    #    engine writer doesn't fight us for the journal.
    src_uri = f"file:{src_path}?mode=ro"
    src_conn = sqlite3.connect(src_uri, uri=True, timeout=30.0)
    src_conn.row_factory = sqlite3.Row

    # 2. Confirm the server has any data at all.
    row = src_conn.execute(
        "SELECT id FROM servers WHERE id = ?", (server_id,),
    ).fetchone()
    if row is None:
        src_conn.close()
        raise ValueError(
            f"No server with id {server_id!r} in media.db; nothing to capture."
        )

    # 3. Create the destination and replay the schema DDL.
    dst_conn = sqlite3.connect(str(snapshot_path), timeout=30.0, isolation_level=None)
    # WAL-with-fallback for Docker Desktop / Windows.
    from server._sqlite_journal import set_journal_mode
    set_journal_mode(dst_conn, db_label="snapshot.db")
    dst_conn.execute("PRAGMA synchronous=NORMAL")

    try:
        _copy_schema(src_conn, dst_conn)

        # 4. Add the self-describing metadata tables. Created here so
        #    even if step 5's cross-file copy raises, the file on
        #    disk at least has the schema. Empty-row scenarios stay
        #    readable rather than wedging the orphan reconcile path.
        _create_meta_tables(dst_conn)

        # 5. Cross-file INSERT...SELECT via ATTACH. Pass the requested
        #    metric set through so the per-table copy loop can gate
        #    its INSERTs.
        # L1: bind the path rather than f-string it in. ``src_path`` is
        # internal/trusted today, but a path containing a single quote
        # would break the statement - parameterising removes the sharp
        # edge at zero cost.
        dst_conn.execute("ATTACH DATABASE ? AS src", (str(src_path),))
        try:
            counters = _copy_rows(dst_conn, server_id, metrics)
        finally:
            try:
                dst_conn.execute("DETACH DATABASE src")
            except Exception:
                pass

        # 6. Populate the two metadata tables now that the metric
        #    tables are settled. snapshot_users is derived from the
        #    actual rows just written, not from the source server's
        #    full user list - zero-activity users never appear.
        captured_at_resolved = float(captured_at) if captured_at is not None else time.time()
        _write_snapshot_meta(
            dst_conn,
            snapshot_id=snapshot_id or "",
            server_id=server_id,
            server_name=server_name,
            captured_at=captured_at_resolved,
            libraries=list(libraries or []),
            metrics=list(metrics) if metrics is not None else list(_METRIC_TO_TABLE.keys()),
            created_by=created_by,
            # Surface a user-count summary in the snapshot
            # description string. The create_snapshot_db path
            # doesn't have direct access to a managed-user roster, so
            # we approximate via the user_display_names map size; if
            # that's also unset, _synthesize_snapshot_description
            # omits the count gracefully.
            user_count=len(user_display_names or {}),
        )
        _write_snapshot_users(
            dst_conn,
            metrics=metrics,
            user_display_names=user_display_names or {},
            server_id=server_id,
            src_conn=src_conn,
        )
    finally:
        src_conn.close()
        dst_conn.close()

    # 7. Stat the finished file for the registry row.
    counters["file_size"] = int(snapshot_path.stat().st_size)

    # Audit trail: snapshot .db creation is a major write that should
    # show in db_access.log alongside the matching media.db ingest and
    # the snapshots.db registry insert below it. ``ingest_snapshot_payload``
    # already audits the media.db write; this closes the gap on the
    # snapshot .db file itself.
    try:
        from services.run_logs import db_access as db_access_log
        _row_total = (
            counters.get("server_items", 0)
            + counters.get("items", 0)
            + counters.get("watch_events", 0)
            + counters.get("ratings", 0)
            + counters.get("playlists", 0)
            + counters.get("collections", 0)
        )
        db_access_log.log_write(
            table="snapshot.db",
            where={
                "snapshot_db": snapshot_path.name,
                "server_id": server_id,
                "snapshot_id": snapshot_id or "",
            },
            affected_rows=_row_total,
            intent=(
                f"create per-server snapshot .db "
                f"(file_size={counters.get('file_size', 0)} bytes, "
                f"metrics={','.join(metrics) if metrics else 'all'})"
            ),
        )
    except Exception:
        pass

    return counters


# ── Internal: schema replication ────────────────────────────────────────────

def _copy_schema(src: sqlite3.Connection, dst: sqlite3.Connection) -> None:
    """
    Read every CREATE TABLE / CREATE INDEX statement from the source's
    ``sqlite_master`` and apply them to the destination. Triggers and
    views are not used by media.db today; if they're ever added the
    same query catches them.
    """
    rows = src.execute(
        "SELECT type, name, sql FROM sqlite_master "
        "WHERE sql IS NOT NULL "
        "AND name NOT LIKE 'sqlite_%' "
        "ORDER BY CASE type WHEN 'table' THEN 0 ELSE 1 END, name"
    ).fetchall()
    for r in rows:
        name = r["name"]
        if name in _SKIP_TABLES:
            # Don't carry the managed_users / global_tombstones DDL
            # into the snapshot - it'd just be an empty table on
            # restore, slightly confusing.
            continue
        sql = r["sql"]
        if not sql:
            continue
        try:
            dst.execute(sql)
        except sqlite3.OperationalError as e:
            # Re-creating an index on an absent column shouldn't
            # happen given the ordering above (tables come first)
            # but guard anyway so a malformed entry doesn't fail
            # the whole capture.
            log.warning("Skipped schema entry %r: %s", name, e)
    dst.commit()


# ── Internal: row filtering ────────────────────────────────────────────────

def _copy_rows(
    dst: sqlite3.Connection,
    server_id: str,
    metrics: Optional[List[str]],
) -> Dict[str, int]:
    """
    Copy the filtered rowset from the ATTACHed ``src`` into ``dst``.
    The order matters because ``server_items`` references ``items``;
    insert items first so the FK (when present) passes.

    Metric gating
    -------------
    ``metrics`` is the end user-requested subset of
    ``{"watch_history", "ratings", "playlists", "collections"}``.

    Per-table behaviour:

    * ``servers``        - always one row copied.
    * ``items``          - always copied; the items table is the
                           GUID-keyed pool, not metric-scoped.
    * ``server_items``   - always copied; resolver Tier-0 cache.
    * Metric tables      - copied ONLY if the table's metric label
                           appears in ``metrics``. Tables for
                           unrequested metrics keep their schema
                           (created by ``_copy_schema`` above) but
                           receive no rows. ``metrics=None`` means
                           "copy everything" - legacy behaviour for
                           orphan-recovery and ad-hoc tooling.
    """
    counters: Dict[str, int] = {}

    # Set of tables to populate for the requested metric set. When
    # metrics is None, fall through to the legacy "copy every metric
    # table" behaviour.
    if metrics is None:
        wanted_metric_tables = set(_METRIC_TABLES)
    else:
        wanted_metric_tables = {
            _METRIC_TO_TABLE[m] for m in metrics if m in _METRIC_TO_TABLE
        }

    # Explicit column lists (discovered from the destination schema)
    # instead of ``SELECT *``: the copy is then keyed by column NAME,
    # so it stays correct even if a future migration reorders columns
    # between the live media.db and a freshly-built snapshot schema.
    def _cols(table: str) -> str:
        return ", ".join(
            r[1] for r in dst.execute(f"PRAGMA table_info({table})").fetchall()
        )

    # ── servers (one row) ───────────────────────────────────────────────
    _server_cols = _cols("servers")
    cur = dst.execute(
        f"INSERT INTO servers ({_server_cols}) "
        f"SELECT {_server_cols} FROM src.servers WHERE id = ?",
        (server_id,),
    )
    counters["servers"] = cur.rowcount

    # ── items: only those referenced by this server's server_items ──────
    _item_cols = _cols("items")
    cur = dst.execute(
        f"INSERT INTO items ({_item_cols}) "
        f"SELECT {_item_cols} FROM src.items WHERE id IN ("
        "  SELECT item_id FROM src.server_items WHERE server_id = ?"
        ")",
        (server_id,),
    )
    counters["items"] = cur.rowcount

    # ── per-server tables ───────────────────────────────────────────────
    for table in _PER_SERVER_TABLES:
        # ``server_items`` is always copied - resolver cache.
        # Other tables are metric-gated.
        if table in _METRIC_TABLES and table not in wanted_metric_tables:
            counters[table] = 0
            continue
        try:
            _t_cols = _cols(table)
            cur = dst.execute(
                f"INSERT INTO {table} ({_t_cols}) "
                f"SELECT {_t_cols} FROM src.{table} WHERE server_id = ?",
                (server_id,),
            )
            counters[table] = cur.rowcount
        except sqlite3.OperationalError as e:
            # Table might not exist in this media.db version. Log and
            # continue rather than failing the whole capture.
            log.warning("Skipped per-server copy of %r: %s", table, e)
            counters[table] = 0

    dst.commit()
    return counters


# ── Internal: metadata tables (self-describing artefact) ────────────────────

def _create_meta_tables(dst: sqlite3.Connection) -> None:
    """
    Create ``snapshot_meta`` + ``snapshot_users`` + ``library_sections``
    in the destination snapshot DB. Always runs - these tables are
    part of every snapshot's schema regardless of which metrics were
    captured. Idempotent CREATE IF NOT EXISTS so a recovered orphan
    getting re-stamped is harmless.

    Schema version contract:

    Each snapshot .db carries a ``schema_version`` column on
    ``snapshot_meta`` so the restore path can refuse files written by
    older / mismatched builds with a clear error. The current build's
    version comes from :data:`SNAPSHOT_SCHEMA_VERSION`. Older
    snapshots that pre-date this column (column missing entirely)
    are treated as ``schema_version = 0`` and refused at restore.

    ``library_sections`` is the integrity anchor for library identity:
    every per-server row (server_items, watch_events, ratings,
    playlists, collections) references it via ``section_key``. Unlike
    media.db this table CAN be FK-enforced safely because the snapshot
    DB is written once in a single transaction and never updated;
    referential integrity is checked at commit time and a missing
    parent fails the whole snapshot atomically (the right behaviour -
    a partial / inconsistent .db on disk is worse than a re-run).
    """
    dst.executescript("""
        CREATE TABLE IF NOT EXISTS snapshot_meta (
            snapshot_id     TEXT NOT NULL,
            server_id       TEXT NOT NULL,
            server_name     TEXT NOT NULL,
            captured_at     REAL NOT NULL,
            libraries_json  TEXT NOT NULL,
            metrics_json    TEXT NOT NULL,
            created_by      TEXT,
            schema_version  INTEGER NOT NULL DEFAULT 0,
            -- Short human-readable summary of what this snapshot
            -- contains (users, libraries, metrics). Populated at
            -- capture time; older snapshots without this column
            -- fall through to filename-derived content on read.
            description     TEXT
        );
        CREATE TABLE IF NOT EXISTS snapshot_users (
            user_handle     TEXT PRIMARY KEY,
            display_name    TEXT,
            is_owner        INTEGER NOT NULL DEFAULT 0,
            -- Distinguishes users captured WITH data (had_data=1)
            -- from roster users captured with zero activity
            -- (had_data=0). The Exports panel renders
            -- "N captured / M roster" using these counts.
            had_data        INTEGER NOT NULL DEFAULT 0,
            -- The application-wide canonical user identifier from
            -- media.db v12. Without this column the snapshot is
            -- self-describing for everything EXCEPT the identity of
            -- the users it captured, breaking cross-install identity
            -- preservation. backend_user_id is the per-backend native
            -- id (Plex.tv numeric userID; J/E GUID); kept alongside
            -- so a snapshot loaded on a fresh install can ground its
            -- users against either the end user's identity_map (by
            -- app_user_uuid) OR the backend's auto-link path (by
            -- (service_type, backend_user_id)).
            app_user_uuid   TEXT,
            backend_user_id TEXT
        );
        CREATE TABLE IF NOT EXISTS library_sections (
            server_id      TEXT NOT NULL,
            section_key    INTEGER NOT NULL,
            section_title  TEXT NOT NULL,
            section_type   TEXT NOT NULL,
            first_seen_at  REAL NOT NULL,
            last_seen_at   REAL NOT NULL,
            PRIMARY KEY (server_id, section_key)
        );
        CREATE INDEX IF NOT EXISTS idx_library_sections_title
            ON library_sections(server_id, section_title);
    """)
    dst.commit()


# Snapshot file schema version. Bumped whenever the on-disk shape
# changes in a way that requires re-capture (not just additive). The
# restore-side reader refuses files where snapshot_meta.schema_version
# is less than this constant; the error message tells the end user
# which version they have and which is required.
#
# v15 introduced the library_sections anchor and made section_key
# required on every per-server row.
#
# v16 adds app_user_uuid + backend_user_id columns to snapshot_users
# and populates the app_user_uuid column on server_users (DDL was
# cloned from media.db v12; rows arrive NULL on v15 snapshots).
# Existing v15 snapshots become unreadable on upgrade per the
# refuse-at-load policy in server/snapshot_serializer.py; end users
# re-capture per the Legacy Support Policy (pre-release).
#
# v17: the items table gains hierarchy columns (show_title /
# season_index / episode_index / artist / album from media.db v18,
# grandparent_guid from media.db v19). The snapshot's items DDL is
# cloned from media.db so the columns arrive automatically; this
# bump exists so the refuse-at-load gate re-captures pre-v17
# snapshots that lack the hierarchy data the resolver's new
# hierarchy tier consumes.
#
# v18: the ratings table gains is_favorite (media.db v20), the
# backend-neutral favorite face of the per-user affinity record.
# Jellyfin/Emby favorites have no home in the rating-only table and
# were dropped on capture; the bump re-captures pre-v18 snapshots so
# favorites are preserved for cross-backend restore.
SNAPSHOT_SCHEMA_VERSION = 18


def _synthesize_snapshot_description(
    *,
    server_name: str,
    libraries: List[str],
    metrics: List[str],
    user_count: Optional[int],
) -> str:
    """
    Build a one-line summary of a snapshot's contents for the Exports
    listing. Format:

        "Plex1 · 3 user(s) · Movies, TV Shows, Music (3 libraries) · watch_history, ratings, playlists, collections"

    Empty / unknown sections are dropped so a snapshot with no users
    captured reads as "Plex1 · Movies, TV Shows · watch_history, ratings".
    """
    parts: List[str] = []
    if server_name:
        parts.append(server_name)
    if user_count is not None and user_count > 0:
        parts.append(f"{user_count} user(s)")
    if libraries:
        first = ", ".join(libraries[:4])
        more = "" if len(libraries) <= 4 else f", +{len(libraries) - 4} more"
        parts.append(f"{first}{more} ({len(libraries)} library/ies)")
    if metrics:
        parts.append(", ".join(metrics))
    return " · ".join(parts)


def _write_snapshot_meta(
    dst: sqlite3.Connection,
    *,
    snapshot_id: str,
    server_id: str,
    server_name: str,
    captured_at: float,
    libraries: List[str],
    metrics: List[str],
    created_by: Optional[str],
    description: Optional[str] = None,
    user_count: Optional[int] = None,
) -> None:
    """
    Insert the single ``snapshot_meta`` row. Re-runnable: if a row
    already exists (re-stamping a recovered orphan), the existing
    row is replaced with the freshly-supplied values.

    ``description`` is an optional short human-readable summary
    stamped on the snapshot at capture time. If the caller doesn't
    pre-compute one,
    we synthesize a generic summary from the libraries + metrics +
    user_count so every fresh snapshot has at least a populated
    field (older snapshots get a NULL description and the reader
    falls back to filename-derived content).
    """
    if description is None:
        description = _synthesize_snapshot_description(
            server_name=server_name,
            libraries=libraries,
            metrics=metrics,
            user_count=user_count,
        )
    dst.execute("DELETE FROM snapshot_meta")
    dst.execute(
        """
        INSERT INTO snapshot_meta (
            snapshot_id, server_id, server_name, captured_at,
            libraries_json, metrics_json, created_by, schema_version,
            description
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            snapshot_id, server_id, server_name, captured_at,
            json.dumps(libraries), json.dumps(metrics), created_by,
            SNAPSHOT_SCHEMA_VERSION, description,
        ),
    )
    # No commit here: this function is called from two paths, and the
    # ``build_snapshot_db_from_payloads`` path wraps every row-write in
    # one explicit BEGIN/COMMIT. A mid-flight ``dst.commit()`` would
    # close that outer transaction early, leaving the explicit COMMIT
    # at the end of the caller to fail with "cannot commit - no
    # transaction is active." The legacy ``create_snapshot_db`` path
    # opens its connection in autocommit (isolation_level=None) so the
    # INSERTs above are already individually committed.


def _write_snapshot_users(
    dst: sqlite3.Connection,
    *,
    metrics: Optional[List[str]],
    user_display_names: Dict[str, str],
    server_id: str = "",
    src_conn: Optional[sqlite3.Connection] = None,
) -> None:
    """
    Populate ``snapshot_users`` with the full roster captured during
    this snapshot run: the owner plus every managed user from
    ``user_display_names``. Each row carries a ``had_data`` flag set
    to 1 if the user appears in at least one populated metric table,
    0 otherwise.

    The full roster is written, not only users with data:
    zero-activity managed users must not be dropped or the registry's
    user_count under-counts by the roster minus active-users delta.
    The end user's Exports panel renders "N captured / M roster"
    using both counts so a server with 8 managed users where 3 have
    actual data shows "3 of 9".

    Display name is filled from the end user-supplied map when
    available; missing entries land as NULL. ``is_owner`` is true
    iff the handle is the empty string (``""``), which is the
    project convention for server-owner data across every metric
    table.
    """
    # Determine which tables we actually populated. ``metrics=None``
    # means "all of them" (legacy / recovery path); otherwise only
    # the end user-requested subset.
    if metrics is None:
        populated_tables = list(_METRIC_TABLES)
    else:
        populated_tables = [
            _METRIC_TO_TABLE[m] for m in metrics if m in _METRIC_TO_TABLE
        ]

    # Build the set of handles that had data in this capture (union
    # across populated metric tables). Empty handles (owner) are
    # valid and preserved through the DISTINCT.
    had_data_handles: set = set()
    if populated_tables:
        union_parts = [
            f"SELECT user_handle FROM {t}"
            for t in populated_tables
        ]
        union_sql = " UNION ".join(union_parts)
        had_data_handles = {
            (r[0] or "") for r in dst.execute(
                f"SELECT DISTINCT user_handle FROM ({union_sql})"
            ).fetchall()
        }

    # Compose the full roster: owner + every managed user from the
    # end user-supplied display-name map + any handle that had data
    # but wasn't in the supplied roster (safety net for engine paths
    # that discover users dynamically).
    roster: set = {""}  # owner is always present in a snapshot
    roster.update(user_display_names.keys())
    roster.update(had_data_handles)

    # Build a {handle -> (app_user_uuid, backend_user_id)} lookup
    # from the source media.db once so each per-handle write doesn't
    # pay a roundtrip. Empty handle ("" = owner) keys both tables:
    # prefer managed_users (the authoritative app_user_uuid origin),
    # fall back to server_users. NULL on both sides is acceptable -
    # rows land with NULL identity, callers downstream still get the
    # display_name + had_data signal.
    identity_lookup: Dict[str, Dict[str, Optional[str]]] = {}
    if src_conn is not None and server_id:
        try:
            mu_rows = src_conn.execute(
                "SELECT username, app_user_uuid, backend_user_id "
                "FROM managed_users WHERE server_id = ?",
                (server_id,),
            ).fetchall()
            for r in mu_rows:
                identity_lookup[r["username"] or ""] = {
                    "app_user_uuid":   r["app_user_uuid"],
                    "backend_user_id": r["backend_user_id"],
                }
        except sqlite3.OperationalError:
            pass
        try:
            su_rows = src_conn.execute(
                "SELECT user_handle, app_user_uuid, backend_user_id "
                "FROM server_users WHERE server_id = ?",
                (server_id,),
            ).fetchall()
            for r in su_rows:
                h = r["user_handle"] or ""
                # Don't overwrite a managed_users entry; server_users
                # is the fallback when managed_users had no row for
                # this handle (rare).
                if h not in identity_lookup:
                    identity_lookup[h] = {
                        "app_user_uuid":   r["app_user_uuid"],
                        "backend_user_id": r["backend_user_id"],
                    }
        except sqlite3.OperationalError:
            pass

    for handle in roster:
        display = user_display_names.get(handle) or None
        is_owner = 1 if handle == "" else 0
        had_data = 1 if handle in had_data_handles else 0
        identity = identity_lookup.get(handle, {})
        dst.execute(
            """
            INSERT OR REPLACE INTO snapshot_users
                (user_handle, display_name, is_owner, had_data,
                 app_user_uuid, backend_user_id)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                handle, display, is_owner, had_data,
                identity.get("app_user_uuid"),
                identity.get("backend_user_id"),
            ),
        )
    # No commit here: see _write_snapshot_meta for the contract.


# ── Payload-direct builder ──────────────────────────────────────────────────
#
# ``build_snapshot_db_from_payloads`` is the live snapshot writer:
# it consumes the in-memory list of per-library ``export_data`` dicts
# that ``services.snapshot.plex_native.snapshotter.snapshot_library`` appends to
# ``state._snapshot_payloads`` and writes the .db directly from that
# data. media.db is never read; the snapshot file is a point-in-time
# record of what the live-fetch payload reported.
#
# ``create_snapshot_db`` above is kept for the orphan-reconcile path
# (rebuilding a missing snapshot.db from media.db when a registry row
# exists but the file is gone) - that is the one legitimate place
# where reading from media.db is correct. Live snapshot capture must
# use the payload-direct path.

def build_snapshot_db_from_payloads(
    *,
    snapshot_path: Path,
    snapshot_id: Optional[str] = None,
    server_id: str,
    server_name: str = "",
    libraries: Optional[List[str]] = None,
    metrics: Optional[List[str]] = None,
    captured_at: Optional[float] = None,
    created_by: Optional[str] = None,
    user_display_names: Optional[Dict[str, str]] = None,
    payloads: List[Dict[str, Any]],
) -> Dict[str, int]:
    """
    Write a per-server snapshot .db directly from in-memory payloads.

    ``payloads`` is the list collected by ``state._snapshot_payloads``
    during one run, one entry per library captured (each entry follows
    the .plexexport.json shape: ``items`` block + ``users`` block).
    The snapshot file's schema is borrowed from media.db's DDL (read
    only - no row data) so a snapshot remains interchangeable with the
    cumulative store for tooling that consumes either.

    Within the snapshot.db, ``items.id`` is its own AUTOINCREMENT space
    distinct from media.db's. Membership lists in ``playlists`` /
    ``collections`` reference those local ids; the JSON serializer's
    ``items_by_id`` map already resolves through this local namespace.

    Returns the same counter shape as :func:`create_snapshot_db` so
    callers (registry row writer) can keep the same logging /
    audit lines.
    """
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    if snapshot_path.exists():
        snapshot_path.unlink()

    # 1. Get schema DDL from media.db.
    #
    # We REUSE the shared media_db connection rather than opening a
    # fresh ``mode=ro`` URI connection. A second connection opened
    # with ``file:.../media.db?mode=ro`` surfaces
    # ``sqlite3.OperationalError: unable to open database file`` on
    # the first query because media.db is open in WAL mode with chmod
    # 0o600 (see media_db.init_media_db) and a read-only URI
    # connection refuses to create/access the ``-shm`` coordination
    # file SQLite needs to bridge readers and the live writer. Routing
    # through the existing shared autocommit connection sidesteps the
    # issue entirely - same process, same UID, no second open. We only
    # read ``sqlite_master`` (and one row from ``servers`` later), so
    # the dedup/concurrency story is unchanged.
    from server import media_db as _media_db
    src_path = _media_db._db_path()
    if not src_path.is_file():
        raise RuntimeError(
            f"media.db not found at {src_path}; cannot borrow snapshot schema."
        )
    src_conn = _media_db._require_conn()
    src_conn.row_factory = sqlite3.Row

    dst_conn = sqlite3.connect(str(snapshot_path), timeout=30.0, isolation_level=None)
    # WAL-with-fallback for Docker Desktop / Windows.
    from server._sqlite_journal import set_journal_mode
    set_journal_mode(dst_conn, db_label="snapshot.db")
    dst_conn.execute("PRAGMA synchronous=NORMAL")
    # Enforce FK on the snapshot DB. Unlike media.db (where FK
    # enforcement is off for bulk-insert perf), the snapshot file is
    # written once in a single transaction and never updated, so
    # referential integrity violations need to fail the snapshot
    # atomically rather than leave an inconsistent file on disk. A
    # write referencing a missing ``library_sections`` row will
    # raise ``sqlite3.IntegrityError`` at COMMIT time.
    dst_conn.execute("PRAGMA foreign_keys=ON")
    dst_conn.row_factory = sqlite3.Row

    _copy_schema(src_conn, dst_conn)
    _create_meta_tables(dst_conn)

    # Wrap every row write below in a single explicit transaction.
    # The connection is opened with isolation_level=None (autocommit),
    # so without an explicit BEGIN every per-row INSERT is its own
    # transaction with its own fsync. On a multi-library / multi-user
    # snapshot that means tens-to-hundreds of thousands of one-row
    # commits and dominates the post-engine 6-8 minute hang between
    # "libraries 100%" and the job actually finishing. One BEGIN /
    # COMMIT pair collapses the whole write phase into a single fsync
    # at the end.
    #
    # Schema DDL above runs OUTSIDE the transaction on purpose: SQLite
    # implicitly commits any active transaction before executing DDL,
    # so opening BEGIN before _copy_schema / _create_meta_tables would
    # be a no-op anyway. We start the transaction immediately after.
    dst_conn.execute("BEGIN")

    # 2. Write the one-row ``servers`` entry. Pull the live row from
    #    media.db so url / machine_id stay consistent with the
    #    registry. Falls back to a minimal row when no media.db entry
    #    exists yet (very early first-run edge case). Reuses the
    #    shared media_db connection - same WAL-mode-with-restricted-
    #    perms reasoning as step 1 above.
    try:
        row = src_conn.execute(
            "SELECT * FROM servers WHERE id = ?", (server_id,),
        ).fetchone()
        if row is not None:
            dst_conn.execute(
                "INSERT INTO servers (id, name, service, url, machine_id, added_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    row["id"], row["name"], row["service"], row["url"],
                    row["machine_id"], row["added_at"],
                ),
            )
        else:
            dst_conn.execute(
                "INSERT INTO servers (id, name, service, url, machine_id, added_at) "
                "VALUES (?, ?, 'plex', '', NULL, ?)",
                (server_id, server_name, time.time()),
            )
    except sqlite3.OperationalError:
        # servers table can't accept the row (schema mismatch); leave
        # it empty and let the registry's metadata take over.
        pass

    counters: Dict[str, int] = {
        "servers": 1,
        "items": 0,
        "server_items": 0,
        "watch_events": 0,
        "ratings": 0,
        "playlists": 0,
        "collections": 0,
    }

    # 3. Walk every payload and write rows. The per-snapshot items.id
    #    space is local: we keep a GUID-set → items.id map keyed by
    #    canonical-GUID tuple so the same item across libraries /
    #    users gets one row.
    from services.translation.guid_translator import normalize_guids
    _GUID_COL = {
        "imdb": "imdb_id",
        "tmdb": "tmdb_id",
        "tvdb": "tvdb_id",
        "musicbrainz": "musicbrainz_id",
        "plex": "plex_guid",
    }
    items_by_guid: Dict[str, int] = {}              # canonical guid → items.id
    server_items_seen: set = set()                  # (item_id, rating_key) dedup
    rk_to_item_id: Dict[str, int] = {}              # ratingKey (str) → items.id
    captured_at_resolved = float(captured_at) if captured_at is not None else time.time()

    def _upsert_item(rec: Dict[str, Any]) -> Optional[int]:
        guids = rec.get("guids") or []
        if not isinstance(guids, list):
            return None
        title = rec.get("title") or ""
        if not title:
            return None
        media_type = rec.get("type") or "movie"
        canon = normalize_guids(guids)
        # Try GUID-keyed dedup first.
        for g in canon:
            iid = items_by_guid.get(g)
            if iid is not None:
                # Touch updated_at on the existing row.
                dst_conn.execute(
                    "UPDATE items SET updated_at = ? WHERE id = ?",
                    (captured_at_resolved, iid),
                )
                # Re-key any guid aliases we haven't seen yet so they
                # all resolve to the same row.
                for g2 in canon:
                    items_by_guid.setdefault(g2, iid)
                return iid
        # New row.
        cols: Dict[str, Optional[str]] = {
            "imdb_id": None, "tmdb_id": None, "tvdb_id": None,
            "musicbrainz_id": None, "plex_guid": None,
        }
        for g in canon:
            prefix, _, payload = g.partition("://")
            if not payload:
                continue
            col = _GUID_COL.get(prefix)
            if col and cols[col] is None:
                cols[col] = payload
        year = rec.get("year")
        if year is not None and not isinstance(year, int):
            try:
                year = int(year)
            except (TypeError, ValueError):
                year = None
        filepath = rec.get("filepath") or rec.get("path") or None
        # Hierarchy fields. Populated by both the Plex serializer
        # and the adapter path (Jellyfin/Emby). The engine_dict keys
        # are:
        #   * "show_title"   -> items.show_title    (episodes)
        #   * "parent_index" -> items.season_index  (episodes)
        #   * "episode_index"-> items.episode_index (episodes)
        #   * "artist"       -> items.artist        (tracks)
        #   * "album"        -> items.album         (tracks)
        # Empty strings get coerced to NULL so the column is clean
        # for non-applicable types (movies, photos, etc.).
        show_title = (rec.get("show_title") or "").strip() or None
        artist = (rec.get("artist") or "").strip() or None
        album = (rec.get("album") or "").strip() or None
        def _to_int_or_none(v: Any) -> Optional[int]:
            if v is None or v == "":
                return None
            try:
                return int(v)
            except (TypeError, ValueError):
                return None
        season_index = _to_int_or_none(rec.get("parent_index"))
        episode_index = _to_int_or_none(rec.get("episode_index"))
        # Parent GUID (series GUID for episodes, artist GUID for
        # tracks). Empty string coerced to NULL - clean for movies +
        # GUID-less backends.
        grandparent_guid = (rec.get("grandparent_guid") or "").strip() or None
        cur = dst_conn.execute(
            "INSERT INTO items ("
            "imdb_id, tmdb_id, tvdb_id, musicbrainz_id, plex_guid, "
            "title, media_type, year, filepath_suffix, "
            "show_title, season_index, episode_index, artist, album, "
            "grandparent_guid, "
            "created_at, updated_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                cols["imdb_id"], cols["tmdb_id"], cols["tvdb_id"],
                cols["musicbrainz_id"], cols["plex_guid"],
                title, media_type, year, filepath,
                show_title, season_index, episode_index, artist, album,
                grandparent_guid,
                captured_at_resolved, captured_at_resolved,
            ),
        )
        iid = int(cur.lastrowid)
        counters["items"] += 1
        for g in canon:
            items_by_guid.setdefault(g, iid)
        return iid

    def _record_server_item(item_id: int, rating_key: Any, section_key: int) -> None:
        if rating_key is None:
            return
        # rating_key is kept as a string, not coerced with int():
        # Jellyfin/Emby rating keys are opaque GUID strings, and
        # int() on one raised ValueError - which silently dropped
        # the server_items row AND the rk_to_item_id entry that
        # playlist / collection / watch-event member resolution
        # depends on. The server_items.rating_key column has INTEGER
        # affinity, so a numeric Plex key still stores as an integer;
        # a non-numeric GUID stores as text. .strip() preserves the
        # whitespace tolerance the old int() coercion had.
        rk_key = str(rating_key).strip()
        if not rk_key:
            return
        if not isinstance(section_key, int) or section_key <= 0:
            raise ValueError(
                f"_record_server_item: section_key required (got {section_key!r}). "
                "See v0.15 schema-anchor invariant."
            )
        key = (item_id, rk_key)
        if key in server_items_seen:
            return
        try:
            dst_conn.execute(
                "INSERT OR REPLACE INTO server_items "
                "(item_id, server_id, rating_key, section_key, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (item_id, server_id, rk_key, int(section_key), captured_at_resolved),
            )
            server_items_seen.add(key)
            rk_to_item_id[rk_key] = item_id
            counters["server_items"] += 1
        except sqlite3.OperationalError:
            pass

    def _resolve_members(members: List[Dict[str, Any]]) -> List[int]:
        out: List[int] = []
        for rec in members or []:
            # Prefer rating_key lookup (it's set after _upsert_item /
            # _record_server_item ran for the same record earlier in
            # the walk). Fall back to GUID lookup for safety.
            rk = rec.get("rating_key")
            iid: Optional[int] = None
            if rk is not None:
                try:
                    iid = rk_to_item_id.get(str(rk).strip())
                except (TypeError, ValueError):
                    iid = None
            if iid is None:
                canon = normalize_guids(rec.get("guids") or [])
                for g in canon:
                    iid = items_by_guid.get(g)
                    if iid is not None:
                        break
            if iid is not None:
                out.append(iid)
        return out

    # Extract per-payload library identity. The capture path
    # in services.snapshot.plex_native.snapshotter populates these fields on every payload;
    # we refuse to write a snapshot if any payload is missing them.
    # See the schema-anchor invariant.
    def _payload_section_info(p: Dict[str, Any]) -> Tuple[int, str, str]:
        sec_id = p.get("library_section_id")
        sec_title = str(p.get("library") or "")
        sec_type = str(p.get("library_section_type") or "")
        if sec_id is None:
            raise ValueError(
                f"build_snapshot_db_from_payloads: payload missing "
                f"library_section_id (library={sec_title!r}). The capture "
                "path must populate this; see v0.15 schema-anchor invariant."
            )
        try:
            sec_id_int = int(sec_id)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"build_snapshot_db_from_payloads: library_section_id must be int "
                f"(got {sec_id!r}): {exc}"
            )
        if sec_id_int <= 0:
            raise ValueError(
                f"build_snapshot_db_from_payloads: library_section_id must be > 0 "
                f"(got {sec_id_int}, library={sec_title!r}). 0 is the 'unknown' "
                "sentinel and never appears in a valid payload."
            )
        if not sec_title:
            raise ValueError(
                "build_snapshot_db_from_payloads: payload missing 'library' (section title)"
            )
        if not sec_type:
            raise ValueError(
                f"build_snapshot_db_from_payloads: payload missing "
                f"library_section_type (library={sec_title!r})"
            )
        return sec_id_int, sec_title, sec_type

    # Pre-pass: validate every payload and upsert the library_sections
    # dimension rows BEFORE any per-server-table writes. Doing this
    # first means FK enforcement on the snapshot.db catches a missing
    # parent the moment a write attempts to reference it.
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        sec_id_int, sec_title, sec_type = _payload_section_info(payload)
        try:
            dst_conn.execute(
                """
                INSERT INTO library_sections (
                    server_id, section_key, section_title, section_type,
                    first_seen_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(server_id, section_key) DO UPDATE SET
                    section_title = excluded.section_title,
                    section_type  = excluded.section_type,
                    last_seen_at  = excluded.last_seen_at
                """,
                (server_id, sec_id_int, sec_title, sec_type,
                 captured_at_resolved, captured_at_resolved),
            )
        except sqlite3.OperationalError as exc:
            raise RuntimeError(
                f"Could not write library_sections row for {sec_title!r} "
                f"(section_key={sec_id_int}): {exc}"
            )

    # ── Walk every payload ──────────────────────────────────────────
    # Pass 1: ingest every record into items + server_items so the
    # rating_key → items.id map is fully populated before we resolve
    # playlist / collection memberships in Pass 2. Each call carries
    # the payload's section_key forward so server_items rows are
    # stamped with library identity at insert time.
    def _walk_item_records(block: Dict[str, Any], section_key: int) -> None:
        for rec in (block.get("watch_history") or []):
            iid = _upsert_item(rec)
            if iid is not None:
                _record_server_item(iid, rec.get("rating_key"), section_key)
        for rec in (block.get("ratings") or []):
            iid = _upsert_item(rec)
            if iid is not None:
                _record_server_item(iid, rec.get("rating_key"), section_key)
        for pl in (block.get("playlists") or []):
            for rec in (pl.get("items") or []):
                iid = _upsert_item(rec)
                if iid is not None:
                    _record_server_item(iid, rec.get("rating_key"), section_key)
        for col in (block.get("collections") or []):
            for rec in (col.get("items") or []):
                iid = _upsert_item(rec)
                if iid is not None:
                    _record_server_item(iid, rec.get("rating_key"), section_key)

    # Unified users map. Owner is the role='owner' block; the legacy
    # empty-handle convention is the fallback for mid-transition
    # payloads. One pass over the users map handles both.
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        sec_id_int, _, _ = _payload_section_info(payload)
        for udata in (payload.get("users") or {}).values():
            if isinstance(udata, dict):
                _walk_item_records(udata, sec_id_int)

    # Pass 2: watch_events, ratings, playlists (with members),
    # collections (with members). Library-level data uses
    # user_handle=""; per-user data uses user_handle=<username>. The
    # dedup discipline (server-wide vs user-private playlists /
    # collections by rating_key) mirrors ingest_snapshot_payload.
    wanted_metric_tables: set
    if metrics is None:
        wanted_metric_tables = set(_METRIC_TABLES)
    else:
        wanted_metric_tables = {
            _METRIC_TO_TABLE[m] for m in metrics if m in _METRIC_TO_TABLE
        }

    owner_playlist_keys: set = set()
    owner_collection_keys: set = set()
    # First pass: locate each payload's owner block (role='owner') and
    # collect its playlist/collection rating_keys so per-user variants
    # can be deduped against them.
    def _find_owner_block(p: Dict[str, Any]) -> Dict[str, Any]:
        users = p.get("users") or {}
        for _h, _ub in users.items():
            if isinstance(_ub, dict) and _ub.get("role") == "owner":
                return _ub
        if "" in users and isinstance(users[""], dict):
            return users[""]
        return {}

    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        owner_block = _find_owner_block(payload)
        for pl in (owner_block.get("playlists") or []):
            rk = pl.get("rating_key")
            if rk is not None:
                try:
                    owner_playlist_keys.add(int(rk))
                except (TypeError, ValueError):
                    pass
        for col in (owner_block.get("collections") or []):
            rk = col.get("rating_key")
            if rk is not None:
                try:
                    owner_collection_keys.add(int(rk))
                except (TypeError, ValueError):
                    pass

    def _write_watch_events(user_handle: str, records: List[Dict[str, Any]],
                            section_key: int) -> None:
        if "watch_events" not in wanted_metric_tables:
            return
        for rec in records or []:
            rk = rec.get("rating_key")
            if rk is None:
                continue
            try:
                iid = rk_to_item_id.get(str(rk).strip())
            except (TypeError, ValueError):
                iid = None
            if iid is None:
                continue
            try:
                dst_conn.execute(
                    "INSERT OR REPLACE INTO watch_events "
                    "(item_id, server_id, user_handle, section_key, view_count, view_offset, "
                    " last_viewed_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        iid, server_id, user_handle, int(section_key),
                        int(rec.get("view_count") or 0),
                        int(rec.get("view_offset") or 0),
                        rec.get("last_viewed_at"),
                        captured_at_resolved,
                    ),
                )
                counters["watch_events"] += 1
            except sqlite3.OperationalError:
                continue

    def _write_ratings(user_handle: str, records: List[Dict[str, Any]],
                       section_key: int) -> None:
        if "ratings" not in wanted_metric_tables:
            return
        for rec in records or []:
            rk = rec.get("rating_key")
            if rk is None:
                continue
            try:
                iid = rk_to_item_id.get(str(rk).strip())
                rating_val = float(rec.get("rating") or rec.get("user_rating") or 0.0)
            except (TypeError, ValueError):
                continue
            if iid is None:
                continue
            # is_favorite is the backend-neutral favorite face of the
            # affinity record. None when the capturing backend has no
            # favorite concept (Plex); 0/1 for Jellyfin/Emby.
            _fav = rec.get("is_favorite")
            fav_val = None if _fav is None else (1 if _fav else 0)
            try:
                dst_conn.execute(
                    "INSERT OR REPLACE INTO ratings "
                    "(item_id, server_id, user_handle, section_key, rating, "
                    " is_favorite, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (iid, server_id, user_handle, int(section_key),
                     rating_val, fav_val, captured_at_resolved),
                )
                counters["ratings"] += 1
            except sqlite3.OperationalError:
                continue

    def _write_playlist_row(user_handle: str, pl: Dict[str, Any],
                            section_key: int) -> None:
        if "playlists" not in wanted_metric_tables:
            return
        name = str(pl.get("name") or pl.get("title") or "")
        if not name:
            return
        item_ids = _resolve_members(pl.get("items") or [])
        try:
            dst_conn.execute(
                "INSERT OR REPLACE INTO playlists "
                "(server_id, user_handle, section_key, name, description, is_smart, "
                " smart_filter_json, item_ids_json, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    server_id, user_handle, int(section_key), name,
                    pl.get("description"),
                    1 if bool(pl.get("smart")) else 0,
                    pl.get("smart_content"),
                    json.dumps(item_ids),
                    captured_at_resolved,
                ),
            )
            counters["playlists"] += 1
        except sqlite3.OperationalError:
            pass

    def _write_collection_row(user_handle: str, col: Dict[str, Any],
                              section_key: int) -> None:
        if "collections" not in wanted_metric_tables:
            return
        name = str(col.get("name") or col.get("title") or "")
        if not name:
            return
        item_ids = _resolve_members(col.get("items") or [])
        try:
            dst_conn.execute(
                "INSERT OR REPLACE INTO collections "
                "(server_id, user_handle, section_key, name, item_ids_json, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    server_id, user_handle, int(section_key), name,
                    json.dumps(item_ids),
                    captured_at_resolved,
                ),
            )
            counters["collections"] += 1
        except sqlite3.OperationalError:
            pass

    def _write_server_user_row(user_handle: str, role: str,
                               display_name: Optional[str],
                               backend: str = "plex",
                               backend_user_id: Optional[str] = None,
                               app_user_uuid: Optional[str] = None) -> None:
        """Insert a server_users identity row into the snapshot .db so
        the on-demand serializer can rebuild a payload with proper role
        + display_name fields. INSERT OR IGNORE is safe because the
        UNIQUE(server_id, user_handle) constraint prevents dup rows.

        ``app_user_uuid`` is populated from media.db at capture time;
        the column has existed on the cloned DDL since media.db v12.
        When the caller doesn't supply ``app_user_uuid``, the helper
        looks up the canonical value from media.db's managed_users
        first, then server_users. Best-effort: a NULL result lands as
        a NULL column and the downstream resolver falls through to
        backend_user_id / username matching."""
        resolved_app_uuid = app_user_uuid
        if resolved_app_uuid is None:
            try:
                row = src_conn.execute(
                    "SELECT app_user_uuid FROM managed_users "
                    "WHERE server_id = ? AND username = ?",
                    (server_id, user_handle),
                ).fetchone()
                if row is not None and row["app_user_uuid"]:
                    resolved_app_uuid = row["app_user_uuid"]
                else:
                    row = src_conn.execute(
                        "SELECT app_user_uuid FROM server_users "
                        "WHERE server_id = ? AND user_handle = ?",
                        (server_id, user_handle),
                    ).fetchone()
                    if row is not None and row["app_user_uuid"]:
                        resolved_app_uuid = row["app_user_uuid"]
            except sqlite3.OperationalError:
                pass
        try:
            dst_conn.execute(
                "INSERT OR IGNORE INTO server_users "
                "(server_id, user_handle, display_name, role, backend, "
                " backend_user_id, app_user_uuid, created_at, last_seen_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (server_id, user_handle, display_name, role, backend,
                 backend_user_id, resolved_app_uuid,
                 captured_at_resolved, captured_at_resolved),
            )
        except sqlite3.OperationalError:
            # Older schema in the destination snapshot DB (shouldn't
            # happen if create_snapshot_db copied the current media.db
            # DDL, but harmless if it does).
            pass

    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        # Every payload carries its library identity. The pre-pass
        # above validated + wrote the dimension row; here we extract
        # the section_key for forwarding into every per-row write.
        sec_id_int, _, _ = _payload_section_info(payload)
        owner_block = _find_owner_block(payload)
        # Owner identity row + owner / server-wide records under
        # user_handle="" (the legacy DB sentinel for server-owner
        # data).
        if owner_block:
            _write_server_user_row(
                "", "owner",
                owner_block.get("display_name"),
                backend_user_id=owner_block.get("backend_user_id"),
            )
        _write_watch_events("", owner_block.get("watch_history") or [], sec_id_int)
        _write_ratings("", owner_block.get("ratings") or [], sec_id_int)
        for pl in (owner_block.get("playlists") or []):
            _write_playlist_row("", pl, sec_id_int)
        for col in (owner_block.get("collections") or []):
            _write_collection_row("", col, sec_id_int)

        # Per-user blocks: dedup playlists / collections against the
        # owner-side rating_key sets so a library-wide entry doesn't
        # land N times. Skip the owner block we already processed.
        for handle, udata in (payload.get("users") or {}).items():
            if not isinstance(udata, dict) or not handle:
                continue
            if udata.get("role") == "owner":
                continue
            _write_server_user_row(
                handle, "managed",
                udata.get("display_name") or handle,
                backend_user_id=udata.get("backend_user_id"),
            )
            _write_watch_events(handle, udata.get("watch_history") or [], sec_id_int)
            _write_ratings(handle, udata.get("ratings") or [], sec_id_int)
            for pl in (udata.get("playlists") or []):
                rk = pl.get("rating_key")
                if rk is not None:
                    try:
                        if int(rk) in owner_playlist_keys:
                            continue
                    except (TypeError, ValueError):
                        pass
                _write_playlist_row(handle, pl, sec_id_int)
            for col in (udata.get("collections") or []):
                rk = col.get("rating_key")
                if rk is not None:
                    try:
                        if int(rk) in owner_collection_keys:
                            continue
                    except (TypeError, ValueError):
                        pass
                _write_collection_row(handle, col, sec_id_int)

    # 4. Meta tables (snapshot_meta + snapshot_users).
    # Pass the user_count for the description summary. We count the
    # user_display_names dict entries (the union of owner + every
    # managed user that produced data in this capture).
    _phase_d_user_count = len(user_display_names or {})
    _write_snapshot_meta(
        dst_conn,
        snapshot_id=snapshot_id or "",
        server_id=server_id,
        server_name=server_name,
        captured_at=captured_at_resolved,
        libraries=list(libraries or []),
        metrics=list(metrics) if metrics is not None else list(_METRIC_TO_TABLE.keys()),
        created_by=created_by,
        user_count=_phase_d_user_count,
    )
    _write_snapshot_users(
        dst_conn,
        metrics=metrics,
        user_display_names=user_display_names or {},
        server_id=server_id,
        src_conn=src_conn,
    )

    # Matching COMMIT for the BEGIN above. dst_conn.commit() is
    # a no-op in autocommit mode (every prior statement already committed
    # individually) - the explicit COMMIT is what flushes the WAL once
    # for the whole row-write phase.
    #
    # Defensive: only issue COMMIT when a transaction is actually open.
    # If anything mid-flight ran a DDL or called .commit() internally,
    # the transaction may have closed early. Issuing COMMIT against an
    # autocommit connection raises ``OperationalError: cannot commit -
    # no transaction is active`` and the prior writes would be lost to
    # the end user behind a confusing error. The right writers are
    # commit-free (see _write_snapshot_meta / _write_snapshot_users)
    # but this guard makes a future regression visible as a warning
    # instead of a hard fail.
    if dst_conn.in_transaction:
        dst_conn.execute("COMMIT")
    else:
        log.warning(
            "snapshot .db write completed without an active transaction - "
            "an intermediate writer closed the BEGIN block early. Rows were "
            "still written (autocommit fallback) but the WAL fsync didn't "
            "happen as one batch."
        )
    dst_conn.close()

    counters["file_size"] = int(snapshot_path.stat().st_size)

    # Audit trail: this is the live-capture writer and mirrors the
    # audit line from ``create_snapshot_db`` so the snapshot .db
    # creation shows up in db_access.log next to the media.db ingest
    # and the snapshots.db registry insert.
    try:
        from services.run_logs import db_access as db_access_log
        _row_total = (
            counters.get("server_items", 0)
            + counters.get("items", 0)
            + counters.get("watch_events", 0)
            + counters.get("ratings", 0)
            + counters.get("playlists", 0)
            + counters.get("collections", 0)
        )
        db_access_log.log_write(
            table="snapshot.db",
            where={
                "snapshot_db": snapshot_path.name,
                "server_id": server_id,
                "snapshot_id": snapshot_id or "",
            },
            affected_rows=_row_total,
            intent=(
                f"write per-server snapshot .db from payloads "
                f"(file_size={counters.get('file_size', 0)} bytes, "
                f"libraries={len(libraries or [])}, "
                f"metrics={','.join(metrics) if metrics else 'all'})"
            ),
        )
    except Exception:
        pass

    # Post-capture integrity validation. Gated by the
    # ``validate_snapshot_after_capture`` setting (default ON). Runs
    # the structural validator against a temp copy of the
    # freshly-written .db. Errors abort the capture by raising
    # RuntimeError; warnings are logged but do not abort.
    try:
        from services import snapshot_validator
        if snapshot_validator.is_after_capture_enabled():
            report = snapshot_validator.validate_snapshot(snapshot_path)
            log.info(
                "post-capture validation: %s",
                report.format_summary_line(),
            )
            for issue in report.warnings:
                log.warning(
                    "snapshot validation warning [%s @ %s]: %s",
                    issue.code, issue.where or "-", issue.message,
                )
            if not report.ok:
                err_lines = [
                    f"[{i.code} @ {i.where or '-'}] {i.message}"
                    for i in report.errors
                ]
                raise RuntimeError(
                    "Snapshot integrity check failed after capture. "
                    "The .db was written but is structurally invalid; "
                    "the job will be marked failed so a bad artefact "
                    "is not stored. Errors:\n  - "
                    + "\n  - ".join(err_lines)
                )
    except RuntimeError:
        # Real validation failure; surface to the caller so the job
        # record gets the error.
        raise
    except Exception:
        # Validator itself crashed (e.g. snapshot_validator import
        # broken). Log loudly but DO NOT block the capture: the
        # snapshot file is still on disk and a future re-validation
        # via the dev tool can catch any issue. The capture path
        # shouldn't fail because of a telemetry / introspection bug.
        log.exception(
            "post-capture validator crashed unexpectedly; the snapshot "
            "was still written and will be returned. Re-validate via "
            "the Developer tool if you need a structural check."
        )

    return counters


def count_snapshot_users(snapshot_db_path: Any) -> Dict[str, int]:
    """
    Read user counts from the freshly-written snapshot's
    ``snapshot_users`` table. Returns
    ``{"total": M, "with_data": N}`` where ``total`` is every row
    (owner + every roster managed user) and ``with_data`` is the
    subset with ``had_data=1`` (users who appear in at least one
    populated metric table).

    Counts come from this snapshot's own ``snapshot_users`` table, not
    from ``media.db``: the owner must be included, and ``media.db`` is
    the cumulative cross-snapshot store rather than this snapshot's
    own contents, so under the default
    ``cache_snapshot_payloads_to_media_db=False`` setting it could be
    empty or hold data from a different snapshot.
    """
    try:
        conn = sqlite3.connect(
            f"file:{snapshot_db_path}?mode=ro", uri=True, timeout=5.0,
        )
    except sqlite3.OperationalError:
        return {"total": 0, "with_data": 0}
    try:
        try:
            row = conn.execute(
                "SELECT "
                "  COUNT(*) AS total, "
                "  SUM(CASE WHEN had_data = 1 THEN 1 ELSE 0 END) AS with_data "
                "FROM snapshot_users"
            ).fetchone()
        except sqlite3.OperationalError:
            # Older snapshot without the had_data column - fall back
            # to a simple row count and assume every row had data
            # (legacy snapshots only ever wrote with-data rows).
            row = conn.execute(
                "SELECT COUNT(*) AS total, COUNT(*) AS with_data FROM snapshot_users"
            ).fetchone()
        if row is None:
            return {"total": 0, "with_data": 0}
        return {
            "total": int(row[0] or 0),
            "with_data": int(row[1] or 0),
        }
    except sqlite3.OperationalError:
        return {"total": 0, "with_data": 0}
    finally:
        conn.close()
