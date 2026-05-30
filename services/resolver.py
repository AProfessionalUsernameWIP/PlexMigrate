"""
Item serialization and four-tier resolution for Hestia-MediaManager.

serialize_item / serialize_playlist / serialize_collection convert live
plexapi objects to JSON-ready dicts for the export file. resolve_item
tries four strategies in order (GUID → exact path → suffix path → fuzzy
title) to find the same item on the target server.
"""

import logging
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from plexapi.exceptions import NotFound, PlexApiException
from plexapi.server import PlexServer

import services.state as state


# Module logger for the serialize_* helpers, which take no ``logger``
# argument. Engine resolution paths get their logger passed in; these
# serialization helpers fall back to the shared engine logger so a
# swallowed Plex error is still diagnosable.
_log = logging.getLogger("plexmigrate")


# ── Path / GUID Helpers ───────────────────────────────────────────────────────

def _disable_autoreload(*objs) -> None:
    """
    Turn off plexapi's implicit per-item ``reload()`` on the given
    objects. Used by the snapshot ``serialize_*`` helpers + the
    perf-#2 ``_bulk_fetch_for_filters`` path.

    plexapi reloads a *partial* object the instant you read an
    attribute whose value is ``None`` / ``[]`` - and it cannot tell
    "[] because the listing was partial" from "[] because the item
    genuinely has none". An unmatched episode (no metadata-agent match
    -> no guids) therefore triggers a full ``/library/metadata``
    reload the moment ``serialize_item`` touches ``item.guids``. Worse,
    plexapi's reload bundles ``includeMarkers`` / ``includeChapters``,
    which makes Plex run slow on-demand intro/chapter analysis -
    ~20-30s per item. That is the per-show slowdown in the snapshot
    logs (matched shows instant, unmatched shows 20s/episode).

    The bulk ``search*`` / container responses already include guids +
    media inline for items that have them, so disabling autoreload on
    the snapshot path loses nothing real: matched items keep their
    inline data, unmatched items correctly serialize with empty guids,
    and no hidden round-trip ever fires. Best-effort and silent - a
    plexapi object that doesn't expose ``_autoReload`` is simply left
    as-is.

    Tunable escape hatch: when
    ``services.tunables.plexapi_autoreload_enabled()`` returns true,
    this function becomes a no-op so vanilla plexapi behaviour
    returns. Default false (autoreload disabled). Use only for
    diagnostic / recovery scenarios where a bulk response is genuinely
    missing data - accepting the per-item reload cost knowingly.
    """
    # Tunable check up front. ``True`` = leave autoreload alone (no-op).
    # Lazy import keeps this module loadable in CLI-only checkouts.
    try:
        from services import tunables
        if tunables.plexapi_autoreload_enabled():
            return
    except Exception:
        # Tunables unavailable (CLI bootstrap, missing settings.json):
        # fall through and disable autoreload - matches historical
        # behaviour before the tunable existed.
        pass
    for obj in objs:
        try:
            obj._autoReload = False
        except Exception:
            pass


def _safe_file_path(item) -> str:
    """
    Extracts the first media file path from a Plex item, or returns "".

    Not every Plex item has media attached. Accessing item.media[0].parts[0].file
    directly would raise an IndexError or AttributeError. This helper makes it safe.
    """
    try:
        return item.media[0].parts[0].file
    except (AttributeError, IndexError):
        return ""


def _normalize_path_parts(path: str) -> List[str]:
    """
    Converts any file path to a normalised list of lowercase path components.

    Cross-platform imports fail on exact filepath comparison because the root
    differs (C:\\Media vs /mnt/plex) and the separator differs (\\ vs /).
    Normalising to lowercase forward-slash parts and stripping the root lets
    the suffix matching tier compare just the shared tail.

    Examples:
        "C:\\Media\\Music\\Pink Floyd\\DSOTM\\01 Speak.flac"
            → ["media", "music", "pink floyd", "dsotm", "01 speak.flac"]
        "/mnt/plex/Music/Pink Floyd/DSOTM/01 Speak.flac"
            → ["music", "pink floyd", "dsotm", "01 speak.flac"]
    """
    p = path.replace("\\", "/").lower()
    if len(p) >= 3 and p[1] == ":" and p[2] == "/":
        p = p[3:]
    p = p.lstrip("/")
    return [part for part in p.split("/") if part]


def _suffix_key(path: str, n: int) -> str:
    """Returns the last *n* normalised components of *path* joined with '/'."""
    parts = _normalize_path_parts(path)
    return "/".join(parts[-n:]) if len(parts) >= n else "/".join(parts)


