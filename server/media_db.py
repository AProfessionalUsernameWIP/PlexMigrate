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
from services.user_uuid import (
    build_app_user_uuid,
    generate_user_key,
    server_uid_from_app_user_uuid,
    slugify_host_name,
)


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
    # disappears across every server until the end user unhides
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
    # results may be stale" before letting the end user press the
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
    # Migration v9: managed_users share-state columns. The user-filter
    # work (Plan[USER-FILTER]-2026-05-15.md) needs to distinguish three
    # row classes that look identical today:
    #
    #   * ``active_share=1``: user currently has an active share on
    #     this server. ``shared_state_refreshed_at`` records when we
    #     last confirmed this via the Plex.tv shared_servers endpoint.
    #   * ``active_share=0``: user used to have a share but the server
    #     no longer reports them in shared_servers. Stale - end user
    #     removed access, or revoked them, or Plex.tv pruned. The
    #     panel renders them greyed-out with a "No active share"
    #     badge; the engine skips them with a clear log line.
    #   * ``is_pin_protected=1``: user is a Plex Home account with a
    #     PIN set. Distinguished from "no longer shared" so the engine
    #     can emit "PIN-protected, save PIN under User Management"
    #     instead of a generic warning.
    #
    # All three columns nullable / default 0 / default NULL so existing
    # rows survive the migration. The sync flow populates them on the
    # next sync.
    (9, """
        ALTER TABLE managed_users
            ADD COLUMN active_share INTEGER NOT NULL DEFAULT 1;
        ALTER TABLE managed_users
            ADD COLUMN is_pin_protected INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE managed_users
            ADD COLUMN shared_state_refreshed_at REAL;
    """),
    # Migration v10 (2026-05-15 follow-up #3): canonical per-user
    # identifier. Pre-v10 the share-state refresh matched
    # ``managed_users.username`` (sourced from ``server.systemAccounts()``,
    # often the display name like "Crystal Jean") against the Plex.tv
    # ``shared_servers`` payload (keyed by handle like "crystalj1"). Even
    # with multi-alias enrichment from ``account.users()``, this remains
    # fragile to Unicode, emoji, diacritics, punctuation variants,
    # whitespace differences, and duplicate display names. Storing the
    # stable Plex.tv numeric ``userID`` per row and matching by it
    # eliminates the whole class of name-matching bugs and gives Feature
    # 5 (live watch sync, identity links) the canonical column it needs.
    #
    # Additive nullable column; rows seeded before v10 carry NULL until
    # the next refresh resolves them via alias matching, at which point
    # the ID is backfilled. The index supports the per-server lookup
    # path used by ``_refresh_share_state`` and (later) the sync
    # dispatcher's identity resolver.
    (10, """
        ALTER TABLE managed_users
            ADD COLUMN backend_user_id TEXT;
        CREATE INDEX IF NOT EXISTS idx_managed_users_backend_user_id
            ON managed_users(server_id, backend_user_id);
    """),
    (11, """
        -- Plan[RUN-JOB-UI] follow-up: cross-server user identity
        -- mapping. Pairs (server_a, handle_a) <-> (server_b, handle_b)
        -- so the engine can resolve "same human" across servers and
        -- backends. The pair is bidirectional; queries walk both
        -- (a -> b) and (b -> a) lookups.
        --
        -- ``source`` carries the provenance: 'manual' for end user-
        -- authored mappings, 'auto_copy' for mappings written by the
        -- POST /api/users/copy_to_destination flow. Useful for the
        -- mapping panel UI to badge which rows the end user added vs
        -- which the system inferred.
        --
        -- The UNIQUE constraint covers exact-pair duplicates only;
        -- a row (A, alice, B, alice) is distinct from (B, alice, A,
        -- alice) at the table level but the read helper canonicalises
        -- pair direction so the listing UI never shows both forms.
        CREATE TABLE IF NOT EXISTS user_identity_map (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            server_a_id   TEXT NOT NULL,
            user_a_handle TEXT NOT NULL,
            server_b_id   TEXT NOT NULL,
            user_b_handle TEXT NOT NULL,
            source        TEXT NOT NULL DEFAULT 'manual'
                                  CHECK (source IN ('manual', 'auto_copy')),
            created_at    REAL NOT NULL,
            UNIQUE(server_a_id, user_a_handle, server_b_id, user_b_handle)
        );
        CREATE INDEX IF NOT EXISTS idx_user_identity_map_a
            ON user_identity_map(server_a_id, user_a_handle);
        CREATE INDEX IF NOT EXISTS idx_user_identity_map_b
            ON user_identity_map(server_b_id, user_b_handle);
    """),
    # Migration v12 (USER-MGMT-IDENTITY-AUDIT follow-up): app-generated
    # stable user identifier (``app_user_uuid``) on every (server, user)
    # row plus a re-key of ``user_identity_map`` from (server_id,
    # user_handle) tuples to (app_user_uuid_a, app_user_uuid_b) pairs.
    #
    # Format of app_user_uuid is documented in
    # :mod:`services.user_uuid`. Canonical 4-part form:
    #     <Service>-<HostNameSlug>-<server_uid>-<userkey>
    # Example: ``Plex-JadeTV-plex_a1b2c3d4-a3f9c2d8``.
    #
    # Two-step shape:
    #
    #   (a) ADD COLUMN app_user_uuid TEXT on ``managed_users`` +
    #       ``server_users``. Column is nullable here; the Python
    #       boot-time backfill (:func:`_backfill_app_user_uuids`)
    #       fills any NULL row on first init after upgrade. The
    #       partial UNIQUE indexes enforce uniqueness only on filled
    #       rows so the backfill never sees a constraint conflict
    #       between two NULLs.
    #
    #   (b) DROP the v11 ``user_identity_map`` table and re-create
    #       with (``user_a_uuid``, ``user_b_uuid``, source, created_at).
    #       Pre-release Legacy Policy applies (see CLAUDE.md): any
    #       end user-authored rows from the v11 shape are lost in
    #       the upgrade. The auto-link helper
    #       (:func:`auto_link_identity_map_by_backend_user_id`) re-
    #       derives same-backend pairs on the next managed-users sync;
    #       cross-backend end user mappings need to be re-entered via
    #       the Cross-Platform Preflight modal or the User Mapping
    #       panel after upgrade. A release note in the closing
    #       Finding[USER-MGMT-IDENTITY-IMPL] doc spells this out.
    #
    # Server-rename behaviour: on every server rename,
    # :func:`rewrite_app_user_uuid_host_slug_for_server` walks every
    # row carrying a UUID whose server_uid portion matches the renamed
    # server and updates the HostNameSlug segment. The server_uid +
    # userkey portion is the immutable identity anchor; the slug is
    # cosmetic.
    (12, """
        ALTER TABLE managed_users ADD COLUMN app_user_uuid TEXT;
        ALTER TABLE server_users  ADD COLUMN app_user_uuid TEXT;

        CREATE UNIQUE INDEX IF NOT EXISTS idx_managed_users_app_uuid
            ON managed_users(app_user_uuid)
            WHERE app_user_uuid IS NOT NULL;
        CREATE UNIQUE INDEX IF NOT EXISTS idx_server_users_app_uuid
            ON server_users(app_user_uuid)
            WHERE app_user_uuid IS NOT NULL;

        DROP TABLE IF EXISTS user_identity_map;
        CREATE TABLE user_identity_map (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            user_a_uuid   TEXT NOT NULL,
            user_b_uuid   TEXT NOT NULL,
            source        TEXT NOT NULL DEFAULT 'manual'
                                  CHECK (source IN ('manual', 'auto_copy')),
            created_at    REAL NOT NULL,
            UNIQUE(user_a_uuid, user_b_uuid)
        );
        CREATE INDEX IF NOT EXISTS idx_user_identity_map_a
            ON user_identity_map(user_a_uuid);
        CREATE INDEX IF NOT EXISTS idx_user_identity_map_b
            ON user_identity_map(user_b_uuid);
    """),
    # Migration v13 (USER-MGMT-IDENTITY-IMPL follow-up): transitive
    # closure on add_identity_map + cascade on delete.
    #
    # End user-reported bug: when an Emby account was manually mapped
    # to one of three Plex owners already linked to each other via
    # auto_copy rows (same Plex.tv backend_user_id), the new Emby row
    # only showed ONE Plex owner in its Identity Links panel - the
    # one it was directly mapped to. The other two Plex owners were
    # reachable transitively through the existing equivalence class
    # but the data model only stored direct edges.
    #
    # Fix: when add_identity_map writes a new edge, fan out across
    # the bipartite product of the two equivalence classes it joins.
    # The fanned-out rows carry ``derived_from_id`` pointing at the
    # row they were derived from. delete_identity_map cascades that
    # column: deleting a parent row drops every child that pointed
    # at it. This keeps deletion semantics intuitive (end user
    # removes the manual edge they typed, the transitive copies it
    # spawned go away).
    #
    # NULL derived_from_id semantics:
    #   * Manual rows the end user typed         -> NULL
    #   * auto_link by backend_user_id rows      -> NULL (independent
    #     of any other edge; the cross-server backend_user_id is the
    #     provenance, not another identity_map row)
    #   * Transitive fanout rows                 -> the id of the
    #     manual row that triggered the fanout
    #
    # Additive only; rows from v12 keep their default NULL and behave
    # as standalone (no cascade victims). New manual writes from v13+
    # populate derived_from_id correctly on every fan-out row.
    (13, """
        ALTER TABLE user_identity_map
            ADD COLUMN derived_from_id INTEGER;
        CREATE INDEX IF NOT EXISTS idx_user_identity_map_derived_from
            ON user_identity_map(derived_from_id)
            WHERE derived_from_id IS NOT NULL;
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
# on every end user's next start. Change it only when the schema
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
    # Best-effort backfill of any (server, user) row missing its
    # app_user_uuid. Runs OUTSIDE _init_lock so server_registry's own
    # lock can't deadlock against ours. Idempotent: every subsequent
    # call no-ops on installs where every row already carries a UUID.
    try:
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
    # Generate the app_user_uuid OUTSIDE the writer lock: the generator
    # uses SELECT probes (WAL handles read isolation lock-free). The
    # COALESCE inside ON CONFLICT preserves an existing UUID on update,
    # so this freshly-generated value is only used when the conflict
    # path picks NULL (new insert OR pre-backfill row).
    candidate_uuid = generate_unique_app_user_uuid(
        service_type=backend or "plex",
        server_id=server_id,
        server_uid=server_id,
    )
    with _DB_LOCK:
        conn.execute(
            """
            INSERT INTO server_users (
                server_id, user_handle, display_name, role, backend,
                backend_user_id, app_user_uuid, created_at, last_seen_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(server_id, user_handle) DO UPDATE SET
                display_name    = COALESCE(excluded.display_name,
                                           server_users.display_name),
                backend_user_id = COALESCE(excluded.backend_user_id,
                                           server_users.backend_user_id),
                app_user_uuid   = COALESCE(server_users.app_user_uuid,
                                           excluded.app_user_uuid),
                last_seen_at    = excluded.last_seen_at
            """,
            (server_id, handle, display_name, inferred_role, backend,
             backend_user_id, candidate_uuid, now, now),
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


# ── Library walk + last_seen tracking (Rule 2) ──────────────────────────────
#
# The library-walk job runs out-of-band (separate from snapshot /
# import / direct-transfer) and exists for one purpose: tick
# ``last_seen_at`` on every (item, server) row whose item is still
# present on the live server. The Prune Missing Items action reads
# those timestamps to identify items the server hasn't reported in
# more than N days - candidates the end user may want to remove.
#
# Critical design notes:
#
# * Auto-deletion is **never** triggered by the walk itself. Absence
#   on a single scan is not proof of permanent removal (library scan
#   could have missed, file could be temporarily offline). The walk
#   *only* refreshes timestamps; pruning is a separate, explicit,
#   end user-initiated action.
# * The walk records its own provenance in ``library_walks`` so the
#   prune UI can show "no walk in last X days, results may be stale"
#   before the end user commits to a destructive sweep.


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

    What gets deleted (end user-confirmed, never automatic):

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
    # rather than silently pruning nothing, so the end user gets a
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
    # Audit: dimension-table writes are end user-meaningful state
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
# Per-(server, username) records of the end users / managed users
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
    # Share-state columns (migration v9). Rows registered before v9
    # default to active_share=1 / is_pin_protected=0, and have NULL
    # refreshed_at. ``keys`` lookup handles the not-yet-migrated case
    # defensively; production code paths use the migrated columns.
    try:
        active_share = bool(row["active_share"])
    except (IndexError, KeyError):
        active_share = True
    try:
        is_pin_protected = bool(row["is_pin_protected"])
    except (IndexError, KeyError):
        is_pin_protected = False
    try:
        shared_state_refreshed_at = row["shared_state_refreshed_at"]
    except (IndexError, KeyError):
        shared_state_refreshed_at = None
    # Migration v10. Canonical per-user identifier (Plex.tv numeric
    # userID for ``service_type == 'plex'`` rows; the equivalent
    # backend-native id for Jellyfin / Emby once those adapters land).
    # Null on rows that pre-date v10 or that haven't yet resolved via
    # the share-state refresh path.
    try:
        backend_user_id = row["backend_user_id"]
    except (IndexError, KeyError):
        backend_user_id = None
    # Migration v12. App-generated stable user identifier
    # (USER-MGMT-IDENTITY-AUDIT follow-up). Null on rows that pre-date
    # v12 or that the boot-time backfill has not yet processed; the
    # resolution helper falls back to backend_user_id + username
    # matching when this is None.
    try:
        app_user_uuid = row["app_user_uuid"]
    except (IndexError, KeyError):
        app_user_uuid = None
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
        # Share-state (2026-05-15). UI consumers render greyed-out
        # rows for ``active_share=False`` and a PIN badge for
        # ``is_pin_protected=True``. ``shared_state_refreshed_at`` is
        # surfaced in tooltips so the end user knows how fresh the
        # determination is. ``None`` means "never refreshed since
        # the share-state migration landed" - the next sync will
        # populate it.
        "active_share": active_share,
        "is_pin_protected": is_pin_protected,
        "shared_state_refreshed_at": shared_state_refreshed_at,
        "backend_user_id": backend_user_id,
        "app_user_uuid": app_user_uuid,
    }


_MANAGED_USER_COLUMNS = (
    "id, server_id, username, display_name, service_type, kind, "
    "machine_identifier, auth_token_enc, plex_home_pin_enc, "
    "service_password_enc, last_seen, tombstoned, created_at, updated_at, "
    "active_share, is_pin_protected, shared_state_refreshed_at, "
    "backend_user_id, app_user_uuid"
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
    silently wipe every end user-typed PIN).

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
    # Generate a candidate app_user_uuid outside the writer lock. The
    # COALESCE on the ON CONFLICT path preserves any existing UUID;
    # the candidate is only used when the conflict picks NULL (new row
    # OR pre-backfill row).
    candidate_uuid = generate_unique_app_user_uuid(
        service_type=service_type or "plex",
        server_id=server_id,
        server_uid=server_id,
    )
    with _DB_LOCK:
        conn.execute(
            """
            INSERT INTO managed_users (
                server_id, username, display_name, service_type, kind,
                machine_identifier, last_seen, app_user_uuid,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(server_id, username) DO UPDATE SET
                display_name       = COALESCE(excluded.display_name, managed_users.display_name),
                service_type       = excluded.service_type,
                kind               = excluded.kind,
                machine_identifier = COALESCE(excluded.machine_identifier, managed_users.machine_identifier),
                last_seen          = COALESCE(excluded.last_seen, managed_users.last_seen),
                app_user_uuid      = COALESCE(managed_users.app_user_uuid, excluded.app_user_uuid),
                updated_at         = excluded.updated_at
            """,
            (
                server_id, username, display_name, service_type, kind,
                machine_identifier, last_seen, candidate_uuid, now, now,
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

    server_row = server_registry.get_server_by_id(server_id, include_token=False) or {}
    machine_id = server_row.get("machine_identifier") or None
    # Read the registered backend type so Jellyfin / Emby users get the
    # correct service_type stamped on their managed_users row. Was
    # hardcoded 'plex' when this helper landed (live API was Plex-only
    # at the time); after developer's adapter PRs every backend goes
    # through here so the hardcode mislabels every Emby + Jellyfin row
    # as Plex. The registered row's service_type column is the source of
    # truth (servers.json drives it; the CHECK constraint on
    # managed_users.service_type enforces the same three values).
    server_service_type = (server_row.get("service_type") or "plex").strip().lower()
    if server_service_type not in ("plex", "emby", "jellyfin"):
        server_service_type = "plex"  # defensive: unknown backend falls back
    now = time.time()
    # PR-11.1 - usernames the end user has globally tombstoned never
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
                service_type=server_service_type,
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
    # ── Share-state cross-reference (2026-05-15) ─────────────────────
    # After the per-row upserts above (which keep the "do we know
    # this user exists on this server" surface unchanged), refresh
    # the three Plex.tv-sourced share-state columns:
    #   * active_share        - actually has an active share on THIS server
    #   * is_pin_protected    - has a Plex Home PIN set
    #   * shared_state_refreshed_at - when we last confirmed via Plex.tv
    #
    # Best-effort. A failed fetch leaves prior state intact and a
    # warning lands on the run log; the UI surfaces refreshed_at so
    # the end user can tell whether the badge is fresh.
    share_state_error: Optional[str] = None
    try:
        _refresh_share_state(
            server_id=server_id, machine_id=machine_id, logger=log_,
        )
    except Exception as exc:  # pragma: no cover (defensive)
        log_.exception(
            "share-state cross-reference failed for server %r", server_id,
        )
        share_state_error = f"{type(exc).__name__}: {exc}"

    # USER-MGMT-IDENTITY-AUDIT R-2: after every sync, re-derive
    # auto_copy identity_map rows from same-(service_type,
    # backend_user_id) pairs across servers. Closes the cross-server
    # owner case (and same-human-different-username case) with zero
    # end user action. Best-effort: a failure here never blocks the
    # sync result. Idempotent so repeated calls write each pair once
    # and silently skip duplicates on subsequent runs.
    auto_link_error: Optional[str] = None
    auto_link_pairs = 0
    try:
        auto_link_summary = auto_link_identity_map_by_backend_user_id()
        auto_link_pairs = auto_link_summary.get("pairs_written") or 0
    except Exception as exc:  # pragma: no cover (defensive)
        log_.exception(
            "auto_link_identity_map_by_backend_user_id failed for "
            "server %r; identity-map auto-derivation will retry on "
            "next sync.",
            server_id,
        )
        auto_link_error = f"{type(exc).__name__}: {exc}"

    return {
        "synced": synced,
        "skipped_global_tombstones": skipped_global,
        "source_error": result.get("error"),
        "share_state_error": share_state_error,
        "auto_link_pairs_written": auto_link_pairs,
        "auto_link_error": auto_link_error,
        "error": None,
    }


def _refresh_share_state(
    *,
    server_id: str,
    machine_id: Optional[str],
    logger: logging.Logger,
) -> None:
    """
    Cross-reference the live Plex.tv shared_servers + home/users
    endpoints against the local managed_users table for ``server_id``
    and stamp the three share-state columns
    (``active_share``, ``is_pin_protected``,
    ``shared_state_refreshed_at``).

    Resolution rules per row:

      * ``active_share=1`` when the username appears in
        ``shared_servers`` for this machine identifier, OR when the
        row is the owner row (the admin is always "shared" with
        themselves). ``active_share=0`` otherwise.
      * ``is_pin_protected=1`` when the username appears in
        ``/api/home/users`` with ``protected=1``. ``0`` otherwise.

    Fails soft. Missing machine_id, missing PlexAccount, network
    failure, or empty fetch results leave prior state intact (no
    columns updated) and we log a warning.
    """
    if not machine_id:
        logger.debug(
            "_refresh_share_state: server %r has no machine_identifier; "
            "skipping share-state refresh.", server_id,
        )
        return

    # Connect to Plex via the registered server's stored token so we
    # have an account object to introspect. We use the existing
    # connect_to_server primitive rather than building a parallel
    # auth path; failures here mean "couldn't refresh," not "prune".
    from server import server_registry
    try:
        _conn = server_registry.connect_registered_server(
            server_id, logger=logger,
        )
        srv = _conn.server
    except Exception:
        logger.warning(
            "_refresh_share_state: could not connect to server %r; "
            "share-state refresh skipped.", server_id,
        )
        return

    try:
        account = srv.myPlexAccount()
    except Exception:
        logger.warning(
            "_refresh_share_state: myPlexAccount() failed for server %r; "
            "share-state refresh skipped.", server_id,
        )
        return

    from services import plex_shares
    shared = plex_shares.fetch_shared_servers(account, machine_id)
    protected_map = plex_shares.fetch_home_users_protected_map(account)
    # Friends index gives us {plex_user_id: {username, title, email}} for
    # every friend on the account. SharedServer payloads from Plex.tv
    # routinely omit `title` / `email`; the friends list always carries
    # them. We use it both for alias enrichment (legacy path, for rows
    # not yet ID-resolved) and as a sanity cross-check for the IDs we
    # do see.
    friends_index = plex_shares.fetch_friends_index(account)

    # Owner's Plex.tv userID. Stored on the owner managed_users row for
    # Feature 5 cross-server identity links; not used for active_share
    # decisions (owner rows are unconditionally active). Best-effort
    # against several attribute names plexapi exposes across versions.
    owner_user_id = ""
    for attr in ("id", "userID", "userid"):
        val = getattr(account, attr, "") or ""
        if val:
            owner_user_id = str(val).strip()
            break

    # Defensive: if BOTH share + protected fetches failed, do not touch
    # the columns - the prior state plus a stale timestamp is more
    # honest than zeroing everything out. friends_index alone can't
    # determine active_share so we don't gate on it.
    if shared is None and protected_map is None:
        logger.warning(
            "_refresh_share_state: both shared_servers and home/users "
            "lookups failed for server %r; share-state preserved.",
            server_id,
        )
        return

    # ── Build matcher indices ───────────────────────────────────────
    # Migration v10 (2026-05-15 follow-up #3): once a row has its
    # ``backend_user_id`` populated, we match on that and ignore the
    # alias set entirely. The alias path is a backfill fallback for
    # rows that pre-date v10 or haven't yet been resolved.
    #
    #   * ``shared_by_userid`` maps Plex.tv userID -> SharedServer
    #     entry. The ID-first matcher is one dict lookup per row.
    #   * ``alias_to_userid`` maps lowercased alias string ->
    #     Plex.tv userID. When an alias hits, we both flip
    #     active_share=1 AND backfill the row's backend_user_id so
    #     subsequent refreshes use the definitive ID path.
    shared_by_userid: Dict[str, Dict[str, Any]] = {}
    alias_to_userid: Dict[str, str] = {}
    # Defensive: a SharedServer entry without a plex_user_id (older
    # plexapi shapes, sparse XML) still carries aliases we can match
    # on. Track them in ``seen_aliases`` so name matching still works
    # for the row; backfill simply can't happen in that case.
    seen_aliases: set = set()
    if shared is not None:
        for entry in shared:
            pid = (entry.get("plex_user_id") or "").strip()
            if pid:
                shared_by_userid[pid] = entry
            # Direct fields straight off the SharedServer payload.
            for key in ("username", "title", "email"):
                val = (entry.get(key) or "").strip().lower()
                if val:
                    seen_aliases.add(val)
                    if pid:
                        alias_to_userid[val] = pid
            # Enriched fields from the friends index (SharedServer
            # payloads often omit title/email; friends always carries
            # them).
            if pid and friends_index and pid in friends_index:
                friend = friends_index[pid]
                for key in ("username", "title", "email"):
                    val = (friend.get(key) or "").strip().lower()
                    if val:
                        seen_aliases.add(val)
                        alias_to_userid[val] = pid

    # PIN lookup is keyed by whatever name plexapi / REST returned for
    # the home-user row. Lowercase normalised the same way so a
    # SystemAccount display-name row resolves against a /api/home/users
    # title row.
    protected_lookup: Dict[str, bool] = {}
    if protected_map is not None:
        for k, v in protected_map.items():
            key = (k or "").strip().lower()
            if key:
                protected_lookup[key] = bool(v)

    now = time.time()
    conn = _require_conn()
    with _DB_LOCK:
        rows = conn.execute(
            "SELECT id, username, kind, backend_user_id "
            "FROM managed_users WHERE server_id = ?",
            (server_id,),
        ).fetchall()
        for r in rows:
            uname = (r["username"] or "").strip().lower()
            kind = r["kind"] or "managed"
            existing_uid = (r["backend_user_id"] or "").strip()

            # Three-arm decision:
            #   * shared fetch failed -> leave active_share alone (None)
            #   * owner -> always active; backfill ID if missing
            #   * managed -> ID-first match, alias fallback w/ backfill
            backfill_uid: Optional[str] = None
            if kind == "owner":
                active_val: Optional[int] = 1
                if not existing_uid and owner_user_id:
                    backfill_uid = owner_user_id
            elif shared is None:
                active_val = None
            elif existing_uid:
                # Definitive ID-based match. Immune to name variants,
                # Unicode quirks, display-name drift, and same-name
                # collisions.
                active_val = 1 if existing_uid in shared_by_userid else 0
            else:
                matched_uid = alias_to_userid.get(uname)
                if matched_uid:
                    active_val = 1
                    # Persist the ID so the next refresh skips alias
                    # matching entirely for this row.
                    backfill_uid = matched_uid
                elif uname in seen_aliases:
                    # SharedServer entry carried no plex_user_id but
                    # the alias still matched. Stay active; just
                    # can't backfill.
                    active_val = 1
                else:
                    active_val = 0

            # PIN status. None means "couldn't fetch home/users";
            # leave the column alone. Friends never appear in
            # /api/home/users so they fall through to the default
            # `0` from `protected_lookup.get(uname, False)`.
            if protected_map is None:
                pin_val: Optional[int] = None
            else:
                pin_val = 1 if protected_lookup.get(uname, False) else 0

            conn.execute(
                """
                UPDATE managed_users
                SET active_share = COALESCE(?, active_share),
                    is_pin_protected = COALESCE(?, is_pin_protected),
                    shared_state_refreshed_at = ?,
                    backend_user_id = COALESCE(?, backend_user_id)
                WHERE id = ?
                """,
                (active_val, pin_val, now, backfill_uid, r["id"]),
            )


def set_managed_user_share_state(
    *,
    server_id: str,
    username: str,
    active_share: Optional[bool] = None,
    is_pin_protected: Optional[bool] = None,
    refreshed_at: Optional[float] = None,
    backend_user_id: Optional[str] = None,
) -> None:
    """
    Stamp the share-state columns on a single managed_users row.

    Each parameter is independent: ``None`` means "do not change this
    column" so the helper composes with the COALESCE-based update in
    :func:`_refresh_share_state`. ``refreshed_at`` defaults to
    ``time.time()`` so test seeds get a sensible non-NULL timestamp.
    ``backend_user_id`` (migration v10) seeds the canonical Plex.tv
    userID for tests that want to verify the ID-first matcher path.

    Public surface for test seeding and the future per-server "refresh
    shared state" path. Live sync still routes through
    :func:`sync_managed_users_from_live` -> :func:`_refresh_share_state`.
    Raises ``ValueError`` if the row isn't present.
    """
    if not server_id or not username:
        raise ValueError("server_id and username are required")
    conn = _require_conn()
    ts = refreshed_at if refreshed_at is not None else time.time()
    a_val = None if active_share is None else (1 if active_share else 0)
    p_val = None if is_pin_protected is None else (1 if is_pin_protected else 0)
    with _DB_LOCK:
        cur = conn.execute(
            """
            UPDATE managed_users
            SET active_share = COALESCE(?, active_share),
                is_pin_protected = COALESCE(?, is_pin_protected),
                shared_state_refreshed_at = ?,
                backend_user_id = COALESCE(?, backend_user_id)
            WHERE server_id = ? AND username = ?
            """,
            (a_val, p_val, ts, backend_user_id, server_id, username),
        )
        if cur.rowcount == 0:
            raise ValueError(
                f"no managed_users row for server_id={server_id!r} username={username!r}"
            )


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
# The User Management 'Hide user' modal lets the end user pick which
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
    # end user can also see "clear" operations.
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

# ── app_user_uuid helpers (USER-MGMT-IDENTITY-AUDIT follow-up) ──────────────
#
# The app-generated stable user identifier (``app_user_uuid``) is the
# cross-server identity anchor populated on every managed_users /
# server_users row and used as the primary key in user_identity_map
# pairs. Format and design rationale live in :mod:`services.user_uuid`.

def _server_host_name(server_id: str) -> str:
    """Look up the friendly server name for the HostNameSlug portion of
    an app_user_uuid. Best-effort: returns the empty string (which the
    slugifier collapses to ``"Unnamed"``) when the registry can't be
    read or the server id is unknown. Avoids hard-coupling media_db
    init order to server_registry being fully available."""
    try:
        # Late import to avoid the legacy media_db <- server_registry
        # cycle: server_registry already imports media_db for
        # ``rewrite_server_ids_in_identity_map`` and friends, and
        # importing it at module load here would invert the dependency
        # under some test orders.
        from server import server_registry
        row = server_registry.get_server_by_id(server_id, include_token=False)
        if not row:
            return ""
        return str(row.get("name") or "")
    except Exception:
        return ""


def generate_unique_app_user_uuid(
    *,
    service_type: str,
    server_id: str,
    server_uid: str,
    max_attempts: int = 8,
) -> str:
    """Generate an ``app_user_uuid`` that is not already present in
    ``managed_users.app_user_uuid`` OR ``server_users.app_user_uuid``.

    Retries up to ``max_attempts`` times on UNIQUE collision (the
    8-hex userkey has ~4.29B distinct values per server so a single-
    retry case is essentially never hit in practice; the loop is a
    correctness guarantee, not a hot path).

    Raises ``RuntimeError`` if every attempt collides - this only
    happens when the database is corrupt enough that the partial
    unique indexes are broken, in which case the caller wants the
    loud failure rather than silently inserting a duplicate.

    ``server_id`` is the registered server's id used to look up the
    friendly name; ``server_uid`` is the same string when developer's
    prefixed UID scheme is in play (the v9 boot migration rewrites
    bare UUIDs to ``<service>_<uuid>`` form), so both args usually
    carry the same value. They are kept distinct so a future caller
    that wants to mint a UUID for a row before the server is fully
    registered (e.g. a test fixture) can pass an explicit server_uid.
    """
    host_name = _server_host_name(server_id)
    conn = _require_conn()
    for _ in range(max(1, int(max_attempts))):
        user_key = generate_user_key()
        candidate = build_app_user_uuid(
            service_type=service_type,
            host_name=host_name,
            server_uid=server_uid,
            user_key=user_key,
        )
        # Probe both tables; either hit means we need a fresh key.
        # Cheap O(1) lookups via the partial unique indexes.
        hit_mu = conn.execute(
            "SELECT 1 FROM managed_users WHERE app_user_uuid = ? LIMIT 1",
            (candidate,),
        ).fetchone()
        hit_su = conn.execute(
            "SELECT 1 FROM server_users WHERE app_user_uuid = ? LIMIT 1",
            (candidate,),
        ).fetchone()
        if hit_mu is None and hit_su is None:
            return candidate
    raise RuntimeError(
        "generate_unique_app_user_uuid: exhausted retries; "
        "the app_user_uuid space appears exhausted for this server. "
        "Inspect managed_users.app_user_uuid for duplicates."
    )


def _backfill_app_user_uuids() -> None:
    """Walk ``managed_users`` and ``server_users`` for rows with NULL
    ``app_user_uuid`` and fill them with freshly-generated UUIDs.

    Idempotent: a second call after every row is filled sees an empty
    work queue and exits immediately. Safe to call from
    :func:`init_media_db` on every boot.

    Best-effort: per-row insert failures are logged and the loop
    continues. A row left NULL stays NULL and will be retried on the
    next call; resolution paths fall back to handle matching until the
    row is filled.
    """
    if _conn is None:
        return  # init still in flight; caller will retry
    rows_mu = _conn.execute(
        "SELECT server_id, service_type, username "
        "FROM managed_users WHERE app_user_uuid IS NULL"
    ).fetchall()
    rows_su = _conn.execute(
        "SELECT server_id, backend, user_handle "
        "FROM server_users WHERE app_user_uuid IS NULL"
    ).fetchall()
    if not rows_mu and not rows_su:
        return
    log.info(
        "Backfilling app_user_uuid: %d managed_users row(s) + "
        "%d server_users row(s).",
        len(rows_mu), len(rows_su),
    )
    with _DB_LOCK:
        for r in rows_mu:
            try:
                uuid = generate_unique_app_user_uuid(
                    service_type=r["service_type"] or "plex",
                    server_id=r["server_id"],
                    server_uid=r["server_id"],
                )
                _conn.execute(
                    "UPDATE managed_users SET app_user_uuid = ? "
                    "WHERE server_id = ? AND username = ? "
                    "AND app_user_uuid IS NULL",
                    (uuid, r["server_id"], r["username"]),
                )
            except Exception:
                log.exception(
                    "_backfill_app_user_uuids: failed for managed_users "
                    "(server=%r, username=%r); will retry next boot",
                    r["server_id"], r["username"],
                )
        for r in rows_su:
            try:
                uuid = generate_unique_app_user_uuid(
                    service_type=r["backend"] or "plex",
                    server_id=r["server_id"],
                    server_uid=r["server_id"],
                )
                _conn.execute(
                    "UPDATE server_users SET app_user_uuid = ? "
                    "WHERE server_id = ? AND user_handle = ? "
                    "AND app_user_uuid IS NULL",
                    (uuid, r["server_id"], r["user_handle"]),
                )
            except Exception:
                log.exception(
                    "_backfill_app_user_uuids: failed for server_users "
                    "(server=%r, handle=%r); will retry next boot",
                    r["server_id"], r["user_handle"],
                )


def get_managed_user_app_uuid(
    server_id: str, username: str,
) -> Optional[str]:
    """Return the ``app_user_uuid`` for the ``managed_users`` row
    matching ``(server_id, username)``, or ``None`` if the row does not
    exist (or has not yet been backfilled - which should not happen on
    a fully-initialised install but is defensive)."""
    if not (server_id or "").strip() or not (username or "").strip():
        return None
    conn = _require_conn()
    row = conn.execute(
        "SELECT app_user_uuid FROM managed_users "
        "WHERE server_id = ? AND username = ?",
        (str(server_id), str(username)),
    ).fetchone()
    if row is None:
        return None
    return row["app_user_uuid"]


def get_server_user_app_uuid(
    server_id: str, user_handle: str,
) -> Optional[str]:
    """``managed_users``-sibling lookup against ``server_users``."""
    if not (server_id or "").strip():
        return None
    conn = _require_conn()
    row = conn.execute(
        "SELECT app_user_uuid FROM server_users "
        "WHERE server_id = ? AND user_handle = ?",
        (str(server_id), str(user_handle or "")),
    ).fetchone()
    if row is None:
        return None
    return row["app_user_uuid"]


def get_row_by_app_user_uuid(app_user_uuid: str) -> Optional[Dict[str, Any]]:
    """Resolve an ``app_user_uuid`` back to its (server, user) coordinates.

    Returns a dict ``{table, server_id, handle, display_name, service_type,
    role, backend_user_id, app_user_uuid}`` or ``None`` if the UUID does
    not exist on either table. The ``table`` field is ``"managed_users"``
    or ``"server_users"`` so the caller knows which side to address.

    Used by the resolution helper and the UI identity-links panel to
    walk identity_map edges back to the row that holds the credentials
    and display fields.
    """
    if not (app_user_uuid or "").strip():
        return None
    conn = _require_conn()
    row = conn.execute(
        "SELECT server_id, username AS handle, display_name, service_type, "
        "kind AS role, app_user_uuid "
        "FROM managed_users WHERE app_user_uuid = ?",
        (app_user_uuid,),
    ).fetchone()
    if row is not None:
        # Pull backend_user_id from the same row.
        bk = conn.execute(
            "SELECT backend_user_id FROM managed_users "
            "WHERE app_user_uuid = ?",
            (app_user_uuid,),
        ).fetchone()
        return {
            "table":            "managed_users",
            "server_id":        row["server_id"],
            "handle":           row["handle"],
            "display_name":     row["display_name"],
            "service_type":     row["service_type"],
            "role":             row["role"],
            "backend_user_id":  bk["backend_user_id"] if bk else None,
            "app_user_uuid":    row["app_user_uuid"],
        }
    row = conn.execute(
        "SELECT server_id, user_handle AS handle, display_name, backend AS service_type, "
        "role, backend_user_id, app_user_uuid "
        "FROM server_users WHERE app_user_uuid = ?",
        (app_user_uuid,),
    ).fetchone()
    if row is not None:
        return {
            "table":            "server_users",
            "server_id":        row["server_id"],
            "handle":           row["handle"],
            "display_name":     row["display_name"],
            "service_type":     row["service_type"],
            "role":             row["role"],
            "backend_user_id":  row["backend_user_id"],
            "app_user_uuid":    row["app_user_uuid"],
        }
    return None


# ── user_identity_map (v12 shape: app_user_uuid pairs) ──────────────────────
#
# Storage shape after migration v12: ``(user_a_uuid, user_b_uuid,
# source, created_at)``. Lookups walk both directions
# (a -> b and b -> a) so the caller doesn't need to know which side of
# the pair the user lives on.

def _equivalence_class_for_uuid(app_user_uuid: str) -> List[str]:
    """Return every ``app_user_uuid`` reachable from the supplied one
    via ``user_identity_map`` edges (BFS).

    Includes the starting UUID itself. Each row in identity_map is a
    bidirectional edge, so a single row connects both endpoints'
    classes. Empty list when ``app_user_uuid`` is missing or itself
    has no edges.

    Used by :func:`add_identity_map` to fan out a new manual edge
    across the bipartite product of the two classes it joins so
    every transitively-equivalent pair lands as a row in identity_map.
    """
    if not (app_user_uuid or "").strip():
        return []
    conn = _require_conn()
    seen: set = {app_user_uuid}
    frontier: List[str] = [app_user_uuid]
    while frontier:
        next_frontier: List[str] = []
        for uuid in frontier:
            rows = conn.execute(
                """
                SELECT user_a_uuid, user_b_uuid FROM user_identity_map
                WHERE user_a_uuid = ? OR user_b_uuid = ?
                """,
                (uuid, uuid),
            ).fetchall()
            for r in rows:
                other = r["user_b_uuid"] if r["user_a_uuid"] == uuid else r["user_a_uuid"]
                if other not in seen:
                    seen.add(other)
                    next_frontier.append(other)
        frontier = next_frontier
    return sorted(seen)


def add_identity_map(
    *,
    user_a_uuid: Optional[str] = None,
    user_b_uuid: Optional[str] = None,
    server_a_id: Optional[str] = None,
    user_a_handle: Optional[str] = None,
    server_b_id: Optional[str] = None,
    user_b_handle: Optional[str] = None,
    source: str = "manual",
) -> Optional[int]:
    """Insert one (A, B) identity-link pair plus its transitive closure.

    Two call shapes are supported so existing callers in
    developer's CRUD endpoints (which build the pair from
    ``(server_id, user_handle)`` tuples) keep working unchanged:

      * UUID-direct:  ``add_identity_map(user_a_uuid=..., user_b_uuid=...)``
      * Tuple-resolve: ``add_identity_map(server_a_id=..., user_a_handle=...,
                       server_b_id=..., user_b_handle=...)``

    When the tuple-resolve form is used, the helper looks up each side's
    ``app_user_uuid`` from ``managed_users`` first, then falls back to
    ``server_users``. Raises ``ValueError`` if either side cannot be
    resolved to a UUID (end user must register / sync the server first).

    Transitive fanout (v13+): after the primary row is written, the
    helper walks the equivalence class of each endpoint and writes one
    auto_copy row per missing bipartite pair. Each fanned-out row
    carries ``derived_from_id`` pointing at the primary row so
    :func:`delete_identity_map` can cascade-delete them cleanly when
    the parent is removed.

    Returns the new row id of the PRIMARY pair on insert, ``None``
    when the primary pair already exists (UNIQUE conflict; the
    transitive fanout is still attempted defensively in case the
    equivalence class grew since the last write).
    """
    a_uuid = (user_a_uuid or "").strip() or None
    b_uuid = (user_b_uuid or "").strip() or None
    if a_uuid is None:
        if not (server_a_id or "").strip() or not (user_a_handle or "").strip():
            raise ValueError(
                "add_identity_map: provide either user_a_uuid OR both "
                "server_a_id and user_a_handle."
            )
        a_uuid = (
            get_managed_user_app_uuid(server_a_id, user_a_handle)
            or get_server_user_app_uuid(server_a_id, user_a_handle)
        )
        if not a_uuid:
            raise ValueError(
                f"add_identity_map: no app_user_uuid for "
                f"(server={server_a_id!r}, handle={user_a_handle!r}); "
                f"sync managed_users for that server first."
            )
    if b_uuid is None:
        if not (server_b_id or "").strip() or not (user_b_handle or "").strip():
            raise ValueError(
                "add_identity_map: provide either user_b_uuid OR both "
                "server_b_id and user_b_handle."
            )
        b_uuid = (
            get_managed_user_app_uuid(server_b_id, user_b_handle)
            or get_server_user_app_uuid(server_b_id, user_b_handle)
        )
        if not b_uuid:
            raise ValueError(
                f"add_identity_map: no app_user_uuid for "
                f"(server={server_b_id!r}, handle={user_b_handle!r}); "
                f"sync managed_users for that server first."
            )
    if a_uuid == b_uuid:
        raise ValueError(
            "add_identity_map: refuse to map a UUID to itself."
        )
    src = (source or "manual").strip().lower()
    if src not in ("manual", "auto_copy"):
        src = "manual"
    conn = _require_conn()
    # Compute equivalence classes BEFORE the primary insert. After the
    # insert the two classes are merged (the new edge bridges them),
    # so capturing each side's class beforehand is the only way to
    # know which cross-pairs need to be fanned out.
    class_a = _equivalence_class_for_uuid(a_uuid)
    class_b = _equivalence_class_for_uuid(b_uuid)
    # Primary insert.
    primary_id: Optional[int] = None
    try:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO user_identity_map (
                user_a_uuid, user_b_uuid, source, created_at, derived_from_id
            ) VALUES (?, ?, ?, ?, NULL)
            """,
            (a_uuid, b_uuid, src, time.time()),
        )
        if (cur.rowcount or 0) > 0 and cur.lastrowid:
            primary_id = int(cur.lastrowid)
    except Exception:
        log.exception("add_identity_map: primary insert failed")
        raise
    # Transitive fanout. Even when the primary insert was a duplicate
    # no-op (primary_id is None), we still fan out so a class that
    # has grown since the last fan attempt catches up.
    #
    # The fanout target is the BIPARTITE PRODUCT of class_a and
    # class_b minus the pair we just inserted. Each derived row
    # carries derived_from_id pointing at the primary row when one
    # exists; otherwise NULL (the cascade has nothing to track).
    if class_a and class_b:
        now = time.time()
        # Compute the parent id used for derived_from on every
        # fanned-out row. When the primary was a no-op, look up the
        # existing row id for the (a, b) ordered pair so the cascade
        # still works.
        parent_id = primary_id
        if parent_id is None:
            existing = conn.execute(
                "SELECT id FROM user_identity_map "
                "WHERE user_a_uuid = ? AND user_b_uuid = ?",
                (a_uuid, b_uuid),
            ).fetchone()
            if existing:
                parent_id = int(existing["id"])
        # Walk every cross-pair. Skip the primary's exact-ordered pair
        # so we don't write a derived duplicate of the parent itself.
        for x in class_a:
            for y in class_b:
                if x == y:
                    continue
                if x == a_uuid and y == b_uuid:
                    continue  # the primary row
                # Order the pair lexicographically so (X, Y) and (Y, X)
                # collapse to one canonical row. The UNIQUE constraint
                # on (user_a_uuid, user_b_uuid) is on the ordered pair;
                # without canonicalisation we would write both directions.
                lo, hi = (x, y) if x < y else (y, x)
                try:
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO user_identity_map (
                            user_a_uuid, user_b_uuid, source, created_at,
                            derived_from_id
                        ) VALUES (?, ?, 'auto_copy', ?, ?)
                        """,
                        (lo, hi, now, parent_id),
                    )
                except Exception:
                    log.exception(
                        "add_identity_map: transitive fanout row "
                        "(%r, %r) insert failed; continuing.",
                        lo, hi,
                    )
    return primary_id


def delete_identity_map(map_id: int) -> bool:
    """Remove one identity-map row by id and cascade to its derived
    children (rows with ``derived_from_id`` equal to this row's id).

    The cascade only touches rows the helper itself wrote during a
    transitive fanout; auto_link-by-backend_user_id rows and manual
    rows the end user typed separately are never collateral damage.

    Returns True when at least the named row was deleted, False when
    ``map_id`` did not exist.
    """
    try:
        map_id = int(map_id)
    except (TypeError, ValueError):
        return False
    conn = _require_conn()
    # Cascade first so the parent row's id is still valid for the
    # children's WHERE clause. SQLite doesn't enforce ON DELETE CASCADE
    # on FKs added via ALTER TABLE ADD COLUMN; we do it explicitly.
    try:
        children = conn.execute(
            "DELETE FROM user_identity_map WHERE derived_from_id = ?",
            (map_id,),
        )
        cascaded = children.rowcount or 0
    except Exception:
        log.exception(
            "delete_identity_map: cascade-delete for parent id=%r "
            "failed; continuing with parent delete.",
            map_id,
        )
        cascaded = 0
    cur = conn.execute(
        "DELETE FROM user_identity_map WHERE id = ?", (map_id,),
    )
    if (cur.rowcount or 0) > 0 and cascaded:
        log.info(
            "delete_identity_map: removed parent id=%r and %d cascaded "
            "child row(s).", map_id, cascaded,
        )
    return (cur.rowcount or 0) > 0


def list_identity_maps() -> List[Dict[str, Any]]:
    """Return every identity-map row in insertion order.

    Each row carries both the v12 UUID pair AND the resolved
    ``(server_id, user_handle)`` tuples on each side, so the v11-shaped
    UI surfaces (developer's UserMappingPanel) keep rendering without a
    payload migration. UUIDs that no longer resolve to a row
    (server removed, user deleted) surface ``server_id=None`` /
    ``user_handle=None`` on that side; the panel can show a dimmed
    "unresolved" badge in that case.
    """
    conn = _require_conn()
    rows = conn.execute(
        """
        SELECT id, user_a_uuid, user_b_uuid, source, created_at
        FROM user_identity_map
        ORDER BY created_at ASC, id ASC
        """,
    ).fetchall()
    out: List[Dict[str, Any]] = []
    for r in rows:
        a_row = get_row_by_app_user_uuid(r["user_a_uuid"]) or {}
        b_row = get_row_by_app_user_uuid(r["user_b_uuid"]) or {}
        out.append({
            "id":             int(r["id"]),
            "user_a_uuid":    r["user_a_uuid"],
            "user_b_uuid":    r["user_b_uuid"],
            "server_a_id":    a_row.get("server_id"),
            "user_a_handle":  a_row.get("handle"),
            "server_b_id":    b_row.get("server_id"),
            "user_b_handle":  b_row.get("handle"),
            "source":         r["source"],
            "created_at":     float(r["created_at"]),
        })
    return out


def get_identity_maps_for_user(
    server_id: str, user_handle: str,
) -> List[Dict[str, Any]]:
    """Return every (other_server, other_handle, other_uuid) row linked
    to the supplied (server_id, user_handle).

    Resolves (server_id, user_handle) -> app_user_uuid first, then
    walks both A->B and B->A so the caller doesn't need to know which
    side of the pair the user lives on. Empty list when the source
    has no app_user_uuid yet (pre-backfill row) or no map entries.
    """
    if not (server_id or "").strip() or not (user_handle or "").strip():
        return []
    my_uuid = (
        get_managed_user_app_uuid(server_id, user_handle)
        or get_server_user_app_uuid(server_id, user_handle)
    )
    if not my_uuid:
        return []
    return get_identity_maps_for_uuid(my_uuid)


def get_identity_maps_for_uuid(
    app_user_uuid: str,
) -> List[Dict[str, Any]]:
    """Same as :func:`get_identity_maps_for_user` but takes the
    app_user_uuid directly. Preferred entry point for the resolver:
    one lookup instead of two."""
    if not (app_user_uuid or "").strip():
        return []
    conn = _require_conn()
    rows = conn.execute(
        """
        SELECT id, user_a_uuid, user_b_uuid, source, created_at
        FROM user_identity_map
        WHERE user_a_uuid = ? OR user_b_uuid = ?
        ORDER BY created_at ASC, id ASC
        """,
        (app_user_uuid, app_user_uuid),
    ).fetchall()
    out: List[Dict[str, Any]] = []
    for r in rows:
        if r["user_a_uuid"] == app_user_uuid:
            other_uuid = r["user_b_uuid"]
        else:
            other_uuid = r["user_a_uuid"]
        other_row = get_row_by_app_user_uuid(other_uuid) or {}
        out.append({
            "id":                 int(r["id"]),
            "other_user_uuid":    other_uuid,
            "other_server_id":    other_row.get("server_id"),
            "other_user_handle":  other_row.get("handle"),
            "other_display_name": other_row.get("display_name"),
            "other_service_type": other_row.get("service_type"),
            "other_role":         other_row.get("role"),
            "source":             r["source"],
            "created_at":         float(r["created_at"]),
        })
    return out


# ── Auto-link by backend_user_id (R-2) ──────────────────────────────────────
#
# Plex.tv issues stable numeric userIDs per human. Two managed_users
# rows with the same (service_type, backend_user_id) across distinct
# server_ids ARE the same human by the backend's own definition. This
# helper walks for those pairs and writes auto_copy identity_map rows.
# Idempotent (INSERT OR IGNORE) so it can run after every managed-users
# sync without producing duplicates. Scoped by service_type so a
# coincidental ID collision across backend ID spaces never creates a
# false link.

def auto_link_identity_map_by_backend_user_id() -> Dict[str, int]:
    """Derive ``identity_map`` entries from same-(service_type, backend_user_id)
    rows across distinct server_ids.

    Returns ``{"pairs_written": int, "pairs_skipped_duplicate": int,
    "groups_seen": int}``. Best-effort: per-pair failures are caught and
    logged so a single corrupt row never blocks the rest.

    Safe to call repeatedly; subsequent runs no-op on already-mapped
    pairs via INSERT OR IGNORE.
    """
    conn = _require_conn()
    # Pull every (service_type, backend_user_id, server_id, app_user_uuid)
    # tuple where backend_user_id is populated and the row has its UUID
    # backfilled. Group in Python (SQLite GROUP_CONCAT is awkward to
    # parse safely).
    rows = conn.execute(
        """
        SELECT service_type, backend_user_id, server_id, app_user_uuid
        FROM managed_users
        WHERE backend_user_id IS NOT NULL
          AND backend_user_id <> ''
          AND app_user_uuid IS NOT NULL
        """,
    ).fetchall()
    # Group by (service_type, backend_user_id).
    groups: Dict[Tuple[str, str], List[Tuple[str, str]]] = {}
    for r in rows:
        key = (str(r["service_type"]), str(r["backend_user_id"]))
        groups.setdefault(key, []).append(
            (str(r["server_id"]), str(r["app_user_uuid"])),
        )
    pairs_written = 0
    pairs_skipped = 0
    groups_with_dupes = 0
    for (_svc, _bk_id), members in groups.items():
        # De-duplicate by server_id: an installation should only have
        # one managed_users row per (server_id, username), but defensively
        # we collapse here too.
        by_server: Dict[str, str] = {}
        for server_id, uuid in members:
            by_server.setdefault(server_id, uuid)
        if len(by_server) < 2:
            continue
        groups_with_dupes += 1
        # For every (A, B) ordered pair with distinct server_ids, write
        # one identity_map row. We don't write both (A,B) and (B,A) -
        # the bidirectional read helper handles either direction.
        server_uuids = sorted(by_server.items())  # deterministic order
        for i, (_srv_a, uuid_a) in enumerate(server_uuids):
            for (_srv_b, uuid_b) in server_uuids[i + 1:]:
                if uuid_a == uuid_b:
                    continue
                try:
                    new_id = add_identity_map(
                        user_a_uuid=uuid_a,
                        user_b_uuid=uuid_b,
                        source="auto_copy",
                    )
                    if new_id is None:
                        pairs_skipped += 1
                    else:
                        pairs_written += 1
                except Exception:
                    log.exception(
                        "auto_link_identity_map_by_backend_user_id: "
                        "failed to insert (%r, %r); continuing.",
                        uuid_a, uuid_b,
                    )
    if pairs_written or pairs_skipped or groups_with_dupes:
        log.info(
            "auto_link_identity_map_by_backend_user_id: %d new pair(s), "
            "%d duplicate(s) skipped, %d group(s) with cross-server duplicates.",
            pairs_written, pairs_skipped, groups_with_dupes,
        )
    return {
        "pairs_written":           pairs_written,
        "pairs_skipped_duplicate": pairs_skipped,
        "groups_seen":             groups_with_dupes,
    }


# ── Slug rewriter on server rename (R-2 follow-on) ──────────────────────────

def rewrite_app_user_uuid_host_slug_for_server(
    server_uid: str, new_host_name: str,
) -> Dict[str, int]:
    """Refresh the HostNameSlug portion of every stored ``app_user_uuid``
    whose ``server_uid`` portion matches ``server_uid``.

    The server_uid + userkey segments are immutable: only the cosmetic
    slug shifts so identity_map links stay valid. Walks
    ``managed_users.app_user_uuid``, ``server_users.app_user_uuid``,
    AND both columns of ``user_identity_map`` so every stored UUID for
    the renamed server moves in lockstep.

    Returns ``{"managed_users": int, "server_users": int,
    "identity_map_a": int, "identity_map_b": int}`` row counts.
    """
    if not (server_uid or "").strip():
        return {
            "managed_users":   0,
            "server_users":    0,
            "identity_map_a":  0,
            "identity_map_b":  0,
        }
    new_slug = slugify_host_name(new_host_name)
    conn = _require_conn()
    counts = {
        "managed_users":   0,
        "server_users":    0,
        "identity_map_a":  0,
        "identity_map_b":  0,
    }
    with _DB_LOCK:
        for table, count_key in (
            ("managed_users", "managed_users"),
            ("server_users",  "server_users"),
        ):
            rows = conn.execute(
                f"SELECT rowid, app_user_uuid FROM {table} "
                f"WHERE app_user_uuid IS NOT NULL"
            ).fetchall()
            for r in rows:
                rowid_val = r[0]
                stored = r[1]
                if server_uid_from_app_user_uuid(stored) != server_uid:
                    continue
                try:
                    from services.user_uuid import rewrite_host_slug
                    new_uuid = rewrite_host_slug(stored, new_host_name)
                except Exception:
                    continue
                if new_uuid == stored:
                    continue
                conn.execute(
                    f"UPDATE {table} SET app_user_uuid = ? WHERE rowid = ?",
                    (new_uuid, rowid_val),
                )
                counts[count_key] += 1
        # identity_map carries the UUID twice (one per side).
        rows = conn.execute(
            "SELECT id, user_a_uuid, user_b_uuid "
            "FROM user_identity_map"
        ).fetchall()
        from services.user_uuid import rewrite_host_slug
        for r in rows:
            mutated_a = mutated_b = False
            new_a = r["user_a_uuid"]
            new_b = r["user_b_uuid"]
            if server_uid_from_app_user_uuid(new_a) == server_uid:
                try:
                    candidate = rewrite_host_slug(new_a, new_host_name)
                    if candidate != new_a:
                        new_a = candidate
                        mutated_a = True
                except Exception:
                    pass
            if server_uid_from_app_user_uuid(new_b) == server_uid:
                try:
                    candidate = rewrite_host_slug(new_b, new_host_name)
                    if candidate != new_b:
                        new_b = candidate
                        mutated_b = True
                except Exception:
                    pass
            if not (mutated_a or mutated_b):
                continue
            try:
                conn.execute(
                    "UPDATE user_identity_map SET user_a_uuid = ?, "
                    "user_b_uuid = ? WHERE id = ?",
                    (new_a, new_b, r["id"]),
                )
                if mutated_a:
                    counts["identity_map_a"] += 1
                if mutated_b:
                    counts["identity_map_b"] += 1
            except sqlite3.IntegrityError:
                # UNIQUE(user_a_uuid, user_b_uuid) collision means the
                # post-rewrite pair already exists (e.g. end user
                # renamed the server to a name that yields the same
                # slug as an old auto_copy row). Drop this duplicate.
                conn.execute(
                    "DELETE FROM user_identity_map WHERE id = ?",
                    (r["id"],),
                )
    if any(counts.values()):
        log.info(
            "rewrite_app_user_uuid_host_slug_for_server(%r): "
            "managed_users=%d, server_users=%d, identity_map_a=%d, "
            "identity_map_b=%d (new slug=%r)",
            server_uid, counts["managed_users"], counts["server_users"],
            counts["identity_map_a"], counts["identity_map_b"], new_slug,
        )
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


# ── Server-UID boot migration helpers (Plan[SERVER-UID-IDENTITY]) ───────────
#
# When the boot-time `migrate_server_ids_add_backend_prefix` upgrade
# in `server/server_registry.py` rewrites bare-UUID server rows to the
# new prefixed form (`<service_type>_<uuid>`), every other surface
# that references those ids by string also needs to be rewritten.
# These two helpers do the bulk SQL update for the media.db side:
# user_identity_map (v11) and managed_users (v3+). Best-effort:
# failures are caught + logged + non-fatal so a bad rewrite doesn't
# crash the engine boot.

def _rewrite_server_uid_inside_app_user_uuid(
    stored: str, old_to_new: Dict[str, str],
) -> Optional[str]:
    """If ``stored``'s server_uid portion appears in ``old_to_new``,
    return a new UUID with the server_uid replaced. Returns ``None``
    when the UUID is malformed or its server_uid is not in the map."""
    server_uid = server_uid_from_app_user_uuid(stored)
    if not server_uid or server_uid not in old_to_new:
        return None
    new_uid = old_to_new[server_uid]
    # Rebuild using the existing (Service, HostNameSlug, userkey)
    # segments; only swap the server_uid portion.
    try:
        from services.user_uuid import parse_app_user_uuid, build_app_user_uuid
        parts = parse_app_user_uuid(stored)
    except Exception:
        return None
    return build_app_user_uuid(
        service_type=parts["service"],
        host_name=parts["host_slug"],
        server_uid=new_uid,
        user_key=parts["user_key"],
    )


def rewrite_server_ids_in_identity_map(
    old_to_new: Dict[str, str],
) -> int:
    """Bulk-update ``user_identity_map`` rows whose A or B
    ``app_user_uuid`` carries a server_uid that appears in
    ``old_to_new``. Returns the total number of column-updates.

    v12 changed the storage shape from ``(server_a_id, user_a_handle,
    server_b_id, user_b_handle)`` to ``(user_a_uuid, user_b_uuid)``;
    this helper now rewrites the server_uid embedded inside each
    UUID rather than a top-level column. The server-rename helper
    :func:`rewrite_app_user_uuid_host_slug_for_server` handles the
    parallel slug-only refresh; this helper handles the full
    server_uid swap performed by developer's boot migration.

    Idempotent: rerunning with an empty / no-match map is a no-op.
    Defensive: catches per-row update failure and continues."""
    if not old_to_new:
        return 0
    conn = _require_conn()
    n = 0
    with _DB_LOCK:
        rows = conn.execute(
            "SELECT id, user_a_uuid, user_b_uuid FROM user_identity_map"
        ).fetchall()
        for r in rows:
            new_a = _rewrite_server_uid_inside_app_user_uuid(
                r["user_a_uuid"], old_to_new,
            )
            new_b = _rewrite_server_uid_inside_app_user_uuid(
                r["user_b_uuid"], old_to_new,
            )
            if new_a is None and new_b is None:
                continue
            try:
                conn.execute(
                    "UPDATE user_identity_map SET user_a_uuid = ?, "
                    "user_b_uuid = ? WHERE id = ?",
                    (new_a or r["user_a_uuid"],
                     new_b or r["user_b_uuid"],
                     r["id"]),
                )
                if new_a is not None:
                    n += 1
                if new_b is not None:
                    n += 1
            except sqlite3.IntegrityError:
                # UNIQUE collision: a post-rewrite pair already exists.
                # Drop the duplicate row.
                conn.execute(
                    "DELETE FROM user_identity_map WHERE id = ?",
                    (r["id"],),
                )
            except Exception:
                log.exception(
                    "rewrite_server_ids_in_identity_map: row %r update "
                    "failed; continuing.",
                    r["id"],
                )
    return n


def rewrite_server_ids_in_managed_users(
    old_to_new: Dict[str, str],
) -> int:
    """Bulk-update ``managed_users.server_id`` AND
    ``managed_users.app_user_uuid`` rows whose server_id (or whose
    UUID's embedded server_uid) appears in ``old_to_new``. Returns
    the count of column-updates.

    Also walks ``server_users`` for the same rewrites so both per-user
    tables stay in lockstep. The (server_id, username) /
    (server_id, user_handle) UNIQUE constraints are preserved because
    the new prefixed id is unique by construction.
    """
    if not old_to_new:
        return 0
    conn = _require_conn()
    n = 0
    with _DB_LOCK:
        # 1. Top-level server_id columns (unchanged from v11 semantics).
        for old_id, new_id in old_to_new.items():
            try:
                cur = conn.execute(
                    "UPDATE managed_users SET server_id = ? "
                    "WHERE server_id = ?", (new_id, old_id),
                )
                n += cur.rowcount or 0
                cur = conn.execute(
                    "UPDATE server_users SET server_id = ? "
                    "WHERE server_id = ?", (new_id, old_id),
                )
                n += cur.rowcount or 0
            except Exception:
                log.exception(
                    "rewrite_server_ids_in_managed_users: pair "
                    "(%r -> %r) server_id update failed; continuing.",
                    old_id, new_id,
                )
        # 2. Embedded server_uid inside app_user_uuid (v12 addition).
        for table in ("managed_users", "server_users"):
            try:
                rows = conn.execute(
                    f"SELECT rowid, app_user_uuid FROM {table} "
                    f"WHERE app_user_uuid IS NOT NULL"
                ).fetchall()
            except sqlite3.OperationalError:
                # Pre-v12 row; column doesn't exist yet.
                continue
            for r in rows:
                rowid_val = r[0]
                old_uuid_val = r[1]
                new_uuid = _rewrite_server_uid_inside_app_user_uuid(
                    old_uuid_val, old_to_new,
                )
                if new_uuid is None:
                    continue
                try:
                    conn.execute(
                        f"UPDATE {table} SET app_user_uuid = ? "
                        f"WHERE rowid = ?",
                        (new_uuid, rowid_val),
                    )
                    n += 1
                except Exception:
                    log.exception(
                        "rewrite_server_ids_in_managed_users: "
                        "app_user_uuid rewrite for %s.rowid=%r failed; "
                        "continuing.",
                        table, rowid_val,
                    )
    return n
