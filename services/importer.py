"""
Import pipeline for PlexMigrate.

Contains the four additive-merge import functions (watch history, playlists,
collections, ratings), the per-file orchestrator (import_backup_file), and
the top-level runner (run_import) that manages the dashboard / progress display
and parallelises across multiple backup files.
"""

import concurrent.futures
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from plexapi.collection import Collection
from plexapi.playlist import Playlist
from plexapi.server import PlexServer

import services.state as state
from services.state import (
    console,
    _lib_successes,
    _lib_failures,
)
from services.auth import get_home_users
from services.dashboard import (
    DashboardState,
    _advance_lib,
    _build_dashboard,
    _check_terminal_size,
    _current_item,
    _keyboard_thread,
    _make_progress,
)
from services.logging_ops import (
    _fmt_media_line,
    _record_failure,
    _record_success,
    _tz_now,
    write_library_logs,
    write_troubleshoot_log,
    write_unresolved_log,
)
from services.resolver import (
    _category_for_failure,
    _normalize_path_parts,
    _safe_file_path,
    _section_leaf_items,
    resolve_item,
)

from rich.live import Live


# ── Direct HTTP Write Helpers ─────────────────────────────────────────────────

def _scrobble(base_url: str, rating_key: int, token: str) -> None:
    """
    Calls the Plex scrobble endpoint to increment an item's view count by one.

    Args:
        base_url (str): Plex server base URL, e.g. "http://localhost:32400".
        rating_key (int): The item's ratingKey on the target server.
        token (str): Plex auth token.

    Side effects:
        Makes one HTTP GET request. Increments viewCount by 1.
    """
    url = f"{base_url}/:/scrobble"
    params = {
        "key": rating_key,
        "identifier": "com.plexapp.plugins.library",
        "X-Plex-Token": token,
    }
    state._session.get(url, params=params, timeout=5)


def _set_resume_position(base_url: str, rating_key: int, offset_ms: int, token: str) -> None:
    """
    Sets the resume position (viewOffset) for an item via the Plex progress endpoint.

    Args:
        base_url (str): Plex server base URL.
        rating_key (int): The item's ratingKey on the target server.
        offset_ms (int): Resume position in milliseconds.
        token (str): Plex auth token.

    Side effects:
        Makes one HTTP GET request. Sets viewOffset on the item.
    """
    url = f"{base_url}/:/progress"
    params = {
        "key": rating_key,
        "identifier": "com.plexapp.plugins.library",
        "time": offset_ms,
        "state": "stopped",
        "hasMDE": 1,
        "X-Plex-Token": token,
    }
    state._session.get(url, params=params, timeout=5)


def _rate_item(base_url: str, rating_key: int, rating: float, token: str) -> None:
    """
    Sets a star rating on an item via the Plex /:/rate endpoint.

    Args:
        base_url (str): Plex server base URL.
        rating_key (int): The item's ratingKey on the target server.
        rating (float): Rating value between 0 and 10.
        token (str): Plex auth token.

    Side effects:
        Makes one HTTP PUT request. Updates the item's userRating.
    """
    url = f"{base_url}/:/rate"
    params = {
        "key": rating_key,
        "identifier": "com.plexapp.plugins.library",
        "rating": rating,
        "X-Plex-Token": token,
    }
    state._session.put(url, params=params, timeout=10)


# ── Import — Additive Merge Functions ─────────────────────────────────────────

