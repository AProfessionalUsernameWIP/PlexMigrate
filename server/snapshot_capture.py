"""
PR-13 - Snapshot capture. Materialises a per-server SQLite snapshot
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
  out of the snapshot entirely. PR-11's sync re-populates after a
  restore.
* ``global_tombstones``  - operator-only UI state; not data.
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
from typing import Any, Dict, List, Optional


log = logging.getLogger("plexmigrate.server.snapshot_capture")


# Per-server tables: rows are filtered by server_id.
_PER_SERVER_TABLES = (
    "server_items",
    "watch_events",
    "ratings",
    "playlists",
    "collections",
)

# Mapping from the operator-facing metric label ("watch_history" etc.)
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
    # PR-13 isolation fix: every parameter below is required for the
    # snapshot to be self-describing AND scope-correct. Defaults exist
    # so existing callers (orphan reconcile, ad-hoc tooling) keep
    # working, but the job runner now supplies all of them.
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
    ``metrics`` is the operator-requested subset of
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
        # previous capture failed mid-write or the operator picked
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
    dst_conn.execute("PRAGMA journal_mode=WAL")
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
        )
        _write_snapshot_users(
            dst_conn,
            metrics=metrics,
            user_display_names=user_display_names or {},
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
        from services import db_access_log
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
    ``metrics`` is the operator-requested subset of
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

    # ── servers (one row) ───────────────────────────────────────────────
    cur = dst.execute(
        "INSERT INTO servers SELECT * FROM src.servers WHERE id = ?",
        (server_id,),
    )
    counters["servers"] = cur.rowcount

    # ── items: only those referenced by this server's server_items ──────
    cur = dst.execute(
        "INSERT INTO items "
        "SELECT * FROM src.items WHERE id IN ("
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
            cur = dst.execute(
                f"INSERT INTO {table} SELECT * FROM src.{table} WHERE server_id = ?",
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
    Create ``snapshot_meta`` + ``snapshot_users`` in the destination.
    Always runs - both tables are part of every snapshot's schema
    regardless of which metrics were captured. Idempotent CREATE IF
    NOT EXISTS so a recovered orphan getting re-stamped is harmless.
    """
    dst.executescript("""
        CREATE TABLE IF NOT EXISTS snapshot_meta (
            snapshot_id     TEXT NOT NULL,
            server_id       TEXT NOT NULL,
            server_name     TEXT NOT NULL,
            captured_at     REAL NOT NULL,
            libraries_json  TEXT NOT NULL,
            metrics_json    TEXT NOT NULL,
            created_by      TEXT
        );
        CREATE TABLE IF NOT EXISTS snapshot_users (
            user_handle     TEXT PRIMARY KEY,
            display_name    TEXT,
            is_owner        INTEGER NOT NULL DEFAULT 0
        );
    """)
    dst.commit()


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
) -> None:
    """
    Insert the single ``snapshot_meta`` row. Re-runnable: if a row
    already exists (re-stamping a recovered orphan), the existing
    row is replaced with the freshly-supplied values.
    """
    dst.execute("DELETE FROM snapshot_meta")
    dst.execute(
        """
        INSERT INTO snapshot_meta (
            snapshot_id, server_id, server_name, captured_at,
            libraries_json, metrics_json, created_by
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            snapshot_id, server_id, server_name, captured_at,
            json.dumps(libraries), json.dumps(metrics), created_by,
        ),
    )
    dst.commit()


def _write_snapshot_users(
    dst: sqlite3.Connection,
    *,
    metrics: Optional[List[str]],
    user_display_names: Dict[str, str],
) -> None:
    """
    Populate ``snapshot_users`` from the DISTINCT ``user_handle``
    values found in the per-server metric tables that were actually
    populated by ``_copy_rows``. Zero-activity users (handles present
    on the source server but absent from every populated table) are
    never inserted here.

    Display name is filled from the operator-supplied map when
    available; missing entries land as NULL. ``is_owner`` is true
    iff the handle is the empty string (``""``), which is the
    project convention for server-owner data across every metric
    table.
    """
    # Determine which tables we actually populated. ``metrics=None``
    # means "all of them" (legacy / recovery path); otherwise only
    # the operator-requested subset.
    if metrics is None:
        populated_tables = list(_METRIC_TABLES)
    else:
        populated_tables = [
            _METRIC_TO_TABLE[m] for m in metrics if m in _METRIC_TO_TABLE
        ]

    if not populated_tables:
        # Nothing to derive from - leave snapshot_users empty.
        dst.commit()
        return

    # UNION across only the populated tables. Empty strings (owner)
    # are valid handles and are preserved through the DISTINCT.
    union_parts = [
        f"SELECT user_handle FROM {t}"
        for t in populated_tables
    ]
    union_sql = " UNION ".join(union_parts)
    handles = {
        (r[0] or "") for r in dst.execute(
            f"SELECT DISTINCT user_handle FROM ({union_sql})"
        ).fetchall()
    }

    for handle in handles:
        display = user_display_names.get(handle) or None
        is_owner = 1 if handle == "" else 0
        dst.execute(
            """
            INSERT OR REPLACE INTO snapshot_users
                (user_handle, display_name, is_owner)
            VALUES (?, ?, ?)
            """,
            (handle, display, is_owner),
        )
    dst.commit()


# ── Summary helper for the registry row ─────────────────────────────────────

def summarise_libraries(server_id: str) -> List[str]:
    """
    Best-effort: return the distinct ``library`` names known for this
    server. Used to populate the registry row so the panel can show
    "3 libraries captured" without parsing the .db file again.

    media.db today doesn't carry an explicit ``library`` column on
    ``items`` (PR-13 won't add one), so we return an empty list
    rather than guess. Callers will replace this with whatever the
    capture pipeline knows from the engine context (per-library JSON
    filenames, for example).
    """
    return []


# ── Payload-direct builder (Rule 1) ─────────────────────────────────────────
#
# ``build_snapshot_db_from_payloads`` is the post-Rule-1 snapshot writer:
# it consumes the in-memory list of per-library ``export_data`` dicts
# that ``services.snapshotter.snapshot_library`` appends to
# ``state._snapshot_payloads`` and writes the .db directly from that
# data. media.db is never read; the snapshot file is a point-in-time
# record of what the live-fetch payload reported.
#
# ``create_snapshot_db`` above is kept for the orphan-reconcile path
# (rebuilding a missing snapshot.db from media.db when a registry row
# exists but the file is gone) - that's the one legitimate place where
# reading from media.db is correct. Live snapshot capture must use the
# new path.

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

    # 1. Get schema DDL from media.db (RO). We only read sqlite_master;
    #    no row data crosses the boundary.
    from server import media_db as _media_db
    src_path = _media_db._db_path()
    if not src_path.is_file():
        raise RuntimeError(
            f"media.db not found at {src_path}; cannot borrow snapshot schema."
        )
    src_uri = f"file:{src_path}?mode=ro"
    src_conn = sqlite3.connect(src_uri, uri=True, timeout=30.0)
    src_conn.row_factory = sqlite3.Row

    dst_conn = sqlite3.connect(str(snapshot_path), timeout=30.0, isolation_level=None)
    dst_conn.execute("PRAGMA journal_mode=WAL")
    dst_conn.execute("PRAGMA synchronous=NORMAL")
    dst_conn.row_factory = sqlite3.Row

    try:
        _copy_schema(src_conn, dst_conn)
        _create_meta_tables(dst_conn)
    finally:
        src_conn.close()

    # v0.13.x: wrap every row write below in a single explicit
    # transaction. Pre-fix, the connection was opened with
    # isolation_level=None (autocommit) so every per-row INSERT was its
    # own transaction with its own fsync. On a multi-library /
    # multi-user snapshot that meant tens-to-hundreds of thousands of
    # one-row commits and dominated the post-engine 6-8 minute hang
    # the operator saw between "libraries 100%" and the job actually
    # finishing. One BEGIN / COMMIT pair collapses the whole write
    # phase into a single fsync at the end.
    #
    # Schema DDL above runs OUTSIDE the transaction on purpose: SQLite
    # implicitly commits any active transaction before executing DDL,
    # so opening BEGIN before _copy_schema / _create_meta_tables would
    # be a no-op anyway. We start the transaction immediately after.
    dst_conn.execute("BEGIN")

    # 2. Write the one-row ``servers`` entry. Pull the live row from
    #    media.db so url / machine_id stay consistent with the
    #    registry. Falls back to a minimal row when no media.db entry
    #    exists yet (very early first-run edge case).
    try:
        src_conn = sqlite3.connect(src_uri, uri=True, timeout=10.0)
        src_conn.row_factory = sqlite3.Row
        row = src_conn.execute(
            "SELECT * FROM servers WHERE id = ?", (server_id,),
        ).fetchone()
        src_conn.close()
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
    from services.guid_translator import normalize_guids
    _GUID_COL = {
        "imdb": "imdb_id",
        "tmdb": "tmdb_id",
        "tvdb": "tvdb_id",
        "musicbrainz": "musicbrainz_id",
        "plex": "plex_guid",
    }
    items_by_guid: Dict[str, int] = {}              # canonical guid → items.id
    server_items_seen: set = set()                  # (item_id, rating_key) dedup
    rk_to_item_id: Dict[int, int] = {}              # ratingKey → items.id
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
        cur = dst_conn.execute(
            "INSERT INTO items ("
            "imdb_id, tmdb_id, tvdb_id, musicbrainz_id, plex_guid, "
            "title, media_type, year, filepath_suffix, "
            "created_at, updated_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                cols["imdb_id"], cols["tmdb_id"], cols["tvdb_id"],
                cols["musicbrainz_id"], cols["plex_guid"],
                title, media_type, year, filepath,
                captured_at_resolved, captured_at_resolved,
            ),
        )
        iid = int(cur.lastrowid)
        counters["items"] += 1
        for g in canon:
            items_by_guid.setdefault(g, iid)
        return iid

    def _record_server_item(item_id: int, rating_key: Any) -> None:
        if rating_key is None:
            return
        try:
            rk_int = int(rating_key)
        except (TypeError, ValueError):
            return
        key = (item_id, rk_int)
        if key in server_items_seen:
            return
        try:
            dst_conn.execute(
                "INSERT OR REPLACE INTO server_items "
                "(item_id, server_id, rating_key, updated_at) "
                "VALUES (?, ?, ?, ?)",
                (item_id, server_id, rk_int, captured_at_resolved),
            )
            server_items_seen.add(key)
            rk_to_item_id[rk_int] = item_id
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
                    iid = rk_to_item_id.get(int(rk))
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

    # ── Walk every payload ──────────────────────────────────────────
    # Pass 1: ingest every record into items + server_items so the
    # rating_key → items.id map is fully populated before we resolve
    # playlist / collection memberships in Pass 2.
    def _walk_item_records(block: Dict[str, Any]) -> None:
        for rec in (block.get("watch_history") or []):
            iid = _upsert_item(rec)
            if iid is not None:
                _record_server_item(iid, rec.get("rating_key"))
        for rec in (block.get("ratings") or []):
            iid = _upsert_item(rec)
            if iid is not None:
                _record_server_item(iid, rec.get("rating_key"))
        for pl in (block.get("playlists") or []):
            for rec in (pl.get("items") or []):
                iid = _upsert_item(rec)
                if iid is not None:
                    _record_server_item(iid, rec.get("rating_key"))
        for col in (block.get("collections") or []):
            for rec in (col.get("items") or []):
                iid = _upsert_item(rec)
                if iid is not None:
                    _record_server_item(iid, rec.get("rating_key"))

    # v0.13.0: unified users map. Owner is the role='owner' block;
    # the legacy empty-handle convention is the fallback for mid-
    # transition payloads. One pass over the users map handles both.
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        for udata in (payload.get("users") or {}).values():
            if isinstance(udata, dict):
                _walk_item_records(udata)

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

    def _write_watch_events(user_handle: str, records: List[Dict[str, Any]]) -> None:
        if "watch_events" not in wanted_metric_tables:
            return
        for rec in records or []:
            rk = rec.get("rating_key")
            if rk is None:
                continue
            try:
                iid = rk_to_item_id.get(int(rk))
            except (TypeError, ValueError):
                iid = None
            if iid is None:
                continue
            try:
                dst_conn.execute(
                    "INSERT OR REPLACE INTO watch_events "
                    "(item_id, server_id, user_handle, view_count, view_offset, "
                    " last_viewed_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        iid, server_id, user_handle,
                        int(rec.get("view_count") or 0),
                        int(rec.get("view_offset") or 0),
                        rec.get("last_viewed_at"),
                        captured_at_resolved,
                    ),
                )
                counters["watch_events"] += 1
            except sqlite3.OperationalError:
                continue

    def _write_ratings(user_handle: str, records: List[Dict[str, Any]]) -> None:
        if "ratings" not in wanted_metric_tables:
            return
        for rec in records or []:
            rk = rec.get("rating_key")
            if rk is None:
                continue
            try:
                iid = rk_to_item_id.get(int(rk))
                rating_val = float(rec.get("rating") or rec.get("user_rating") or 0.0)
            except (TypeError, ValueError):
                continue
            if iid is None:
                continue
            try:
                dst_conn.execute(
                    "INSERT OR REPLACE INTO ratings "
                    "(item_id, server_id, user_handle, rating, updated_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (iid, server_id, user_handle, rating_val, captured_at_resolved),
                )
                counters["ratings"] += 1
            except sqlite3.OperationalError:
                continue

    def _write_playlist_row(user_handle: str, pl: Dict[str, Any]) -> None:
        if "playlists" not in wanted_metric_tables:
            return
        name = str(pl.get("name") or pl.get("title") or "")
        if not name:
            return
        item_ids = _resolve_members(pl.get("items") or [])
        try:
            dst_conn.execute(
                "INSERT OR REPLACE INTO playlists "
                "(server_id, user_handle, name, description, is_smart, "
                " smart_filter_json, item_ids_json, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    server_id, user_handle, name,
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

    def _write_collection_row(user_handle: str, col: Dict[str, Any]) -> None:
        if "collections" not in wanted_metric_tables:
            return
        name = str(col.get("name") or col.get("title") or "")
        if not name:
            return
        item_ids = _resolve_members(col.get("items") or [])
        try:
            dst_conn.execute(
                "INSERT OR REPLACE INTO collections "
                "(server_id, user_handle, name, item_ids_json, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    server_id, user_handle, name,
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
                               backend_user_id: Optional[str] = None) -> None:
        """Insert a server_users identity row into the snapshot .db so
        the on-demand serializer can rebuild a payload with proper role
        + display_name fields. INSERT OR IGNORE is safe because the
        UNIQUE(server_id, user_handle) constraint prevents dup rows."""
        try:
            dst_conn.execute(
                "INSERT OR IGNORE INTO server_users "
                "(server_id, user_handle, display_name, role, backend, "
                " backend_user_id, created_at, last_seen_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (server_id, user_handle, display_name, role, backend,
                 backend_user_id, captured_at_resolved, captured_at_resolved),
            )
        except sqlite3.OperationalError:
            # Pre-v0.13.0 schema in the destination snapshot DB (shouldn't
            # happen if create_snapshot_db copied today's media.db DDL,
            # but harmless if it does).
            pass

    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        owner_block = _find_owner_block(payload)
        # Owner identity row + owner / server-wide records under
        # user_handle="" (the legacy DB sentinel kept for one release
        # while the FK column propagates).
        if owner_block:
            _write_server_user_row(
                "", "owner",
                owner_block.get("display_name"),
                backend_user_id=owner_block.get("backend_user_id"),
            )
        _write_watch_events("", owner_block.get("watch_history") or [])
        _write_ratings("", owner_block.get("ratings") or [])
        for pl in (owner_block.get("playlists") or []):
            _write_playlist_row("", pl)
        for col in (owner_block.get("collections") or []):
            _write_collection_row("", col)

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
            _write_watch_events(handle, udata.get("watch_history") or [])
            _write_ratings(handle, udata.get("ratings") or [])
            for pl in (udata.get("playlists") or []):
                rk = pl.get("rating_key")
                if rk is not None:
                    try:
                        if int(rk) in owner_playlist_keys:
                            continue
                    except (TypeError, ValueError):
                        pass
                _write_playlist_row(handle, pl)
            for col in (udata.get("collections") or []):
                rk = col.get("rating_key")
                if rk is not None:
                    try:
                        if int(rk) in owner_collection_keys:
                            continue
                    except (TypeError, ValueError):
                        pass
                _write_collection_row(handle, col)

    # 4. Meta tables (snapshot_meta + snapshot_users).
    _write_snapshot_meta(
        dst_conn,
        snapshot_id=snapshot_id or "",
        server_id=server_id,
        server_name=server_name,
        captured_at=captured_at_resolved,
        libraries=list(libraries or []),
        metrics=list(metrics) if metrics is not None else list(_METRIC_TO_TABLE.keys()),
        created_by=created_by,
    )
    _write_snapshot_users(
        dst_conn,
        metrics=metrics,
        user_display_names=user_display_names or {},
    )

    # v0.13.x: matching COMMIT for the BEGIN above. dst_conn.commit() is
    # a no-op in autocommit mode (every prior statement already committed
    # individually) - the explicit COMMIT is what flushes the WAL once
    # for the whole row-write phase.
    dst_conn.execute("COMMIT")
    dst_conn.close()

    counters["file_size"] = int(snapshot_path.stat().st_size)

    # Audit trail: this is the live-capture writer (post-Rule-1) and
    # mirrors the audit line from ``create_snapshot_db`` so the snapshot
    # .db creation shows up in db_access.log next to the media.db
    # ingest and the snapshots.db registry insert.
    try:
        from services import db_access_log
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

    return counters


def count_distinct_users(server_id: str) -> int:
    """
    Count distinct ``user_handle`` values across the per-server
    tables for this server. The DB doesn't enumerate users directly;
    this is an approximation good enough for the panel's "5 users"
    metadata.
    """
    from server import media_db
    conn = sqlite3.connect(
        f"file:{media_db._db_path()}?mode=ro", uri=True, timeout=10.0,
    )
    try:
        row = conn.execute(
            "SELECT COUNT(DISTINCT user_handle) AS n FROM ("
            "  SELECT user_handle FROM watch_events WHERE server_id = ? AND user_handle != '' "
            "  UNION "
            "  SELECT user_handle FROM ratings WHERE server_id = ? AND user_handle != '' "
            "  UNION "
            "  SELECT user_handle FROM playlists WHERE server_id = ? AND user_handle != '' "
            "  UNION "
            "  SELECT user_handle FROM collections WHERE server_id = ? AND user_handle != '' "
            ")",
            (server_id, server_id, server_id, server_id),
        ).fetchone()
        return int(row[0] if row else 0)
    finally:
        conn.close()
