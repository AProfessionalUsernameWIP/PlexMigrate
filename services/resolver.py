"""
Item serialization and four-tier resolution for PlexMigrate.

serialize_item / serialize_playlist / serialize_collection convert live
plexapi objects to JSON-ready dicts for the backup file. resolve_item
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


# ── Path / GUID Helpers ───────────────────────────────────────────────────────

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
    elif item.type == "track":
        data["artist"] = getattr(item, "grandparentTitle", "")
        data["album"] = getattr(item, "parentTitle", "")

    if data.get("last_viewed_at"):
        try:
            data["last_viewed_at"] = str(item.lastViewedAt)
        except Exception:
            data["last_viewed_at"] = None

    return data


def serialize_playlist(playlist, prefetched_items: Optional[List] = None) -> Dict:
    """
    Serializes a Plex playlist and all its items to a JSON-ready dict.

    Playlists are references to other items — they don't contain the media
    themselves, just pointers to it. We save enough information about each
    item (GUIDs + file path + title) to find it again on the target server
    using the three-tier match logic.

    Smart playlists (playlist.smart == True) are defined by a filter query stored
    in playlist.content. The query contains server-specific library section IDs
    that cannot be transferred, so their items are not serialized.

    If ``prefetched_items`` is supplied, it is used in place of
    ``playlist.items()`` to avoid the second round-trip when the caller
    already fetched the item list (see export_playlists' shared cache).
    """
    is_smart = getattr(playlist, "smart", False)
    smart_content = getattr(playlist, "content", "") if is_smart else ""

    items = []

    if not is_smart:
        try:
            source = prefetched_items if prefetched_items is not None else playlist.items()
            for position, item in enumerate(source):
                guids = _all_guids(item)
                filepath = _safe_file_path(item)
                entry: Dict[str, Any] = {
                    "title": item.title,
                    "type": item.type,
                    "guids": guids,
                    "filepath": filepath,
                    "position": position,
                }
                if item.type == "track":
                    entry["artist"] = getattr(item, "grandparentTitle", "")
                    entry["album"] = getattr(item, "parentTitle", "")
                elif item.type == "episode":
                    entry["show_title"] = getattr(item, "grandparentTitle", "")
                    entry["season_title"] = getattr(item, "parentTitle", "")
                items.append(entry)
        except Exception:
            pass

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

    Safe to call from multiple threads concurrently — only the first
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
        # Surface this on the dashboard's Currently Processing panel —
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
                    f"signal ready within 300s — proceeding without cache."
                )
                return
            time.sleep(0.05)


def serialize_collection(collection) -> Dict:
    """
    Serializes a Plex collection and all its member items to a JSON-ready dict.

    Collections are groupings of items by reference. We save GUIDs and file
    paths for every member so we can reconstruct the collection on the target.
    """
    items = []

    try:
        for item in collection.items():
            guids = _all_guids(item)
            filepath = _safe_file_path(item)
            items.append({
                "title": item.title,
                "type": item.type,
                "guids": guids,
                "filepath": filepath,
            })
    except Exception:
        pass

    return {
        "name": collection.title,
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

    Tier 1 — GUID (most reliable): plex:// and mb:// GUIDs are assigned by
        global databases and mean the same thing on any server.

    Tier 2 — Exact file path: The absolute path to the media file on disk.
        Works when the folder structure is identical on both servers (or remapped).

    Tier 2.5 — Suffix path match (cross-platform): Strips the root prefix and
        compares the last 3 then 2 normalised path components. Resolves
        Windows→Linux or Linux→Windows migrations. Uses an O(1) suffix index.

    Tier 3 — Fuzzy title match (last resort): Search by title, filtered by
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

    # ── TIER 2: File Path Match ────────────────────────────────────────────────
    if filepath:
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
                            logger.debug(
                                f"[TIER:filepath-suffix] Resolved '{title}' "
                                f"via {_n}-part suffix match"
                            )
                            return _sfx_matches[0], "filepath-suffix", ""
                        elif len(_sfx_matches) > 1:
                            logger.debug(
                                f"[TIER:filepath-suffix] Ambiguous: "
                                f"{len(_sfx_matches)} candidates for '{title}' "
                                f"at {_n} parts — falling through to fuzzy"
                            )
                            break
            else:
                for candidate in section.all():
                    cpath = _safe_file_path(candidate)
                    if cpath and cpath == filepath:
                        logger.debug(
                            f"[TIER:filepath] Resolved '{title}' via full scan"
                        )
                        return candidate, "filepath", ""
        except Exception as e:
            logger.debug(f"[TIER:filepath] Scan error for '{title}': {e}")

    # ── TIER 3: Fuzzy Title Match ──────────────────────────────────────────────
    try:
        artist = stored.get("artist", "")
        show = stored.get("show_title", "")

        if item_type == "track" and artist:
            results = section.search(title=title, libtype="track")
            matches = [r for r in results if r.grandparentTitle == artist]
        elif item_type == "episode" and show:
            results = section.search(title=title, libtype="episode")
            matches = [r for r in results if r.grandparentTitle == show]
        else:
            results = section.search(title=title)
            matches = results

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
        reason = "local:// GUID only — no MusicBrainz match, path not found, no unique title match"
        return None, "none", reason

    # When the backup carried a filepath but nothing on the target matched
    # it (exact, suffix, or fuzzy), include "path not found" in the reason
    # so the troubleshooting categoriser routes this to file_path_not_found
    # rather than the generic no_tier_match bucket.
    if filepath:
        return None, "none", (
            f"All three tiers exhausted — path not found on target server "
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
    if state._dashboard:
        if item is not None:
            if tier == "GUID":
                state._dashboard.inc_guid()
            elif tier == "filepath":
                state._dashboard.inc_filepath()
            elif tier == "filepath-suffix":
                state._dashboard.inc_suffix()
            elif tier == "fuzzy":
                state._dashboard.inc_fuzzy()
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