def import_watch_history(
    server: PlexServer,
    section,
    watch_items: List[Dict],
    token: str,
    base_url: str,
    logger: logging.Logger,
    log_dir: str,
    remap: Optional[Tuple[str, str]],
    strict_match: bool,
    scan_cache: Optional[Dict[str, Any]] = None,
    scan_lock: Optional[threading.Lock] = None,
    user: str = "Plex Owner",
) -> None:
    """
    Imports Play Count for a library using additive-only merge logic.

    Merge rules:
        - View count: only INCREASED, never reduced.
        - Resume position: only set if the target item has NO saved progress.

    Args:
        server (PlexServer): Active connection to the target server.
        section: Target library section.
        watch_items (List[Dict]): Serialized watched item dicts from the backup.
        token (str): Plex auth token for direct API calls.
        base_url (str): Plex server base URL for direct API calls.
        logger (Logger): Shared logger.
        log_dir (str): Where to write the per-library logs after processing.
        remap (tuple, optional): Path root translation.
        strict_match (bool): Whether to require exactly one fuzzy match result.

    Side effects:
        Makes HTTP requests to the target Plex server.
        Calls _record_success() and _record_failure() (thread-safe).
        Calls write_library_logs() after all items are processed.
    """
    lib_name = section.title
    total = len(watch_items)
    logger.info(f"Importing Play Count for {lib_name}: {total} items")

    scrobble_sem = threading.Semaphore(state.SCROBBLE_WORKERS)

    def process(stored: Dict) -> None:
        # Surface this item on the dashboard's "Currently Processing"
        # panel. Phase advances from "resolving" → "scrobbling" so the
        # user can tell which workers are doing GUID/filepath lookups
        # vs which are mid-HTTP-write. Age (started_at) is preserved
        # across the transition by set_current_item so the column
        # reflects total time on this item, not just the current phase.
        if state._dashboard:
            state._dashboard.set_current_item(
                lib_name, stored.get("type", "?"), stored.get("title", ""),
                phase="resolving",
            )
            state._dashboard.inc_watch()
        try:
            live_item, tier, reason = resolve_item(
                server, section, stored, logger, remap, strict_match,
                scan_cache, scan_lock,
            )
            ts = _tz_now()

            if not live_item:
                cat = _category_for_failure(stored, reason, tier)
                _record_failure(lib_name, {
                    "ts": ts, "tier": tier,
                    "title": stored["title"],
                    "guid": stored.get("guids", [""])[0],
                    "filepath": stored.get("filepath", ""),
                    "reason": reason,
                    "library": lib_name,
                    "type": stored.get("type", "?"),
                    "parent": stored.get("album", stored.get("season_title", "")),
                    "grandparent": stored.get("artist", stored.get("show_title", "")),
                }, cat)
                logger.warning(f"[UNRESOLVED] {lib_name} | {stored['title']} | {reason}")
                if state._media_logger:
                    state._media_logger.debug(_fmt_media_line(
                        "IMPORT", lib_name, stored.get("type", "?"), stored["title"],
                        user=user, tier=tier, result="UNRESOLVED", reason=reason,
                    ))
                return
        finally:
            # Only clear on the unresolved-return path; the resolved
            # path keeps the slot occupied and advances its phase below.
            if state._dashboard and not (locals().get("live_item")):
                state._dashboard.clear_current_item()

        # Advance phase to "scrobbling" for the write portion. We don't
        # construct a fresh CurrentItem — set_current_item preserves
        # started_at so Age keeps counting from when resolve started.
        if state._dashboard:
            state._dashboard.set_current_item(
                lib_name, stored.get("type", "?"), stored.get("title", ""),
                phase="scrobbling",
            )

        try:
            current_view_count = getattr(live_item, "viewCount", 0) or 0
            current_view_offset = getattr(live_item, "viewOffset", 0) or 0
            stored_view_count = stored.get("view_count", 0) or 0
            stored_view_offset = stored.get("view_offset", 0) or 0

            views_to_add = max(0, stored_view_count - current_view_count)

            if views_to_add == 0:
                _record_success(lib_name, {
                    "ts": ts, "tier": tier,
                    "title": stored["title"],
                    "type": stored.get("type", "?"),
                    "action": f"skipped — target viewCount ({current_view_count}) >= stored ({stored_view_count})",
                })
                logger.debug(f"[{lib_name}] Skipped (view count not higher): {stored['title']}")
                if state._media_logger:
                    state._media_logger.debug(_fmt_media_line(
                        "IMPORT", lib_name, stored.get("type", "?"), stored["title"],
                        user=user, tier=tier,
                        plays=f"stored={stored_view_count} target={current_view_count}",
                        result="skipped",
                    ))
                return

            # Bound the per-item scrobble burst to VIEWCOUNT_INCREMENT_CAP so
            # a runaway backup (e.g. stale viewCount of 99999) cannot hammer
            # Plex with thousands of writes for one track. The log line below
            # reports the *actual* number fired, plus the stored target — the
            # delta tells the user whether a re-run is needed to advance further.
            actual_views = min(views_to_add, state.VIEWCOUNT_INCREMENT_CAP)
            capped = actual_views < views_to_add

            with scrobble_sem:
                for _ in range(actual_views):
                    _scrobble(base_url, live_item.ratingKey, token)

                if current_view_offset == 0 and stored_view_offset > 0:
                    _set_resume_position(base_url, live_item.ratingKey, stored_view_offset, token)

            if capped:
                action = (
                    f"added {actual_views} view(s) — capped at {state.VIEWCOUNT_INCREMENT_CAP} "
                    f"(stored {stored_view_count}, target was {current_view_count}; "
                    f"re-run import to advance further)"
                )
                plays_str = f"+{actual_views}/{views_to_add} (capped)"
            else:
                action = f"added {actual_views} view(s) (total now ≥{stored_view_count})"
                plays_str = f"+{actual_views} → ≥{stored_view_count}"

            _record_success(lib_name, {
                "ts": ts, "tier": tier,
                "title": stored["title"],
                "type": stored.get("type", "?"),
                "action": action,
            })
            logger.debug(f"[{lib_name}] Updated view count: {stored['title']} (+{actual_views})")
            if state._media_logger:
                state._media_logger.debug(_fmt_media_line(
                    "IMPORT", lib_name, stored.get("type", "?"), stored["title"],
                    user=user, tier=tier,
                    plays=plays_str,
                    result="OK",
                ))

        except Exception as e:
            _record_failure(lib_name, {
                "ts": ts, "tier": tier,
                "title": stored["title"],
                "guid": stored.get("guids", [""])[0],
                "filepath": stored.get("filepath", ""),
                "reason": str(e),
                "library": lib_name,
                "type": stored.get("type", "?"),
            }, "api_error")
            logger.warning(
                f"[{lib_name}] API error restoring watch for '{stored['title']}': {e}"
            )
        finally:
            if state._dashboard:
                state._dashboard.clear_current_item()

    with concurrent.futures.ThreadPoolExecutor(max_workers=state.MAX_WORKERS) as pool:
        futures = {pool.submit(process, item): item for item in watch_items}
        for f in concurrent.futures.as_completed(futures):
            _advance_lib(lib_name)
            exc = f.exception()
            if exc:
                logger.error(f"Thread error in Play Count import: {exc}")

    write_library_logs(log_dir, lib_name, total)


