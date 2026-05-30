"""media.db schema migrations - the ordered ``_MIGRATIONS`` data table.

Extracted from ``server.media_db`` so the ~830-line migration list does
not dominate the package. Pure data: an append-only, ordered list of
``(version, sql_script)`` tuples that ``_core._apply_migrations`` replays
on boot. Never edit a shipped migration - append a new tuple.
"""

from __future__ import annotations

from typing import List, Tuple


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
            -- v18 hierarchy columns (show_title, season_index,
            -- episode_index, artist, album) are added by the migration
            -- runner, not declared here. The base schema represents
            -- the v8 / CURRENT_SCHEMA_VERSION shape; every subsequent
            -- column lives in the _MIGRATIONS list so fresh installs
            -- and upgraded installs end up with identical schemas.
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
    # server_items join table. Maps the service-agnostic
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
    # managed_users table. One row per (server_id, username).
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
    # Add ``kind`` column so the JobFormPanel direct-transfer
    # picker can render its Owner / Managed badge from DB reads.
    # SQLite ALTER TABLE ADD COLUMN accepts a CHECK constraint when
    # the DEFAULT value satisfies it for every existing row -
    # 'managed' is the safe default for rows written before this
    # migration runs.
    (4, """
        ALTER TABLE managed_users
            ADD COLUMN kind TEXT NOT NULL DEFAULT 'managed'
                CHECK (kind IN ('owner', 'managed'));
    """),
    # Tombstones. Per-server tombstone is a flag on the
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
    # server_users identity table + FK migration.
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
    # Library section identity as the integrity anchor for
    # snapshot/restore. See module docstring for the
    # invariant. ``library_sections`` is the per-server dimension
    # table; every per-server row in server_items / watch_events /
    # ratings / playlists / collections carries ``section_key``
    # referencing it.
    #
    # The boot path in server/app.py auto-archives any media.db at
    # schema_version < 8 BEFORE this migration runs, so the ALTER
    # always operates on empty tables. ``DEFAULT 0`` is just there
    # to satisfy SQLite's ALTER TABLE ADD COLUMN NOT NULL syntactic
    # requirement; the 0 value is treated as the "invalid" sentinel
    # by every read path and never legitimately appears.
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
    # work needs to distinguish three row classes:
    #
    #   * ``active_share=1``: user currently has an active share on
    #     this server. ``shared_state_refreshed_at`` records when we
    #     last confirmed this via the Plex.tv shared_servers endpoint.
    #   * ``active_share=0``: user once had a share but the server
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
    # Migration v10: canonical per-user identifier. Matching the
    # share-state refresh on ``managed_users.username`` (sourced from
    # ``server.systemAccounts()``, often a display name like "Crystal
    # Jean") against the Plex.tv ``shared_servers`` payload (keyed by
    # handle like "crystalj1") is fragile - even with multi-alias
    # enrichment from ``account.users()`` it breaks on Unicode, emoji,
    # diacritics, punctuation variants, whitespace differences, and
    # duplicate display names. Storing the stable Plex.tv numeric
    # ``userID`` per row and matching by it eliminates the whole class
    # of name-matching bugs and gives Feature 5 (live watch sync,
    # identity links) the canonical column it needs.
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
        -- Cross-server user identity mapping.
        -- Pairs (server_a, handle_a) <-> (server_b, handle_b)
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
    # :mod:`services.identity.user_uuid`. Canonical 4-part form:
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
    # Migration v14 (duplicate-rows bug fix): collapse any
    # (A, B) + (B, A) pairs in ``user_identity_map`` into a single
    # canonical row. ``add_identity_map``'s primary insert
    # wrote whatever ordered pair the caller passed; the transitive
    # fanout wrote the canonical (lo, hi) pair. When the two orderings
    # disagreed, both rows persisted because the UNIQUE constraint is
    # on the ORDERED tuple, so the same other-account showed up twice
    # in the Identity Links view of User Management.
    #
    # The dedup logic:
    #   1. For every pair where BOTH (A,B) and (B,A) exist:
    #      keep the row whose ordered pair is canonical (a < b),
    #      delete the non-canonical one. ``source`` priority:
    #      'manual' wins over 'auto_copy' so an operator-authored
    #      mapping isn't silently demoted to an auto link.
    #      ``created_at`` priority: older row wins (preserves
    #      provenance for audit trails).
    #   2. For every row where (a > b) (non-canonical) AND no
    #      sibling (b, a) exists: rewrite in place by swapping the
    #      columns so the row becomes canonical. No data loss.
    #
    # Best-effort idempotent: re-running this migration on an
    # already-canonical table is a no-op. Wrapped in a single
    # transaction so a mid-step failure leaves the table unchanged.
    #
    # ``derived_from_id`` is NULLED on rows that point at a deleted
    # parent so the v13 cascade-delete semantics don't fire on
    # already-canonicalised siblings. The next ``add_identity_map``
    # call that touches the equivalence class re-populates the
    # derived_from chain naturally via the fanout's INSERT OR IGNORE
    # path.
    (14, """
        -- Step 1: for each (a, b) where a sibling (b, a) exists,
        -- collapse to the canonical (lower-uuid, higher-uuid) row.
        -- We use a temp table to hold the keepers' ids; the rest get
        -- DELETEd. CTE-style would be cleaner but SQLite's UPDATE...
        -- FROM is restricted; an explicit two-pass is more portable.
        -- For each pair of mirrored rows (A,B)+(B,A) decide which to
        -- keep: manual beats auto_copy, then older created_at wins.
        -- The id < id join visits each mirrored pair exactly once.
        CREATE TEMP TABLE _v14_collapse_keepers AS
        SELECT
            CASE
                WHEN m1.source = 'manual' AND m2.source != 'manual' THEN m1.id
                WHEN m2.source = 'manual' AND m1.source != 'manual' THEN m2.id
                WHEN m1.created_at <= m2.created_at THEN m1.id
                ELSE m2.id
            END AS keeper_id,
            CASE
                WHEN m1.source = 'manual' AND m2.source != 'manual' THEN m2.id
                WHEN m2.source = 'manual' AND m1.source != 'manual' THEN m1.id
                WHEN m1.created_at <= m2.created_at THEN m2.id
                ELSE m1.id
            END AS loser_id
        FROM user_identity_map m1
        INNER JOIN user_identity_map m2
            ON m1.user_a_uuid = m2.user_b_uuid
           AND m1.user_b_uuid = m2.user_a_uuid
           AND m1.id < m2.id;

        -- Null out any derived_from_id that points at a row we're
        -- about to delete; the survivor row's id is the new parent.
        UPDATE user_identity_map
        SET derived_from_id = (
            SELECT keeper_id FROM _v14_collapse_keepers k
            WHERE k.loser_id = user_identity_map.derived_from_id
        )
        WHERE derived_from_id IN (SELECT loser_id FROM _v14_collapse_keepers);

        -- Delete the loser rows.
        DELETE FROM user_identity_map
        WHERE id IN (SELECT loser_id FROM _v14_collapse_keepers WHERE loser_id IS NOT NULL);

        DROP TABLE _v14_collapse_keepers;

        -- Step 2: rewrite remaining non-canonical rows (a > b) by
        -- swapping columns. After Step 1 these have no sibling, so
        -- the swap can't collide with the UNIQUE index.
        UPDATE user_identity_map
        SET user_a_uuid = user_b_uuid,
            user_b_uuid = user_a_uuid
        WHERE user_a_uuid > user_b_uuid;
    """),
    # Migration v15 (owner-duplication bug fix): clean
    # already-corrupted ``managed_users`` rows where the same human
    # appears twice on one server (once as ``kind='owner'`` carrying
    # the operator's Plex.tv email, once as ``kind='managed'`` carrying
    # a display-name the local SystemAccount was relabelled to - e.g.,
    # "Kai").
    #
    # The forward fix lives in :mod:`services.identity.plex_owner_identity` +
    # the two call sites (PlexAdapter.list_users + get_server_users)
    # so subsequent syncs no longer write the duplicate. This
    # migration is the one-shot cleanup for DBs that already have it.
    #
    # Match shape (per server_id, OR semantics - any signal wins):
    #   1. managed.username equals the owner's username outright
    #      (case-insensitive).
    #   2. managed.username equals the owner's email
    #      (case-insensitive; rare but defensible - the operator's
    #      server-side label IS the email).
    #   3. managed.username equals the owner's email local-part
    #      (the bit before ``@``, case-insensitive). This is the
    #      operator-reported case: owner=spellofslytherin@gmail.com,
    #      managed.username equals 'spellofslytherin'.
    #
    # The owner row itself (``kind='owner'``) is NEVER deleted - we
    # only collapse the redundant kind='managed' rows. Credential
    # cells, tombstone flags, identity_map references on the dropped
    # rows are lost (the row didn't represent a distinct human;
    # nothing legitimate was stored there). app_user_uuid entries in
    # user_identity_map that pointed at the deleted managed row would
    # need re-authoring by the operator; the auto-link helper will
    # re-derive same-(service_type, backend_user_id) auto_copy rows
    # against the owner row on the next sync.
    #
    # Idempotent: a second run sees no matching rows and is a no-op.
    (15, """
        DELETE FROM managed_users
        WHERE id IN (
            SELECT m.id
            FROM managed_users m
            INNER JOIN managed_users o
                ON o.server_id = m.server_id
               AND o.kind = 'owner'
               AND m.kind = 'managed'
               AND (
                   -- Signal 1: managed name matches owner's name
                   LOWER(m.username) = LOWER(o.username)
                   -- Signal 2: managed name matches owner's email
                   -- (when owner.username IS the email)
                   OR LOWER(m.username) = LOWER(o.username)
                   -- Signal 3: managed name matches owner's email
                   -- local-part. SQLite-portable extraction via
                   -- substr + instr.
                   OR (
                       INSTR(o.username, '@') > 0
                       AND LOWER(m.username) = LOWER(SUBSTR(o.username, 1, INSTR(o.username, '@') - 1))
                   )
               )
        );
    """),
    # Migration v16: multi-backend PIN storage. Adds dedicated
    # encrypted columns for Emby's EasyPassword and Jellyfin's
    # EasyPassword so each backend's PIN-equivalent has its own slot
    # on the row. Plex keeps ``plex_home_pin_enc``; the kind-to-column
    # mapping in :data:`_CRED_COLUMN` selects the right one per write.
    #
    # Cross-backend PIN propagation (the operator's "apply across
    # backends" opt-in on the User Management write surface) routes the
    # plaintext to whichever PIN column matches the linked row's
    # service_type - see :func:`_propagate_pin_across_links`. The same
    # encrypted blob lands in whatever column the destination backend
    # uses for its PIN; the credential kinds are distinguished at the
    # API layer so reveal + has_*_pin computation stay tight per
    # backend.
    #
    # Additive: both columns are TEXT NULL, no defaults, no constraints
    # to break existing rows.
    (16, """
        ALTER TABLE managed_users ADD COLUMN emby_easy_pin_enc TEXT;
        ALTER TABLE managed_users ADD COLUMN jellyfin_easy_pin_enc TEXT;
    """),
    # Migration v17: the three signal columns the auth-health filter +
    # auto-tombstone sweeper read and write. Defaults are safe: every
    # existing row reads
    # ``last_auth_status='unknown'`` (so no row is filtered out
    # accidentally) and ``consecutive_auth_failures=0`` (so no row
    # crosses the tombstone threshold without an actual probe).
    #
    # Additive: TEXT + INTEGER columns with explicit defaults.
    (17, """
        ALTER TABLE managed_users ADD COLUMN last_auth_status TEXT NOT NULL DEFAULT 'unknown';
        ALTER TABLE managed_users ADD COLUMN last_auth_checked_at INTEGER;
        ALTER TABLE managed_users ADD COLUMN consecutive_auth_failures INTEGER NOT NULL DEFAULT 0;
    """),
    # Migration v18: preserve item-hierarchy context (show / season /
    # episode for TV episodes; artist / album for music tracks) all
    # the way to the snapshot.db. ItemSnapshot carries these from the
    # adapter and engine_dict emits them, so the persistence layer
    # stores them too. Without these, restore-side resolution of
    # same-titled episodes ("Pilot" exists on every show) and
    # same-titled tracks falls back to GUID-only matching, which
    # fails when source + destination use different metadata
    # providers (Plex tvdb vs Emby imdb etc.). Five nullable
    # columns - one read per resolve, two writes per upsert, no
    # cost when not populated.
    (18, """
        ALTER TABLE items ADD COLUMN show_title    TEXT;
        ALTER TABLE items ADD COLUMN season_index  INTEGER;
        ALTER TABLE items ADD COLUMN episode_index INTEGER;
        ALTER TABLE items ADD COLUMN artist        TEXT;
        ALTER TABLE items ADD COLUMN album         TEXT;
        CREATE INDEX IF NOT EXISTS idx_items_show_title
            ON items(show_title);
        CREATE INDEX IF NOT EXISTS idx_items_artist
            ON items(artist);
    """),
    # Migration v19: the parent item's cross-server GUID (Plex
    # grandparentGuid; the series GUID for an episode, the artist GUID
    # for a track). A parent-GUID match is immune to localized titles
    # + agent drift, so the resolver's hierarchy tier prefers it over
    # the title/index columns from v18. Additive nullable column;
    # NULL for movies + for any item whose backend did not expose a
    # parent GUID.
    (19, """
        ALTER TABLE items ADD COLUMN grandparent_guid TEXT;
        CREATE INDEX IF NOT EXISTS idx_items_grandparent_guid
            ON items(grandparent_guid);
    """),
    # Migration v20: per-user item affinity is a backend-neutral
    # concept with two faces - a numeric rating (Plex userRating,
    # 0-10) and a binary favorite (Jellyfin/Emby IsFavorite). The
    # ratings table holds both faces so Jellyfin/Emby favorites have
    # a home. is_favorite is nullable: Plex captures leave it NULL
    # (Plex has no per-item favorite), J/E captures write 0/1.
    (20, """
        ALTER TABLE ratings ADD COLUMN is_favorite INTEGER;
    """),
    # CONSOLE-17: provenance columns for users created by the
    # destination-side user-creation flow (services.user_management.creation).
    # ``source_user_handle`` records the source-server handle the row
    # was copied from so the identity resolver can walk
    # source -> destination on subsequent runs; ``created_via_user_creation``
    # flags rows the flow itself wrote (vs rows discovered by a live
    # sync). ``backend_user_id`` already exists (migration v10).
    # Additive: both columns nullable / explicit default, no
    # constraints to break existing rows.
    (21, """
        ALTER TABLE managed_users ADD COLUMN source_user_handle TEXT;
        ALTER TABLE managed_users
            ADD COLUMN created_via_user_creation INTEGER NOT NULL DEFAULT 0;
    """),
    # CONSOLE-08: the Jellyfin/Emby library GUID. library_sections.
    # section_key is a CRC32 hash of that GUID (the v0.15 invariant
    # needs a positive int), so the GUID itself had no home - and the
    # snapshot->mirror writethrough could not reproduce the section_id
    # the live mirror sync uses (which keys on the raw GUID). This
    # column preserves it. NULL for Plex (its numeric section_key IS
    # the identifier) and for pre-v22 rows.
    (22, """
        ALTER TABLE library_sections ADD COLUMN library_guid TEXT;
    """),
]
