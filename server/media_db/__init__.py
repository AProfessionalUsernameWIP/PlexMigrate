"""
Media-state database (Feature 3 foundation).

This is the "SQLite as primary, JSON as snapshot format" data layer.
It owns the schema, migration scaffolding, and public API so the
database can be created, opened, queried, and stats can be surfaced
via ``GET /api/db/stats``. The DB is the primary write path: the
snapshotter writes here, the importer reads here first, and the
resolver consults it as tier 0.

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
  :func:`services.translation.guid_translator.normalize_guid` before they hit the
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
Most tables hold **media-state metadata only** - what's been
watched, what playlists exist, item rating values. The
``managed_users`` table DOES hold sensitive credentials
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


from ._schema import (
    _MIGRATIONS,
)
from ._core import (
    CURRENT_SCHEMA_VERSION,
    _DB_LOCK,
    _DB_NAME,
    _UNKNOWN_SECTION_KEY,
    _apply_migrations,
    _close_for_tests,
    _db_path,
    _init_lock,
    _require_conn,
    get_schema_version,
    get_stats,
    init_media_db,
    log,
    purge_server_data,
    resolve_registry_id,
    upsert_server_row,
)
from .items import (
    find_rating_key_on_server,
    has_any_items_for_server,
    lookup_item_by_guids,
    record_server_item,
    upsert_item,
    upsert_library_section,
)
from .library_walk import (
    count_stale_items,
    finish_library_walk,
    get_last_walk_summary,
    list_library_walks,
    list_stale_items,
    prune_stale_items,
    record_item_sighting,
    start_library_walk,
)
from .identity import (
    _backfill_app_user_uuids,
    _equivalence_class_for_uuid,
    _rewrite_server_uid_inside_app_user_uuid,
    _server_host_name,
    add_identity_map,
    auto_link_identity_map_by_backend_user_id,
    delete_identity_map,
    generate_unique_app_user_uuid,
    get_identity_maps_for_user,
    get_identity_maps_for_uuid,
    get_managed_user_app_uuid,
    get_row_by_app_user_uuid,
    get_server_user_app_uuid,
    list_identity_maps,
    rewrite_app_user_uuid_host_slug_for_server,
    rewrite_server_ids_in_identity_map,
    rewrite_server_ids_in_managed_users,
)
from .managed_users import (
    MANAGED_USER_CREDENTIAL_KINDS,
    PIN_KINDS,
    PIN_KIND_FOR_SERVICE,
    _CRED_COLUMN,
    _MANAGED_USER_COLUMNS,
    _PIN_COLUMN_FOR_SERVICE,
    _PIN_KINDS,
    _propagate_display_name_across_links,
    _propagate_pin_across_links,
    _refresh_share_state,
    _row_to_managed_user,
    add_global_tombstone,
    backfill_pin_across_links,
    delete_managed_user,
    get_managed_user,
    get_managed_user_credential,
    is_globally_tombstoned,
    list_global_tombstone_usernames,
    list_global_tombstones,
    list_managed_users,
    remove_global_tombstone,
    reset_managed_user_auth_signal,
    set_managed_user_credential,
    set_managed_user_display_name,
    set_managed_user_share_state,
    set_managed_user_tombstone,
    sync_managed_users_from_live,
    update_managed_user_auth_signal,
    upsert_managed_user,
)
from .userstate import (
    get_or_create_server_user,
    get_rating,
    get_watch_event,
    record_watch_event,
    upsert_collection,
    upsert_playlist,
    upsert_rating,
)
from .snapshot_ingest import (
    ingest_snapshot_payload,
)

__all__ = [
    "CURRENT_SCHEMA_VERSION",
    "MANAGED_USER_CREDENTIAL_KINDS",
    "PIN_KINDS",
    "PIN_KIND_FOR_SERVICE",
    "_CRED_COLUMN",
    "_DB_LOCK",
    "_DB_NAME",
    "_MANAGED_USER_COLUMNS",
    "_MIGRATIONS",
    "_PIN_COLUMN_FOR_SERVICE",
    "_PIN_KINDS",
    "_UNKNOWN_SECTION_KEY",
    "_apply_migrations",
    "_backfill_app_user_uuids",
    "_close_for_tests",
    "_conn",
    "_db_path",
    "_equivalence_class_for_uuid",
    "_init_lock",
    "_propagate_display_name_across_links",
    "_propagate_pin_across_links",
    "_refresh_share_state",
    "_require_conn",
    "_rewrite_server_uid_inside_app_user_uuid",
    "_row_to_managed_user",
    "_server_host_name",
    "add_global_tombstone",
    "add_identity_map",
    "auto_link_identity_map_by_backend_user_id",
    "backfill_pin_across_links",
    "count_stale_items",
    "delete_identity_map",
    "delete_managed_user",
    "find_rating_key_on_server",
    "finish_library_walk",
    "generate_unique_app_user_uuid",
    "get_identity_maps_for_user",
    "get_identity_maps_for_uuid",
    "get_last_walk_summary",
    "get_managed_user",
    "get_managed_user_app_uuid",
    "get_managed_user_credential",
    "get_or_create_server_user",
    "get_rating",
    "get_row_by_app_user_uuid",
    "get_schema_version",
    "get_server_user_app_uuid",
    "get_stats",
    "get_watch_event",
    "has_any_items_for_server",
    "ingest_snapshot_payload",
    "init_media_db",
    "is_globally_tombstoned",
    "list_global_tombstone_usernames",
    "list_global_tombstones",
    "list_identity_maps",
    "list_library_walks",
    "list_managed_users",
    "list_stale_items",
    "log",
    "lookup_item_by_guids",
    "prune_stale_items",
    "purge_server_data",
    "record_item_sighting",
    "record_server_item",
    "record_watch_event",
    "remove_global_tombstone",
    "reset_managed_user_auth_signal",
    "resolve_registry_id",
    "rewrite_app_user_uuid_host_slug_for_server",
    "rewrite_server_ids_in_identity_map",
    "rewrite_server_ids_in_managed_users",
    "set_managed_user_credential",
    "set_managed_user_display_name",
    "set_managed_user_share_state",
    "set_managed_user_tombstone",
    "start_library_walk",
    "sync_managed_users_from_live",
    "update_managed_user_auth_signal",
    "upsert_collection",
    "upsert_item",
    "upsert_library_section",
    "upsert_managed_user",
    "upsert_playlist",
    "upsert_rating",
    "upsert_server_row",
]


def __getattr__(name: str):
    """Delegate attribute access to _core for mutable module-level variables
    like _conn that get reassigned during initialization."""
    if name == '_conn':
        from . import _core
        return _core._conn
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