def import_playlists(
    server: PlexServer,
    section,
    playlists: List[Dict],
    logger: logging.Logger,
    log_dir: str,
    remap: Optional[Tuple[str, str]],
    strict_match: bool,
    scan_cache: Optional[Dict[str, Any]] = None,
    scan_lock: Optional[threading.Lock] = None,
    existing_playlists: Optional[Dict[str, Any]] = None,
) -> None:
    """
    Imports playlists using additive union merge — never deletes or overwrites.

    Merge rules:
        - If a playlist does NOT exist: create it. Log [CREATED].
        - If it DOES exist: append missing items only. Log [APPENDED].

    Two-phase design:
        Phase 1 (parallel): Resolve all items across all non-smart playlists.
        Phase 2 (sequential): Create or merge playlists one at a time.

    Args:
        server (PlexServer): Active connection to the target server.
        section: Target library section.
        playlists (List[Dict]): Serialized playlist dicts from the backup.
        logger (Logger): Shared logger.
        log_dir (str): Unused here; included for signature symmetry.
        remap (tuple, optional): Path root translation.
        strict_match (bool): Whether to require exactly one fuzzy match.
        scan_cache (dict, optional): Shared filepath→item dict.
        scan_lock (Lock, optional): Protects the initial build of scan_cache.
        existing_playlists (dict, optional): Pre-fetched {title: Playlist} dict.
    """
    lib_name = section.title

    if existing_playlists is None:
        existing_playlists = {pl.title: pl for pl in server.playlists()}

    resolve_tasks: List[Tuple[int, Dict]] = []
    for pl_idx, pl_data in enumerate(playlists):
        if not pl_data.get("smart", False):
            for stored in pl_data.get("items", []):
                resolve_tasks.append((pl_idx, stored))

    resolved_by_pl: Dict[int, List[Tuple[Any, int]]] = {}

    def _resolve_one(pl_idx: int, stored: Dict) -> Tuple[int, Optional[Any], int]:
        item = resolve_item(
            server, section, stored, logger, remap, strict_match,
            scan_cache, scan_lock,
        )[0]
        return pl_idx, item, stored.get("position", 0)

    if resolve_tasks:
        with concurrent.futures.ThreadPoolExecutor(max_workers=state.MAX_WORKERS) as pool:
            futs = [pool.submit(_resolve_one, pl_idx, stored)
                    for pl_idx, stored in resolve_tasks]
            for fut in concurrent.futures.as_completed(futs):
                _advance_lib(lib_name)
                try:
                    pl_idx, item, position = fut.result()
                    if item:
                        resolved_by_pl.setdefault(pl_idx, []).append((item, position))
                except Exception as exc:
                    logger.warning(f"[{lib_name}] Playlist item resolution error: {exc}")

    for pl_idx, pl_data in enumerate(playlists):
        pl_name = pl_data["name"]
        ts = _tz_now()

        if state._dashboard:
            state._dashboard.set_current_item(
                lib_name, "playlist", pl_name, phase="merging",
            )
            state._dashboard.inc_playlist()

        # P0-3: advance the per-library progress for every playlist we
        # consider, regardless of which exit branch we end up taking
        # (smart-skip, no-items-resolved, or processed). Without this,
        # smart playlists and empty-resolved playlists are counted in
        # _lib_total() but never advanced, leaving the bar stuck short
        # of 100%.
        try:
            if pl_data.get("smart", False):
                smart_content = pl_data.get("smart_content", "")
                logger.info(
                    f"Skipping smart playlist '{pl_name}' "
                    f"(smart_content: {smart_content or 'unavailable'})"
                )
                _record_failure(lib_name, {
                    "ts": ts, "tier": "none", "title": pl_name,
                    "reason": "Smart playlist — must be recreated manually on target server",
                    "library": lib_name, "type": "playlist",
                    "guid": "", "filepath": "",
                }, "smart_playlist_skipped")
                continue

            resolved_with_positions = resolved_by_pl.get(pl_idx, [])
            resolved_with_positions.sort(key=lambda x: x[1])
            resolved_items = [item for item, _ in resolved_with_positions]

            if not resolved_items:
                logger.warning(f"Playlist '{pl_name}': no items resolved, skipping")
                continue

            if pl_name not in existing_playlists:
                try:
                    Playlist.create(server, pl_name, items=resolved_items)
                    _record_success(lib_name, {
                        "ts": ts, "tier": "N/A", "title": pl_name,
                        "type": "playlist",
                        "action": f"[CREATED] with {len(resolved_items)} items",
                    })
                    logger.info(f"Created playlist '{pl_name}' with {len(resolved_items)} items")
                except Exception as e:
                    _record_failure(lib_name, {
                        "ts": ts, "tier": "none", "title": pl_name,
                        "reason": str(e), "library": lib_name, "type": "playlist",
                        "guid": "", "filepath": "",
                    }, "api_error")
                    logger.error(f"Failed to create playlist '{pl_name}': {e}")

            else:
                existing_pl = existing_playlists[pl_name]
                try:
                    existing_keys: Set[int] = {
                        item.ratingKey for item in existing_pl.items()
                    }
                    items_to_add = [i for i in resolved_items if i.ratingKey not in existing_keys]
                    items_present = [i for i in resolved_items if i.ratingKey in existing_keys]

                    if items_to_add:
                        existing_pl.addItems(items_to_add)
                        _record_success(lib_name, {
                            "ts": ts, "tier": "N/A", "title": pl_name,
                            "type": "playlist",
                            "action": f"[APPENDED] {len(items_to_add)} new item(s)",
                        })
                        logger.info(
                            f"Appended {len(items_to_add)} item(s) to existing playlist '{pl_name}'"
                        )

                    for item in items_present:
                        _record_success(lib_name, {
                            "ts": ts, "tier": "N/A", "title": item.title,
                            "type": item.type,
                            "action": f"[SKIPPED — already in playlist '{pl_name}']",
                        })

                    if not items_to_add:
                        logger.info(
                            f"Playlist '{pl_name}': all {len(items_present)} item(s) already present"
                        )

                except Exception as e:
                    _record_failure(lib_name, {
                        "ts": ts, "tier": "none", "title": pl_name,
                        "reason": str(e), "library": lib_name, "type": "playlist",
                        "guid": "", "filepath": "",
                    }, "api_error")
                    logger.error(f"Failed to update playlist '{pl_name}': {e}")
        finally:
            if state._dashboard:
                state._dashboard.clear_current_item()
            _advance_lib(lib_name)