def _section_leaf_items(section) -> List:
    """
    Returns the correct leaf-level items for a library section.

    section.all() returns different object levels depending on library type:
      - Movies  → Movie objects    (have media.parts.file) ✓
      - Music   → Artist objects   (no media.parts.file) ✗
      - TV      → Show objects     (no media.parts.file) ✗
    For scan_cache and suffix index building we need leaf objects with file paths.
    """
    libtype = getattr(section, "type", "")
    if libtype == "artist":
        return section.searchTracks()
    elif libtype == "show":
        return section.searchEpisodes()
    return section.all()


def _all_guids(item) -> List[str]:
    """
    Returns all GUIDs for a Plex item as a list of strings.

    Plex items can have multiple GUIDs from different sources: plex://, mb://,
    imdb://, tvdb://, etc. We save all of them so the import has the best chance
    of finding a match on the new server.
    """
    try:
        return [g.id for g in item.guids]
    except AttributeError:
        return []


# ── Serialization ─────────────────────────────────────────────────────────────

def serialize_item(item, user: str = "") -> Dict:
    """
    Converts a Plex media item to a JSON-serializable dictionary.

    python-plexapi objects cannot be written directly to JSON. This function
    extracts every field we need for import and puts it into a plain dict.
    We save both GUIDs and file paths because either one might work on the
    target server depending on the situation.

    Args:
        item: A python-plexapi media object (track, movie, episode, etc.).
        user (str): Optional username to associate with this record.

    Returns:
        Dict with all fields needed for import-time matching and restoration.
    """
    # Kill plexapi's implicit per-item reload before touching any
    # attribute - an unmatched item (empty guids) would otherwise
    # trigger a slow ``/library/metadata`` round-trip here. See
    # ``_disable_autoreload``.
    _disable_autoreload(item)

    guids = _all_guids(item)
    filepath = _safe_file_path(item)

    data: Dict[str, Any] = {
        "title": item.title,
        "type": item.type,
        "rating_key": item.ratingKey,
        "guids": guids,
        "filepath": filepath,
        "view_count": getattr(item, "viewCount", 0) or 0,
        "last_viewed_at": getattr(item, "lastViewedAt", None),
        "view_offset": getattr(item, "viewOffset", 0) or 0,
        "user_rating": getattr(item, "userRating", None),
        "added_at": str(getattr(item, "addedAt", "")),
        "user": user,
        "library_section_id": getattr(item, "librarySectionID", None),
    }

    if item.type == "episode":
        data["show_title"] = getattr(item, "grandparentTitle", "")
        data["season_title"] = getattr(item, "parentTitle", "")
        # numeric
        # season + episode indices. ``parentIndex`` is the season
        # number, ``index`` the episode number. Without these,
        # cross-server episode matching can't disambiguate same-
        # titled episodes ("Pilot" exists in S1 of every show).
        _pi = getattr(item, "parentIndex", None)
        if _pi is not None:
            try:
                data["parent_index"] = int(_pi)
            except (TypeError, ValueError):
                pass
        _ei = getattr(item, "index", None)
        if _ei is not None:
            try:
                data["episode_index"] = int(_ei)
            except (TypeError, ValueError):
                pass
        # The series' cross-server GUID — the strongest hierarchy
        # match key (immune to localized titles + agent drift).
        _gpg = getattr(item, "grandparentGuid", "") or ""
        if _gpg:
            data["grandparent_guid"] = str(_gpg)
    elif item.type == "track":
        data["artist"] = getattr(item, "grandparentTitle", "")
        data["album"] = getattr(item, "parentTitle", "")
        # The artist's cross-server GUID — same role as the series
        # GUID for episodes.
        _gpg = getattr(item, "grandparentGuid", "") or ""
        if _gpg:
            data["grandparent_guid"] = str(_gpg)

    if data.get("last_viewed_at"):
        try:
            data["last_viewed_at"] = str(item.lastViewedAt)
        except Exception:
            data["last_viewed_at"] = None

    return data


