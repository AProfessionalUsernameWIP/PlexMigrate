"""
Media-state database (v0.12.0 - Feature 3 foundation).

Roadmap reference: Part 6 of ``roadmapplan4.md``.

This is the start of the "SQLite as primary, JSON as snapshot format"
shift. v0.12.0 ships the **schema, migration scaffolding, and public
API** so the database can be created, opened, queried, and stats can
be surfaced via ``GET /api/db/stats``. The engine integration that
makes the DB the primary write path (snapshotter writes here, importer
reads here first, resolver tier 0) is staged for v0.12.1 - landing it
in the same release that ships the data layer would conflate "is the
DB correct" with "is the engine refactor correct" and make any
regression hard to bisect.

Schema philosophy
-----------------
* **Items are keyed by upstream metadata IDs** (IMDb, TVDB, TMDB,
  MusicBrainz) - *not* Plex's ``ratingKey``. The ratingKey is
  per-server and per-install; the upstream id is the same on every
  Plex server, and any future Jellyfin / Emby target. This
  is what makes cross-server and (later) cross-service matching
  efficient.

* **One ``items`` row per logical entity**, regardless of how many
  Plex servers have that item. Per-server / per-user state lives in
  the side tables (``watch_events``, ``ratings``, ``playlists``,
  ``collections``) keyed by ``(item_id, server_id, user_handle)``.

* **All GUIDs are normalised** by
  :func:`services.guid_translator.normalize_guid` before they hit the
  database so a legacy ``com.plexapp.agents.thetvdb://121361?lang=en``
  and a modern ``tvdb://121361`` collapse to the same row.

* **``schema_version`` table** + ordered migrations let us evolve
  the schema without losing data. Adding a column is a new migration
  that bumps the version one notch.

Concurrency
-----------
* WAL journal mode is enabled at DB-creation time. Readers don't
  block writers and vice-versa - important during a fan-out where
  multiple destination threads update the DB while the WS broadcaster
  reads stats.
* Writes acquire :data:`_DB_LOCK`. Reads do not - WAL handles read
  isolation at the SQLite level. Same pattern as
  :mod:`server.server_registry`'s `_REG_LOCK`.
* A single shared connection per process (created in
  :func:`init_media_db`) with ``check_same_thread=False``. Cheap
  per-call cursors run inside the lock for writes, lock-free for
  reads.

Threat model
------------
v0.12.0-v0.12.1 held **media-state metadata only** - what's been
watched, what playlists exist, item rating values. PR-10 introduces
a ``managed_users`` table that DOES hold sensitive credentials
(per-user Plex tokens, Plex Home PINs, Emby/Jellyfin passwords).
Every credential cell is Fernet-encrypted via
:mod:`server.secrets` before write, matching the pattern
``server_registry.py`` already uses for server tokens. The DB file
(``server_data/media.db``) shares the bind-mount with the existing
JSON artefacts; the file-level access posture is the same as
``servers.json`` and ``schedules.json``. Compromise of the data
volume requires compromise of ``.keyfile`` too before any credential
can be decrypted.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from server.persistence import get_data_dir
from services.guid_translator import normalize_guid, normalize_guids


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


# ── Migration table ─────────────────────────────────────────────────────────
#
# Migrations are applied in order. Each entry is
# ``(version, sql_script)``. Adding a new migration means appending a
# new tuple - never edit a shipped migration. ``_apply_migrations``
# stops at the highest applied version on each boot.

_MIGRATIONS: List[Tuple[int, str]] = [
    (1, """
        CREATE TABLE IF NOT EXISTS schema_version (
            version    INTEGER PRIMARY KEY,
            applied_at REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS servers (
            id          TEXT PRIMARY KEY,
            name        TEXT NOT NULL,
            service     TEXT NOT NULL DEFAULT 'plex',
            url         TEXT NOT NULL,
            machine_id  TEXT,
            added_at    REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS items (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            imdb_id         TEXT,
            tmdb_id         TEXT,
            tvdb_id         TEXT,
            musicbrainz_id  TEXT,
            plex_guid       TEXT,
            title           TEXT NOT NULL,
            media_type      TEXT NOT NULL,
            year            INTEGER,
            filepath_suffix TEXT,
            created_at      REAL,
            updated_at      REAL
        );
        CREATE INDEX IF NOT EXISTS idx_items_imdb   ON items(imdb_id);
        CREATE INDEX IF NOT EXISTS idx_items_tmdb   ON items(tmdb_id);
        CREATE INDEX IF NOT EXISTS idx_items_tvdb   ON items(tvdb_id);
        CREATE INDEX IF NOT EXISTS idx_items_mb     ON items(musicbrainz_id);
        CREATE INDEX IF NOT EXISTS idx_items_suffix ON items(filepath_suffix);

        CREATE TABLE IF NOT EXISTS watch_events (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id        INTEGER NOT NULL REFERENCES items(id),
            server_id      TEXT NOT NULL,
            user_handle    TEXT NOT NULL DEFAULT '',
            view_count     INTEGER NOT NULL DEFAULT 0,
            view_offset    INTEGER NOT NULL DEFAULT 0,
            last_viewed_at REAL,
            updated_at     REAL NOT NULL,
            UNIQUE(item_id, server_id, user_handle)
        );
        CREATE INDEX IF NOT EXISTS idx_watch_server_user
            ON watch_events(server_id, user_handle);

        CREATE TABLE IF NOT EXISTS playlists (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            server_id         TEXT NOT NULL,
            user_handle       TEXT NOT NULL DEFAULT '',
            name              TEXT NOT NULL,
            description       TEXT,
            is_smart          INTEGER NOT NULL DEFAULT 0,
            smart_filter_json TEXT,
            item_ids_json     TEXT,
            updated_at        REAL NOT NULL,
            UNIQUE(server_id, user_handle, name)
        );
        CREATE INDEX IF NOT EXISTS idx_playlists_server_user
            ON playlists(server_id, user_handle);

        CREATE TABLE IF NOT EXISTS ratings (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id     INTEGER NOT NULL REFERENCES items(id),
            server_id   TEXT NOT NULL,
            user_handle TEXT NOT NULL DEFAULT '',
            rating      REAL NOT NULL,
            updated_at  REAL NOT NULL,
            UNIQUE(item_id, server_id, user_handle)
        );
        CREATE INDEX IF NOT EXISTS idx_ratings_server_user
            ON ratings(server_id, user_handle);

        CREATE TABLE IF NOT EXISTS collections (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            server_id     TEXT NOT NULL,
            user_handle   TEXT NOT NULL DEFAULT '',
            name          TEXT NOT NULL,
            item_ids_json TEXT,
            updated_at    REAL NOT NULL,
            UNIQUE(server_id, user_handle, name)
        );
        CREATE INDEX IF NOT EXISTS idx_collections_server_user
            ON collections(server_id, user_handle);
    """),
    # v0.12.1 - server_items join table. Maps the service-agnostic
    # ``items.id`` to the per-server, per-install Plex ``ratingKey``.
    # This is what makes resolver Tier-0 useful: once we've seen an
    # item on a server, we can skip the slow ``getByGuid`` round-trip
    # on subsequent runs and go straight to ``fetchItem(ratingKey)``.
    #
    # ``UNIQUE(item_id, server_id)`` so a re-snapshot just updates the
    # row rather than accumulating duplicates. The reverse index on
    # ``(server_id, rating_key)`` lets the resolver invert the lookup
    # when an engine path has the ratingKey and needs the items.id.
    (2, """
        CREATE TABLE IF NOT EXISTS server_items (
            item_id     INTEGER NOT NULL REFERENCES items(id),
            server_id   TEXT NOT NULL,
            rating_key  INTEGER NOT NULL,
            updated_at  REAL NOT NULL,
            UNIQUE(item_id, server_id),
            UNIQUE(server_id, rating_key)
        );
        CREATE INDEX IF NOT EXISTS idx_server_items_server
            ON server_items(server_id);
    """),
    # PR-10 - managed_users table. One row per (server_id, username).
    # Credential cells (auth_token_enc, plex_home_pin_enc,
    # service_password_enc) are Fernet ciphertext written via
    # server.secrets.encrypt_str; they are NULL/empty when no credential
    # has been stored. Metadata cells (display_name, service_type,
    # machine_identifier, last_seen) are plaintext.
    #
    # service_type defaults to 'plex' so the live-API sync helper can
    # upsert a row without knowing the service yet; the CHECK
    # constraint keeps the column to the three values the rest of the
    # app actually knows how to talk to.
    (3, """
        CREATE TABLE IF NOT EXISTS managed_users (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            server_id             TEXT NOT NULL,
            username              TEXT NOT NULL,
            display_name          TEXT,
            service_type          TEXT NOT NULL DEFAULT 'plex'
                                  CHECK (service_type IN ('plex', 'emby', 'jellyfin')),
            machine_identifier    TEXT,
            auth_token_enc        TEXT,
            plex_home_pin_enc     TEXT,
            service_password_enc  TEXT,
            last_seen             REAL,
            created_at            REAL NOT NULL,
            updated_at            REAL NOT NULL,
            UNIQUE(server_id, username)
        );
        CREATE INDEX IF NOT EXISTS idx_managed_users_server
            ON managed_users(server_id);
    """),
    # PR-11 - add ``kind`` column so the JobFormPanel direct-transfer
    # picker can render its existing Owner / Managed badge after
    # switching from live-API reads to DB reads. SQLite ALTER TABLE
    # ADD COLUMN accepts a CHECK constraint when the DEFAULT value
    # satisfies it for every existing row - 'managed' is the safe
    # default for the rows PR-10 may have already written before this
    # migration lands.
    (4, """
        ALTER TABLE managed_users
            ADD COLUMN kind TEXT NOT NULL DEFAULT 'managed'
                CHECK (kind IN ('owner', 'managed'));
    """),
    # PR-11.1 - tombstones. Per-server tombstone is a flag on the
    # managed_users row (creds preserved across hide/unhide, the
    # sync helper never resets it). Global tombstones are a separate
    # tiny table keyed by username only: a globally-tombstoned
    # username is never upserted into managed_users, so the row
    # disappears across every server until the operator unhides
    # globally.
    (5, """
        ALTER TABLE managed_users
            ADD COLUMN tombstoned INTEGER NOT NULL DEFAULT 0;

        CREATE TABLE IF NOT EXISTS global_tombstones (
            username       TEXT PRIMARY KEY,
            tombstoned_at  REAL NOT NULL
        );
    """),
    # Rule 2 - per-server item sighting timestamp + provenance for the
    # background library-walk job that produces it.
    #
    # ``server_items.last_seen_at`` is the column the library-walk job
    # touches every time it confirms an item is still present on the
    # source server. The resolver / ingest paths keep updating
    # ``server_items.updated_at`` as before; ``last_seen_at`` is
    # specifically the "server scan confirmed me" timestamp - distinct
    # because an item with no metric activity (no watch, no rating,
    # not in any playlist or collection) would never see its
    # ``updated_at`` move and would falsely look stale.
    #
    # ``library_walks`` records each walk's outcome: which server,
    # when it started, when it finished, how many items were ticked,
    # whether the walk completed or aborted. The Prune Missing Items
    # UI reads this so it can warn "no walk in the last 7 days -
    # results may be stale" before letting the operator press the
    # destructive button.
    #
    # ``items.last_seen_at`` exists too so an item visible on ANY
    # server tracked by this install still has a recency signal. The
    # walk job updates BOTH columns: the per-(item,server) row in
    # server_items and the global high-water mark on items.
    (6, """
        ALTER TABLE items
            ADD COLUMN last_seen_at REAL;
        ALTER TABLE server_items
            ADD COLUMN last_seen_at REAL;
        CREATE INDEX IF NOT EXISTS idx_items_last_seen
            ON items(last_seen_at);
        CREATE INDEX IF NOT EXISTS idx_server_items_last_seen
            ON server_items(server_id, last_seen_at);

        CREATE TABLE IF NOT EXISTS library_walks (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            server_id       TEXT NOT NULL,
            started_at      REAL NOT NULL,
            finished_at     REAL,
            status          TEXT NOT NULL DEFAULT 'running'
                            CHECK (status IN ('running', 'completed', 'failed', 'cancelled')),
            items_seen      INTEGER NOT NULL DEFAULT 0,
            libraries_seen  INTEGER NOT NULL DEFAULT 0,
            error_message   TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_library_walks_server_started
            ON library_walks(server_id, started_at DESC);
    """),
    # v0.13.0 - server_users identity table + FK migration.
    #
    # Replaces the ``user_handle = ''`` owner sentinel with a real
    # per-server user record. The owner becomes a row with
    # ``role = 'owner'``; managed users get ``role = 'managed'``. The
    # design is multi-backend up-front: future Jellyfin/Emby adapters
    # will write rows with ``backend = 'jellyfin'`` / ``'emby'`` and
    # the engine code reads them through one uniform API.
    #
    # This migration is ADDITIVE. The ``user_handle`` columns on the
    # wide tables (watch_events / ratings / playlists / collections)
    # stay in place as a denormalized fallback while every caller
    # switches to the FK over the next few commits. A later migration
    # drops them once nothing reads them.
    #
    # Backfill semantics:
    #   * Every distinct (server_id, user_handle) tuple already present
    #     in any wide table gets a server_users row.
    #   * user_handle = '' -> role = 'owner'; everything else
    #     -> role = 'managed'.
    #   * display_name / backend_user_id are left NULL; the next
    #     snapshot or library-walk run upserts those via
    #     ``get_or_create_server_user`` which knows the live names.
    (7, """
        CREATE TABLE IF NOT EXISTS server_users (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            server_id        TEXT NOT NULL,
            user_handle      TEXT NOT NULL,
            display_name     TEXT,
            role             TEXT NOT NULL
                             CHECK (role IN ('owner', 'managed')),
            backend          TEXT NOT NULL DEFAULT 'plex'
                             CHECK (backend IN ('plex', 'emby', 'jellyfin')),
            backend_user_id  TEXT,
            created_at       REAL NOT NULL,
            last_seen_at     REAL,
            UNIQUE(server_id, user_handle)
        );
        CREATE INDEX IF NOT EXISTS idx_server_users_server
            ON server_users(server_id);
        CREATE INDEX IF NOT EXISTS idx_server_users_role
            ON server_users(role);

        ALTER TABLE watch_events ADD COLUMN server_user_id INTEGER
            REFERENCES server_users(id);
        ALTER TABLE ratings      ADD COLUMN server_user_id INTEGER
            REFERENCES server_users(id);
        ALTER TABLE playlists    ADD COLUMN server_user_id INTEGER
            REFERENCES server_users(id);
        ALTER TABLE collections  ADD COLUMN server_user_id INTEGER
            REFERENCES server_users(id);

        CREATE INDEX IF NOT EXISTS idx_watch_events_server_user_fk
            ON watch_events(server_user_id);
        CREATE INDEX IF NOT EXISTS idx_ratings_server_user_fk
            ON ratings(server_user_id);
        CREATE INDEX IF NOT EXISTS idx_playlists_server_user_fk
            ON playlists(server_user_id);
        CREATE INDEX IF NOT EXISTS idx_collections_server_user_fk
            ON collections(server_user_id);

        -- Backfill server_users from every (server_id, user_handle)
        -- tuple already present in the wide tables. INSERT OR IGNORE
        -- so a tuple seen in multiple tables only produces one row.
        INSERT OR IGNORE INTO server_users
            (server_id, user_handle, role, backend, created_at)
        SELECT DISTINCT server_id, user_handle,
               CASE WHEN user_handle = '' THEN 'owner' ELSE 'managed' END,
               'plex',
               CAST(strftime('%s', 'now') AS REAL)
        FROM (
            SELECT server_id, user_handle FROM watch_events
            UNION
            SELECT server_id, user_handle FROM ratings
            UNION
            SELECT server_id, user_handle FROM playlists
            UNION
            SELECT server_id, user_handle FROM collections
        );

        -- Point each wide-table row at its server_users row. The
        -- correlated subquery matches on the (server_id, user_handle)
        -- UNIQUE so each row gets exactly one id back.
        UPDATE watch_events SET server_user_id = (
            SELECT su.id FROM server_users su
            WHERE su.server_id = watch_events.server_id
              AND su.user_handle = watch_events.user_handle
        ) WHERE server_user_id IS NULL;

        UPDATE ratings SET server_user_id = (
            SELECT su.id FROM server_users su
            WHERE su.server_id = ratings.server_id
              AND su.user_handle = ratings.user_handle
        ) WHERE server_user_id IS NULL;

        UPDATE playlists SET server_user_id = (
            SELECT su.id FROM server_users su
            WHERE su.server_id = playlists.server_id
              AND su.user_handle = playlists.user_handle
        ) WHERE server_user_id IS NULL;

        UPDATE collections SET server_user_id = (
            SELECT su.id FROM server_users su
            WHERE su.server_id = collections.server_id
              AND su.user_handle = collections.user_handle
        ) WHERE server_user_id IS NULL;
    """),
    # v0.15 - library section identity as the integrity anchor for
    # snapshot/restore. See module docstring for the
    # invariant. ``library_sections`` is the per-server dimension
    # table; every per-server row in server_items / watch_events /
    # ratings / playlists / collections carries ``section_key``
    # referencing it.
    #
    # No legacy data fits this schema - a pre-v0.15 media.db with
    # existing rows would hit the DEFAULT 0 sentinel on ALTER. The
    # boot path in server/app.py auto-archives any media.db at
    # schema_version < 8 BEFORE this migration runs, so the ALTER
    # always operates on empty tables. ``DEFAULT 0`` is just there
    # to satisfy SQLite's ALTER TABLE ADD COLUMN NOT NULL syntactic
    # requirement; the 0 value is treated as the "invalid / pre-v0.15"
    # sentinel by every read path and never legitimately appears.
    #
    # Foreign keys: NOT declared at the SQLite level on media.db.
    # ``library_sections`` has a composite primary key (server_id,
    # section_key); SQLite requires a single-column FK target to be
    # UNIQUE on its own, and section_key alone is not (the same
    # numeric key can legitimately appear on multiple servers). An
    # ALTER TABLE ADD COLUMN also cannot syntactically declare a
    # multi-column FK, so the only way to get DB-level FK enforcement
    # on media.db would be to recreate every per-server table - too
    # invasive for the gain. Enforcement lives in Python:
    # ``ingest_snapshot_payload`` upserts the library_sections row
    # before any per-server-table write, and ``record_server_item`` /
    # ``record_watch_event`` / ``upsert_rating`` / ``upsert_playlist`` /
    # ``upsert_collection`` each raise ``ValueError`` on a missing or
    # zero section_key. The snapshot .db file IS FK-enforced (its
    # parallel library_sections table has a single-column PK and
    # ``PRAGMA foreign_keys=ON``), so the on-disk artefact stays
    # atomically correct even if a future regression slipped past the
    # Python guards.
    #
    # See ``databasechanges.md`` for the rationale and the
    # pre-release decision to drop the broken REFERENCES clause that
    # was originally in this migration.
    (8, """
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

        ALTER TABLE server_items
            ADD COLUMN section_key INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE watch_events
            ADD COLUMN section_key INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE ratings
            ADD COLUMN section_key INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE playlists
            ADD COLUMN section_key INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE collections
            ADD COLUMN section_key INTEGER NOT NULL DEFAULT 0;

        CREATE INDEX IF NOT EXISTS idx_server_items_section
            ON server_items(server_id, section_key);
        CREATE INDEX IF NOT EXISTS idx_watch_events_section
            ON watch_events(server_id, section_key);
        CREATE INDEX IF NOT EXISTS idx_ratings_section
            ON ratings(server_id, section_key);
        CREATE INDEX IF NOT EXISTS idx_playlists_section
            ON playlists(server_id, section_key);
        CREATE INDEX IF NOT EXISTS idx_collections_section
            ON collections(server_id, section_key);
    """),
]


# ── Schema version contract ──────────────────────────────────────────────────
#
# CURRENT_SCHEMA_VERSION is the version this build expects. The boot
# path in server/app.py reads the version of any existing media.db
# BEFORE migrations run and auto-archives older databases (see
# ``server.app._archive_old_media_db_if_needed``). After that, the
# migration runner brings a freshly-created DB up to this version.
#
# Bumping this constant is the trigger for the auto-archive behaviour
# on every operator's next start. Change it only when the schema
# break is significant enough that backfill is infeasible - adding a
# nullable column doesn't require a bump; adding a NOT NULL anchor
# column does.
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
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        _apply_migrations(conn)
        _conn = conn
        _initialised = True
        log.info("media.db initialised at %s (schema v%d)",
                 path, get_schema_version())


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
        # ``executescript`` issues an implicit COMMIT before running and
        # parses BEGIN/COMMIT tokens literally inside the script, so
        # the cleanest atomic wrapper is to temporarily switch the
        # connection out of autocommit, run the DDL + the version
        # bump as one explicit transaction, then restore.
        prior_isolation = conn.isolation_level
        conn.isolation_level = ""  # deferred - auto-BEGIN on next write
        try:
            conn.executescript(script)
            conn.execute(
                "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                (version, time.time()),
            )
            conn.commit()
            log.info("Applied media.db migration v%d", version)
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        finally:
            conn.isolation_level = prior_isolation


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
    the highest ``updated_at`` across content tables (so operators
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
        except sqlite3.OperationalError:
            counts[table] = 0
        # Only content tables have an updated_at column.
        if table != "servers":
            try:
                row = conn.execute(
                    f"SELECT MAX(updated_at) AS t FROM {table}"
                ).fetchone()
                last_updated[table] = float(row["t"]) if row and row["t"] is not None else None
            except sqlite3.OperationalError:
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


def lookup_item_by_guids(guids: List[str]) -> Optional[int]:
    """
    Find an existing ``items.id`` whose GUID columns match any of the
    provided GUIDs. Returns ``None`` if no match.

    Input GUIDs are normalised via :func:`normalize_guids` before
    lookup so a legacy-format input still matches a row written from
    the modern-format equivalent. Search order follows GUID priority:
    IMDb / TMDB / TVDB (movie + show metadata) then MusicBrainz
    (music tracks). The first match wins - duplicate-GUID rows
    shouldn't exist by construction (``upsert_item`` ensures it).
    """
    conn = _require_conn()
    canon = normalize_guids(guids)
    if not canon:
        return None
    for g in canon:
        prefix, _, payload = g.partition("://")
        if not payload:
            continue
        col = {
            "imdb": "imdb_id",
            "tmdb": "tmdb_id",
            "tvdb": "tvdb_id",
            "musicbrainz": "musicbrainz_id",
            "plex": "plex_guid",
        }.get(prefix)
        if col is None:
            continue
        row = conn.execute(
            f"SELECT id FROM items WHERE {col} = ? LIMIT 1", (payload,)
        ).fetchone()
        if row is not None:
            return int(row["id"])
    return None


# ── Public write API ─────────────────────────────────────────────────────────

def upsert_item(
    *,
    guids: List[str],
    title: str,
    media_type: str,
    year: Optional[int] = None,
    filepath_suffix: Optional[str] = None,
) -> int:
    """
    Insert or update one item row, returning ``items.id``.

    Identity is by GUID: if any of the normalised input GUIDs already
    matches an ``items`` row, that row is updated in place; otherwise
    a new row is inserted. The unique GUID columns receive the
    payload-only portion (``tt0133093`` rather than
    ``imdb://tt0133093``) so SQL comparisons stay fast and the
    ``UNIQUE`` constraints in future migrations can be added without
    a re-write.

    Writes acquire :data:`_DB_LOCK`.
    """
    if not title:
        raise ValueError("title is required for upsert_item")
    if not media_type:
        raise ValueError("media_type is required for upsert_item")

    canon = normalize_guids(guids)
    cols: Dict[str, Optional[str]] = {
        "imdb_id": None, "tmdb_id": None, "tvdb_id": None,
        "musicbrainz_id": None, "plex_guid": None,
    }
    for g in canon:
        prefix, _, payload = g.partition("://")
        if not payload:
            continue
        mapped = {
            "imdb": "imdb_id",
            "tmdb": "tmdb_id",
            "tvdb": "tvdb_id",
            "musicbrainz": "musicbrainz_id",
            "plex": "plex_guid",
        }.get(prefix)
        if mapped is not None and cols[mapped] is None:
            cols[mapped] = payload

    conn = _require_conn()
    now = time.time()
    with _DB_LOCK:
        existing_id = lookup_item_by_guids(canon)
        if existing_id is not None:
            conn.execute(
                """
                UPDATE items SET
                    imdb_id        = COALESCE(?, imdb_id),
                    tmdb_id        = COALESCE(?, tmdb_id),
                    tvdb_id        = COALESCE(?, tvdb_id),
                    musicbrainz_id = COALESCE(?, musicbrainz_id),
                    plex_guid      = COALESCE(?, plex_guid),
                    title          = ?,
                    media_type     = ?,
                    year           = COALESCE(?, year),
                    filepath_suffix= COALESCE(?, filepath_suffix),
                    updated_at     = ?
                WHERE id = ?
                """,
                (
                    cols["imdb_id"], cols["tmdb_id"], cols["tvdb_id"],
                    cols["musicbrainz_id"], cols["plex_guid"],
                    title, media_type, year, filepath_suffix,
                    now, existing_id,
                ),
            )
            return existing_id
        cur = conn.execute(
            """
            INSERT INTO items (
                imdb_id, tmdb_id, tvdb_id, musicbrainz_id, plex_guid,
                title, media_type, year, filepath_suffix,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                cols["imdb_id"], cols["tmdb_id"], cols["tvdb_id"],
                cols["musicbrainz_id"], cols["plex_guid"],
                title, media_type, year, filepath_suffix,
                now, now,
            ),
        )
        return int(cur.lastrowid)


def get_or_create_server_user(
    *,
    server_id: str,
    user_handle: str,
    role: Optional[str] = None,
    display_name: Optional[str] = None,
    backend: str = "plex",
    backend_user_id: Optional[str] = None,
) -> int:
    """
    Return the ``server_users.id`` for ``(server_id, user_handle)``,
    creating the row on first encounter.

    Role defaulting: when ``role`` is None, an empty ``user_handle``
    infers ``'owner'`` (matches the legacy sentinel) and any other
    handle infers ``'managed'``. Callers that already know the role
    (the snapshotter, the live-API sync helpers) should pass it
    explicitly so a future engine that decides to give the owner a
    real handle still labels it correctly.

    ``display_name`` / ``backend_user_id`` are LWW on conflict (a
    non-NULL incoming value overwrites; a NULL leaves the prior value
    alone). ``role`` is fixed at INSERT time and never updated on
    conflict - changing a user's role is a separate, explicit op,
    not a side effect of upserting their watch history. ``backend``
    similarly stays at its first-write value.

    The CHECK constraints on the table reject unknown roles and
    backends, so a typo here surfaces as an IntegrityError rather
    than a silently malformed row.
    """
    handle = user_handle or ""
    inferred_role = role or ("owner" if handle == "" else "managed")
    conn = _require_conn()
    now = time.time()
    with _DB_LOCK:
        conn.execute(
            """
            INSERT INTO server_users (
                server_id, user_handle, display_name, role, backend,
                backend_user_id, created_at, last_seen_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(server_id, user_handle) DO UPDATE SET
                display_name    = COALESCE(excluded.display_name,
                                           server_users.display_name),
                backend_user_id = COALESCE(excluded.backend_user_id,
                                           server_users.backend_user_id),
                last_seen_at    = excluded.last_seen_at
            """,
            (server_id, handle, display_name, inferred_role, backend,
             backend_user_id, now, now),
        )
        row = conn.execute(
            "SELECT id FROM server_users "
            "WHERE server_id = ? AND user_handle = ?",
            (server_id, handle),
        ).fetchone()
        return int(row["id"])


def record_watch_event(
    *,
    item_id: int,
    server_id: str,
    user_handle: str,
    view_count: int,
    section_key: int,
    view_offset: int = 0,
    last_viewed_at: Optional[float] = None,
    role: Optional[str] = None,
    display_name: Optional[str] = None,
    backend_user_id: Optional[str] = None,
) -> None:
    """
    Upsert one ``(item, server, user)`` watch-state row.

    The role / display_name / backend_user_id keyword args are
    forwarded to :func:`get_or_create_server_user`; callers that know
    the user's identity (the snapshotter, direct transfer) pass them
    so the ``server_users`` row gets populated with real data on
    first sight. Callers that only have a handle (legacy code paths
    during the transition) can omit them - the row is created with
    NULL display_name and inferred role.

    ``section_key`` is required (v0.15+). Watch events without library
    identity break restore (items can't be matched to the correct
    destination library); the function refuses to write rather than
    silently produce data that restore will mishandle.
    """
    if not isinstance(section_key, int) or section_key <= 0:
        raise ValueError(
            f"record_watch_event: section_key must be a positive int "
            f"(got {section_key!r}). See v0.15 schema-anchor invariant."
        )
    server_user_id = get_or_create_server_user(
        server_id=server_id,
        user_handle=user_handle,
        role=role,
        display_name=display_name,
        backend_user_id=backend_user_id,
    )
    conn = _require_conn()
    now = time.time()
    with _DB_LOCK:
        conn.execute(
            """
            INSERT INTO watch_events (
                item_id, server_id, user_handle, server_user_id, section_key,
                view_count, view_offset, last_viewed_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(item_id, server_id, user_handle) DO UPDATE SET
                server_user_id = excluded.server_user_id,
                section_key    = excluded.section_key,
                view_count     = excluded.view_count,
                view_offset    = excluded.view_offset,
                last_viewed_at = COALESCE(excluded.last_viewed_at, watch_events.last_viewed_at),
                updated_at     = excluded.updated_at
            """,
            (item_id, server_id, user_handle or "", server_user_id, int(section_key),
             int(view_count), int(view_offset), last_viewed_at, now),
        )


def upsert_rating(
    *,
    item_id: int,
    server_id: str,
    user_handle: str,
    rating: float,
    section_key: int,
    role: Optional[str] = None,
    display_name: Optional[str] = None,
    backend_user_id: Optional[str] = None,
) -> None:
    """Upsert one star-rating row. See :func:`record_watch_event` for the
    role / display_name / backend_user_id forwarding semantics.

    ``section_key`` is required (v0.15+) - see record_watch_event for
    the integrity-anchor rationale.
    """
    if not isinstance(section_key, int) or section_key <= 0:
        raise ValueError(
            f"upsert_rating: section_key must be a positive int "
            f"(got {section_key!r}). See v0.15 schema-anchor invariant."
        )
    server_user_id = get_or_create_server_user(
        server_id=server_id,
        user_handle=user_handle,
        role=role,
        display_name=display_name,
        backend_user_id=backend_user_id,
    )
    conn = _require_conn()
    now = time.time()
    with _DB_LOCK:
        conn.execute(
            """
            INSERT INTO ratings (
                item_id, server_id, user_handle, server_user_id, section_key,
                rating, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(item_id, server_id, user_handle) DO UPDATE SET
                server_user_id = excluded.server_user_id,
                section_key    = excluded.section_key,
                rating         = excluded.rating,
                updated_at     = excluded.updated_at
            """,
            (item_id, server_id, user_handle or "", server_user_id, int(section_key),
             float(rating), now),
        )


def upsert_playlist(
    *,
    server_id: str,
    user_handle: str,
    name: str,
    is_smart: bool,
    smart_filter: Optional[str],
    item_ids: List[int],
    section_key: int,
    description: Optional[str] = None,
) -> int:
    """
    Upsert one playlist row. ``item_ids`` is stored as a JSON array
    in ``item_ids_json`` - denormalised on purpose because playlist
    membership is read as a whole list (never queried by individual
    item-id) and storing ordering matters.

    **Dedup discipline - server-wide vs user-private (v0.12.1).**
    The UNIQUE constraint is ``(server_id, user_handle, name)``, so
    the SAME name CAN appear in multiple rows when scoped to different
    users. To prevent the bug where a server-wide playlist gets
    written N times (once per home user), callers MUST split inputs
    before calling this function:

    * Server-wide playlists are those visible to every user - call
      with ``user_handle=""`` ONCE per playlist. Do not call again
      inside a per-user loop for that same playlist.

    * User-private playlists must be deduplicated against the
      server-wide set **by rating_key** (NOT by name), then written
      with ``user_handle=<username>``. Name-based dedup loses a
      legitimately personal playlist that happens to share a name
      with a server-wide one. The rating_key dedup pattern in
      :func:`server.direct_transfer._gather_users_data` is the
      reference implementation.

    :func:`ingest_snapshot_payload` below applies this discipline
    automatically when callers feed it a full snapshot payload.

    ``section_key`` (v0.15+, required): the primary library section
    this playlist belongs to. Plex audio playlists can technically
    span multiple sections, but we anchor each playlist row to its
    primary section for restore matching. Cross-section membership
    is still preserved through the items list - each member item's
    own section_key in server_items remains accurate.
    """
    if not isinstance(section_key, int) or section_key <= 0:
        raise ValueError(
            f"upsert_playlist: section_key must be a positive int "
            f"(got {section_key!r}). See v0.15 schema-anchor invariant."
        )
    server_user_id = get_or_create_server_user(
        server_id=server_id, user_handle=user_handle,
    )
    conn = _require_conn()
    now = time.time()
    with _DB_LOCK:
        conn.execute(
            """
            INSERT INTO playlists (
                server_id, user_handle, server_user_id, section_key, name, description,
                is_smart, smart_filter_json, item_ids_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(server_id, user_handle, name) DO UPDATE SET
                server_user_id    = excluded.server_user_id,
                section_key       = excluded.section_key,
                description       = excluded.description,
                is_smart          = excluded.is_smart,
                smart_filter_json = excluded.smart_filter_json,
                item_ids_json     = excluded.item_ids_json,
                updated_at        = excluded.updated_at
            """,
            (
                server_id, user_handle or "", server_user_id, int(section_key),
                name, description,
                1 if is_smart else 0,
                smart_filter,
                json.dumps(list(item_ids or [])),
                now,
            ),
        )
        row = conn.execute(
            "SELECT id FROM playlists WHERE server_id = ? AND user_handle = ? AND name = ?",
            (server_id, user_handle or "", name),
        ).fetchone()
        return int(row["id"])


def upsert_collection(
    *,
    server_id: str,
    user_handle: str,
    name: str,
    item_ids: List[int],
    section_key: int,
) -> int:
    """
    Upsert one collection row. Same shape as :func:`upsert_playlist`.

    **Dedup discipline - server-wide vs user-private (v0.12.1).**
    Same rule applies: a server-wide collection is one row with
    ``user_handle=""``; user-private collections (Plex Pass) belong
    under ``user_handle=<username>``. Callers MUST NOT iterate every
    home user and call this with the same library-level collection -
    that produces N duplicate rows (one per user) and corrupts
    cross-user reasoning.

    The correct integration pattern is the one already used by
    :func:`server.direct_transfer._gather_users_data`: capture the
    owner-side collection rating_keys into a set, then for each
    user's collection list, filter out anything whose rating_key
    matches before writing user-scoped rows. Dedup by rating_key
    rather than name - a user can legitimately have a personal
    collection that happens to share a name with a server-wide one.

    :func:`ingest_snapshot_payload` below applies this discipline
    automatically when callers feed it a full snapshot payload.

    ``section_key`` (v0.15+, required): the library section this
    collection belongs to. Plex collections are typically scoped to
    one library (a Movies collection lives in Movies); see
    upsert_playlist for the rationale on per-row anchoring.
    """
    if not isinstance(section_key, int) or section_key <= 0:
        raise ValueError(
            f"upsert_collection: section_key must be a positive int "
            f"(got {section_key!r}). See v0.15 schema-anchor invariant."
        )
    server_user_id = get_or_create_server_user(
        server_id=server_id, user_handle=user_handle,
    )
    conn = _require_conn()
    now = time.time()
    with _DB_LOCK:
        conn.execute(
            """
            INSERT INTO collections (
                server_id, user_handle, server_user_id, section_key, name,
                item_ids_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(server_id, user_handle, name) DO UPDATE SET
                server_user_id = excluded.server_user_id,
                section_key    = excluded.section_key,
                item_ids_json  = excluded.item_ids_json,
                updated_at     = excluded.updated_at
            """,
            (server_id, user_handle or "", server_user_id, int(section_key), name,
             json.dumps(list(item_ids or [])), now),
        )
        row = conn.execute(
            "SELECT id FROM collections WHERE server_id = ? AND user_handle = ? AND name = ?",
            (server_id, user_handle or "", name),
        ).fetchone()
        return int(row["id"])


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

      * ``server_registry.remove_server`` when the operator deletes a
        server with ``cascade_delete_on_server_remove = true`` and
        ``prevent_cascade_delete = false``.
      * The "Clear media.db data" UI action in the Servers panel -
        operator-explicit request, runs regardless of the cascade
        setting.

    Both sites wrap this call in a try/except so a failed purge
    surfaces to the operator rather than being swallowed.

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


# ── Library walk + last_seen tracking (Rule 2) ──────────────────────────────
#
# The library-walk job runs out-of-band (separate from snapshot /
# import / direct-transfer) and exists for one purpose: tick
# ``last_seen_at`` on every (item, server) row whose item is still
# present on the live server. The Prune Missing Items action reads
# those timestamps to identify items the server hasn't reported in
# more than N days - candidates the operator may want to remove.
#
# Critical design notes:
#
# * Auto-deletion is **never** triggered by the walk itself. Absence
#   on a single scan is not proof of permanent removal (library scan
#   could have missed, file could be temporarily offline). The walk
#   *only* refreshes timestamps; pruning is a separate, explicit,
#   operator-initiated action.
# * The walk records its own provenance in ``library_walks`` so the
#   prune UI can show "no walk in last X days, results may be stale"
#   before the operator commits to a destructive sweep.


def start_library_walk(server_id: str) -> int:
    """
    Insert a ``library_walks`` row with status='running' and return
    its primary key. The library-walk job updates this row's
    ``items_seen`` / ``libraries_seen`` as it progresses and stamps
    ``finished_at`` + ``status`` at the end.
    """
    conn = _require_conn()
    now = time.time()
    with _DB_LOCK:
        cur = conn.execute(
            """
            INSERT INTO library_walks (server_id, started_at, status)
            VALUES (?, ?, 'running')
            """,
            (server_id, now),
        )
        return int(cur.lastrowid)


def record_item_sighting(
    *, server_id: str, rating_key: int, walk_at: Optional[float] = None,
) -> Optional[int]:
    """
    Tick ``last_seen_at`` on the (item, server) row for one item the
    library walk just confirmed present on the live server. Returns
    the ``items.id`` that was touched, or ``None`` if no row exists
    for this rating_key (the resolver hasn't cached it yet - the walk
    is purely a refresh pass, never a discovery pass).

    The walk job calls this once per rating_key as it iterates the
    server's library. ``rating_key`` is mandatory because the walk
    enumerates by Plex section, where ratingKey is what python-plexapi
    surfaces.
    """
    conn = _require_conn()
    ts = float(walk_at if walk_at is not None else time.time())
    with _DB_LOCK:
        row = conn.execute(
            "SELECT item_id FROM server_items "
            "WHERE server_id = ? AND rating_key = ? LIMIT 1",
            (server_id, int(rating_key)),
        ).fetchone()
        if row is None:
            return None
        item_id = int(row["item_id"])
        conn.execute(
            "UPDATE server_items SET last_seen_at = ? "
            "WHERE item_id = ? AND server_id = ?",
            (ts, item_id, server_id),
        )
        conn.execute(
            "UPDATE items SET last_seen_at = ? WHERE id = ? "
            # COALESCE keeps the column monotonic - never go backwards.
            "AND (last_seen_at IS NULL OR last_seen_at < ?)",
            (ts, item_id, ts),
        )
        return item_id


def finish_library_walk(
    walk_id: int,
    *,
    status: str,
    items_seen: int,
    libraries_seen: int,
    error_message: Optional[str] = None,
) -> None:
    """
    Mark a walk row finished. ``status`` must be one of
    ``completed`` / ``failed`` / ``cancelled``. ``error_message`` is
    only stored when status != completed.
    """
    if status not in ("completed", "failed", "cancelled"):
        raise ValueError(f"invalid walk status: {status!r}")
    conn = _require_conn()
    with _DB_LOCK:
        conn.execute(
            """
            UPDATE library_walks
            SET finished_at = ?, status = ?, items_seen = ?,
                libraries_seen = ?, error_message = ?
            WHERE id = ?
            """,
            (
                time.time(), status, int(items_seen),
                int(libraries_seen),
                error_message if status != "completed" else None,
                walk_id,
            ),
        )


def list_library_walks(
    server_id: Optional[str] = None,
    limit: int = 20,
) -> List[Dict[str, Any]]:
    """
    Return the most recent walk rows, newest first. Filtered to one
    server when ``server_id`` is given.
    """
    conn = _require_conn()
    if server_id:
        rows = conn.execute(
            """
            SELECT * FROM library_walks
            WHERE server_id = ?
            ORDER BY started_at DESC
            LIMIT ?
            """,
            (server_id, int(limit)),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM library_walks ORDER BY started_at DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
    return [dict(r) for r in rows]


def get_last_walk_summary(server_id: str) -> Optional[Dict[str, Any]]:
    """
    Latest completed walk row for one server, or ``None`` when no
    walk has ever finished. Powers the Servers panel's "Last walked"
    column and the Prune UI's freshness warning.
    """
    conn = _require_conn()
    row = conn.execute(
        """
        SELECT * FROM library_walks
        WHERE server_id = ? AND status = 'completed'
        ORDER BY started_at DESC LIMIT 1
        """,
        (server_id,),
    ).fetchone()
    return dict(row) if row is not None else None


def list_stale_items(
    *,
    server_id: str,
    older_than_seconds: float,
    limit: int = 1000,
) -> List[Dict[str, Any]]:
    """
    Return items on ``server_id`` whose per-server ``last_seen_at``
    is present AND older than ``now - older_than_seconds``.

    Items with ``last_seen_at IS NULL`` are deliberately EXCLUDED: a
    missing timestamp means the library-walk job has never confirmed
    the item one way or the other (the column is only ever set by a
    walk). Treating "never walked" as "stale" would let the very first
    prune - run before any walk has populated timestamps - qualify the
    entire library and destroy all its watch history and ratings.
    Absence of evidence is not evidence of staleness.

    Cap defaults at 1000 so a huge library doesn't dump everything
    into one response. The UI surfaces the truncation explicitly.

    NOTE: this function NEVER deletes. It's a query that powers the
    Prune Missing Items UI's preview list; deletion is a separate
    explicit call to :func:`prune_stale_items`.
    """
    conn = _require_conn()
    cutoff = time.time() - float(older_than_seconds)
    rows = conn.execute(
        """
        SELECT i.id AS item_id, i.title, i.media_type, i.year,
               i.filepath_suffix, si.rating_key, si.last_seen_at
        FROM server_items si
        JOIN items i ON i.id = si.item_id
        WHERE si.server_id = ?
          AND si.last_seen_at IS NOT NULL
          AND si.last_seen_at < ?
        ORDER BY si.last_seen_at ASC, i.title
        LIMIT ?
        """,
        (server_id, cutoff, int(limit)),
    ).fetchall()
    return [dict(r) for r in rows]


def count_stale_items(
    *, server_id: str, older_than_seconds: float,
) -> int:
    """
    Cheap COUNT(*) for the Prune Missing Items confirmation modal.

    Counts only rows with a non-NULL ``last_seen_at`` older than the
    cutoff - a never-walked item (NULL timestamp) is not stale, see
    :func:`list_stale_items` for why.
    """
    conn = _require_conn()
    cutoff = time.time() - float(older_than_seconds)
    row = conn.execute(
        """
        SELECT COUNT(*) AS n
        FROM server_items
        WHERE server_id = ?
          AND last_seen_at IS NOT NULL
          AND last_seen_at < ?
        """,
        (server_id, cutoff),
    ).fetchone()
    return int(row["n"]) if row else 0


def prune_stale_items(
    *,
    server_id: str,
    older_than_seconds: float,
    dry_run: bool = False,
) -> Dict[str, int]:
    """
    Delete every per-server row for items not seen within the cutoff.
    Returns ``{server_items, watch_events, ratings, playlists_touched,
    collections_touched, items_orphaned}``. ``dry_run=True`` returns
    the same shape with the counts that WOULD be deleted but writes
    nothing.

    What gets deleted (operator-confirmed, never automatic):

    * ``server_items`` rows for the stale items on this server. The
      resolver's Tier-0 cache loses these entries for this server
      only. Other servers' caches are untouched.
    * Dependent ``watch_events`` and ``ratings`` rows for these
      (item, server) pairs.
    * Playlist / collection ``item_ids_json`` lists have stale ids
      filtered out (the playlist row itself is kept; just trimmed).

    What is preserved:

    * The ``items`` row itself is preserved when ANY other server
      still references it. Items keyed only by GUID across servers
      stay intact for cross-server matching.
    * Snapshot ``.db`` files are NEVER touched - Rule 3 (snapshots
      are historical records).

    Two H2 data-loss guards (see :func:`list_stale_items`):

    * Never-walked rows (``last_seen_at IS NULL``) are NOT prunable -
      a missing timestamp is not evidence the item is gone.
    * The whole call is refused (non-dry-run) until at least one
      library walk has completed for the server, so a prune run before
      any walk can't silently no-op or, worse, act on partial data.
    """
    conn = _require_conn()
    cutoff = time.time() - float(older_than_seconds)

    # H2: a destructive prune is only meaningful once a library walk
    # has actually populated last_seen_at timestamps. Refuse outright
    # rather than silently pruning nothing, so the operator gets a
    # clear reason instead of a confusing "0 items pruned" result.
    if not dry_run and get_last_walk_summary(server_id) is None:
        raise ValueError(
            "Refusing to prune: no completed library walk exists for this "
            "server. Run a library walk first so 'last seen' timestamps "
            "are populated."
        )

    counters = {
        "server_items": 0, "watch_events": 0, "ratings": 0,
        "playlists_touched": 0, "collections_touched": 0,
        "items_orphaned": 0,
    }

    with _DB_LOCK:
        stale = conn.execute(
            "SELECT item_id FROM server_items "
            "WHERE server_id = ? "
            "  AND last_seen_at IS NOT NULL "
            "  AND last_seen_at < ?",
            (server_id, cutoff),
        ).fetchall()
        stale_ids = {int(r["item_id"]) for r in stale}
        counters["server_items"] = len(stale_ids)
        if not stale_ids:
            return counters

        if dry_run:
            # Counts only.
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM watch_events "
                "WHERE server_id = ? "
                f"  AND item_id IN ({','.join('?' * len(stale_ids))})",
                (server_id, *stale_ids),
            ).fetchone()
            counters["watch_events"] = int(row["n"]) if row else 0
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM ratings "
                "WHERE server_id = ? "
                f"  AND item_id IN ({','.join('?' * len(stale_ids))})",
                (server_id, *stale_ids),
            ).fetchone()
            counters["ratings"] = int(row["n"]) if row else 0
            return counters

        # M12: the prune is several destructive statements on an
        # autocommit connection (isolation_level=None). Without an
        # explicit transaction an exception midway (e.g. a malformed
        # item_ids_json) would leave a partially-applied prune with no
        # rollback. Wrap the whole block so it commits all-or-nothing.
        placeholders = ",".join("?" * len(stale_ids))
        conn.execute("BEGIN")
        try:
            we_cur = conn.execute(
                f"DELETE FROM watch_events WHERE server_id = ? "
                f"  AND item_id IN ({placeholders})",
                (server_id, *stale_ids),
            )
            counters["watch_events"] = we_cur.rowcount
            ra_cur = conn.execute(
                f"DELETE FROM ratings WHERE server_id = ? "
                f"  AND item_id IN ({placeholders})",
                (server_id, *stale_ids),
            )
            counters["ratings"] = ra_cur.rowcount

            # Playlist / collection trim: load each row, filter the JSON
            # array, write it back when changed. Cheap because the
            # WHERE narrows by server.
            import json as _json
            for tbl, counter_key in (
                ("playlists", "playlists_touched"),
                ("collections", "collections_touched"),
            ):
                rows = conn.execute(
                    f"SELECT id, item_ids_json FROM {tbl} WHERE server_id = ?",
                    (server_id,),
                ).fetchall()
                for row in rows:
                    try:
                        ids = _json.loads(row["item_ids_json"] or "[]")
                    except Exception:
                        continue
                    kept = [i for i in ids if int(i) not in stale_ids]
                    if len(kept) != len(ids):
                        conn.execute(
                            f"UPDATE {tbl} SET item_ids_json = ?, updated_at = ? "
                            f"WHERE id = ?",
                            (_json.dumps(kept), time.time(), row["id"]),
                        )
                        counters[counter_key] += 1

            si_cur = conn.execute(
                f"DELETE FROM server_items WHERE server_id = ? "
                f"  AND item_id IN ({placeholders})",
                (server_id, *stale_ids),
            )
            counters["server_items"] = si_cur.rowcount

            # Items orphaned by this purge (no remaining server_items
            # row anywhere). Delete them too - keeping orphan item rows
            # would bloat the GUID-keyed pool indefinitely. Snapshot
            # files copy the items row at capture time, so any
            # historical reference is preserved there.
            orphan = conn.execute(
                f"SELECT id FROM items WHERE id IN ({placeholders}) "
                f"  AND NOT EXISTS (SELECT 1 FROM server_items WHERE item_id = items.id)",
                tuple(stale_ids),
            ).fetchall()
            if orphan:
                orphan_ids = [int(r["id"]) for r in orphan]
                op_cur = conn.execute(
                    f"DELETE FROM items WHERE id IN "
                    f"({','.join('?' * len(orphan_ids))})",
                    tuple(orphan_ids),
                )
                counters["items_orphaned"] = op_cur.rowcount

            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    try:
        from services import db_access_log
        db_access_log.log_write(
            table="items,server_items,watch_events,ratings,playlists,collections",
            where={"server_id": server_id, "older_than_seconds": older_than_seconds},
            affected_rows=sum(counters.values()),
            intent=f"prune stale items (counters={counters})",
        )
    except Exception:
        log.exception("db_access_log emit failed for prune_stale_items")
    return counters


# ── server_items: per-server rating_key cache (v0.12.1) ─────────────────────

def upsert_library_section(
    *,
    server_id: str,
    section_key: int,
    section_title: str,
    section_type: str,
) -> None:
    """
    Upsert the (server_id, section_key) row in ``library_sections``.

    This is the integrity anchor row that every per-server table's
    ``section_key`` column references. The function is called by
    ``ingest_snapshot_payload`` at the top of every snapshot ingest,
    BEFORE any server_items / watch_events / ratings / playlists /
    collections rows are written that reference this section.

    ``first_seen_at`` is preserved across re-ingests; only
    ``last_seen_at`` and the human-readable title/type update.
    Audited via ``db_access_log.log_write``.
    """
    if not server_id:
        raise ValueError("upsert_library_section: server_id required")
    if not isinstance(section_key, int) or section_key <= 0:
        raise ValueError(
            f"upsert_library_section: section_key must be a positive int "
            f"(got {section_key!r}). Use _UNKNOWN_SECTION_KEY constant "
            "if you have a legitimate sentinel reason - but no production "
            "path should ever pass 0."
        )
    if not section_title:
        raise ValueError("upsert_library_section: section_title required")
    if not section_type:
        raise ValueError("upsert_library_section: section_type required")
    conn = _require_conn()
    now = time.time()
    with _DB_LOCK:
        conn.execute(
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
            (server_id, int(section_key), section_title, section_type, now, now),
        )
    # Audit: dimension-table writes are operator-meaningful state
    # changes. Volume is low (one per library per snapshot run) so the
    # audit log doesn't bloat.
    try:
        from services import db_access_log
        db_access_log.log_write(
            table="media.db:library_sections",
            where={"server_id": server_id, "section_key": int(section_key)},
            affected_rows=1,
            intent=f"upsert library {section_title!r} (type={section_type})",
        )
    except Exception:
        pass


def record_server_item(
    *, item_id: int, server_id: str, rating_key: int, section_key: int,
) -> None:
    """
    Cache the ``ratingKey`` an ``items.id`` resolves to on one
    specific server. Used by the snapshotter as it walks a library -
    every item it serializes goes into ``items`` (by upstream GUID)
    and ``server_items`` (by per-server ratingKey).

    On subsequent runs against the same server, the resolver's
    Tier-0 lookup uses :func:`find_rating_key_on_server` to bypass
    the slow ``getByGuid`` round-trip and go straight to a single
    ``fetchItem(ratingKey)`` call. That's the v0.12.1 speed-up.

    Conflict handling:

    The table has two UNIQUE constraints:

        UNIQUE(item_id, server_id)
        UNIQUE(server_id, rating_key)

    SQLite's ``ON CONFLICT`` clause only attaches to ONE of them. The
    pre-fix code only handled ``(item_id, server_id)`` and tripped
    ``IntegrityError`` whenever a rating_key was reused under a
    different item_id - which happens legitimately when an upstream
    Plex item's GUID set changes between runs and a freshly-upserted
    items.id wants to claim a rating_key that was previously bound
    to an items.id that no longer exists in the new payload. The
    cure is ``INSERT OR REPLACE``: SQLite drops every row that
    violates either UNIQUE and writes the new one. server_items is
    a cache rebuilt from each snapshot run, so dropping a stale
    binding is exactly the intent.
    """
    if not isinstance(section_key, int) or section_key <= 0:
        raise ValueError(
            f"record_server_item: section_key must be a positive int "
            f"(got {section_key!r}). The caller (typically "
            "ingest_snapshot_payload) is responsible for guaranteeing "
            "this; no row may enter server_items without library "
            "identity. See v0.15 schema-anchor invariant."
        )
    conn = _require_conn()
    now = time.time()
    with _DB_LOCK:
        conn.execute(
            """
            INSERT OR REPLACE INTO server_items
                (item_id, server_id, rating_key, section_key, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (int(item_id), server_id, int(rating_key), int(section_key), now),
        )


def find_rating_key_on_server(*, guids: List[str], server_id: str) -> Optional[int]:
    """
    Resolver Tier-0 lookup. Given a list of GUIDs and a target
    server's ``server_id``, return the cached ``ratingKey`` if both
    sides have been seen before, else ``None``.

    Two-stage lookup:
      1. ``lookup_item_by_guids`` to resolve any of the provided
         GUIDs to an ``items.id`` (service-agnostic).
      2. Join against ``server_items`` to get the per-server
         ``rating_key`` if one was cached.

    Returns ``None`` if either stage misses - the resolver falls
    through to the existing tiers cleanly when the cache is cold.
    """
    item_id = lookup_item_by_guids(guids)
    if item_id is None:
        return None
    conn = _require_conn()
    row = conn.execute(
        "SELECT rating_key FROM server_items WHERE item_id = ? AND server_id = ?",
        (item_id, server_id),
    ).fetchone()
    return int(row["rating_key"]) if row else None


def has_any_items_for_server(server_id: str) -> bool:
    """
    True iff media.db has at least one ``server_items`` row tagged
    with this ``server_id``. Used by the snapshot-payload caching
    decision (services.snapshotter._should_cache_payload_to_media_db)
    to detect a "first run" for an unseeded server and trigger a
    one-shot ingest that populates the resolver Tier 0 GUID cache.

    Returns False on any DB error (caller treats failure as "not
    first run" so we never trigger an unexpected ingest).
    """
    if not server_id:
        return False
    try:
        conn = _require_conn()
        row = conn.execute(
            "SELECT 1 FROM server_items WHERE server_id = ? LIMIT 1",
            (server_id,),
        ).fetchone()
    except Exception:
        return False
    return row is not None


# ── Snapshot-payload ingestion with dedup discipline (v0.12.1) ─────────────────

def ingest_snapshot_payload(server_id: str, payload: Dict[str, Any]) -> Dict[str, int]:
    """
    Write one library's snapshot payload into the DB with the correct
    server-wide vs user-private split - the single chokepoint that
    enforces the dedup discipline documented on :func:`upsert_playlist`
    and :func:`upsert_collection`.

    Input shape (v0.13.0 unified-users JSON schema):

    .. code-block:: python

        {
            "library": "Movies",
            "snapshot_meta": {"server_id": ..., "backend": "plex", ...},
            "users": {
                "<owner-handle>": {
                    "role": "owner",
                    "display_name": "Plex Owner",
                    "backend_user_id": "1234567",
                    "watch_history": [...],
                    "playlists":     [...],
                    "collections":   [...],
                    "ratings":       [...],
                },
                "<managed-handle>": {
                    "role": "managed",
                    "display_name": "...",
                    "watch_history": [...], "playlists": [...], ...
                },
                ...
            },
        }

    The owner is identified by ``role == 'owner'`` in the users map,
    not by a magic empty-string key. Internally we still write the
    owner's wide-table rows under ``user_handle = ''`` (the legacy DB
    sentinel) for one release while every caller switches to the
    ``server_user_id`` FK; the ``server_users`` table is the
    authoritative source of role / display_name / backend identity.

    Walk order:

    1. **Items + server_items** - for every item-bearing entry across
       every user's block, upsert into ``items`` keyed by upstream
       GUID, then record the per-server ratingKey in ``server_items``.
       This is the data that lights up resolver Tier-0 on subsequent
       runs. One pass over the unified users map (no separate owner
       walk needed any more).

    2. **Owner block - server-wide collections + playlists** - write
       the ``role == 'owner'`` user's playlists / collections with
       ``user_handle=""``. Capture the rating_key set for the
       per-user dedup step. Also pre-creates the owner's
       ``server_users`` row with the display_name from the JSON so
       it lands on first ingest rather than waiting for a later walk.

    3. **Managed users** - for each ``role == 'managed'`` user, write
       watch / ratings / playlists / collections under
       ``user_handle=<handle>``, BUT filter user-private collections
       / playlists against the owner-side rating_key set so a
       library-level collection visible to every user doesn't get
       written N times.

    Returns a small counter dict so callers (orchestrators) can log
    or assert "ingested K items / M watch events" etc.
    """
    counters = {
        "items": 0, "server_items": 0, "watch_events": 0, "ratings": 0,
        "playlists": 0, "collections": 0, "playlists_skipped_dup": 0,
        "collections_skipped_dup": 0,
    }
    if not isinstance(payload, dict):
        return counters
    library_name = str(payload.get("library") or "")

    # v0.15 integrity-anchor contract: every payload MUST carry
    # library_section_id (Plex's numeric section key) and
    # library_section_type. This is what makes per-library restore
    # correct on the other side.
    section_key_raw = payload.get("library_section_id")
    section_type = str(payload.get("library_section_type") or "")
    if section_key_raw is None:
        raise ValueError(
            "ingest_snapshot_payload: payload missing library_section_id. "
            "Every per-library payload must carry the Plex section key as "
            "the integrity anchor for restore. The capture path in "
            "services.snapshotter.snapshot_library is responsible for "
            "supplying it; see v0.15 schema-anchor invariant."
        )
    try:
        section_key = int(section_key_raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"ingest_snapshot_payload: library_section_id must be an int "
            f"(got {section_key_raw!r}): {exc}"
        )
    if section_key <= 0:
        raise ValueError(
            f"ingest_snapshot_payload: library_section_id must be > 0 "
            f"(got {section_key}). Plex section keys start at 1; 0 is the "
            "sentinel for 'unknown / pre-v0.15' rows and never appears in "
            "a valid payload."
        )
    if not section_type:
        raise ValueError(
            "ingest_snapshot_payload: payload missing library_section_type "
            "(movie / show / artist / etc). Required for the library_sections "
            "dimension row."
        )
    if not library_name:
        raise ValueError("ingest_snapshot_payload: payload missing 'library' (section title)")

    # Upsert the dimension row BEFORE any per-server-table writes so
    # the FK target exists. media.db doesn't enforce FKs at SQLite
    # level (perf) but snapshot.db does, and we want the same ordering
    # everywhere for consistency.
    upsert_library_section(
        server_id=server_id,
        section_key=section_key,
        section_title=library_name,
        section_type=section_type,
    )

    # ── Pass 1: items + server_items ────────────────────────────────
    def _ingest_item_record(rec: Dict[str, Any]) -> Optional[int]:
        """Upsert one item and cache its server rating_key."""
        guids = rec.get("guids") or []
        if not isinstance(guids, list):
            return None
        title = rec.get("title") or ""
        if not title:
            return None
        media_type = (rec.get("type") or "movie")
        year = rec.get("year")
        if year is not None and not isinstance(year, int):
            try:
                year = int(year)
            except (TypeError, ValueError):
                year = None
        filepath = rec.get("filepath") or rec.get("path")
        try:
            iid = upsert_item(
                guids=guids,
                title=title,
                media_type=media_type,
                year=year,
                filepath_suffix=filepath,
            )
        except Exception:
            return None
        counters["items"] += 1
        rating_key = rec.get("rating_key")
        if rating_key is not None:
            try:
                record_server_item(
                    item_id=iid, server_id=server_id,
                    rating_key=int(rating_key),
                    section_key=section_key,
                )
                counters["server_items"] += 1
            except (TypeError, ValueError):
                pass
        return iid

    # Map (rating_key on this server) → items.id so per-event writes
    # below can resolve the row without re-running upsert_item.
    rating_key_to_item_id: Dict[int, int] = {}

    def _walk_items_block(items_block: Dict[str, Any]) -> None:
        for rec in (items_block.get("watch_history") or []):
            iid = _ingest_item_record(rec)
            rk = rec.get("rating_key")
            if iid is not None and rk is not None:
                try:
                    rating_key_to_item_id[int(rk)] = iid
                except (TypeError, ValueError):
                    pass
        for rec in (items_block.get("ratings") or []):
            iid = _ingest_item_record(rec)
            rk = rec.get("rating_key")
            if iid is not None and rk is not None:
                try:
                    rating_key_to_item_id[int(rk)] = iid
                except (TypeError, ValueError):
                    pass
        for pl in (items_block.get("playlists") or []):
            for rec in (pl.get("items") or []):
                iid = _ingest_item_record(rec)
                rk = rec.get("rating_key")
                if iid is not None and rk is not None:
                    try:
                        rating_key_to_item_id[int(rk)] = iid
                    except (TypeError, ValueError):
                        pass
        for col in (items_block.get("collections") or []):
            for rec in (col.get("items") or []):
                iid = _ingest_item_record(rec)
                rk = rec.get("rating_key")
                if iid is not None and rk is not None:
                    try:
                        rating_key_to_item_id[int(rk)] = iid
                    except (TypeError, ValueError):
                        pass

    # v0.13.0: one pass over the unified users map. Owner and managed
    # users share the same block shape, so a single loop handles both.
    users_map = payload.get("users") or {}
    for udata in users_map.values():
        if isinstance(udata, dict):
            _walk_items_block(udata)

    # Find the owner block (the unique user with role='owner').
    # If a payload has no role='owner' entry we fall back to the
    # empty-string handle for compatibility with mid-transition
    # snapshots, then finally give up gracefully (writes still
    # land for managed users, just no server-wide rows).
    owner_handle: Optional[str] = None
    owner_block: Dict[str, Any] = {}
    for h, ub in users_map.items():
        if isinstance(ub, dict) and ub.get("role") == "owner":
            owner_handle = h
            owner_block = ub
            break
    if owner_handle is None and "" in users_map and isinstance(users_map[""], dict):
        owner_handle = ""
        owner_block = users_map[""]
    # Eagerly upsert the owner's server_users row so display_name /
    # backend_user_id land now, not on the next walk.
    if owner_block:
        get_or_create_server_user(
            server_id=server_id, user_handle="",
            role="owner",
            display_name=owner_block.get("display_name"),
            backend_user_id=owner_block.get("backend_user_id"),
        )

    def _resolve_member_ids(members: List[Dict[str, Any]]) -> List[int]:
        """
        Translate a list of member records to internal items.id.

        Primary path: ``rating_key`` → ``rating_key_to_item_id`` map
        built by ``_walk_items_block``. This is the fast path that
        modern snapshots take.

        GUID fallback: legacy payloads (pre-v0.13.0) didn't include
        ``rating_key`` on playlist / collection member entries, so the
        map miss is structural rather than an error condition. Fall
        through to a GUID-based ``lookup_item_by_guids`` so those
        payloads still get a populated ``item_ids_json``. Without this
        fallback every playlist/collection in a re-ingested legacy
        archive comes back empty.
        """
        out: List[int] = []
        for rec in members or []:
            rk = rec.get("rating_key")
            iid: Optional[int] = None
            if rk is not None:
                try:
                    iid = rating_key_to_item_id.get(int(rk))
                except (TypeError, ValueError):
                    iid = None
            if iid is None:
                guids = rec.get("guids") or []
                if isinstance(guids, list) and guids:
                    try:
                        iid = lookup_item_by_guids(guids)
                    except Exception:
                        iid = None
            if iid is not None:
                out.append(iid)
        return out

    # ── Pass 2: owner block (server-wide rows, user_handle="") ─────
    # Owner-side watch / ratings / playlists / collections all land
    # under the empty-string DB handle. The owner's display_name and
    # backend_user_id have already been pushed into server_users
    # above. Capture rating-key sets so the managed-user pass below
    # can dedup library-level rows it sees re-emitted under personal
    # handles.
    owner_playlists  = owner_block.get("playlists")   or []
    owner_collections = owner_block.get("collections") or []
    owner_display    = owner_block.get("display_name")
    owner_backend_id = owner_block.get("backend_user_id")

    owner_playlist_keys = {
        int(pl["rating_key"]) for pl in owner_playlists
        if pl.get("rating_key") is not None
    }
    owner_collection_keys = {
        int(c["rating_key"]) for c in owner_collections
        if c.get("rating_key") is not None
    }

    for pl in owner_playlists:
        try:
            upsert_playlist(
                server_id=server_id,
                user_handle="",
                name=str(pl.get("name") or pl.get("title") or ""),
                is_smart=bool(pl.get("smart") or False),
                smart_filter=pl.get("smart_content"),
                description=pl.get("description"),
                item_ids=_resolve_member_ids(pl.get("items") or []),
                section_key=section_key,
            )
            counters["playlists"] += 1
        except Exception:
            continue
    for col in owner_collections:
        try:
            upsert_collection(
                server_id=server_id,
                user_handle="",
                name=str(col.get("name") or col.get("title") or ""),
                item_ids=_resolve_member_ids(col.get("items") or []),
                section_key=section_key,
            )
            counters["collections"] += 1
        except Exception:
            continue

    # Owner watch_history / ratings (server-wide, user_handle="").
    for rec in (owner_block.get("watch_history") or []):
        rk = rec.get("rating_key")
        if rk is None:
            continue
        try:
            rk_int = int(rk)
        except (TypeError, ValueError):
            continue
        iid = rating_key_to_item_id.get(rk_int)
        if iid is None:
            continue
        try:
            record_watch_event(
                item_id=iid, server_id=server_id, user_handle="",
                role="owner", display_name=owner_display,
                backend_user_id=owner_backend_id,
                view_count=int(rec.get("view_count") or 0),
                view_offset=int(rec.get("view_offset") or 0),
                last_viewed_at=rec.get("last_viewed_at"),
                section_key=section_key,
            )
            counters["watch_events"] += 1
        except Exception:
            continue
    for rec in (owner_block.get("ratings") or []):
        rk = rec.get("rating_key")
        if rk is None:
            continue
        try:
            rk_int = int(rk)
            rating_val = float(rec.get("rating") or 0.0)
        except (TypeError, ValueError):
            continue
        iid = rating_key_to_item_id.get(rk_int)
        if iid is None:
            continue
        try:
            upsert_rating(
                item_id=iid, server_id=server_id, user_handle="",
                role="owner", display_name=owner_display,
                backend_user_id=owner_backend_id,
                rating=rating_val,
                section_key=section_key,
            )
            counters["ratings"] += 1
        except Exception:
            continue

    # ── Pass 3: managed-user blocks (user_handle=<handle>) ─────────
    # Skip the user we just processed as owner. Everything else is
    # role='managed' (or unmarked, defaulted to managed by
    # get_or_create_server_user). Per-user collections / playlists
    # are deduped by rating_key against the owner-side set so a
    # library-level row visible to every user isn't written N times.
    for username, udata in users_map.items():
        if not isinstance(udata, dict):
            continue
        if username == owner_handle:
            continue
        if not username:
            # Empty handle for a non-owner row is meaningless - skip
            # rather than collide with the owner sentinel.
            continue
        u_display    = udata.get("display_name")
        u_backend_id = udata.get("backend_user_id")
        # Watch events.
        for rec in (udata.get("watch_history") or []):
            rk = rec.get("rating_key")
            if rk is None:
                continue
            try:
                rk_int = int(rk)
            except (TypeError, ValueError):
                continue
            iid = rating_key_to_item_id.get(rk_int)
            if iid is None:
                continue
            try:
                record_watch_event(
                    item_id=iid, server_id=server_id, user_handle=str(username),
                    role="managed", display_name=u_display,
                    backend_user_id=u_backend_id,
                    view_count=int(rec.get("view_count") or 0),
                    view_offset=int(rec.get("view_offset") or 0),
                    last_viewed_at=rec.get("last_viewed_at"),
                    section_key=section_key,
                )
                counters["watch_events"] += 1
            except Exception:
                continue
        # Per-user ratings.
        for rec in (udata.get("ratings") or []):
            rk = rec.get("rating_key")
            if rk is None:
                continue
            try:
                rk_int = int(rk)
                rating_val = float(rec.get("rating") or 0.0)
            except (TypeError, ValueError):
                continue
            iid = rating_key_to_item_id.get(rk_int)
            if iid is None:
                continue
            try:
                upsert_rating(
                    item_id=iid, server_id=server_id,
                    user_handle=str(username), rating=rating_val,
                    role="managed", display_name=u_display,
                    backend_user_id=u_backend_id,
                    section_key=section_key,
                )
                counters["ratings"] += 1
            except Exception:
                continue
        # Per-user playlists, deduped by rating_key against owner set.
        for pl in (udata.get("playlists") or []):
            rk = pl.get("rating_key")
            if rk is not None and int(rk) in owner_playlist_keys:
                counters["playlists_skipped_dup"] += 1
                continue
            try:
                upsert_playlist(
                    server_id=server_id,
                    user_handle=str(username),
                    name=str(pl.get("name") or pl.get("title") or ""),
                    is_smart=bool(pl.get("smart") or False),
                    smart_filter=pl.get("smart_content"),
                    description=pl.get("description"),
                    item_ids=_resolve_member_ids(pl.get("items") or []),
                    section_key=section_key,
                )
                counters["playlists"] += 1
            except Exception:
                continue
        # Per-user collections, deduped by rating_key against owner set.
        for col in (udata.get("collections") or []):
            rk = col.get("rating_key")
            if rk is not None and int(rk) in owner_collection_keys:
                counters["collections_skipped_dup"] += 1
                continue
            try:
                upsert_collection(
                    server_id=server_id,
                    user_handle=str(username),
                    name=str(col.get("name") or col.get("title") or ""),
                    item_ids=_resolve_member_ids(col.get("items") or []),
                    section_key=section_key,
                )
                counters["collections"] += 1
            except Exception:
                continue

    log.debug(
        "ingest_snapshot_payload: library=%r server=%r counts=%s",
        library_name, server_id, counters,
    )
    try:
        from services import db_access_log
        total_rows = (
            counters.get("items", 0)
            + counters.get("server_items", 0)
            + counters.get("watch_events", 0)
            + counters.get("ratings", 0)
            + counters.get("playlists", 0)
            + counters.get("collections", 0)
        )
        db_access_log.log_write(
            table="items,server_items,watch_events,ratings,playlists,collections",
            where={"server_id": server_id, "library": library_name},
            affected_rows=total_rows,
            intent=(
                f"ingest snapshot payload "
                f"(items={counters.get('items', 0)}, "
                f"server_items={counters.get('server_items', 0)}, "
                f"watch={counters.get('watch_events', 0)}, "
                f"ratings={counters.get('ratings', 0)}, "
                f"playlists={counters.get('playlists', 0)}, "
                f"collections={counters.get('collections', 0)}, "
                f"pl_dup={counters.get('playlists_skipped_dup', 0)}, "
                f"col_dup={counters.get('collections_skipped_dup', 0)})"
            ),
        )
    except Exception:
        pass
    return counters


# ── Managed users (PR-10) ───────────────────────────────────────────────────
#
# Per-(server, username) records of the operators / managed users
# known to each registered server. Stores both metadata (display name,
# service type, machine identifier, last_seen) AND optional encrypted
# credentials (auth token, Plex Home PIN, Emby/Jellyfin password) for
# use by PR-12's pre-flight check and downstream job runs.
#
# Two write surfaces:
#   * upsert_managed_user() - metadata only. Called by the sync helper
#     (PR-10 manual + PR-11 automatic) which talks to the live API.
#     Credentials are preserved across upserts.
#   * set_managed_user_credential() - one credential at a time. Plain-
#     text input is encrypted before write; empty string clears.
#     Called by the User Management write endpoint after db_admin
#     verification.
#
# Reads (list_managed_users, get_managed_user) never return plaintext
# credentials - just ``has_token`` / ``has_pin`` / ``has_password``
# booleans. Decryption is only available via the explicit
# get_managed_user_credential() helper, intended for engine code that
# needs to actually use a stored credential (PR-12).

# Credential kinds, used by the per-credential helpers below. Keeping
# them as a tuple of constants rather than an Enum keeps the code base
# free of an unnecessary import elsewhere.
MANAGED_USER_CREDENTIAL_KINDS = ("auth_token", "plex_home_pin", "service_password")
_CRED_COLUMN = {
    "auth_token": "auth_token_enc",
    "plex_home_pin": "plex_home_pin_enc",
    "service_password": "service_password_enc",
}


def _row_to_managed_user(
    row: sqlite3.Row,
    *,
    global_set: Optional[set] = None,
) -> Dict[str, Any]:
    """
    Public-shape managed-user dict. Never includes plaintext creds.

    ``hidden_scope`` summarises the tombstone state for the UI: 'none'
    when visible, 'server' when this specific (server, username) is
    hidden, 'global' when the username is hidden everywhere. The
    server-scope flag wins ties only when no global tombstone applies.
    Pass ``global_set`` to avoid a per-row lookup against
    ``global_tombstones`` when iterating a list.
    """
    username = row["username"]
    globally_hidden = (
        (global_set is not None and username in global_set)
        or (global_set is None and is_globally_tombstoned(username))
    )
    server_hidden = bool(row["tombstoned"])
    if globally_hidden:
        hidden_scope = "global"
    elif server_hidden:
        hidden_scope = "server"
    else:
        hidden_scope = "none"
    return {
        "id": int(row["id"]),
        "server_id": row["server_id"],
        "username": username,
        "display_name": row["display_name"],
        "service_type": row["service_type"],
        "kind": row["kind"],
        "machine_identifier": row["machine_identifier"],
        "has_token": bool(row["auth_token_enc"]),
        "has_pin": bool(row["plex_home_pin_enc"]),
        "has_password": bool(row["service_password_enc"]),
        "last_seen": row["last_seen"],
        "tombstoned": server_hidden,
        "hidden_scope": hidden_scope,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


_MANAGED_USER_COLUMNS = (
    "id, server_id, username, display_name, service_type, kind, "
    "machine_identifier, auth_token_enc, plex_home_pin_enc, "
    "service_password_enc, last_seen, tombstoned, created_at, updated_at"
)


def list_managed_users(
    server_id: str,
    *,
    include_hidden: bool = False,
) -> List[Dict[str, Any]]:
    """
    Return every managed-user row for one server, oldest first.

    ``include_hidden=False`` (default) filters out rows that are
    tombstoned per-server AND any row whose username is globally
    tombstoned - matches the JobFormPanel picker contract (never
    show a hidden user as selectable). ``include_hidden=True`` is
    used by the User Management 'Show hidden' toggle and by the
    ServersPanel diff effect so the diff correctly excludes
    tombstoned users from 'newly detected' alerts.
    """
    conn = _require_conn()
    rows = conn.execute(
        f"SELECT {_MANAGED_USER_COLUMNS} "
        "FROM managed_users WHERE server_id = ? "
        # owner first so the JobFormPanel picker shows it on top of
        # the picklist - matches the live-API ordering it replaces.
        "ORDER BY CASE kind WHEN 'owner' THEN 0 ELSE 1 END, username ASC",
        (server_id,),
    ).fetchall()
    global_set = list_global_tombstone_usernames()
    out = [_row_to_managed_user(r, global_set=global_set) for r in rows]
    if include_hidden:
        return out
    return [u for u in out if u["hidden_scope"] == "none"]


def get_managed_user(server_id: str, username: str) -> Optional[Dict[str, Any]]:
    """Single-row lookup by (server_id, username). Returns ``None`` if absent."""
    conn = _require_conn()
    row = conn.execute(
        f"SELECT {_MANAGED_USER_COLUMNS} "
        "FROM managed_users WHERE server_id = ? AND username = ?",
        (server_id, username),
    ).fetchone()
    if row is None:
        return None
    return _row_to_managed_user(row)


def upsert_managed_user(
    *,
    server_id: str,
    username: str,
    display_name: Optional[str] = None,
    service_type: str = "plex",
    kind: str = "managed",
    machine_identifier: Optional[str] = None,
    last_seen: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Insert or update a managed-user row's metadata. Credential columns
    are NOT touched here - this is the safe path the sync helper takes
    on every server-connect probe, and preserving existing stored
    credentials across resyncs is required (otherwise a re-sync would
    silently wipe every operator-typed PIN).

    ``service_type`` is validated against the CHECK constraint at the
    DB layer; ``kind`` likewise (PR-11 migration v4). The helpers
    accept the strings as-is.

    Returns the updated row in public shape.
    """
    if not server_id:
        raise ValueError("server_id is required")
    if not username:
        raise ValueError("username is required")
    conn = _require_conn()
    now = time.time()
    with _DB_LOCK:
        conn.execute(
            """
            INSERT INTO managed_users (
                server_id, username, display_name, service_type, kind,
                machine_identifier, last_seen, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(server_id, username) DO UPDATE SET
                display_name       = COALESCE(excluded.display_name, managed_users.display_name),
                service_type       = excluded.service_type,
                kind               = excluded.kind,
                machine_identifier = COALESCE(excluded.machine_identifier, managed_users.machine_identifier),
                last_seen          = COALESCE(excluded.last_seen, managed_users.last_seen),
                updated_at         = excluded.updated_at
            """,
            (
                server_id, username, display_name, service_type, kind,
                machine_identifier, last_seen, now, now,
            ),
        )
    out = get_managed_user(server_id, username)
    assert out is not None  # we just upserted
    return out


def sync_managed_users_from_live(
    server_id: str,
    logger: Optional[logging.Logger] = None,
) -> Dict[str, Any]:
    """
    PR-11 sync helper. Pulls the live user list from the registered
    server and upserts it into ``managed_users``. Metadata-only -
    credential cells are preserved across syncs.

    Sets ``kind='owner'`` for the owner row and ``kind='managed'`` for
    every other entry so the JobFormPanel picker can render its
    existing Owner / Managed badge after switching from live-API to
    DB-backed reads.

    Best-effort: errors from ``get_server_users`` are caught and
    surfaced in the return value rather than raised. Callers
    (server-connect hooks in app.py) treat sync failure as a soft
    warning and let the server-add/test path succeed regardless.

    Returns ``{"synced": int, "source_error": Optional[str], "error": Optional[str]}``.
    """
    log_ = logger or log
    # Late import to avoid a circular dependency at module load time;
    # ``server_registry`` doesn't currently import ``media_db`` but
    # keeping this localised is defensive against future cycles.
    from server import server_registry  # noqa: WPS433 (intentional local import)

    try:
        result = server_registry.get_server_users(server_id, log_)
    except ValueError as exc:
        return {"synced": 0, "source_error": None, "error": str(exc)}
    except ConnectionError as exc:
        return {"synced": 0, "source_error": None, "error": str(exc)}

    machine_id = (
        server_registry.get_server_by_id(server_id, include_token=False) or {}
    ).get("machine_identifier") or None
    now = time.time()
    # PR-11.1 - usernames the operator has globally tombstoned never
    # get upserted. Skipping them here keeps the row count down for
    # installs with many servers and many hidden users.
    global_set = list_global_tombstone_usernames()
    synced = 0
    skipped_global = 0
    for row in (result.get("users") or []):
        raw_name = (row.get("raw_name") or "").strip()
        if not raw_name:
            continue
        if raw_name in global_set:
            skipped_global += 1
            continue
        # ``kind`` comes straight from get_server_users (owner|managed);
        # default to managed if missing (shouldn't happen but defensive).
        kind = row.get("kind") if row.get("kind") in ("owner", "managed") else "managed"
        try:
            upsert_managed_user(
                server_id=server_id,
                username=raw_name,
                display_name=(row.get("display_name") or None),
                # Live API today is Plex-only. PR-11 / Feature 4 in
                # roadmapplan4.md introduces Emby + Jellyfin probes;
                # this defaults to 'plex' and stays correct.
                service_type="plex",
                kind=kind,
                machine_identifier=machine_id,
                last_seen=now,
            )
            synced += 1
        except Exception:
            log_.exception(
                "upsert_managed_user failed for %r on server %r",
                raw_name, server_id,
            )
            continue
    return {
        "synced": synced,
        "skipped_global_tombstones": skipped_global,
        "source_error": result.get("error"),
        "error": None,
    }


# ── Tombstones (PR-11.1) ────────────────────────────────────────────────────
#
# Two scopes:
#   * Per-server: ``managed_users.tombstoned`` flag on one row. The
#     username is hidden on that specific server only; same username
#     on another server stays visible. Credentials are preserved
#     across hide/unhide cycles.
#   * Global: a row in ``global_tombstones`` keyed by username only.
#     The sync helper above skips usernames in this set, and the
#     list query filters them out regardless of the row's per-server
#     tombstoned flag.
#
# The User Management 'Hide user' modal lets the operator pick which
# scope to apply.

def set_managed_user_tombstone(
    *,
    server_id: str,
    username: str,
    tombstoned: bool,
) -> Dict[str, Any]:
    """
    Set or clear the per-server tombstone flag for one managed user.
    Credentials and other metadata are untouched. Raises ``ValueError``
    if no row matches.
    """
    if not server_id or not username:
        raise ValueError("server_id and username are required")
    conn = _require_conn()
    now = time.time()
    with _DB_LOCK:
        cur = conn.execute(
            "UPDATE managed_users SET tombstoned = ?, updated_at = ? "
            "WHERE server_id = ? AND username = ?",
            (1 if tombstoned else 0, now, server_id, username),
        )
        if cur.rowcount == 0:
            raise ValueError(
                f"No managed user {username!r} on server {server_id!r}."
            )
    out = get_managed_user(server_id, username)
    assert out is not None
    # M13: tombstone mutators are auditable state changes on a
    # credential-bearing table - record them like every other write.
    try:
        from services import db_access_log
        db_access_log.log_write(
            table="managed_users",
            field="tombstoned",
            where={"server_id": server_id, "username": username},
            affected_rows=cur.rowcount,
            intent=("set tombstone" if tombstoned else "clear tombstone"),
        )
    except Exception:
        log.exception("db_access_log emit failed for set_managed_user_tombstone")
    return out


def add_global_tombstone(username: str) -> None:
    """
    Hide ``username`` across every registered server. Idempotent. The
    sync helper will skip this username on every future run, and
    ``list_managed_users`` filters it out from the default visible
    set. Existing per-server rows for this username stay in the DB
    (creds preserved) but are reported with ``hidden_scope='global'``.
    """
    uname = (username or "").strip()
    if not uname:
        raise ValueError("username is required")
    conn = _require_conn()
    now = time.time()
    with _DB_LOCK:
        cur = conn.execute(
            "INSERT INTO global_tombstones (username, tombstoned_at) "
            "VALUES (?, ?) "
            "ON CONFLICT(username) DO UPDATE SET tombstoned_at = excluded.tombstoned_at",
            (uname, now),
        )
    # M13: auditable - hides a user across every registered server.
    try:
        from services import db_access_log
        db_access_log.log_write(
            table="global_tombstones",
            where={"username": uname},
            affected_rows=cur.rowcount,
            intent="add global tombstone (hide user on all servers)",
        )
    except Exception:
        log.exception("db_access_log emit failed for add_global_tombstone")


def remove_global_tombstone(username: str) -> None:
    """
    Unhide ``username`` globally. Idempotent. The username becomes
    syncable again on the next sync run.
    """
    uname = (username or "").strip()
    if not uname:
        raise ValueError("username is required")
    conn = _require_conn()
    with _DB_LOCK:
        cur = conn.execute(
            "DELETE FROM global_tombstones WHERE username = ?",
            (uname,),
        )
    # M13: auditable - makes a previously-hidden user syncable again.
    try:
        from services import db_access_log
        db_access_log.log_write(
            table="global_tombstones",
            where={"username": uname},
            affected_rows=cur.rowcount,
            intent="remove global tombstone (unhide user on all servers)",
        )
    except Exception:
        log.exception("db_access_log emit failed for remove_global_tombstone")


def list_global_tombstones() -> List[Dict[str, Any]]:
    """
    Return every globally-tombstoned username with its tombstoned_at
    timestamp. Used by the User Management UI to render a 'Globally
    hidden usernames' list with per-row Unhide controls.
    """
    conn = _require_conn()
    rows = conn.execute(
        "SELECT username, tombstoned_at FROM global_tombstones "
        "ORDER BY tombstoned_at DESC"
    ).fetchall()
    return [
        {"username": r["username"], "tombstoned_at": r["tombstoned_at"]}
        for r in rows
    ]


def list_global_tombstone_usernames() -> set:
    """Fast set lookup used by the list query and sync helper."""
    conn = _require_conn()
    rows = conn.execute("SELECT username FROM global_tombstones").fetchall()
    return {r["username"] for r in rows}


def is_globally_tombstoned(username: str) -> bool:
    """Single-username probe. Cheap enough for one-off checks."""
    if not username:
        return False
    conn = _require_conn()
    row = conn.execute(
        "SELECT 1 FROM global_tombstones WHERE username = ?",
        (username,),
    ).fetchone()
    return row is not None


def set_managed_user_credential(
    *,
    server_id: str,
    username: str,
    kind: str,
    plaintext: str,
) -> Dict[str, Any]:
    """
    Encrypt and write one credential cell. ``plaintext`` is encrypted
    via :func:`server.secrets.encrypt_str` (Fernet) before storage; an
    empty string clears the cell. ``kind`` must be one of
    :data:`MANAGED_USER_CREDENTIAL_KINDS`.

    Caller (the User Management write endpoint) is responsible for
    db_admin verification before calling this.
    """
    if kind not in _CRED_COLUMN:
        raise ValueError(
            f"Unknown credential kind {kind!r}. "
            f"Valid: {', '.join(MANAGED_USER_CREDENTIAL_KINDS)}"
        )
    if not server_id or not username:
        raise ValueError("server_id and username are required")
    from server.secrets import encrypt_str  # local import to avoid cycle on module load

    column = _CRED_COLUMN[kind]
    encrypted = encrypt_str(plaintext or "") or None  # empty string clears
    conn = _require_conn()
    now = time.time()
    with _DB_LOCK:
        cur = conn.execute(
            f"UPDATE managed_users SET {column} = ?, updated_at = ? "
            "WHERE server_id = ? AND username = ?",
            (encrypted, now, server_id, username),
        )
        if cur.rowcount == 0:
            raise ValueError(
                f"No managed user {username!r} on server {server_id!r}."
            )
    out = get_managed_user(server_id, username)
    assert out is not None
    # PR-13 audit trail. Recorded regardless of plaintext/empty so the
    # operator can also see "clear" operations.
    try:
        from services import db_access_log
        db_access_log.log_write(
            table="managed_users",
            field=column,
            where={"server_id": server_id, "username": username},
            affected_rows=cur.rowcount,
            intent=("clear credential" if not plaintext else f"set {kind}"),
        )
    except Exception:
        pass
    return out


def set_managed_user_display_name(
    *,
    server_id: str,
    username: str,
    display_name: Optional[str],
) -> Dict[str, Any]:
    """
    Update the per-user friendly display name. ``None`` or empty
    string clears it (UI falls back to the raw username).
    """
    if not server_id or not username:
        raise ValueError("server_id and username are required")
    dn = (display_name or "").strip() or None
    conn = _require_conn()
    now = time.time()
    with _DB_LOCK:
        cur = conn.execute(
            "UPDATE managed_users SET display_name = ?, updated_at = ? "
            "WHERE server_id = ? AND username = ?",
            (dn, now, server_id, username),
        )
        if cur.rowcount == 0:
            raise ValueError(
                f"No managed user {username!r} on server {server_id!r}."
            )
    out = get_managed_user(server_id, username)
    assert out is not None
    return out


def get_managed_user_credential(
    server_id: str,
    username: str,
    kind: str,
) -> Optional[str]:
    """
    Decrypt and return one credential cell, or ``None`` if not stored.
    Intended for engine code that actually needs to USE a stored
    credential (PR-12 pre-flight, future per-user impersonation). The
    User Management read API never calls this - the panel only ever
    surfaces presence booleans.
    """
    if kind not in _CRED_COLUMN:
        raise ValueError(f"Unknown credential kind {kind!r}.")
    from server.secrets import decrypt_str
    column = _CRED_COLUMN[kind]
    conn = _require_conn()
    row = conn.execute(
        f"SELECT {column} AS enc FROM managed_users "
        "WHERE server_id = ? AND username = ?",
        (server_id, username),
    ).fetchone()
    found = bool(row is not None and row["enc"])
    try:
        from services import db_access_log
        db_access_log.log_read(
            table="managed_users",
            field=column,
            where={"server_id": server_id, "username": username, "found": found},
            intent=f"fetch encrypted {kind} for engine use",
        )
    except Exception:
        pass
    if not found:
        return None
    try:
        return decrypt_str(row["enc"]) or None
    except Exception:
        # Malformed ciphertext (key rotated, file corruption). Treat
        # as "not stored" so the caller falls back to the no-credential
        # path rather than crashing the request.
        log.exception(
            "decrypt failed for managed_user %r on server %r (kind=%s); "
            "falling back to no-credential.",
            username, server_id, kind,
        )
        return None


def delete_managed_user(server_id: str, username: str) -> None:
    """
    Remove a managed-user row entirely. Raises if no such row.

    M13: hard-deletes a row holding Fernet-encrypted credentials
    (auth token, Plex Home PIN, service password), so it emits a
    ``db_access_log`` entry - matching ``purge_server_data`` /
    ``prune_stale_items`` and every other destructive media.db op.
    """
    if not server_id or not username:
        raise ValueError("server_id and username are required")
    conn = _require_conn()
    with _DB_LOCK:
        cur = conn.execute(
            "DELETE FROM managed_users WHERE server_id = ? AND username = ?",
            (server_id, username),
        )
        if cur.rowcount == 0:
            raise ValueError(
                f"No managed user {username!r} on server {server_id!r}."
            )
    try:
        from services import db_access_log
        db_access_log.log_write(
            table="managed_users",
            where={"server_id": server_id, "username": username},
            affected_rows=cur.rowcount,
            intent="delete managed user (encrypted credentials destroyed)",
        )
    except Exception:
        log.exception("db_access_log emit failed for delete_managed_user")


# ── Test / diagnostic helpers ────────────────────────────────────────────────

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