def import_collections(
    server: PlexServer,
    section,
    collections: List[Dict],
    logger: logging.Logger,
    log_dir: str,
    remap: Optional[Tuple[str, str]],
    strict_match: bool,
    scan_cache: Optional[Dict[str, Any]] = None,
    scan_lock: Optional[threading.Lock] = None,
) -> None:
    """
    Imports collections using additive union merge — never deletes or overwrites.

    Merge rules:
        - If collection does NOT exist: create it. Log [CREATED].
        - If it DOES exist: append missing members only. Log [APPENDED].

    Two-phase design (mirrors import_playlists):
        Phase 1 (parallel): Resolve all member items across all collections.
        Phase 2 (sequential): Create or merge collections one at a time.

    Args:
        server (PlexServer): Active connection to the target server.
        section: Target library section.
        collections (List[Dict]): Serialized collection dicts from the backup.
        logger (Logger): Shared logger.
        log_dir (str): Unused here; included for signature symmetry.
        remap (tuple, optional): Path root translation.
        strict_match (bool): Whether to require exactly one fuzzy match.
        scan_cache (dict, optional): Shared filepath→item dict.
        scan_lock (Lock, optional): Protects the initial build of scan_cache.
    """
    lib_name = section.title
    existing_collections = {c.title: c for c in section.collections()}

    resolve_tasks: List[Tuple[int, Dict]] = [
        (coll_idx, stored)
        for coll_idx, coll_data in enumerate(collections)
        for stored in coll_data.get("items", [])
    ]

    resolved_by_coll: Dict[int, List[Any]] = {}

    def _resolve_member(coll_idx: int, stored: Dict) -> Tuple[int, Optional[Any]]:
        item = resolve_item(
            server, section, stored, logger, remap, strict_match,
            scan_cache, scan_lock,
        )[0]
        return coll_idx, item

    if resolve_tasks:
        with concurrent.futures.ThreadPoolExecutor(max_workers=state.MAX_WORKERS) as pool:
            futs = [pool.submit(_resolve_member, coll_idx, stored)
                    for coll_idx, stored in resolve_tasks]
            for fut in concurrent.futures.as_completed(futs):
                _advance_lib(lib_name)
                try:
                    coll_idx, item = fut.result()
                    if item:
                        resolved_by_coll.setdefault(coll_idx, []).append(item)
                except Exception as exc:
                    logger.warning(f"[{lib_name}] Collection item resolution error: {exc}")

    for coll_idx, coll_data in enumerate(collections):
        coll_name = coll_data["name"]
        ts = _tz_now()

        if state._dashboard:
            state._dashboard.set_current_item(
                lib_name, "collection", coll_name, phase="merging",
            )
            state._dashboard.inc_collection()

        resolved_items = resolved_by_coll.get(coll_idx, [])

        if not resolved_items:
            logger.warning(f"Collection '{coll_name}': no items resolved, skipping")
            if state._dashboard:
                state._dashboard.clear_current_item()
            continue

        if coll_name not in existing_collections:
            try:
                Collection.create(server, coll_name, section, items=resolved_items)
                _record_success(lib_name, {
                    "ts": ts, "tier": "N/A", "title": coll_name,
                    "type": "collection",
                    "action": f"[CREATED] with {len(resolved_items)} member(s)",
                })
                logger.info(f"Created collection '{coll_name}' with {len(resolved_items)} items")
            except Exception as e:
                _record_failure(lib_name, {
                    "ts": ts, "tier": "none", "title": coll_name,
                    "reason": str(e), "library": lib_name, "type": "collection",
                    "guid": "", "filepath": "",
                }, "api_error")
                logger.error(f"Failed to create collection '{coll_name}': {e}")

        else:
            existing_coll = existing_collections[coll_name]
            try:
                existing_keys: Set[int] = {
                    item.ratingKey for item in existing_coll.items()
                }
                items_to_add = [i for i in resolved_items if i.ratingKey not in existing_keys]
                items_present = [i for i in resolved_items if i.ratingKey in existing_keys]

                if items_to_add:
                    existing_coll.addItems(items_to_add)
                    _record_success(lib_name, {
                        "ts": ts, "tier": "N/A", "title": coll_name,
                        "type": "collection",
                        "action": f"[APPENDED] {len(items_to_add)} new member(s)",
                    })
                    logger.info(
                        f"Appended {len(items_to_add)} member(s) to collection '{coll_name}'"
                    )

                for item in items_present:
                    _record_success(lib_name, {
                        "ts": ts, "tier": "N/A", "title": item.title,
                        "type": item.type,
                        "action": f"[SKIPPED — already in collection '{coll_name}']",
                    })

                if not items_to_add:
                    logger.info(
                        f"Collection '{coll_name}': all {len(items_present)} member(s) already present"
                    )

            except Exception as e:
                _record_failure(lib_name, {
                    "ts": ts, "tier": "none", "title": coll_name,
                    "reason": str(e), "library": lib_name, "type": "collection",
                    "guid": "", "filepath": "",
                }, "api_error")
                logger.error(f"Failed to update collection '{coll_name}': {e}")

        if state._dashboard:
            state._dashboard.clear_current_item()
        _advance_lib(lib_name)