def serialize_playlist(playlist, prefetched_items: Optional[List] = None) -> Dict:
    """
    Serializes a Plex playlist and all its items to a JSON-ready dict.

    Playlists are references to other items - they don't contain the media
    themselves, just pointers to it. We save enough information about each
    item (GUIDs + file path + title) to find it again on the target server
    using the three-tier match logic.

    Smart playlists (playlist.smart == True) are defined by a filter query stored
    in playlist.content. The query contains server-specific library section IDs
    that cannot be transferred, so their items are not serialized.

    If ``prefetched_items`` is supplied, it is used in place of
    ``playlist.items()`` to avoid the second round-trip when the caller
    already fetched the item list (see snapshot_playlists' shared cache).
    """
    # Kill plexapi's implicit per-item reload on the playlist object
    # and every member before touching attributes (see
    # ``_disable_autoreload``).
    _disable_autoreload(playlist)

    is_smart = getattr(playlist, "smart", False)
    smart_content = getattr(playlist, "content", "") if is_smart else ""

    items = []

    if not is_smart:
        try:
            source = prefetched_items if prefetched_items is not None else playlist.items()
            _disable_autoreload(*source)
            for position, item in enumerate(source):
                guids = _all_guids(item)
                filepath = _safe_file_path(item)
                entry: Dict[str, Any] = {
                    "title": item.title,
                    "type": item.type,
                    "guids": guids,
                    "filepath": filepath,
                    "position": position,
                    # ratingKey is what media_db.ingest_snapshot_payload
                    # uses to map members back to items.id; without it
                    # the playlist row's ``item_ids_json`` ingests as []
                    # and the membership is lost.
                    "rating_key": getattr(item, "ratingKey", None),
                    "year": getattr(item, "year", None),
                }
                if item.type == "track":
                    entry["artist"] = getattr(item, "grandparentTitle", "")
                    entry["album"] = getattr(item, "parentTitle", "")
                elif item.type == "episode":
                    entry["show_title"] = getattr(item, "grandparentTitle", "")
                    entry["season_title"] = getattr(item, "parentTitle", "")
                items.append(entry)
        except Exception as exc:
            # A transient Plex error here yields a silently-empty
            # member list - log it so the truncated playlist is
            # diagnosable rather than looking like an empty playlist.
            _log.warning(
                "serialize_playlist: failed to enumerate items for "
                "playlist %r (captured %d so far): %s",
                getattr(playlist, "title", "?"), len(items), exc,
            )

    return {
        "name": playlist.title,
        "playlist_type": getattr(playlist, "playlistType", ""),
        "description": getattr(playlist, "summary", ""),
        "created_at": str(getattr(playlist, "addedAt", "")),
        "smart": is_smart,
        "smart_content": smart_content,
        "items": items,
    }


# ── Scan-Cache Build Coordinator ─────────────────────────────────────────────

def _build_scan_cache(
    section,
    scan_cache: Dict[str, Any],
    scan_lock: threading.Lock,
    logger: logging.Logger,
) -> None:
    """
    Build scan_cache + suffix index once, with single-builder coordination.

    Safe to call from multiple threads concurrently - only the first
    caller actually enumerates the library; everyone else waits until
    the builder marks the cache ready.

    The enumeration happens OUTSIDE scan_lock so other resolver threads
    can briefly acquire the lock (e.g. for the builder-claim handshake)
    without waiting through the slow ``_section_leaf_items(section)``
    walk on a huge music library.
    """
    if scan_cache.get("__ready__"):
        return

    with scan_lock:
        if scan_cache.get("__ready__"):
            return
        if scan_cache.get("__building__"):
            claim = False
        else:
            scan_cache["__building__"] = True
            claim = True

    if claim:
        # Surface this on the dashboard's Currently Processing panel -
        # scan-cache build can hold a thread for 30+ seconds on a big
        # music library, and without a row the user would just see a
        # "Scan Cache ×1" ThreadPool entry with no idea what it's
        # actually doing. Imported lazily so resolver doesn't drag the
        # dashboard module in for CLI-only call paths.
        from services.dashboard import _current_item
        try:
            logger.debug(f"[scan_cache] Building for '{section.title}'")
            with _current_item(
                section.title,
                "scan_cache",
                f"Building filepath index for '{section.title}'",
                phase="indexing",
            ):
                local_paths: Dict[str, Any] = {}
                local_sfx: Dict[str, List] = {}
                for c in _section_leaf_items(section):
                    cpath = _safe_file_path(c)
                    if cpath:
                        local_paths[cpath] = c
                        parts = _normalize_path_parts(cpath)
                        for n in (3, 2):
                            if len(parts) >= n:
                                key = "/".join(parts[-n:])
                                local_sfx.setdefault(key, []).append(c)
            with scan_lock:
                scan_cache.update(local_paths)
                if local_paths:
                    scan_cache["__suffix_index__"] = local_sfx
                scan_cache["__ready__"] = True
                scan_cache.pop("__building__", None)
        except Exception:
            # Critical: still flip __ready__ so any thread spinning on
            # the wait loop below can exit. Without this, a single
            # Plex error during the scan would leave every resolver
            # thread spinning forever (the "30+ minute / continue
            # run" symptom). We also tag __failed__ so callers can
            # tell the cache is empty by failure rather than by
            # design.
            with scan_lock:
                scan_cache["__ready__"] = True
                scan_cache["__failed__"] = True
                scan_cache.pop("__building__", None)
            raise
    else:
        # Another thread is building. Wait until it publishes
        # __ready__. A 5-minute ceiling makes this defensively
        # bounded: the builder normally finishes in seconds; if
        # something deadlocks upstream we'd rather degrade gracefully
        # to a cache-miss + fuzzy fallback than block forever.
        deadline = time.time() + 300
        while not scan_cache.get("__ready__"):
            if time.time() > deadline:
                logger.warning(
                    f"[scan_cache] Builder for '{section.title}' did not "
                    f"signal ready within 300s - proceeding without cache."
                )
                return
            time.sleep(0.05)


