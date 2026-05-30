"""media.db item identity and per-server item rows.

The GUID-keyed ``items`` table (one row per logical entity) plus the
``library_sections`` / ``server_items`` per-server caches that map a
logical item to its backend-native ``rating_key`` on each server.
"""

from __future__ import annotations

import time
from typing import Dict, List, Optional

from services.translation.guid_translator import normalize_guids

from ._core import _DB_LOCK, _require_conn


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
    show_title: Optional[str] = None,
    season_index: Optional[int] = None,
    episode_index: Optional[int] = None,
    artist: Optional[str] = None,
    album: Optional[str] = None,
    grandparent_guid: Optional[str] = None,
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

    The six hierarchy parameters (show_title / season_index /
    episode_index / artist / album / grandparent_guid) populate the
    v18 + v19 columns. All default None so callers that omit them
    are unaffected; ``ingest_snapshot_payload`` passes them through
    from the snapshot payload. COALESCE on UPDATE means a later
    ingest that lacks a field never NULLs out a value an earlier
    ingest set.

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
                    imdb_id          = COALESCE(?, imdb_id),
                    tmdb_id          = COALESCE(?, tmdb_id),
                    tvdb_id          = COALESCE(?, tvdb_id),
                    musicbrainz_id   = COALESCE(?, musicbrainz_id),
                    plex_guid        = COALESCE(?, plex_guid),
                    title            = ?,
                    media_type       = ?,
                    year             = COALESCE(?, year),
                    filepath_suffix  = COALESCE(?, filepath_suffix),
                    show_title       = COALESCE(?, show_title),
                    season_index     = COALESCE(?, season_index),
                    episode_index    = COALESCE(?, episode_index),
                    artist           = COALESCE(?, artist),
                    album            = COALESCE(?, album),
                    grandparent_guid = COALESCE(?, grandparent_guid),
                    updated_at       = ?
                WHERE id = ?
                """,
                (
                    cols["imdb_id"], cols["tmdb_id"], cols["tvdb_id"],
                    cols["musicbrainz_id"], cols["plex_guid"],
                    title, media_type, year, filepath_suffix,
                    show_title, season_index, episode_index,
                    artist, album, grandparent_guid,
                    now, existing_id,
                ),
            )
            return existing_id
        cur = conn.execute(
            """
            INSERT INTO items (
                imdb_id, tmdb_id, tvdb_id, musicbrainz_id, plex_guid,
                title, media_type, year, filepath_suffix,
                show_title, season_index, episode_index, artist, album,
                grandparent_guid,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                cols["imdb_id"], cols["tmdb_id"], cols["tvdb_id"],
                cols["musicbrainz_id"], cols["plex_guid"],
                title, media_type, year, filepath_suffix,
                show_title, season_index, episode_index, artist, album,
                grandparent_guid,
                now, now,
            ),
        )
        return int(cur.lastrowid)


# ── server_items: per-server rating_key cache ───────────────────────────────

def upsert_library_section(
    *,
    server_id: str,
    section_key: int,
    section_title: str,
    section_type: str,
    # CONSOLE-08: the raw library GUID for Jellyfin/Emby (section_key
    # is a CRC32 hash of it). None for Plex and for callers that don't
    # have it; COALESCE on the upsert preserves any previously-stored
    # value so a later GUID-less upsert never wipes it.
    library_guid: Optional[str] = None,
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
                library_guid, first_seen_at, last_seen_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(server_id, section_key) DO UPDATE SET
                section_title = excluded.section_title,
                section_type  = excluded.section_type,
                library_guid  = COALESCE(excluded.library_guid,
                                         library_sections.library_guid),
                last_seen_at  = excluded.last_seen_at
            """,
            (server_id, int(section_key), section_title, section_type,
             library_guid, now, now),
        )
    # Audit: dimension-table writes are end user-meaningful state
    # changes. Volume is low (one per library per snapshot run) so the
    # audit log doesn't bloat.
    try:
        from services.run_logs import db_access as db_access_log
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
    ``fetchItem(ratingKey)`` call.

    Conflict handling:

    The table has two UNIQUE constraints:

        UNIQUE(item_id, server_id)
        UNIQUE(server_id, rating_key)

    SQLite's ``ON CONFLICT`` clause only attaches to ONE of them.
    Handling just ``(item_id, server_id)`` would trip
    ``IntegrityError`` whenever a rating_key is reused under a
    different item_id - which happens legitimately when an upstream
    Plex item's GUID set changes between runs and a freshly-upserted
    items.id wants to claim a rating_key bound to an items.id that
    no longer exists in the new payload. ``INSERT OR REPLACE``
    handles both: SQLite drops every row that violates either
    UNIQUE and writes the new one. server_items is a cache rebuilt
    from each snapshot run, so dropping a stale binding is exactly
    the intent.
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
    decision (services.snapshot.plex_native.snapshotter._should_cache_payload_to_media_db)
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