def import_ratings(
    server: PlexServer,
    section,
    ratings: List[Dict],
    token: str,
    base_url: str,
    logger: logging.Logger,
    remap: Optional[Tuple[str, str]],
    strict_match: bool,
    scan_cache: Optional[Dict[str, Any]] = None,
    scan_lock: Optional[threading.Lock] = None,
    user: str = "Plex Owner",
) -> None:
    """
    Imports star ratings using additive-only logic — never overwrites existing ratings.

    Merge rule:
        - If the target item has NO rating: set the stored rating. Log [RATING SET].
        - If the target item ALREADY HAS a rating: skip it.

    Args:
        server (PlexServer): Active connection to the target server.
        section: Target library section.
        ratings (List[Dict]): Serialized rating dicts from the backup.
        token (str): Plex auth token for the direct /:/rate API call.
        base_url (str): Plex server base URL.
        logger (Logger): Shared logger.
        remap (tuple, optional): Path root translation.
        strict_match (bool): Whether to require exactly one fuzzy match.
        scan_cache (dict, optional): Shared filepath→item dict.
        scan_lock (Lock, optional): Protects the initial build of scan_cache.
    """
    lib_name = section.title
    total = len(ratings)
    logger.info(f"Importing ratings for {lib_name}: {total} items")

    def process(stored: Dict) -> None:
        if state._dashboard:
            state._dashboard.set_current_item(
                lib_name, stored.get("type", "?"), stored.get("title", ""),
                phase="resolving",
            )
            state._dashboard.inc_rating()
        try:
            live_item, tier, reason = resolve_item(
                server, section, stored, logger, remap, strict_match,
                scan_cache, scan_lock,
            )
            ts = _tz_now()

            if not live_item:
                cat = _category_for_failure(stored, reason, tier)
                _record_failure(lib_name, {
                    "ts": ts, "tier": tier,
                    "title": stored["title"],
                    "guid": stored.get("guids", [""])[0],
                    "filepath": stored.get("filepath", ""),
                    "reason": reason,
                    "library": lib_name,
                    "type": stored.get("type", "?"),
                    "parent": stored.get("album", ""),
                    "grandparent": stored.get("artist", ""),
                }, cat)
                if state._media_logger:
                    state._media_logger.debug(_fmt_media_line(
                        "IMPORT", lib_name, stored.get("type", "?"), stored["title"],
                        user=user, tier=tier, result="UNRESOLVED", reason=reason,
                    ))
                return

            # Advance phase: resolve done, now we'll either skip
            # (target already rated) or write the rating. Either way
            # the work happens under the "rating" label.
            if state._dashboard:
                state._dashboard.set_current_item(
                    lib_name, stored.get("type", "?"), stored.get("title", ""),
                    phase="rating",
                )

            current_rating = getattr(live_item, "userRating", None)

            if current_rating is not None:
                _record_success(lib_name, {
                    "ts": ts, "tier": tier,
                    "title": stored["title"],
                    "type": stored.get("type", "?"),
                    "action": f"[SKIPPED — rating {current_rating} already set on target]",
                })
                logger.debug(
                    f"[{lib_name}] Skipped rating for '{stored['title']}' "
                    f"(target has {current_rating}, backup has {stored['user_rating']})"
                )
                if state._media_logger:
                    state._media_logger.debug(_fmt_media_line(
                        "IMPORT", lib_name, stored.get("type", "?"), stored["title"],
                        user=user, tier=tier,
                        rating=f"target={current_rating} backup={stored['user_rating']}",
                        result="skipped",
                    ))
            else:
                try:
                    _rate_item(base_url, live_item.ratingKey, stored["user_rating"], token)
                    _record_success(lib_name, {
                        "ts": ts, "tier": tier,
                        "title": stored["title"],
                        "type": stored.get("type", "?"),
                        "action": f"[RATING SET] {stored['user_rating']}",
                    })
                    logger.debug(
                        f"[{lib_name}] Set rating {stored['user_rating']} for '{stored['title']}'"
                    )
                    if state._media_logger:
                        state._media_logger.debug(_fmt_media_line(
                            "IMPORT", lib_name, stored.get("type", "?"), stored["title"],
                            user=user, tier=tier,
                            rating=stored["user_rating"],
                            result="SET",
                        ))
                except Exception as e:
                    _record_failure(lib_name, {
                        "ts": ts, "tier": tier,
                        "title": stored["title"],
                        "guid": stored.get("guids", [""])[0],
                        "filepath": stored.get("filepath", ""),
                        "reason": str(e),
                        "library": lib_name,
                        "type": stored.get("type", "?"),
                    }, "api_error")
                    logger.warning(f"[{lib_name}] Failed to set rating for '{stored['title']}': {e}")
        finally:
            if state._dashboard:
                state._dashboard.clear_current_item()

    with concurrent.futures.ThreadPoolExecutor(max_workers=state.MAX_WORKERS) as pool:
        futs = {pool.submit(process, stored): stored for stored in ratings}
        for fut in concurrent.futures.as_completed(futs):
            _advance_lib(lib_name)
            if fut.exception():
                stored = futs[fut]
                logger.warning(
                    f"[{lib_name}] Unexpected error rating '{stored.get('title', '?')}': "
                    f"{fut.exception()}"
                )