def serialize_collection(collection) -> Dict:
    """
    Serializes a Plex collection and all its member items to a JSON-ready dict.

    Collections are groupings of items by reference. We save GUIDs and file
    paths for every member so we can reconstruct the collection on the target.
    """
    # Kill plexapi's implicit per-item reload on the collection object
    # and every member before touching attributes (see
    # ``_disable_autoreload``).
    _disable_autoreload(collection)

    items = []

    try:
        members = list(collection.items())
        _disable_autoreload(*members)
        for item in members:
            guids = _all_guids(item)
            filepath = _safe_file_path(item)
            items.append({
                "title": item.title,
                "type": item.type,
                "guids": guids,
                "filepath": filepath,
                # See serialize_playlist: without rating_key the
                # media.db ingest can't build the rating_key→items.id
                # map and item_ids_json lands empty.
                "rating_key": getattr(item, "ratingKey", None),
                "year": getattr(item, "year", None),
            })
    except Exception as exc:
        # As in serialize_playlist: a transient Plex error here would
        # otherwise produce a silently-truncated member list.
        _log.warning(
            "serialize_collection: failed to enumerate items for "
            "collection %r (captured %d so far): %s",
            getattr(collection, "title", "?"), len(items), exc,
        )

    return {
        "name": collection.title,
        # v0.9.7 Item 9: rating_key surfaces so callers can dedupe by
        # identity - direct-transfer's per-user gather subtracts the
        # owner's collection set from each user's set so library-level
        # collections (visible to all users) don't get double-counted.
        # ``rating_key`` is server-local but stable within one
        # connection, which is exactly the scope we need it in.
        "rating_key": getattr(collection, "ratingKey", None),
        "sort_order": getattr(collection, "collectionSort", 0),
        "has_custom_poster": bool(getattr(collection, "thumb", None)),
        "items": items,
    }


# ── Three-Tier Item Resolution ────────────────────────────────────────────────

