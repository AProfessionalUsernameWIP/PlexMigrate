"""media.db snapshot-payload ingestion.

``ingest_snapshot_payload`` - the single entry point that fans a
captured snapshot payload out across the items / userstate writers
with the dedup discipline the per-user playlist tables require.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ._core import log
from .items import (
    lookup_item_by_guids,
    record_server_item,
    upsert_item,
    upsert_library_section,
)
from .userstate import (
    get_or_create_server_user,
    record_watch_event,
    upsert_collection,
    upsert_playlist,
    upsert_rating,
)


# ── Snapshot-payload ingestion with dedup discipline ───────────────────────

def ingest_snapshot_payload(server_id: str, payload: Dict[str, Any]) -> Dict[str, int]:
    """
    Write one library's snapshot payload into the DB with the correct
    server-wide vs user-private split - the single chokepoint that
    enforces the dedup discipline documented on :func:`upsert_playlist`
    and :func:`upsert_collection`.

    Input shape (unified-users JSON schema):

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

    # Integrity-anchor contract: every payload MUST carry
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
        # Carry the hierarchy fields from the snapshot payload
        # through to the items row. The engine dict + JSON sidecar
        # use ``parent_index`` for the season number; the column is
        # ``season_index``.
        def _ing_int(v: Any) -> Optional[int]:
            if v is None or v == "":
                return None
            try:
                return int(v)
            except (TypeError, ValueError):
                return None
        try:
            iid = upsert_item(
                guids=guids,
                title=title,
                media_type=media_type,
                year=year,
                filepath_suffix=filepath,
                show_title=(rec.get("show_title") or "").strip() or None,
                season_index=_ing_int(rec.get("parent_index")),
                episode_index=_ing_int(rec.get("episode_index")),
                artist=(rec.get("artist") or "").strip() or None,
                album=(rec.get("album") or "").strip() or None,
                grandparent_guid=(rec.get("grandparent_guid") or "").strip() or None,
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

    # One pass over the unified users map. Owner and managed
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

        GUID fallback: legacy payloads don't include
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

    # rating_key is normalised with str(), not int(): Jellyfin/Emby
    # rating keys are opaque GUID strings and int() on one raises
    # ValueError - which (this comprehension is unguarded) would
    # abort the entire snapshot ingest. str() never raises and dedups
    # consistently against the str()-keyed consumer sites in the
    # managed-user pass below; Plex numeric keys stringify losslessly.
    owner_playlist_keys = {
        str(pl["rating_key"]) for pl in owner_playlists
        if pl.get("rating_key") is not None
    }
    owner_collection_keys = {
        str(c["rating_key"]) for c in owner_collections
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
    # Shared capture/ingest predicate: an affinity row is kept only
    # when it carries a real rating or a favorite (SNAP-06). Imported
    # locally - backend_translation is a pure stdlib-only module, so
    # this can never introduce an import cycle.
    from services.backend_translation import affinity_row_is_meaningful
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
        _fav = rec.get("is_favorite")
        _is_fav = None if _fav is None else bool(_fav)
        # Skip noise rows: a 0 / absent rating with no favorite is the
        # absence of affinity, not an affinity worth a DB row.
        if not affinity_row_is_meaningful(rating_val, _is_fav):
            continue
        try:
            upsert_rating(
                item_id=iid, server_id=server_id, user_handle="",
                role="owner", display_name=owner_display,
                backend_user_id=owner_backend_id,
                rating=rating_val,
                is_favorite=_is_fav,
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
            _fav = rec.get("is_favorite")
            _is_fav = None if _fav is None else bool(_fav)
            # Skip noise rows - see the owner ratings loop above.
            if not affinity_row_is_meaningful(rating_val, _is_fav):
                continue
            try:
                upsert_rating(
                    item_id=iid, server_id=server_id,
                    user_handle=str(username), rating=rating_val,
                    is_favorite=_is_fav,
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
            if rk is not None and str(rk) in owner_playlist_keys:
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
            if rk is not None and str(rk) in owner_collection_keys:
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