def import_backup_file(
    server: PlexServer,
    backup_path: str,
    token: str,
    base_url: str,
    logger: logging.Logger,
    log_dir: str,
    remap: Optional[Tuple[str, str]],
    strict_match: bool,
    home_users: Optional[List[Tuple[str, str, PlexServer]]] = None,
    existing_playlists: Optional[Dict[str, Any]] = None,
    sections_by_name: Optional[Dict[str, Any]] = None,
    preloaded_data: Optional[dict] = None,
    stop_event: Optional[threading.Event] = None,
) -> None:
    """
    Imports a single .plexbackup.json file into the target server.

    Reads the file, finds the matching library on the target server, and
    delegates the four data types to their respective import functions.
    If the backup contains per-user data (the "users" key), each user's
    Play Count, playlists, and ratings are imported using that user's own
    token and server connection so the data lands in the correct profile.

    Args:
        server (PlexServer): Active connection to the target server (admin).
        backup_path (str): Filesystem path to the .plexbackup.json file.
        token (str): Admin Plex auth token.
        base_url (str): Plex server base URL for direct API calls.
        logger (Logger): Shared logger.
        log_dir (str): Where per-library logs will be written.
        remap (tuple, optional): Path root translation.
        strict_match (bool): Whether to require exactly one fuzzy match.
        home_users (list, optional): List of (username, token, server) tuples.
        existing_playlists (dict, optional): Pre-fetched {title: Playlist} map.
        sections_by_name (dict, optional): Pre-built {title: section} lookup.
        preloaded_data (dict, optional): Already-parsed JSON from run_import.
    """
    if preloaded_data is not None:
        data = preloaded_data
    else:
        with open(backup_path, encoding="utf-8") as f:
            data = json.load(f)

    lib_name = data.get("library", "Unknown")
    logger.info(f"Importing from {backup_path} → library: {lib_name}")

    task_id = state._lib_task_ids.get(lib_name)

    def _set_phase(name: str) -> None:
        if state._dashboard:
            state._dashboard.set_library_phase(lib_name, name)
            state._dashboard.push_activity("phase", lib_name, f"→ {name}")
        elif state._live_progress and task_id is not None:
            state._live_progress.update(task_id, fields={"phase": name})

    if state._dashboard:
        state._dashboard.set_library_status(lib_name, "active")
        state._dashboard.push_activity("started", lib_name, "Import started")

    if sections_by_name is not None:
        section = sections_by_name.get(lib_name)
    else:
        section = next(
            (s for s in server.library.sections() if s.title == lib_name), None
        )

    if section is None:
        logger.error(
            f"Library '{lib_name}' not found on target server. "
            f"Ensure the library exists and has been scanned before importing."
        )
        return

    items = data.get("items", {})

    scan_cache: Dict[str, Any] = {}
    scan_lock = threading.Lock()

    def _warm_scan_cache():
        with scan_lock:
            if not scan_cache:
                logger.debug(f"[scan_cache] Prefetching filepath + suffix index for '{section.title}'")
                _sfx: Dict[str, List] = {}
                for _item in _section_leaf_items(section):
                    _path = _safe_file_path(_item)
                    if _path:
                        scan_cache[_path] = _item
                        _parts = _normalize_path_parts(_path)
                        for _n in (3, 2):
                            if len(_parts) >= _n:
                                _k = "/".join(_parts[-_n:])
                                _sfx.setdefault(_k, []).append(_item)
                if scan_cache:
                    scan_cache["__suffix_index__"] = _sfx

    threading.Thread(target=_warm_scan_cache, daemon=True).start()

    def _stopped() -> bool:
        return stop_event is not None and stop_event.is_set()

    _set_phase("Play Count" if section.type == "artist" else "Watched")
    import_watch_history(
        server, section, items.get("watch_history", []),
        token, base_url, logger, log_dir, remap, strict_match,
        scan_cache, scan_lock, user=state._plex_owner_name,
    )
    if _stopped():
        logger.info(f"Stop requested — skipping remaining phases for '{lib_name}'.")
        return
    _set_phase("Playlists")
    import_playlists(
        server, section, items.get("playlists", []),
        logger, log_dir, remap, strict_match,
        scan_cache, scan_lock,
        existing_playlists,
    )
    if _stopped():
        logger.info(f"Stop requested — skipping remaining phases for '{lib_name}'.")
        return
    _set_phase("Collections")
    import_collections(
        server, section, items.get("collections", []),
        logger, log_dir, remap, strict_match,
        scan_cache, scan_lock,
    )
    if _stopped():
        logger.info(f"Stop requested — skipping remaining phases for '{lib_name}'.")
        return
    _set_phase("Ratings")
    import_ratings(
        server, section, items.get("ratings", []),
        token, base_url, logger, remap, strict_match,
        scan_cache, scan_lock, user=state._plex_owner_name,
    )

    users_data = data.get("users", {})
    if users_data and not home_users:
        logger.info(
            f"Backup contains data for {len(users_data)} home user(s), but no "
            f"home users are available on this server (requires Plex.tv account). "
            f"Per-user data will be skipped."
        )

    user_lookup: Dict[str, Tuple[str, PlexServer]] = {
        uname: (utok, usrv) for uname, utok, usrv in (home_users or [])
    }

    def _import_user(username: str, user_data: dict) -> str:
        """Import one home user's watch history, playlists, and ratings."""
        if username not in user_lookup:
            logger.info(
                f"Home user '{username}' is in the backup but not on this server — "
                f"skipped. Add them to Plex Home and re-run to import their data."
            )
            return "skipped"

        user_token, user_server = user_lookup[username]

        user_section = next(
            (s for s in user_server.library.sections() if s.title == lib_name),
            None,
        )
        if user_section is None:
            logger.info(
                f"Library '{lib_name}' not accessible to home user '{username}' — skipped"
            )
            return "skipped"

        logger.info(
            f"Importing home user '{username}' — "
            f"library '{lib_name}': "
            f"{len(user_data.get('watch_history', []))} watch history, "
            f"{len(user_data.get('playlists', []))} playlist(s), "
            f"{len(user_data.get('ratings', []))} rating(s)"
        )

        import_watch_history(
            user_server, user_section, user_data.get("watch_history", []),
            user_token, base_url, logger, log_dir, remap, strict_match,
            scan_cache, scan_lock, user=username,
        )
        import_playlists(
            user_server, user_section, user_data.get("playlists", []),
            logger, log_dir, remap, strict_match,
            scan_cache, scan_lock,
            existing_playlists=None,
        )
        import_ratings(
            user_server, user_section, user_data.get("ratings", []),
            user_token, base_url, logger, remap, strict_match,
            scan_cache, scan_lock, user=username,
        )
        return "imported"

    if users_data:
        imported_users: List[str] = []
        skipped_users: List[str] = []
        n_user_workers = min(4, len(users_data))
        with concurrent.futures.ThreadPoolExecutor(max_workers=n_user_workers) as user_pool:
            futs = {
                user_pool.submit(_import_user, uname, udata): uname
                for uname, udata in users_data.items()
            }
            for fut in concurrent.futures.as_completed(futs):
                uname = futs[fut]
                exc = fut.exception()
                if exc:
                    logger.warning(f"Home user '{uname}' import error: {exc}")
                    skipped_users.append(uname)
                elif fut.result() == "imported":
                    imported_users.append(uname)
                else:
                    skipped_users.append(uname)

        if imported_users:
            logger.info(
                f"Home user import complete — {len(imported_users)} imported: "
                f"{sorted(imported_users)}"
            )
        if skipped_users:
            logger.info(
                f"Home user import — {len(skipped_users)} skipped (not on target server): "
                f"{sorted(skipped_users)}. "
                f"Add them to Plex Home and re-run to import their data."
            )