def _resolve_item_impl(
    server: PlexServer,
    section,
    stored: Dict,
    logger: logging.Logger,
    remap: Optional[Tuple[str, str]] = None,
    strict_match: bool = True,
    scan_cache: Optional[Dict[str, Any]] = None,
    scan_lock: Optional[threading.Lock] = None,
) -> Tuple[Optional[Any], str, str]:
    """
    Attempts to find a matching item on the target Plex server using a
    four-tier strategy: GUID, exact path, suffix path, then fuzzy title match.

    Tier 1 - GUID (most reliable): plex:// and mb:// GUIDs are assigned by
        global databases and mean the same thing on any server.

    Tier 2 - Exact file path: The absolute path to the media file on disk.
        Works when the folder structure is identical on both servers (or remapped).

    Tier 2.5 - Suffix path match (cross-platform): Strips the root prefix and
        compares the last 3 then 2 normalised path components. Resolves
        Windows→Linux or Linux→Windows migrations. Uses an O(1) suffix index.

    Tier 3 - Fuzzy title match (last resort): Search by title, filtered by
        artist/show when available. Only used if exactly one result matches
        (or --no-strict-match is set).

    Returns:
        Tuple of (item, tier_name, failure_reason).
        - On success: (PlexObject, "GUID"|"filepath"|"filepath-suffix"|"fuzzy", "")
        - On failure: (None, last_tier_tried, reason_string)
    """
    guids = stored.get("guids", [])
    filepath = stored.get("filepath", "")
    title = stored.get("title", "")
    item_type = stored.get("type", "")

    # ── Apply path remapping if --remap-path was specified ────────────────────
    if remap:
        old_root, new_root = remap
        if filepath:
            filepath = filepath.replace(old_root, new_root, 1).replace("\\", "/")

    # ── TIER 0: server_mirror.db rating-key cache ─────────────────────────────
    # The mirror DB
    # caches the per-server item universe across multiple resolution
    # signals (GUID, full path, path tail). It is fed by snapshot
    # write-through + the cross-feed Direction 1 playlist_cache
    # bootstrap + operator-triggered Sync Now actions. Mirror lookup
    # is microseconds (one SQL query, indexed) versus media.db's Tier
    # 0 which is comparable but narrower in coverage.
    #
    # Falls through cleanly when the mirror DB is uninitialised
    # (tests / CLI-only), empty for this server (cold start), or in
    # always-live mode (operator opt-out).
    try:
        from server import server_mirror_db
        # Probe init state without raising; resolution stays correct
        # if the mirror is not available.
        try:
            server_mirror_db.get_connection()
            _mirror_ready = True
        except (RuntimeError, ImportError):
            _mirror_ready = False
    except ImportError:
        _mirror_ready = False
    if _mirror_ready:
        try:
            from services.mirror_sync.server_mirror import _sm
            # the mirror is
            # keyed on the app registry UID (stamped onto the server
            # object by connect_registered_server), not the backend-
            # native machineIdentifier. Fall back to machineIdentifier
            # for servers connected outside the registry path.
            machine_id = str(
                getattr(server, "_pmig_server_uid", "") or ""
            ) or str(getattr(server, "machineIdentifier", "") or "")
            if machine_id:
                mirror_mode = _sm.effective_mode_for(machine_id)
            else:
                mirror_mode = "always-live"
        except Exception:
            mirror_mode = "always-live"
            machine_id = ""
        if machine_id and mirror_mode != "always-live":
            try:
                mirror_rk: Optional[str] = None
                if guids:
                    mirror_rk = _sm.lookup_by_guids(
                        server_id=machine_id, guids=guids,
                    )
                if mirror_rk is None and filepath:
                    mirror_rk = _sm.lookup_by_full_path(
                        server_id=machine_id, file_path=filepath,
                        item_type_hint=item_type or None,
                    )
                if mirror_rk is None and filepath:
                    mirror_rk = _sm.lookup_by_path_tail(
                        server_id=machine_id, file_path=filepath,
                        tail_components=3,
                        item_type_hint=item_type or None,
                    )
                if mirror_rk is not None:
                    try:
                        item = server.fetchItem(int(mirror_rk))
                        if item:
                            logger.debug(
                                "[TIER:mirror] Resolved '%s' via mirror "
                                "ratingKey=%s on %s",
                                title, mirror_rk, machine_id,
                            )
                            return item, "mirror", ""
                    except (NotFound, PlexApiException, ValueError):
                        # Mirror row is stale: item was removed or
                        # re-keyed live-side. Fall through to live
                        # tiers; a successful match will refresh the
                        # row on the next snapshot writethrough.
                        pass
                    except Exception as e:
                        logger.debug(
                            "[TIER:mirror] fetchItem failed for '%s' "
                            "(rating_key=%s): %s", title, mirror_rk, e,
                        )
            except Exception as e:
                logger.debug(
                    "[TIER:mirror] lookup failed for '%s': %s",
                    title, e,
                )

    # ── TIER 0b: media.db rating-key cache (v0.12.1) ──────────────────────────
    # The fastest possible resolution path. If a previous run on this
    # server already wrote this item's per-server ratingKey to
    # ``server_items`` (keyed by upstream GUID like imdb://tt0133093),
    # we can skip Plex's slow ``getByGuid`` round-trip below and fetch
    # the item by its ratingKey in one call.
    #
    # Falls through cleanly when the DB has no record - empty DB,
    # first run against a new server, etc. - so this is purely
    # additive.
    try:
        from server import media_db
        machine_id = str(getattr(server, "machineIdentifier", "") or "")
        # ``server_items`` rows are keyed by the canonical registry id
        # (``servers.id``), not by the backend-native ``machineIdentifier``
        # that lives on the live plexapi handle. Translate at this
        # boundary so the WHERE clause downstream matches what the
        # snapshotter wrote via ``ingest_snapshot_payload``. Without this
        # translation the lookup never hits and every resolve degrades to
        # the slower network-bound tiers below.
        registry_id = media_db.resolve_registry_id(machine_id) if machine_id else None
        if registry_id and guids:
            cached_rk = media_db.find_rating_key_on_server(
                guids=guids, server_id=registry_id,
            )
            if cached_rk is not None:
                try:
                    item = server.fetchItem(cached_rk)
                    if item:
                        logger.debug(
                            f"[TIER:DB] Resolved '{title}' via cached "
                            f"ratingKey={cached_rk} on {registry_id} "
                            f"(machine_id={machine_id})"
                        )
                        return item, "DB", ""
                except (NotFound, PlexApiException):
                    # Cache was stale - the item was removed or
                    # re-keyed on Plex's side. Fall through to the
                    # network-bound tiers; a successful match there
                    # will refresh the row on the next snapshot.
                    pass
                except Exception as e:
                    logger.debug(
                        f"[TIER:DB] fetchItem failed for '{title}' "
                        f"(rating_key={cached_rk}): {e}"
                    )
    except Exception as e:
        # Any DB error: fall through to the network tiers. Tier 0 is a
        # performance hint, not a correctness path - but log at debug
        # so a persistently-broken cache is still diagnosable rather
        # than silently degrading every resolve to the slow path.
        logger.debug(f"[TIER:DB] cache lookup failed for '{title}': {e}")

    # ── TIER 1: GUID Match ─────────────────────────────────────────────────────
    for guid in guids:
        if guid.startswith("plex://") or guid.startswith("mb://"):
            try:
                item = server.library.getByGuid(guid)
                if item:
                    logger.debug(f"[TIER:GUID] Resolved '{title}' via {guid}")
                    return item, "GUID", ""
            except (NotFound, PlexApiException):
                pass
            except Exception as e:
                logger.debug(f"[TIER:GUID] Error resolving '{title}' via {guid}: {e}")

    # ── Per-tier policy from state ContextVars ─────────────────────────────
    # Defaults are True for both (snapshot / import paths). Direct
    # transfer sets these from settings.transfer_resolution at run
    # start. When a fallback is disabled, the corresponding tier
    # block is skipped entirely - the resolver falls through with a
    # "fallback disabled" reason.
    _allow_filepath = bool(state._resolver_allow_filepath)
    _allow_fuzzy = bool(state._resolver_allow_fuzzy)

    # ── TIER 2: File Path Match ────────────────────────────────────────────────
    if filepath and _allow_filepath:
        cache_is_warm = bool(scan_cache)
        if not cache_is_warm:
            try:
                results = section.search(filters={"media.part.file": filepath})
                if results:
                    logger.debug(f"[TIER:filepath] Resolved '{title}' via file path filter")
                    return results[0], "filepath", ""
            except Exception:
                pass

        # ── Full scan fallback ─────────────────────────────────────────────────
        try:
            if scan_cache is not None and scan_lock is not None:
                _build_scan_cache(section, scan_cache, scan_lock, logger)

                candidate = scan_cache.get(filepath)
                if candidate:
                    logger.debug(
                        f"[TIER:filepath] Resolved '{title}' via cached scan"
                    )
                    return candidate, "filepath", ""

                # ── TIER 2.5: Suffix path match ────────────────────────────────
                sfx_idx = scan_cache.get("__suffix_index__")
                if sfx_idx:
                    _stored_orig = stored.get("filepath", "") or filepath
                    _stored_parts = _normalize_path_parts(_stored_orig)
                    for _n in (3, 2):
                        if len(_stored_parts) < _n:
                            continue
                        _k = "/".join(_stored_parts[-_n:])
                        _sfx_matches = sfx_idx.get(_k, [])
                        if len(_sfx_matches) == 1:
                            _cand = _sfx_matches[0]
                            # A unique path-tail match can still point at
                            # the wrong media family - the suffix index is
                            # keyed on path components alone, with no type
                            # check. Returning a wrong-type item here is
                            # the root cause of cross-type playlist writes
                            # downstream, so reject it and fall through to
                            # fuzzy rather than hand a caller bad data.
                            if item_type and getattr(_cand, "type", "") != item_type:
                                logger.warning(
                                    f"[TIER:filepath-suffix] Rejected wrong-type "
                                    f"match for '{title}': expected {item_type}, "
                                    f"got {getattr(_cand, 'type', '?')} - "
                                    f"falling through to fuzzy"
                                )
                                break
                            logger.debug(
                                f"[TIER:filepath-suffix] Resolved '{title}' "
                                f"via {_n}-part suffix match"
                            )
                            return _cand, "filepath-suffix", ""
                        elif len(_sfx_matches) > 1:
                            logger.debug(
                                f"[TIER:filepath-suffix] Ambiguous: "
                                f"{len(_sfx_matches)} candidates for '{title}' "
                                f"at {_n} parts - falling through to fuzzy"
                            )
                            break
            else:
                # M19: iterate leaf items, not ``section.all()``. On
                # Music / TV sections ``section.all()`` returns Artist /
                # Show objects, which have no ``media.parts.file`` -
                # ``_safe_file_path`` returns "" for every one and every
                # track / episode silently fails this tier. The cached
                # path already uses ``_section_leaf_items``; this makes
                # the cache-cold path behave the same.
                for candidate in _section_leaf_items(section):
                    cpath = _safe_file_path(candidate)
                    if cpath and cpath == filepath:
                        logger.debug(
                            f"[TIER:filepath] Resolved '{title}' via full scan"
                        )
                        return candidate, "filepath", ""
        except Exception as e:
            logger.debug(f"[TIER:filepath] Scan error for '{title}': {e}")

    # ── TIER 2.75: Hierarchy match ─────────────────────────────────────────────
    # A hierarchy match (parent GUID / show / artist +
    # leaf coordinates) is a stronger signal than a bare fuzzy
    # title match, so it runs immediately BEFORE the fuzzy tier.
    #
    # Resolves the classic ambiguity: two episodes both titled
    # "Pilot" are indistinguishable by title, but
    # (series GUID OR show_title) + season + episode is unique.
    # Same for a track titled "Intro" under a known artist + album.
    #
    # Backed by the server_mirror.db hierarchy index. Falls through
    # cleanly when the mirror is uninitialised / cold / always-live
    # mode, or when the stored item carries no hierarchy fields
    # (movies, old pre-v17 snapshots).
    if item_type in ("episode", "track"):
        try:
            from server import server_mirror_db as _smdb
            try:
                _smdb.get_connection()
                _h_mirror_ready = True
            except (RuntimeError, ImportError):
                _h_mirror_ready = False
        except ImportError:
            _h_mirror_ready = False
        if _h_mirror_ready:
            try:
                from services.mirror_sync.server_mirror import _sm_h
                # Phase 4C: app registry UID is the mirror key; fall
                # back to machineIdentifier for non-registry servers.
                _h_machine_id = str(
                    getattr(server, "_pmig_server_uid", "") or ""
                ) or str(getattr(server, "machineIdentifier", "") or "")
                _h_mode = (
                    _sm_h.effective_mode_for(_h_machine_id)
                    if _h_machine_id else "always-live"
                )
            except Exception:
                _h_machine_id = ""
                _h_mode = "always-live"
            if _h_machine_id and _h_mode != "always-live":
                def _h_int(v: Any) -> Optional[int]:
                    if v in (None, ""):
                        return None
                    try:
                        return int(v)
                    except (TypeError, ValueError):
                        return None
                try:
                    hier_rk = _sm_h.lookup_by_hierarchy(
                        server_id=_h_machine_id,
                        item_type=item_type,
                        title=title,
                        grandparent_guid=(stored.get("grandparent_guid") or None),
                        show_title=(stored.get("show_title") or None),
                        season_number=_h_int(stored.get("parent_index")),
                        episode_number=_h_int(stored.get("episode_index")),
                        artist=(stored.get("artist") or None),
                        album=(stored.get("album") or None),
                    )
                    if hier_rk is not None:
                        cand = server.fetchItem(int(hier_rk))
                        if cand and (
                            not item_type
                            or getattr(cand, "type", "") == item_type
                        ):
                            logger.debug(
                                "[TIER:hierarchy] Resolved '%s' via "
                                "hierarchy ratingKey=%s",
                                title, hier_rk,
                            )
                            return cand, "hierarchy", ""
                except Exception as e:
                    logger.debug(
                        "[TIER:hierarchy] lookup failed for '%s': %s",
                        title, e,
                    )

    # ── TIER 3: Fuzzy Title Match ──────────────────────────────────────────────
    if _allow_fuzzy:
        try:
            artist = stored.get("artist", "")
            show = stored.get("show_title", "")

            if item_type == "track" and artist:
                results = section.search(title=title, libtype="track")
                matches = [r for r in results if r.grandparentTitle == artist]
            elif item_type == "episode" and show:
                results = section.search(title=title, libtype="episode")
                matches = [r for r in results if r.grandparentTitle == show]
            elif item_type:
                # Constrain the search to the stored media type. Without
                # a libtype filter a movie and a track that share a
                # title are indistinguishable, and the bare best-guess
                # below would happily return the wrong one - the root
                # cause of cross-type playlist writes downstream.
                results = section.search(title=title, libtype=item_type)
                matches = results
            else:
                results = section.search(title=title)
                matches = results

            # Defence-in-depth: even with a libtype-constrained search,
            # drop any candidate whose type doesn't match the stored
            # item before it can be returned as a match.
            if item_type:
                matches = [m for m in matches if getattr(m, "type", "") == item_type]

            if len(matches) == 1:
                logger.debug(f"[TIER:fuzzy] Resolved '{title}' via unique title match")
                return matches[0], "fuzzy", ""
            elif len(matches) > 1 and not strict_match:
                logger.debug(f"[TIER:fuzzy] Using best-guess for '{title}' (strict-match off)")
                return matches[0], "fuzzy", ""
            elif len(matches) > 1:
                reason = f"Ambiguous: {len(matches)} results for '{title}'"
                logger.warning(f"[TIER:fuzzy] {reason}")
                return None, "fuzzy", reason

        except Exception as e:
            logger.debug(f"[TIER:fuzzy] Search error for '{title}': {e}")

    # ── All tiers failed ──────────────────────────────────────────────────────
    # local:// takes precedence in _category_for_failure (it checks guids
    # before reason text), so its reason can also mention "path not found"
    # without misrouting the category.
    has_local_guid = any(g.startswith("local://") for g in guids)
    if has_local_guid:
        reason = "local:// GUID only - no MusicBrainz match, path not found, no unique title match"
        return None, "none", reason

    # When the export carried a filepath but nothing on the target matched
    # it (exact, suffix, or fuzzy), include "path not found" in the reason
    # so the troubleshooting categoriser routes this to file_path_not_found
    # rather than the generic no_tier_match bucket.
    if filepath:
        return None, "none", (
            f"All three tiers exhausted - path not found on target server "
            f"(stored: {filepath!r}); title {title!r}"
        )

    return None, "none", f"All three tiers exhausted for '{title}'"


