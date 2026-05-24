"""media.db connection, schema bootstrap, and server-row lifecycle.

The base layer of the media_db package: owns the process-wide SQLite
connection and the write lock, runs migrations on boot, and answers
schema-version / stats queries. Every other media_db sub-module imports
``_require_conn`` / ``_DB_LOCK`` (and, where needed, ``log``) from here.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

from server.persistence import get_data_dir

from ._schema import _MIGRATIONS


log = logging.getLogger("plexmigrate.server.media_db")


# ── File location ────────────────────────────────────────────────────────────

_DB_NAME = "media.db"


def _db_path() -> Path:
    return get_data_dir() / _DB_NAME


# ── Connection / lock singletons ─────────────────────────────────────────────
#
# One connection per process with ``check_same_thread=False`` so it
# can be reused across the engine worker pool, the FastAPI request
# threads, and the fan-out destination threads. Writers serialise on
# ``_DB_LOCK``; readers go straight to the connection (WAL guarantees
# they see a consistent snapshot even mid-write).

_DB_LOCK = threading.Lock()
_conn: Optional[sqlite3.Connection] = None
_init_lock = threading.Lock()
_initialised = False


# ── Schema version contract ──────────────────────────────────────────────────
#
# CURRENT_SCHEMA_VERSION is the AUTO-ARCHIVE FLOOR - NOT the latest
# schema version. The boot path in server/app.py reads the version of
# any existing media.db BEFORE migrations run and archives the file
# only when its version is BELOW this constant (see
# ``server.app._archive_old_media_db_if_needed``); a database at or
# above it is migrated forward in place by the runner instead.
#
# So this is intentionally LOWER than the highest entry in
# ``_MIGRATIONS`` (which the runner always migrates a kept database
# up to). Bump it only when a schema break is so significant that
# forward migration is infeasible and an old database must be
# discarded - adding a nullable column does not qualify; adding a
# NOT NULL anchor column does. Raising this value auto-archives every
# end user's database below the new number on their next start, so
# treat a bump as a destructive change.
CURRENT_SCHEMA_VERSION = 8


# Sentinel value for section_key on rows written before this build
# could populate library identity. Restore-time validation refuses to
# act on rows where section_key == _UNKNOWN_SECTION_KEY. In production
# the auto-archive ensures no row ever carries this value, but tests
# and edge cases can detect it explicitly.
_UNKNOWN_SECTION_KEY = 0


# ── Init + migration ─────────────────────────────────────────────────────────

def init_media_db() -> None:
    """
    Open the shared connection, enable WAL, apply pending migrations.
    Idempotent - safe to call from FastAPI's startup hook even if a
    test has already initialised the DB.

    The startup contract is: ``init_media_db()`` is called once per
    process before any other public API in this module. The first
    call performs the heavy lift (file creation, journal mode,
    migrations). Subsequent calls return immediately.
    """
    global _conn, _initialised
    with _init_lock:
        if _initialised:
            return
        path = _db_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            str(path),
            timeout=30.0,
            isolation_level=None,         # autocommit; we manage tx explicitly
            check_same_thread=False,      # shared across worker / FastAPI threads
        )
        conn.row_factory = sqlite3.Row
        # M14: restrict media.db (and its WAL sidecars) to owner-only.
        # The file carries every server's watch history, ratings, and
        # playlist membership. M16: chmod is POSIX-only; on Windows the
        # data-directory ACL is the real boundary and must be locked
        # down separately. Best-effort - never block startup on chmod.
        for _p in (path, path.with_name(path.name + "-wal"),
                   path.with_name(path.name + "-shm")):
            try:
                if _p.exists():
                    os.chmod(_p, 0o600)
            except OSError:
                pass
        # WAL once set persists across reopens, so this is a no-op
        # after first boot. ``synchronous=NORMAL`` is the recommended
        # pairing with WAL - keeps fsync on commit but skips the
        # extra fsync FULL would do, big win for write-heavy paths.
        # WAL-with-fallback. Docker Desktop / Windows bind mounts
        # can't always support WAL's shared-memory file;
        # set_journal_mode falls back to DELETE so the engine boots.
        from server._sqlite_journal import set_journal_mode
        set_journal_mode(conn, db_label="media.db")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        _apply_migrations(conn)
        _conn = conn
        _initialised = True
        log.info("media.db initialised at %s (schema v%d)",
                 path, get_schema_version())
    # Best-effort backfill of any (server, user) row missing its
    # app_user_uuid. Runs OUTSIDE _init_lock so server_registry's own
    # lock can't deadlock against ours. Idempotent: every subsequent
    # call no-ops on installs where every row already carries a UUID.
    try:
        from .identity import _backfill_app_user_uuids
        _backfill_app_user_uuids()
    except Exception:
        log.exception(
            "app_user_uuid backfill failed at init; rows will be filled "
            "on next sync. Resolution paths fall back to handle matching "
            "until that completes."
        )


def _apply_migrations(conn: sqlite3.Connection) -> None:
    """
    Run every migration whose version is higher than the
    ``schema_version`` table's current maximum. Each migration runs
    inside a single transaction so a partial failure leaves the
    schema at its previous version.

    The first-ever migration creates the ``schema_version`` table
    itself, so we have to handle the chicken/egg case where the
    table doesn't exist yet.
    """
    try:
        row = conn.execute(
            "SELECT MAX(version) AS v FROM schema_version"
        ).fetchone()
        current = (row["v"] if row and row["v"] is not None else 0)
    except sqlite3.OperationalError:
        # Fresh database - the schema_version table doesn't exist yet.
        current = 0

    for version, script in _MIGRATIONS:
        if version <= current:
            continue
        # ``executescript`` issues an implicit COMMIT and ignores
        # isolation_level, so the only way to run a multi-statement
        # migration atomically is to embed BEGIN / COMMIT in the
        # script itself. A statement failing mid-script then leaves the
        # BEGIN transaction open; the rollback below undoes every
        # partial DDL statement (SQLite DDL is transactional). The
        # version-bump rides inside the same transaction, so a
        # half-applied migration can never be recorded as complete.
        atomic = (
            "BEGIN;\n"
            f"{script}\n"
            "INSERT INTO schema_version (version, applied_at) "
            f"VALUES ({int(version)}, {time.time()!r});\n"
            "COMMIT;\n"
        )
        try:
            conn.executescript(atomic)
            log.info("Applied media.db migration v%d", version)
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise


def _require_conn() -> sqlite3.Connection:
    """Return the shared connection, raising if init never ran."""
    if _conn is None:
        raise RuntimeError(
            "media_db.init_media_db() must be called before any other "
            "public API in this module."
        )
    return _conn


# ── Public read API ──────────────────────────────────────────────────────────

def get_schema_version() -> int:
    """
    Return the highest applied schema version. Useful for the stats
    endpoint and for diagnostic dumps.
    """
    conn = _require_conn()
    try:
        row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
        return int(row["v"] or 0)
    except sqlite3.OperationalError:
        return 0


def get_stats() -> Dict[str, Any]:
    """
    Return a JSON-safe snapshot of database health: per-table row
    counts, schema version, the on-disk byte size of the DB file, and
    the highest ``updated_at`` across content tables (so end users
    can spot a stale DB).
    """
    conn = _require_conn()
    counts: Dict[str, int] = {}
    last_updated: Dict[str, Optional[float]] = {}
    # The table names below are f-string-interpolated because SQLite
    # cannot bind an identifier - only values. This is safe ONLY
    # because every name comes from the hardcoded literal tuple in this
    # loop; never extend this to accept a caller-supplied table name.
    for table in ("items", "watch_events", "ratings", "playlists", "collections", "servers"):
        try:
            row = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
            counts[table] = int(row["n"] or 0)
        except sqlite3.OperationalError as exc:
            # A table that errors on COUNT(*) is sick (missing /
            # corrupt / locked), not empty. Report 0 for the response
            # shape but log loudly so the zero is not mistaken for an
            # empty table.
            log.warning("get_stats: COUNT(*) on table %r failed: %s", table, exc)
            counts[table] = 0
        # Only content tables have an updated_at column.
        if table != "servers":
            try:
                row = conn.execute(
                    f"SELECT MAX(updated_at) AS t FROM {table}"
                ).fetchone()
                last_updated[table] = float(row["t"]) if row and row["t"] is not None else None
            except sqlite3.OperationalError as exc:
                log.warning(
                    "get_stats: MAX(updated_at) on table %r failed: %s",
                    table, exc,
                )
                last_updated[table] = None

    size_bytes = 0
    try:
        size_bytes = _db_path().stat().st_size
    except OSError:
        pass

    return {
        "schema_version": get_schema_version(),
        "row_counts": counts,
        "last_updated_at": last_updated,
        "size_bytes": size_bytes,
        "path": str(_db_path()),
    }


def upsert_server_row(
    *,
    server_id: str,
    name: str,
    url: str,
    service: str = "plex",
    machine_id: Optional[str] = None,
) -> None:
    """
    Mirror a server registry entry into the DB. Lets the DB
    self-describe its server set without forcing a join against the
    JSON registry every time a stats query runs. ``service`` defaults
    to ``"plex"`` so Feature 4's Jellyfin/Emby support drops in
    without a migration.
    """
    conn = _require_conn()
    now = time.time()
    with _DB_LOCK:
        conn.execute(
            """
            INSERT INTO servers (id, name, service, url, machine_id, added_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name       = excluded.name,
                service    = excluded.service,
                url        = excluded.url,
                machine_id = COALESCE(excluded.machine_id, servers.machine_id)
            """,
            (server_id, name, service, url, machine_id, now),
        )


def resolve_registry_id(server_id_or_machine_id: str) -> Optional[str]:
    """
    Return the canonical ``servers.id`` (registry id) for a given
    identifier. Accepts either a registry id or a backend-native
    machine identifier (Plex ``machineIdentifier``, Jellyfin / Emby
    ``System.Id``) and converts at the boundary so per-server tables
    keyed on ``server_id`` (``server_items`` etc.) always see the
    same single canonical key.

    Resolution order:

    1. If the input already matches a ``servers.id``, return it
       verbatim (idempotent for callers that already hold a registry
       id).
    2. Otherwise look the input up against ``servers.machine_id`` and
       return the matching ``servers.id``.
    3. Return ``None`` when neither match - the caller treats this as
       "no mapping yet" (e.g. first run before the registry row was
       upserted).

    Pure read, no writes. Safe to call before ``upsert_server_row``
    has populated the row for the current run - the caller is
    responsible for falling back gracefully when ``None`` comes back.
    """
    if not server_id_or_machine_id:
        return None
    conn = _require_conn()
    row = conn.execute(
        "SELECT id FROM servers WHERE id = ? LIMIT 1",
        (server_id_or_machine_id,),
    ).fetchone()
    if row is not None:
        return str(row["id"])
    row = conn.execute(
        "SELECT id FROM servers WHERE machine_id = ? LIMIT 1",
        (server_id_or_machine_id,),
    ).fetchone()
    if row is not None:
        return str(row["id"])
    return None


def purge_server_data(server_id: str) -> Dict[str, int]:
    """
    Delete every per-server row in media.db for ``server_id`` and
    return per-table counts for logging / display.

    Tables purged (all ``WHERE server_id = ?``):

        watch_events, ratings, playlists, collections, server_users,
        server_items, servers

    Order matters: the wide tables reference ``server_users`` via
    ``server_user_id`` FK, so they have to be cleared before the
    server_users rows they point at. The list below is in
    purge-safe order.

    The ``items`` table is NOT pruned. Items are GUID-keyed and may
    be shared across multiple servers - the resolver's Tier-0 cache
    for OTHER servers would lose its referent rows if we touched it.

    Called from two places:

      * ``server_registry.remove_server`` when the end user deletes a
        server with ``cascade_delete_on_server_remove = true`` and
        ``prevent_cascade_delete = false``.
      * The "Clear media.db data" UI action in the Servers panel -
        end user-explicit request, runs regardless of the cascade
        setting.

    Both sites wrap this call in a try/except so a failed purge
    surfaces to the end user rather than being swallowed.

    Audit
    -----
    Every successful purge emits one line to ``db_access.log`` with
    per-table counts so the audit trail mirrors every other media.db
    write path.
    """
    if not server_id:
        raise ValueError("server_id is required")
    conn = _require_conn()
    counts: Dict[str, int] = {}
    tables = (
        "watch_events",
        "ratings",
        "playlists",
        "collections",
        "server_users",
        "server_items",
        "servers",
    )
    with _DB_LOCK:
        for t in tables:
            # ``servers`` is keyed by ``id``, not ``server_id``.
            where_col = "id" if t == "servers" else "server_id"
            try:
                cur = conn.execute(
                    f"DELETE FROM {t} WHERE {where_col} = ?", (server_id,),
                )
                counts[t] = cur.rowcount
            except sqlite3.OperationalError as exc:
                # Table missing on older schema - treat as zero rows
                # affected rather than failing the whole purge.
                log.warning("purge_server_data: skipped %r (%s)", t, exc)
                counts[t] = 0

    # Audit-log the purge. Best-effort: a log failure must not break
    # the purge itself.
    try:
        from services import db_access_log
        kv_summary = " ".join(f"{k}={v}" for k, v in counts.items())
        db_access_log.log_event(
            "purge_server_data server_id=%s %s", server_id, kv_summary,
        )
    except Exception:  # pragma: no cover (defensive)
        log.exception("db_access_log emit failed for purge_server_data")
    return counts


def _close_for_tests() -> None:
    """
    Close the shared connection so a test that swaps the data dir
    can recreate the DB from scratch. Not called by production code.
    """
    global _conn, _initialised
    with _init_lock:
        if _conn is not None:
            try:
                _conn.close()
            except Exception:
                pass
        _conn = None
        _initialised = False