def run_import(
    server: PlexServer,
    backup_files: List[str],
    token: str,
    base_url: str,
    logger: logging.Logger,
    log_dir: str,
    remap: Optional[Tuple[str, str]],
    strict_match: bool,
) -> None:
    """
    Runs the full import pipeline for all selected backup files.

    Processes multiple backup files concurrently (up to 3 at once), then
    writes the troubleshooting and unresolved logs once all are done.

    Stop semantics: pressing [Q] (CLI) or POSTing /api/job/stop (server)
    flips ``stop_event``. The dashboard loop stops dispatching new
    library imports and best-effort cancels queued futures; mid-import
    library work is not interrupted. ThreadPoolExecutor's ``f.cancel()``
    only succeeds on futures that have not yet started running.

    Args:
        server (PlexServer): Active connection to the target server.
        backup_files (List[str]): Filesystem paths to .plexbackup.json files.
        token (str): Plex auth token.
        base_url (str): Plex server base URL.
        logger (Logger): Shared logger.
        log_dir (str): Where logs will be written.
        remap (tuple, optional): Path root translation.
        strict_match (bool): Whether to require exactly one fuzzy match.
    """
    # P0-1: wipe accumulators from any previous run so this run's totals
    # and troubleshoot.log don't inherit data from a prior job.
    state.reset_run_state()

    home_users = get_home_users(server, base_url, logger)
    home_user_names: Set[str] = {n for n, _, _ in home_users}
    if home_user_names:
        logger.info(
            f"Target server home users available for import ({len(home_user_names)}): "
            f"{sorted(home_user_names)}"
        )
    else:
        logger.info("No home users available on target server — only owner data will be imported")

    all_playlists: Dict[str, Any] = {pl.title: pl for pl in server.playlists()}
    logger.info(f"Fetched {len(all_playlists)} existing playlist(s) from target server")

    sections_by_name: Dict[str, Any] = {s.title: s for s in server.library.sections()}

    # P1-2: don't hold every backup file in memory at once. Peek each
    # file just long enough to extract its library name, item totals,
    # and user set; then let it go out of scope. import_backup_file
    # will re-open and load the file when its turn comes (it supports
    # this path natively via preloaded_data=None). Peak resident set
    # is now ~n_lib_workers files instead of len(backup_files) files.
    def _peek_metadata(data: dict) -> Tuple[str, int, Set[str]]:
        items = data.get("items", {})
        total = (
            len(items.get("watch_history", []))
            + sum(len(pl.get("items", [])) for pl in items.get("playlists", []))
            + len(items.get("playlists", []))
            + sum(len(c.get("items", [])) for c in items.get("collections", []))
            + len(items.get("collections", []))
            + len(items.get("ratings", []))
        )
        users = set(data.get("users", {}).keys())
        for uname, udata in data.get("users", {}).items():
            if uname in home_user_names:
                total += len(udata.get("watch_history", []))
                total += sum(len(pl.get("items", [])) for pl in udata.get("playlists", []))
                total += len(udata.get("playlists", []))
                total += len(udata.get("ratings", []))
        return data.get("library", ""), total, users

    lib_names: Dict[str, str] = {}
    lib_totals: Dict[str, int] = {}
    backup_users: Set[str] = set()
    readable_files: List[str] = []

    for bf in backup_files:
        try:
            with open(bf, encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception as e:
            logger.error(f"Could not read backup file {bf}: {e}")
            continue
        name, total, users = _peek_metadata(data)
        lib_names[bf] = name or bf
        lib_totals[bf] = total
        backup_users.update(users)
        readable_files.append(bf)
        # `data` falls out of scope here and is collectable.

    if not readable_files:
        logger.error("No readable backup files. Aborting import.")
        return

    if backup_users:
        missing = sorted(backup_users - home_user_names)
        logger.info(
            f"Backup contains data for {len(backup_users)} user(s): "
            f"{sorted(backup_users)}"
        )
        if missing:
            logger.info(
                f"These backup users are not on the target server and will be skipped: "
                f"{missing}"
            )

    n_lib_workers = min(3, len(readable_files))

    # Stop coordination — hoisted before the if/else so both branches share
    # one event. _keyboard_thread in CLI mode reads [Q]; the server-mode
    # stub stashes the event for /api/job/stop to flip.
    stop_event = threading.Event()
    kb = threading.Thread(
        target=_keyboard_thread, args=(log_dir, logger, stop_event), daemon=True
    )
    kb.start()

    def _submit_all(lib_pool):
        fmap: Dict[Any, str] = {}
        for bf in readable_files:
            # Skip submissions for any backup files queued after stop
            # was requested. Already-running futures continue until
            # their import_backup_file's per-phase stop check fires.
            if stop_event.is_set():
                logger.info(f"Stop requested — not submitting '{lib_names[bf]}'.")
                continue
            fut = lib_pool.submit(
                import_backup_file,
                server, bf, token, base_url, logger, log_dir, remap, strict_match,
                home_users, all_playlists, sections_by_name, None,  # preloaded_data=None: load on demand
                stop_event,
            )
            fmap[fut] = lib_names[bf]
        return fmap

    if _check_terminal_size():
        # ── Small-terminal fallback: Rich Progress bars ────────────────────────
        state._live_progress = _make_progress()
        for bf in readable_files:
            lib_name = lib_names[bf]
            state._lib_task_ids[lib_name] = state._live_progress.add_task(
                lib_name, total=max(lib_totals[bf], 1), completed=0, fields={"phase": "Queued"},
            )
        overall_task = state._live_progress.add_task(
            "Overall", total=len(readable_files), completed=0, fields={"phase": ""},
        )
        with Live(state._live_progress, console=console, refresh_per_second=8):
            with concurrent.futures.ThreadPoolExecutor(max_workers=n_lib_workers) as lib_pool:
                futs_map = _submit_all(lib_pool)
                for fut in concurrent.futures.as_completed(futs_map):
                    if fut.exception():
                        logger.error(f"Library import failed: {fut.exception()}")
                    state._live_progress.update(overall_task, advance=1)
        state._live_progress = None
        state._lib_task_ids.clear()

    else:
        # ── Full dashboard mode ────────────────────────────────────────────────
        # Augment any placeholder the job runner created so the
        # activity-feed entries from pre-flight (Plex connect, home-user
        # auth) are preserved when we paint the structured panels.
        if state._dashboard is None:
            state._dashboard = DashboardState(log_dir=log_dir)
        else:
            state._dashboard.log_dir = log_dir
        # Owner + every home user we connected to = total users this run covers.
        state._dashboard.set_user_count(1 + len(home_users))
        for bf in readable_files:
            lib_name = lib_names[bf]
            state._dashboard.add_library(lib_name, total=max(lib_totals[bf], 1))

        try:
            with Live(console=console, refresh_per_second=4) as live:
                state._live_instance = live
                with concurrent.futures.ThreadPoolExecutor(max_workers=n_lib_workers) as lib_pool:
                    futs_map = _submit_all(lib_pool)
                    pending = set(futs_map.keys())
                    while pending and not stop_event.is_set():
                        try:
                            live.update(_build_dashboard(state._dashboard.snapshot(), mode="IMPORT"))
                        except Exception:
                            pass
                        done, pending = concurrent.futures.wait(pending, timeout=0.25)
                        for fut in done:
                            lib_name = futs_map[fut]
                            exc = fut.exception()
                            if exc:
                                logger.error(f"Library import failed: {exc}")
                                state._dashboard.finish_library(lib_name, error=True)
                                state._dashboard.push_activity("error", lib_name, "Import failed")
                            else:
                                state._dashboard.finish_library(lib_name)
                                state._dashboard.push_activity("done", lib_name, "Import complete")
                    if stop_event.is_set():
                        for f in pending:
                            f.cancel()
                if not stop_event.is_set():
                    try:
                        live.update(_build_dashboard(state._dashboard.snapshot(), mode="IMPORT"))
                    except Exception:
                        pass
                    time.sleep(3)
        except Exception as render_err:
            logger.warning(
                f"Dashboard rendering unavailable ({render_err!r}). "
                f"Running without display — see {log_dir}/ for full details."
            )
            with concurrent.futures.ThreadPoolExecutor(max_workers=n_lib_workers) as lib_pool:
                futs_map = _submit_all(lib_pool)
                for fut in concurrent.futures.as_completed(futs_map):
                    lib_name = futs_map[fut]
                    exc = fut.exception()
                    if exc:
                        logger.error(f"Library import failed: {exc}")
                    else:
                        logger.info(f"Completed import for '{lib_name}'")
        finally:
            stop_event.set()
            state._live_instance = None
            state._dashboard = None

    write_troubleshoot_log(log_dir)
    write_unresolved_log(log_dir)

    total_success = sum(len(v) for v in _lib_successes.values())
    total_fail = sum(len(v) for v in _lib_failures.values())

    console.print(f"\n[bold green]Import complete.[/bold green]")
    console.print(f"  Succeeded: {total_success}")
    console.print(f"  Failed:    {total_fail}")

    if total_fail:
        console.print(f"  [yellow]See {log_dir}/ for troubleshooting details.[/yellow]")
    console.print()