def resolve_item(
    server: PlexServer,
    section,
    stored: Dict,
    logger: logging.Logger,
    remap: Optional[Tuple[str, str]] = None,
    strict_match: bool = True,
    scan_cache: Optional[Dict[str, Any]] = None,
    scan_lock: Optional[threading.Lock] = None,
) -> Tuple[Optional[Any], str, str]:
    """
    Thin wrapper around _resolve_item_impl that updates dashboard resolution counters.

    The dashboard Match Resolution panel shows how many items were found via GUID,
    filepath, fuzzy title, or not at all. Rather than scattering _dashboard.inc_*()
    calls across every resolve call site, this wrapper centralises the counter
    updates in one place.

    Returns:
        Same as _resolve_item_impl: (item, tier, reason).
    """
    item, tier, reason = _resolve_item_impl(
        server, section, stored, logger, remap, strict_match, scan_cache, scan_lock
    )
    if state.get_dashboard():
        if item is not None:
            if tier == "GUID":
                state.get_dashboard().inc_guid()
            elif tier == "filepath":
                state.get_dashboard().inc_filepath()
            elif tier == "filepath-suffix":
                state.get_dashboard().inc_suffix()
            elif tier == "fuzzy":
                state.get_dashboard().inc_fuzzy()
            # "mirror" tier is a
            # new resolution path. The Match Resolution panel does
            # not yet have a per-tier counter for it (Phase 2.5
            # frontend task); count as GUID for now since most
            # mirror hits are upstream GUID matches.
            elif tier == "mirror":
                state.get_dashboard().inc_guid()
            # the
            # "hierarchy" tier is a new resolution path. The Match
            # Resolution panel has no per-tier counter for it yet;
            # count as a suffix-class match (a strong-but-not-GUID
            # signal) so the panel's accumulators stay meaningful.
            # The exact tier name still lands in inc_tier below for
            # the run summary.
            elif tier == "hierarchy":
                state.get_dashboard().inc_suffix()
            # ALWAYS log the tier name (including "DB" + "mirror")
            # into the per-tier summary counter. Distinct from the
            # existing inc_guid / inc_filepath / etc. accumulators
            # which feed the Match Resolution panel; this one feeds
            # the run summary + the fuzzy-match warning banner in
            # transfer mode.
            state.get_dashboard().inc_tier(tier)
    return item, tier, reason


def _category_for_failure(stored: Dict, reason: str, tier: str) -> str:
    """
    Maps a failure's stored data and reason to a TROUBLESHOOT_CATEGORIES key.

    Args:
        stored (dict): The stored item dict (contains guids for GUID analysis).
        reason (str): The human-readable failure reason string.
        tier (str): The last tier reached before failure.

    Returns:
        A key from TROUBLESHOOT_CATEGORIES, e.g. "local_guid_no_match".
    """
    guids_str = " ".join(stored.get("guids", []))

    if "local://" in guids_str and tier in ("none", "fuzzy"):
        return "local_guid_no_match"

    if "path not found" in reason.lower():
        return "file_path_not_found"

    if "ambiguous" in reason.lower():
        return "ambiguous_title_match"

    if "api" in reason.lower() or "http" in reason.lower():
        return "api_error"

    return "no_tier_match"
