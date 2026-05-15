"""
Import pipeline for PlexMigrate.

Contains the four additive-merge import functions (watch history, playlists,
collections, ratings), the per-file orchestrator (restore_export_file), and
the top-level runner (run_restore) that manages the dashboard / progress display
and parallelises across multiple export files.
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
)
# NOTE: never ``from services.state import _lib_successes`` or
# ``_lib_failures`` - those names are ContextVar-backed via
# ``state.__getattr__`` (PEP 562). A ``from`` import evaluates the
# proxy ONCE at module load and freezes the resolved value (None at
# that moment) in this module's namespace forever, so subsequent
# ``reset_run_state`` writes are invisible. Always access them via
# ``state._lib_successes`` / ``state._lib_failures`` at use sites.
from services.auth import get_home_users
from services.dashboard import (
    DashboardState,
    _advance_lib,
    _build_dashboard,
    _check_terminal_size,
    _current_item,
    _http_lib_var,
    _keyboard_thread,
    _make_progress,
    _thread_category,
    library_http_context,
    submit_with_context,
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
    _build_scan_cache,
    _category_for_failure,
    resolve_item,
)

from rich.live import Live


# ── Chunked playlist / collection helpers (v0.11.1) ──────────────────────────
#
# Plex's embedded HTTP server caps request URL length around 8 KB. The
# ``Playlist.create(items=…)`` and ``Collection.create(items=…)`` paths
# in python-plexapi join every item's ratingKey into the ``uri=`` query
# parameter - for a static playlist with thousands of members, the
# URL blows past the cap and Plex returns 400 ``bad_request`` without
# creating anything. ``addItems`` has the same shape and the same
# failure mode at scale.
#
# Fix: do the create with a small first batch, then call ``addItems``
# in further small batches until the entire member list lands. Each
# individual call stays well under the URL limit.
#
# 100 items per batch is conservative. A typical Plex ratingKey is
# 5–6 digits + ``%2C`` separator (4 chars URL-encoded), so 100 items
# is ≈900 bytes in the ``uri=`` parameter - leaves ~7 KB of headroom
# for the rest of the URL (base, query keys, machineIdentifier, etc.).
_PLAYLIST_CHUNK_SIZE = 100


# Plex's playlistType enum is one of "audio" / "video" / "photo".
# Map each leaf item type from our snapshot serialiser to the Plex
# playlist family it'd belong in. Used by the append-path conflict
# check above.
_ITEM_TYPE_TO_PLEX_PLAYLIST_TYPE: Dict[str, str] = {
    "track":   "audio",
    "album":   "audio",
    "artist":  "audio",
    "movie":   "video",
    "episode": "video",
    "show":    "video",
    "season":  "video",
    "clip":    "video",
    "photo":   "photo",
}


# Map Plex section.type → playlist family. Used to scope playlist
# lookups so a Music-library restore can't accidentally fetch a
# video-typed playlist that happens to share a name.
_SECTION_TYPE_TO_PLAYLIST_TYPE: Dict[str, str] = {
    "artist": "audio",
    "movie":  "video",
    "show":   "video",
    "photo":  "photo",
}


def _playlist_type_for_section(section: Any) -> str:
    """
    Return the Plex playlistType family ("audio" / "video" / "photo")
    that playlists living in ``section`` belong to. Empty string when
    the section type is unrecognised (defensive: future Plex types
    don't blow up the lookup, they just fall back to name-only).
    """
    libtype = (getattr(section, "type", "") or "").lower()
    return _SECTION_TYPE_TO_PLAYLIST_TYPE.get(libtype, "")


def _plex_playlist_type_for_items(items: List[Any]) -> str:
    """
    Derive the Plex playlistType ("audio" / "video" / "photo") from a
    list of resolved media items. Inspects each item's ``.type`` and
    returns the dominant family. Returns ``""`` when the list is empty
    or no item carries a recognisable type (caller treats empty as
    "skip the type-conflict check" - safer to proceed than to
    false-positive on an unknown future Plex type).
    """
    counts: Dict[str, int] = {}
    for it in items:
        t = getattr(it, "type", "") or ""
        family = _ITEM_TYPE_TO_PLEX_PLAYLIST_TYPE.get(t.lower())
        if family:
            counts[family] = counts.get(family, 0) + 1
    if not counts:
        return ""
    # Majority winner. Tie-breaking by alphabetical (audio < photo <
    # video) is stable + irrelevant in practice since real
    # playlists never split across families.
    return max(sorted(counts.keys()), key=lambda k: counts[k])


def _filter_to_dominant_playlist_type(
    items: List[Any],
) -> Tuple[List[Any], List[Any], str]:
    """
    Split ``items`` into ``(kept, dropped, family)`` where ``kept`` are
    the items belonging to the dominant Plex playlist family and
    ``dropped`` are any off-type items.

    Plex playlists are strictly single-type — a mixed member list must
    never reach a create/append call, or Plex rejects the whole batch
    with "Can not mix media types when building a playlist". A mixed
    list at the write boundary is an *upstream bug* (a wrong-type
    resolver match, or a snapshot read with mixed members), not a
    normal case, so the caller logs the drop as an error and surfaces
    it on the per-container result.

    Items whose ``.type`` is unrecognised are kept (consistent with
    ``_plex_playlist_type_for_items`` choosing to proceed on unknown
    future Plex types rather than false-positive). ``family`` is ``""``
    only when no item carries a recognisable type — caller leaves the
    list untouched in that case.
    """
    family = _plex_playlist_type_for_items(items)
    if not family:
        return list(items), [], ""
    kept: List[Any] = []
    dropped: List[Any] = []
    for it in items:
        t = (getattr(it, "type", "") or "").lower()
        fam = _ITEM_TYPE_TO_PLEX_PLAYLIST_TYPE.get(t)
        if fam is None or fam == family:
            kept.append(it)
        else:
            dropped.append(it)
    return kept, dropped, family


def _create_playlist_chunked(server, name: str, items: List[Any]) -> Any:
    """
    Create a playlist whose member list may exceed Plex's URL-length
    limit. Behaviour matches ``Playlist.create(server, name, items=…)``
    for small lists; for large lists it transparently does one
    ``create`` plus N ``addItems`` calls.

    Returns the created :class:`plexapi.playlist.Playlist` instance.
    Raises whatever ``Playlist.create`` / ``addItems`` raise on failure
    - callers already wrap these in a try/except so we don't catch
    here.
    """
    if not items:
        # Nothing to add; let plexapi's empty-items handling apply.
        return Playlist.create(server, name, items=items)
    first_batch = items[:_PLAYLIST_CHUNK_SIZE]
    rest = items[_PLAYLIST_CHUNK_SIZE:]
    pl = Playlist.create(server, name, items=first_batch)
    for start in range(0, len(rest), _PLAYLIST_CHUNK_SIZE):
        pl.addItems(rest[start:start + _PLAYLIST_CHUNK_SIZE])
    return pl


def _add_to_playlist_chunked(playlist: Any, items: List[Any]) -> None:
    """
    Add ``items`` to ``playlist`` in safe-size chunks. Matches the
    behaviour of ``playlist.addItems(items)`` for small inputs; chunks
    for large ones so the URL-length cap never trips.
    """
    if not items:
        return
    for start in range(0, len(items), _PLAYLIST_CHUNK_SIZE):
        playlist.addItems(items[start:start + _PLAYLIST_CHUNK_SIZE])


def _create_collection_chunked(server, name: str, section: Any, items: List[Any]) -> Any:
    """
    Same chunking strategy as :func:`_create_playlist_chunked` for the
    collection-create path. Plex collections share the same URL-length
    failure mode - a static collection with thousands of members
    cannot be created in one ``Collection.create`` call.
    """
    if not items:
        return Collection.create(server, name, section, items=items)
    first_batch = items[:_PLAYLIST_CHUNK_SIZE]
    rest = items[_PLAYLIST_CHUNK_SIZE:]
    coll = Collection.create(server, name, section, items=first_batch)
    for start in range(0, len(rest), _PLAYLIST_CHUNK_SIZE):
        coll.addItems(rest[start:start + _PLAYLIST_CHUNK_SIZE])
    return coll


def _add_to_collection_chunked(collection: Any, items: List[Any]) -> None:
    """Chunked counterpart to ``collection.addItems(items)``."""
    if not items:
        return
    for start in range(0, len(items), _PLAYLIST_CHUNK_SIZE):
        collection.addItems(items[start:start + _PLAYLIST_CHUNK_SIZE])


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
    }
    # M1: the token rides an HTTP header, not the query string. A
    # query-string token is logged verbatim by Plex's own access log
    # and any intermediary proxy/cache; a header is not.
    state._session.get(
        url, params=params, headers={"X-Plex-Token": token}, timeout=5,
    )


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
    }
    # M1: token in a header, not the query string (see _scrobble).
    state._session.get(
        url, params=params, headers={"X-Plex-Token": token}, timeout=5,
    )


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
    }
    # M1: token in a header, not the query string (see _scrobble).
    state._session.put(
        url, params=params, headers={"X-Plex-Token": token}, timeout=10,
    )


# ── Import - Additive Merge Functions ─────────────────────────────────────────

def restore_watch_history(
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
    stop_event: Optional[threading.Event] = None,
    mode: str = "merge",
    # v0.13.x: Merge-mode watch-count strategy. Two values:
    #   "higher" (default, legacy) - final destination view count is
    #       max(stored, current). Add only the difference when stored
    #       is higher; otherwise no-op. Idempotent across re-runs.
    #   "sum" - final destination view count is current + stored.
    #       Every captured play is treated as a real event that adds
    #       to the destination's count. NOT idempotent: re-running the
    #       same job will double-count. Operator opt-in.
    # Ignored when mode == "replace" (Replace overwrites unconditionally,
    # so there is no notion of combining counts).
    merge_watch_strategy: str = "higher",
) -> None:
    """
    Imports Play Count for a library. Two top-level modes (with a sub-
    strategy under Merge for watch counts only):

    Merge (default):
        - View count: only INCREASED, never reduced. The sub-strategy
          ``merge_watch_strategy`` picks how the increase is computed:
            * ``higher`` (default) - increase by max(0, stored - current),
              so destination ends at max(stored, current). Idempotent.
            * ``sum`` - increase by exactly ``stored``, so destination
              ends at current + stored. Operator opt-in; NOT idempotent.
        - Resume position: only set if the target item has NO saved progress.

    Replace (opt-in, destructive):
        - View count: set to exactly the stored value. If the destination
          has more plays than the snapshot, ``markUnplayed`` resets to 0
          first, then we scrobble up to the stored count (capped by
          VIEWCOUNT_INCREMENT_CAP).
        - Resume position: set to the stored value unconditionally.
        - Used for true point-in-time recovery. Operator-gated by the
          UI's typed-REPLACE confirmation; the safety belt auto-captures
          a pre-replace snapshot of the destination before the engine
          fires.

    Args:
        server (PlexServer): Active connection to the target server.
        section: Target library section.
        watch_items (List[Dict]): Serialized watched item dicts from the export.
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

    # Discover-don't-predict: register the real watch-item count for
    # this library. The watch batch + the total-run tracker grow by
    # exactly this many; the per-item tick happens in the as_completed
    # loop below (NOT via inc_watch, which would double-count the Run
    # Coverage counter - the timing tracker is a separate concern).
    if state.get_dashboard() and total:
        state.get_dashboard().add_batch_total("watch", total)
        state.get_dashboard().add_run_total(total)

    scrobble_sem = threading.Semaphore(state.SCROBBLE_WORKERS)
    # v0.9.7 Item 5: register every worker thread under the right
    # category so the dashboard's Thread Pool panel shows live
    # counts during imports. Music sections use ``play_count``;
    # everything else uses ``watched`` - same mapping the snapshotter
    # uses in gather_watch. Pre-v0.9.7 the import side never called
    # _thread_category, leaving the panel blank for every import
    # path including direct-transfer-import.
    worker_category = "play_count" if section.type == "artist" else "watched"

    def process(stored: Dict) -> None:
        # Stop P4: item-level checkpoint. Every queued resolve task
        # short-circuits here once the stop_event is set, so a soft
        # Stop drains the pool near-instantly instead of resolving
        # every remaining item.
        if stop_event is not None and stop_event.is_set():
            return
        with _thread_category(worker_category):
            _process_one_watch_item(stored)

    def _process_one_watch_item(stored: Dict) -> None:
        # Surface this item on the dashboard's "Currently Processing"
        # panel. Phase advances from "resolving" → "scrobbling" so the
        # user can tell which workers are doing GUID/filepath lookups
        # vs which are mid-HTTP-write. Age (started_at) is preserved
        # across the transition by set_current_item so the column
        # reflects total time on this item, not just the current phase.
        if state.get_dashboard():
            state.get_dashboard().set_current_item(
                lib_name, stored.get("type", "?"), stored.get("title", ""),
                phase="resolving",
            )
            # inc_watch() intentionally NOT called here. The watched-items
            # counter in Run Coverage reflects items gathered from the
            # source server (snapshotter.py increments it during the gather
            # phase). Incrementing again during the import/apply phase
            # double-counts the same items and inflates the displayed
            # total - making it look like twice as much was watched and
            # throwing off any ETA or progress calculation that uses the
            # counter as a signal.
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
                    "guid": (stored.get("guids") or [""])[0],
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
            if state.get_dashboard() and not (locals().get("live_item")):
                state.get_dashboard().clear_current_item()

        # Advance phase to "scrobbling" for the write portion. We don't
        # construct a fresh CurrentItem - set_current_item preserves
        # started_at so Age keeps counting from when resolve started.
        if state.get_dashboard():
            state.get_dashboard().set_current_item(
                lib_name, stored.get("type", "?"), stored.get("title", ""),
                phase="scrobbling",
            )

        try:
            current_view_count = getattr(live_item, "viewCount", 0) or 0
            current_view_offset = getattr(live_item, "viewOffset", 0) or 0
            stored_view_count = stored.get("view_count", 0) or 0
            stored_view_offset = stored.get("view_offset", 0) or 0

            # v0.13.x Replace mode: set viewCount to exactly the stored
            # value. If the destination is currently HIGHER than the
            # snapshot (the operator watched something after the
            # snapshot was taken), reset to 0 first via markUnplayed so
            # the scrobble loop below can bring it back up to the stored
            # value. The resume position is always overwritten.
            if mode == "replace":
                if current_view_count > stored_view_count:
                    try:
                        live_item.markUnplayed()
                        current_view_count = 0
                    except Exception as exc:
                        logger.warning(
                            "[%s] Could not reset viewCount on %r before "
                            "Replace re-scrobble: %s. Stored=%d, target was %d.",
                            lib_name, stored.get("title", "?"), exc,
                            stored_view_count, current_view_count,
                        )
                        # Best-effort: continue with the scrobble path
                        # anyway. The end state will be at least
                        # stored + (original current - stored) - not
                        # perfect, but the operator was warned.
                views_to_add = max(0, stored_view_count - current_view_count)
                set_offset_unconditionally = True
            else:
                # Merge mode. Two sub-strategies select what "additive"
                # means for the view count itself:
                #   higher: bring destination up to max(stored, current) -
                #           idempotent re-runs, the legacy default.
                #   sum:    add the snapshot's count on top of current -
                #           treats every captured play as a real event.
                # Both still satisfy the "Merge never reduces a count"
                # contract; the difference is only the upper bound.
                if merge_watch_strategy == "sum":
                    views_to_add = stored_view_count
                else:
                    views_to_add = max(0, stored_view_count - current_view_count)
                set_offset_unconditionally = False

            if views_to_add == 0:
                # Replace mode still needs to write the resume offset
                # even when view counts already match. Merge mode keeps
                # the legacy skip-with-no-work behaviour.
                if mode == "replace" and stored_view_offset != current_view_offset:
                    try:
                        with scrobble_sem:
                            _set_resume_position(
                                base_url, live_item.ratingKey,
                                stored_view_offset, token,
                            )
                        _record_success(lib_name, {
                            "ts": ts, "tier": tier,
                            "title": stored["title"],
                            "type": stored.get("type", "?"),
                            "action": f"[REPLACED] view_offset = {stored_view_offset}",
                        })
                    except Exception as exc:
                        logger.warning(
                            "[%s] Replace: failed to set view_offset on %r: %s",
                            lib_name, stored.get("title", "?"), exc,
                        )
                    return
                _record_success(lib_name, {
                    "ts": ts, "tier": tier,
                    "title": stored["title"],
                    "type": stored.get("type", "?"),
                    "action": f"skipped - target viewCount ({current_view_count}) >= stored ({stored_view_count})",
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
            # a runaway export (e.g. stale viewCount of 99999) cannot hammer
            # Plex with thousands of writes for one track. The log line below
            # reports the *actual* number fired, plus the stored target - the
            # delta tells the user whether a re-run is needed to advance further.
            actual_views = min(views_to_add, state.VIEWCOUNT_INCREMENT_CAP)
            capped = actual_views < views_to_add

            with scrobble_sem:
                for _ in range(actual_views):
                    _scrobble(base_url, live_item.ratingKey, token)

                # Merge: only set offset when target has no progress.
                # Replace: always overwrite (operator wants point-in-time).
                if set_offset_unconditionally:
                    if stored_view_offset != current_view_offset:
                        _set_resume_position(base_url, live_item.ratingKey, stored_view_offset, token)
                elif current_view_offset == 0 and stored_view_offset > 0:
                    _set_resume_position(base_url, live_item.ratingKey, stored_view_offset, token)

            if capped:
                action = (
                    f"added {actual_views} view(s) - capped at {state.VIEWCOUNT_INCREMENT_CAP} "
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
            if state.get_dashboard():
                state.get_dashboard().clear_current_item()

    with concurrent.futures.ThreadPoolExecutor(max_workers=state.MAX_WORKERS) as pool:
        futures = {submit_with_context(pool, process, item): item for item in watch_items}
        for f in concurrent.futures.as_completed(futures):
            _advance_lib(lib_name)
            # Tick the watch batch + total-run tracker once per item
            # finished. inc_watch() is deliberately not used here (see
            # the note above); the timing tracker needs its own tick.
            if state.get_dashboard():
                state.get_dashboard().tick_batch("watch", 1)
                state.get_dashboard().tick_etr(1)
            exc = f.exception()
            if exc:
                logger.error(f"Thread error in Play Count import: {exc}")

    write_library_logs(log_dir, lib_name, total)


def restore_playlists(
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
    stop_event: Optional[threading.Event] = None,
    mode: str = "merge",
) -> None:
    """
    Imports playlists. Two modes:

    Merge (default, additive union):
        - If a playlist does NOT exist: create it. Log [CREATED].
        - If it DOES exist: append missing items only. Log [APPENDED].

    Replace (opt-in, destructive):
        - If a playlist does NOT exist: create it. Log [CREATED].
        - If it DOES exist: diff against snapshot. Members in snapshot but
          not currently present are added; members currently present but
          not in snapshot are REMOVED. The playlist row itself is
          preserved (not deleted/recreated) so out-of-band metadata
          stays intact. Log [REPLACED] with both counts.

    Two-phase design:
        Phase 1 (parallel): Resolve all items across all non-smart playlists.
        Phase 2 (sequential): Create or merge playlists one at a time.

    Args:
        server (PlexServer): Active connection to the target server.
        section: Target library section.
        playlists (List[Dict]): Serialized playlist dicts from the export.
        logger (Logger): Shared logger.
        log_dir (str): Unused here; included for signature symmetry.
        remap (tuple, optional): Path root translation.
        strict_match (bool): Whether to require exactly one fuzzy match.
        scan_cache (dict, optional): Shared filepath→item dict.
        scan_lock (Lock, optional): Protects the initial build of scan_cache.
        existing_playlists (dict, optional): Pre-fetched {title: Playlist} dict.
    """
    lib_name = section.title

    # PR-1 / Phase B (skip-playlists end-to-end): if there are no
    # playlists in the payload AND the caller didn't hand us a
    # pre-fetched destination map, return without ever calling
    # ``server.playlists()``. Pre-this-gate, a payload with zero
    # playlists still triggered a full destination prefetch here as a
    # side-effect of the lazy default - wasted API traffic on every
    # library that had no playlists to import.
    if not playlists and existing_playlists is None:
        return

    if state.get_dashboard():
        _non_smart_pl = sum(1 for pl_data in playlists if not pl_data.get("smart", False))
        if _non_smart_pl:
            state.get_dashboard().add_batch_total("playlist", _non_smart_pl)
            state.get_dashboard().add_run_total(_non_smart_pl)

    # Scope the existing-playlist lookup to playlists whose
    # ``playlistType`` matches the target section's family. Without
    # this filter, ``server.playlists()`` returns audio + video +
    # photo playlists all collapsed into one ``title -> playlist``
    # dict, and a same-named playlist in a different library
    # (e.g. an "Hazbin Hotel" video playlist on the TV section)
    # would silently outrank the audio one we actually want for the
    # Music section. Plex itself enforces "one media type per
    # playlist" and rejects appends across the boundary, so silently
    # mapping to the wrong-type playlist surfaced as "Can not mix
    # media types when building a playlist: video and audio".
    target_playlist_type = _playlist_type_for_section(section)
    if existing_playlists is None:
        existing_playlists = {
            pl.title: pl
            for pl in server.playlists()
            if not target_playlist_type
            or (getattr(pl, "playlistType", "") or "").lower() == target_playlist_type
        }
    else:
        # Caller supplied a prefetched dict. Filter it the same way -
        # the top-level prefetch in run_restore is shared across
        # sections and pre-includes everything, so per-section
        # filtering happens here.
        if target_playlist_type:
            existing_playlists = {
                title: pl
                for title, pl in existing_playlists.items()
                if (getattr(pl, "playlistType", "") or "").lower() == target_playlist_type
            }

    resolve_tasks: List[Tuple[int, Dict]] = []
    for pl_idx, pl_data in enumerate(playlists):
        if not pl_data.get("smart", False):
            for stored in pl_data.get("items", []):
                resolve_tasks.append((pl_idx, stored))

    resolved_by_pl: Dict[int, List[Tuple[Any, int]]] = {}
    # Rule 4: per-playlist unresolved-member list so the post-merge
    # summary can show {restored, total, skipped_items[]} without
    # losing the title/reason for each miss.
    unresolved_by_pl: Dict[int, List[Dict[str, str]]] = {}

    def _resolve_one(pl_idx: int, stored: Dict) -> Tuple[int, Optional[Any], int, Dict, str]:
        # Stop P4: item-level checkpoint - queued resolves short-circuit
        # once the stop_event is set. Returns the standard "unresolved"
        # tuple shape (item=None) so the caller handles it gracefully.
        if stop_event is not None and stop_event.is_set():
            return pl_idx, None, stored.get("position", 0), stored, "stop requested"
        # v0.9.7 Item 5: register worker under "playlists" so the
        # Thread Pool panel shows live counts during playlist resolve.
        with _thread_category("playlists"):
            item, _tier, reason = resolve_item(
                server, section, stored, logger, remap, strict_match,
                scan_cache, scan_lock,
            )
            return pl_idx, item, stored.get("position", 0), stored, reason

    if resolve_tasks:
        with concurrent.futures.ThreadPoolExecutor(max_workers=state.MAX_WORKERS) as pool:
            futs = [submit_with_context(pool, _resolve_one, pl_idx, stored)
                    for pl_idx, stored in resolve_tasks]
            for fut in concurrent.futures.as_completed(futs):
                _advance_lib(lib_name)
                try:
                    pl_idx, item, position, stored, reason = fut.result()
                    if item:
                        resolved_by_pl.setdefault(pl_idx, []).append((item, position))
                    else:
                        unresolved_by_pl.setdefault(pl_idx, []).append({
                            "title": stored.get("title", "(unknown)"),
                            "type": stored.get("type", ""),
                            "reason": reason or "not found on destination",
                        })
                except Exception as exc:
                    logger.warning(f"[{lib_name}] Playlist item resolution error: {exc}")

    for pl_idx, pl_data in enumerate(playlists):
        pl_name = pl_data["name"]
        ts = _tz_now()

        if state.get_dashboard():
            state.get_dashboard().set_current_item(
                lib_name, "playlist", pl_name, phase="merging",
            )

        # P0-3: advance the per-library progress for every playlist we
        # consider, regardless of which exit branch we end up taking
        # (smart-skip, no-items-resolved, or processed). Without this,
        # smart playlists and empty-resolved playlists are counted in
        # _lib_total() but never advanced, leaving the bar stuck short
        # of 100%.
        try:
            if pl_data.get("smart", False):
                smart_content = pl_data.get("smart_content", "")
                # Rule 5: surface the operator-facing reason at INFO so
                # the run log line is unambiguous and survives without
                # log-level filtering.
                logger.info(
                    "[%s] %s — smart playlist, requires manual recreation on destination "
                    "(filter URL: %s)",
                    lib_name, pl_name, smart_content or "unavailable",
                )
                _record_failure(lib_name, {
                    "ts": ts, "tier": "none", "title": pl_name,
                    "reason": "Smart playlist - must be recreated manually on target server",
                    "library": lib_name, "type": "playlist",
                    "guid": "", "filepath": "",
                }, "smart_playlist_skipped")
                if state.get_dashboard():
                    state.get_dashboard().record_container_result(
                        kind="playlist",
                        name=pl_name,
                        library=lib_name,
                        total=0,
                        restored=0,
                        smart=True,
                        reason="Smart playlist — requires manual recreation on destination",
                    )
                continue

            if state.get_dashboard():
                state.get_dashboard().inc_playlist()

            resolved_with_positions = resolved_by_pl.get(pl_idx, [])
            resolved_with_positions.sort(key=lambda x: x[1])
            resolved_items = [item for item, _ in resolved_with_positions]
            total_members = len(pl_data.get("items") or [])
            unresolved_members = unresolved_by_pl.get(pl_idx, [])

            # Plex playlists are strictly single-type. A resolved item
            # list that spans media families must never reach a
            # create/append call — Plex rejects the whole batch with
            # "Can not mix media types when building a playlist". A
            # mixed list here is an upstream bug (a wrong-type resolver
            # match, or a snapshot read with mixed members), so drop the
            # off-type items, log it as an error so the bug is
            # attributable, and surface the drop on the per-container
            # result. The dominant-family items still restore normally.
            _kept, _dropped_offtype, _family = _filter_to_dominant_playlist_type(resolved_items)
            if _dropped_offtype:
                _off = ", ".join(
                    f"{getattr(d, 'title', '?')!r}({getattr(d, 'type', '?')})"
                    for d in _dropped_offtype[:10]
                )
                logger.error(
                    "[%s] %s — %d off-type item(s) dropped before write "
                    "(playlist family=%r). This indicates a wrong-type "
                    "resolver match or a mixed-type snapshot read upstream. "
                    "Dropped: %s%s",
                    lib_name, pl_name, len(_dropped_offtype), _family, _off,
                    " …" if len(_dropped_offtype) > 10 else "",
                )
                resolved_items = _kept
                unresolved_members = unresolved_members + [
                    {
                        "title": getattr(d, "title", "(unknown)"),
                        "type": getattr(d, "type", ""),
                        "reason": (
                            f"off-type for {_family} playlist — dropped to "
                            f"avoid Plex media-type mix rejection"
                        ),
                    }
                    for d in _dropped_offtype
                ]

            if not resolved_items:
                logger.warning(
                    "[%s] %s — 0/%d items restored (no members resolved on destination)",
                    lib_name, pl_name, total_members,
                )
                if state.get_dashboard():
                    state.get_dashboard().record_container_result(
                        kind="playlist",
                        name=pl_name,
                        library=lib_name,
                        total=total_members,
                        restored=0,
                        skipped_items=unresolved_members,
                        reason="No members resolved on destination",
                    )
                continue

            if pl_name not in existing_playlists:
                try:
                    _create_playlist_chunked(server, pl_name, resolved_items)
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
                # Plex enforces a single media type per playlist
                # ("audio" / "video" / "photo"). Trying to addItems
                # across that boundary fails with a generic API error.
                # Catch it up front so the operator sees a clear
                # "destination playlist X is the wrong type" message
                # instead of "can not mix media types when building".
                # The polluted destination state usually traces to a
                # prior restore run that fanned out to libraries that
                # didn't match the snapshot's scope.
                expected_pl_type = _plex_playlist_type_for_items(resolved_items)
                existing_pl_type = (
                    getattr(existing_pl, "playlistType", None) or ""
                ).lower()
                if (
                    expected_pl_type
                    and existing_pl_type
                    and expected_pl_type != existing_pl_type
                ):
                    reason = (
                        f"Destination playlist '{pl_name}' is type "
                        f"'{existing_pl_type}' but the snapshot's items are "
                        f"type '{expected_pl_type}'. Most often caused by a "
                        f"prior restore run that wrote items from the wrong "
                        f"library into this playlist. Fix: delete '{pl_name}' "
                        f"on the destination server and re-run the restore."
                    )
                    _record_failure(lib_name, {
                        "ts": ts, "tier": "none", "title": pl_name,
                        "reason": reason, "library": lib_name, "type": "playlist",
                        "guid": "", "filepath": "",
                    }, "playlist_type_conflict")
                    logger.error(
                        "Playlist type conflict: %s (destination=%r, snapshot=%r). "
                        "Skipping append; delete the destination playlist and re-run.",
                        pl_name, existing_pl_type, expected_pl_type,
                    )
                    # Surface in the per-container summary so the
                    # Dashboard's restore table shows the operator
                    # the exact playlist that needs cleanup.
                    if state.get_dashboard():
                        state.get_dashboard().record_container_result(
                            kind="playlist",
                            name=pl_name,
                            library=lib_name,
                            total=total_members,
                            restored=0,
                            reason=reason,
                        )
                    continue
                try:
                    existing_items = list(existing_pl.items())
                    existing_keys: Set[int] = {
                        item.ratingKey for item in existing_items
                    }
                    snapshot_keys: Set[int] = {
                        i.ratingKey for i in resolved_items
                    }
                    items_to_add = [
                        i for i in resolved_items if i.ratingKey not in existing_keys
                    ]
                    items_present = [
                        i for i in resolved_items if i.ratingKey in existing_keys
                    ]

                    if mode == "replace":
                        # v0.13.x Replace branch: diff existing vs the
                        # snapshot's resolved set and REMOVE members
                        # that aren't in the snapshot. Adds happen as
                        # in merge mode. The playlist row itself stays
                        # so any out-of-band metadata (poster, sort)
                        # survives.
                        items_to_remove = [
                            it for it in existing_items
                            if it.ratingKey not in snapshot_keys
                        ]
                        if items_to_remove:
                            try:
                                # plexapi's removeItems may not chunk;
                                # err on the side of one call per item
                                # to keep URL lengths bounded.
                                for it in items_to_remove:
                                    existing_pl.removeItems([it])
                            except Exception as exc:
                                logger.warning(
                                    "[%s] Replace: removeItems failed on %r: %s. "
                                    "Continuing with adds; pre-Replace snapshot "
                                    "is the recovery point.",
                                    lib_name, pl_name, exc,
                                )
                        if items_to_add:
                            _add_to_playlist_chunked(existing_pl, items_to_add)
                        _record_success(lib_name, {
                            "ts": ts, "tier": "N/A", "title": pl_name,
                            "type": "playlist",
                            "action": (
                                f"[REPLACED] +{len(items_to_add)} / -{len(items_to_remove)} "
                                f"(snapshot had {len(resolved_items)}, "
                                f"destination had {len(existing_items)})"
                            ),
                        })
                        logger.info(
                            f"Replaced playlist '{pl_name}': "
                            f"+{len(items_to_add)} added, -{len(items_to_remove)} removed"
                        )
                    else:
                        # Merge (default): additive union only.
                        if items_to_add:
                            _add_to_playlist_chunked(existing_pl, items_to_add)
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
                                "action": f"[SKIPPED - already in playlist '{pl_name}']",
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

            # Rule 4: emit the per-container result for the Dashboard
            # summary + JobRecord.summary. Reached on the create-or-
            # merge success path as well as the API-error path so every
            # non-smart playlist with at least one resolved member
            # contributes a row.
            restored_count = len(resolved_items)
            if total_members != restored_count:
                logger.info(
                    "[%s] %s — %d/%d items restored (%d not available on destination)",
                    lib_name, pl_name, restored_count, total_members,
                    total_members - restored_count,
                )
            if state.get_dashboard():
                state.get_dashboard().record_container_result(
                    kind="playlist",
                    name=pl_name,
                    library=lib_name,
                    total=total_members,
                    restored=restored_count,
                    skipped_items=unresolved_members,
                )
        finally:
            if state.get_dashboard():
                state.get_dashboard().clear_current_item()
            _advance_lib(lib_name)


def restore_collections(
    server: PlexServer,
    section,
    collections: List[Dict],
    logger: logging.Logger,
    log_dir: str,
    remap: Optional[Tuple[str, str]],
    strict_match: bool,
    scan_cache: Optional[Dict[str, Any]] = None,
    scan_lock: Optional[threading.Lock] = None,
    stop_event: Optional[threading.Event] = None,
    mode: str = "merge",
) -> None:
    """
    Imports collections. Two modes:

    Merge (default, additive union):
        - If collection does NOT exist: create it. Log [CREATED].
        - If it DOES exist: append missing members only. Log [APPENDED].

    Replace (opt-in, destructive):
        - If collection does NOT exist: create it. Log [CREATED].
        - If it DOES exist: diff against snapshot. Members in snapshot
          but not currently present are added; members currently present
          but not in snapshot are REMOVED. The collection row itself is
          preserved (not deleted/recreated) so out-of-band metadata
          stays intact. Log [REPLACED] with both counts.

    Two-phase design (mirrors restore_playlists):
        Phase 1 (parallel): Resolve all member items across all collections.
        Phase 2 (sequential): Create or merge collections one at a time.

    Args:
        server (PlexServer): Active connection to the target server.
        section: Target library section.
        collections (List[Dict]): Serialized collection dicts from the export.
        logger (Logger): Shared logger.
        log_dir (str): Unused here; included for signature symmetry.
        remap (tuple, optional): Path root translation.
        strict_match (bool): Whether to require exactly one fuzzy match.
        scan_cache (dict, optional): Shared filepath→item dict.
        scan_lock (Lock, optional): Protects the initial build of scan_cache.
    """
    lib_name = section.title
    existing_collections = {c.title: c for c in section.collections()}

    if state.get_dashboard() and collections:
        state.get_dashboard().add_batch_total("collection", len(collections))
        state.get_dashboard().add_run_total(len(collections))

    resolve_tasks: List[Tuple[int, Dict]] = [
        (coll_idx, stored)
        for coll_idx, coll_data in enumerate(collections)
        for stored in coll_data.get("items", [])
    ]

    resolved_by_coll: Dict[int, List[Any]] = {}
    # Rule 4: per-collection unresolved-member list so the post-merge
    # summary carries {restored, total, skipped_items[]}.
    unresolved_by_coll: Dict[int, List[Dict[str, str]]] = {}

    def _resolve_member(coll_idx: int, stored: Dict) -> Tuple[int, Optional[Any], Dict, str]:
        # Stop P4: item-level checkpoint - queued resolves short-circuit
        # once the stop_event is set, returning the standard
        # "unresolved" tuple shape (item=None).
        if stop_event is not None and stop_event.is_set():
            return coll_idx, None, stored, "stop requested"
        # v0.9.7 Item 5: thread category for the Thread Pool panel.
        with _thread_category("collections"):
            item, _tier, reason = resolve_item(
                server, section, stored, logger, remap, strict_match,
                scan_cache, scan_lock,
            )
            return coll_idx, item, stored, reason

    if resolve_tasks:
        with concurrent.futures.ThreadPoolExecutor(max_workers=state.MAX_WORKERS) as pool:
            futs = [submit_with_context(pool, _resolve_member, coll_idx, stored)
                    for coll_idx, stored in resolve_tasks]
            for fut in concurrent.futures.as_completed(futs):
                _advance_lib(lib_name)
                try:
                    coll_idx, item, stored, reason = fut.result()
                    if item:
                        resolved_by_coll.setdefault(coll_idx, []).append(item)
                    else:
                        unresolved_by_coll.setdefault(coll_idx, []).append({
                            "title": stored.get("title", "(unknown)"),
                            "type": stored.get("type", ""),
                            "reason": reason or "not found on destination",
                        })
                except Exception as exc:
                    logger.warning(f"[{lib_name}] Collection item resolution error: {exc}")

    for coll_idx, coll_data in enumerate(collections):
        coll_name = coll_data["name"]
        ts = _tz_now()

        if state.get_dashboard():
            state.get_dashboard().set_current_item(
                lib_name, "collection", coll_name, phase="merging",
            )
            state.get_dashboard().inc_collection()

        resolved_items = resolved_by_coll.get(coll_idx, [])
        total_members = len(coll_data.get("items") or [])
        unresolved_members = unresolved_by_coll.get(coll_idx, [])

        if not resolved_items:
            logger.warning(
                "[%s] %s — 0/%d members restored (no members resolved on destination)",
                lib_name, coll_name, total_members,
            )
            if state.get_dashboard():
                state.get_dashboard().record_container_result(
                    kind="collection",
                    name=coll_name,
                    library=lib_name,
                    total=total_members,
                    restored=0,
                    skipped_items=unresolved_members,
                    reason="No members resolved on destination",
                )
                state.get_dashboard().clear_current_item()
            continue

        if coll_name not in existing_collections:
            try:
                _create_collection_chunked(server, coll_name, section, resolved_items)
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
                existing_items = list(existing_coll.items())
                existing_keys: Set[int] = {
                    item.ratingKey for item in existing_items
                }
                snapshot_keys: Set[int] = {
                    i.ratingKey for i in resolved_items
                }
                items_to_add = [
                    i for i in resolved_items if i.ratingKey not in existing_keys
                ]
                items_present = [
                    i for i in resolved_items if i.ratingKey in existing_keys
                ]

                if mode == "replace":
                    # v0.13.x Replace branch: diff + remove members
                    # not in snapshot. Adds happen as in merge. The
                    # collection row stays so metadata survives.
                    items_to_remove = [
                        it for it in existing_items
                        if it.ratingKey not in snapshot_keys
                    ]
                    if items_to_remove:
                        try:
                            for it in items_to_remove:
                                existing_coll.removeItems([it])
                        except Exception as exc:
                            logger.warning(
                                "[%s] Replace: removeItems failed on collection %r: %s. "
                                "Pre-Replace snapshot is the recovery point.",
                                lib_name, coll_name, exc,
                            )
                    if items_to_add:
                        _add_to_collection_chunked(existing_coll, items_to_add)
                    _record_success(lib_name, {
                        "ts": ts, "tier": "N/A", "title": coll_name,
                        "type": "collection",
                        "action": (
                            f"[REPLACED] +{len(items_to_add)} / -{len(items_to_remove)} "
                            f"(snapshot had {len(resolved_items)}, "
                            f"destination had {len(existing_items)})"
                        ),
                    })
                    logger.info(
                        f"Replaced collection '{coll_name}': "
                        f"+{len(items_to_add)} added, -{len(items_to_remove)} removed"
                    )
                else:
                    # Merge (default): additive union only.
                    if items_to_add:
                        _add_to_collection_chunked(existing_coll, items_to_add)
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
                            "action": f"[SKIPPED - already in collection '{coll_name}']",
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

        # Rule 4: per-container result for the Dashboard summary +
        # JobRecord.summary.
        restored_count = len(resolved_items)
        if total_members != restored_count:
            logger.info(
                "[%s] %s — %d/%d members restored (%d not available on destination)",
                lib_name, coll_name, restored_count, total_members,
                total_members - restored_count,
            )
        if state.get_dashboard():
            state.get_dashboard().record_container_result(
                kind="collection",
                name=coll_name,
                library=lib_name,
                total=total_members,
                restored=restored_count,
                skipped_items=unresolved_members,
            )
            state.get_dashboard().clear_current_item()
        _advance_lib(lib_name)


def restore_ratings(
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
    stop_event: Optional[threading.Event] = None,
    mode: str = "merge",
) -> None:
    """
    Imports star ratings. Two modes:

    Merge (default, additive-only):
        - If the target item has NO rating: set the stored rating. Log [RATING SET].
        - If the target item ALREADY HAS a rating: skip it.

    Replace (opt-in, destructive):
        - Always set to the stored rating, overwriting any current value.
        - Used for true point-in-time recovery.

    Args:
        server (PlexServer): Active connection to the target server.
        section: Target library section.
        ratings (List[Dict]): Serialized rating dicts from the export.
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

    if state.get_dashboard() and total:
        state.get_dashboard().add_batch_total("rating", total)
        state.get_dashboard().add_run_total(total)

    def process(stored: Dict) -> None:
        # Stop P4: item-level checkpoint - queued tasks short-circuit
        # once the stop_event is set so a soft Stop drains fast.
        if stop_event is not None and stop_event.is_set():
            return
        with _thread_category("ratings"):
            _process_one_rating(stored)

    def _process_one_rating(stored: Dict) -> None:
        if state.get_dashboard():
            state.get_dashboard().set_current_item(
                lib_name, stored.get("type", "?"), stored.get("title", ""),
                phase="resolving",
            )
            state.get_dashboard().inc_rating()
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
                    "guid": (stored.get("guids") or [""])[0],
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
            if state.get_dashboard():
                state.get_dashboard().set_current_item(
                    lib_name, stored.get("type", "?"), stored.get("title", ""),
                    phase="rating",
                )

            current_rating = getattr(live_item, "userRating", None)

            # Export-side rating value. The gather (snapshot_ratings)
            # writes ``user_rating``; the snapshot serializer (when
            # reconstructing JSON from a snapshot .db) writes ``rating``.
            # Read both keys so this code path works for legacy JSON
            # archives AND for JSON that round-tripped through the .db.
            export_rating = stored.get("rating")
            if export_rating is None:
                export_rating = stored.get("user_rating")

            # v0.13.x Replace mode: drop the "skip if target already
            # has a rating" gate. Always overwrite to the stored value
            # for true point-in-time semantics.
            if mode == "merge" and current_rating is not None:
                _record_success(lib_name, {
                    "ts": ts, "tier": tier,
                    "title": stored["title"],
                    "type": stored.get("type", "?"),
                    "action": f"[SKIPPED - rating {current_rating} already set on target]",
                })
                logger.debug(
                    f"[{lib_name}] Skipped rating for '{stored['title']}' "
                    f"(target has {current_rating}, export has {export_rating})"
                )
                if state._media_logger:
                    state._media_logger.debug(_fmt_media_line(
                        "IMPORT", lib_name, stored.get("type", "?"), stored["title"],
                        user=user, tier=tier,
                        rating=f"target={current_rating} export={export_rating}",
                        result="skipped",
                    ))
            else:
                if export_rating is None:
                    logger.warning(
                        f"[{lib_name}] Rating record for '{stored.get('title','?')}' "
                        "carries no value (neither 'rating' nor 'user_rating'); skipped."
                    )
                    return
                # In Replace mode, a same-value rating is a no-op API
                # call but still worth logging as REPLACED so the audit
                # trail shows the run was destructive.
                already_matches = (
                    mode == "replace"
                    and current_rating is not None
                    and float(current_rating) == float(export_rating)
                )
                try:
                    if not already_matches:
                        _rate_item(base_url, live_item.ratingKey, export_rating, token)
                    action_tag = "[REPLACED]" if mode == "replace" else "[RATING SET]"
                    note = " (no change - already matched)" if already_matches else ""
                    _record_success(lib_name, {
                        "ts": ts, "tier": tier,
                        "title": stored["title"],
                        "type": stored.get("type", "?"),
                        "action": f"{action_tag} {export_rating}{note}",
                    })
                    logger.debug(
                        f"[{lib_name}] Set rating {export_rating} for '{stored['title']}'"
                    )
                    if state._media_logger:
                        state._media_logger.debug(_fmt_media_line(
                            "IMPORT", lib_name, stored.get("type", "?"), stored["title"],
                            user=user, tier=tier,
                            rating=export_rating,
                            result="SET",
                        ))
                except Exception as e:
                    _record_failure(lib_name, {
                        "ts": ts, "tier": tier,
                        "title": stored["title"],
                        "guid": (stored.get("guids") or [""])[0],
                        "filepath": stored.get("filepath", ""),
                        "reason": str(e),
                        "library": lib_name,
                        "type": stored.get("type", "?"),
                    }, "api_error")
                    logger.warning(f"[{lib_name}] Failed to set rating for '{stored['title']}': {e}")
        finally:
            if state.get_dashboard():
                state.get_dashboard().clear_current_item()

    with concurrent.futures.ThreadPoolExecutor(max_workers=state.MAX_WORKERS) as pool:
        futs = {submit_with_context(pool, process, stored): stored for stored in ratings}
        for fut in concurrent.futures.as_completed(futs):
            _advance_lib(lib_name)
            if fut.exception():
                stored = futs[fut]
                logger.warning(
                    f"[{lib_name}] Unexpected error rating '{stored.get('title', '?')}': "
                    f"{fut.exception()}"
                )


def restore_export_file(
    server: PlexServer,
    export_path: str,
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
    include_playlists: bool = True,
    # PR-3 / Phase D - additional include_* flags for the other three
    # data types. Defaults preserve pre-Phase-D behaviour exactly.
    include_watch_history: bool = True,
    include_ratings: bool = True,
    include_collections: bool = True,
    # Reconstructed-snapshot support: the payload from
    # ``snapshot_serializer.build_payload_from_db`` collapses all
    # libraries into one virtual entry with library name
    # "All Libraries (reconstructed from snapshot DB)". That label is
    # not a real section, so the standard ``s.title == lib_name``
    # lookup fails. When this override is set, the importer uses the
    # supplied real section title for the lookup and accumulator key;
    # the payload's own "library" field is ignored. ``run_restore``
    # detects ``snapshot_meta.reconstructed_from_db`` on the payload
    # and submits one task per target section with this set.
    target_section_name_override: Optional[str] = None,
    # v0.13.x restore mode. "merge" = legacy additive behaviour (never
    # destroys data); "replace" = true point-in-time, overwrites view
    # counts / ratings / playlist+collection membership to match the
    # snapshot exactly. Forwarded to every restore_* primitive.
    mode: str = "merge",
    # v0.13.x: sub-strategy for Merge mode's watch-count math. "higher"
    # (default) = destination ends at max(stored, current); "sum" =
    # destination ends at current + stored. Ignored when mode=="replace"
    # since Replace overwrites unconditionally. See restore_watch_history.
    merge_watch_strategy: str = "higher",
) -> None:
    """
    Imports a single .plexexport.json file into the target server.

    Reads the file, finds the matching library on the target server, and
    delegates the four data types to their respective import functions.
    If the export contains per-user data (the "users" key), each user's
    Play Count, playlists, and ratings are imported using that user's own
    token and server connection so the data lands in the correct profile.

    Args:
        server (PlexServer): Active connection to the target server (admin).
        export_path (str): Filesystem path to the .plexexport.json file.
        token (str): Admin Plex auth token.
        base_url (str): Plex server base URL for direct API calls.
        logger (Logger): Shared logger.
        log_dir (str): Where per-library logs will be written.
        remap (tuple, optional): Path root translation.
        strict_match (bool): Whether to require exactly one fuzzy match.
        home_users (list, optional): List of (username, token, server) tuples.
        existing_playlists (dict, optional): Pre-fetched {title: Playlist} map.
        sections_by_name (dict, optional): Pre-built {title: section} lookup.
        preloaded_data (dict, optional): Already-parsed JSON from run_restore.
    """
    if preloaded_data is not None:
        data = preloaded_data
    else:
        with open(export_path, encoding="utf-8") as f:
            data = json.load(f)

    # When the payload was reconstructed from a snapshot .db, the
    # serializer emits a single virtual library entry. The override
    # (set by run_restore once per target section) is what makes the
    # one payload import correctly across every real target library.
    if target_section_name_override:
        lib_name = target_section_name_override
        logger.info(
            f"Importing from {export_path} → reconstructed payload, "
            f"target library: {lib_name}"
        )
    else:
        lib_name = data.get("library", "Unknown")
        logger.info(f"Importing from {export_path} → library: {lib_name}")

    # v0.9.6 Feature 2: tag every Plex API call this library makes with
    # the library name so the Network panel can attribute traffic
    # per-library. Set on the task-local context (the restore_export_file
    # call is itself dispatched via submit_with_context from run_restore,
    # so each library task has its own context - no leakage across
    # libraries).
    _http_lib_var.set(lib_name)
    # v0.9.6 Feature 1: the per-library work the OWNER does (admin's
    # watch history, playlists, etc) is attributed to the owner's
    # email. Per-user blocks inside the loop below override this
    # temporarily and restore it on exit.
    # v0.9.7 Item 4: gate on ``state._current_user_visible`` so the
    # field stays null during standard imports and unscoped direct
    # transfers. Set only when the user explicitly narrowed the run.
    if state.get_dashboard() and state._current_user_visible:
        state.get_dashboard().set_current_user(state._plex_owner_email or None)

    task_id = state._lib_task_ids.get(lib_name)

    def _set_phase(name: str) -> None:
        if state.get_dashboard():
            state.get_dashboard().set_library_phase(lib_name, name)
            state.get_dashboard().push_activity("phase", lib_name, f"→ {name}")
        elif state._live_progress and task_id is not None:
            state._live_progress.update(task_id, fields={"phase": name})

    if state.get_dashboard():
        state.get_dashboard().set_library_status(lib_name, "active")
        state.get_dashboard().push_activity("started", lib_name, "Import started")

    if sections_by_name is not None:
        section = sections_by_name.get(lib_name)
        if section is None:
            # Plex may have hidden a library mid-refresh during the
            # one-shot fetch at run start (or returned a partial list
            # while warming up). One self-healing retry: refetch the
            # section list and update the shared cache in place so any
            # subsequent libraries benefit from the warmer response too.
            logger.warning(
                "Library '%s' missing from initial section cache - "
                "refetching from destination...", lib_name,
            )
            try:
                time.sleep(1)
                refreshed = {s.title: s for s in server.library.sections()}
            except Exception as exc:
                logger.error(
                    "Section refetch failed for library '%s': %s", lib_name, exc,
                )
                refreshed = None
            if refreshed:
                sections_by_name.clear()
                sections_by_name.update(refreshed)
                section = sections_by_name.get(lib_name)
                if section is not None:
                    logger.info(
                        "Library '%s' found after refetch (initial fetch was "
                        "partial - destination was likely warming up).",
                        lib_name,
                    )
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

    # v0.13.0: unified users map. The owner is identified by
    # ``role == 'owner'`` rather than living at a top-level ``items``
    # block. Find them once here; the four restore_* calls below
    # read ``owner_block`` instead of the old ``items`` dict.
    # Mid-transition payloads with no explicit role fall back to the
    # legacy empty-handle convention.
    users_map = data.get("users") or {}
    owner_handle: Optional[str] = None
    owner_block: Dict[str, Any] = {}
    for _h, _ub in users_map.items():
        if isinstance(_ub, dict) and _ub.get("role") == "owner":
            owner_handle = _h
            owner_block = _ub
            break
    if owner_handle is None and "" in users_map and isinstance(users_map[""], dict):
        owner_handle = ""
        owner_block = users_map[""]

    scan_cache: Dict[str, Any] = {}
    scan_lock = threading.Lock()

    # v0.9.5: build the scan cache synchronously on this thread before
    # launching the watch-history resolver pool. Workers only read
    # from the cache during resolution - they never write - so the
    # shared coordination machinery in _build_scan_cache (claim
    # handshake, __building__ marker, spin-wait loop) is only
    # exercised during this single up-front call. By the time the
    # ThreadPoolExecutor inside restore_watch_history fires up, every
    # subsequent _build_scan_cache call sees __ready__ on the first
    # lock-free dict read and returns immediately - no lock acquired,
    # no spin-wait. The shared coordinator stays in resolver.py as a
    # defensive fallback for any future call site that doesn't
    # pre-build, but on this path it's a no-op.
    #
    # Trade-off: each library's watch-history phase now has a hard
    # ``t_build`` lower bound (30-60 s on a big TV library) before
    # the first resolver runs. For libraries with thousands of
    # watched items the saving from removing the spin-wait dominates;
    # for tiny libraries the build dominates. Net positive on real
    # workloads.
    try:
        _build_scan_cache(section, scan_cache, scan_lock, logger)
    except Exception as e:
        logger.warning(
            f"[scan_cache] Build failed for '{section.title}': {e} - "
            f"resolvers will fall back to fuzzy matching."
        )

    def _stopped() -> bool:
        return stop_event is not None and stop_event.is_set()

    if include_watch_history:
        _set_phase("Play Count" if section.type == "artist" else "Watched")
        restore_watch_history(
            server, section, owner_block.get("watch_history", []),
            token, base_url, logger, log_dir, remap, strict_match,
            scan_cache, scan_lock, user=state._plex_owner_name,
            stop_event=stop_event,
            mode=mode,
            merge_watch_strategy=merge_watch_strategy,
        )
    else:
        # PR-6: per-library skip notices are INFO. They're expected
        # normal-operations output that confirms the operator's
        # filter choice on a per-library basis - not noise. The
        # redundant top-level summary was removed in PR-6 instead.
        logger.info(
            "[%s] Watch history import skipped (include_watch_history=False)", lib_name,
        )
    if _stopped():
        logger.info(f"Stop requested - skipping remaining phases for '{lib_name}'.")
        return
    if include_playlists:
        _set_phase("Playlists")
        restore_playlists(
            server, section, owner_block.get("playlists", []),
            logger, log_dir, remap, strict_match,
            scan_cache, scan_lock,
            existing_playlists,
            stop_event=stop_event,
            mode=mode,
        )
    else:
        logger.info(
            "[%s] Playlist import skipped (include_playlists=False)", lib_name,
        )
    if _stopped():
        logger.info(f"Stop requested - skipping remaining phases for '{lib_name}'.")
        return
    if include_collections:
        _set_phase("Collections")
        restore_collections(
            server, section, owner_block.get("collections", []),
            logger, log_dir, remap, strict_match,
            scan_cache, scan_lock,
            stop_event=stop_event,
            mode=mode,
        )
    else:
        logger.info(
            "[%s] Collection import skipped (include_collections=False)", lib_name,
        )
    if _stopped():
        logger.info(f"Stop requested - skipping remaining phases for '{lib_name}'.")
        return
    if include_ratings:
        _set_phase("Ratings")
        restore_ratings(
            server, section, owner_block.get("ratings", []),
            token, base_url, logger, remap, strict_match,
            scan_cache, scan_lock, user=state._plex_owner_name,
            stop_event=stop_event,
            mode=mode,
        )
    else:
        logger.info(
            "[%s] Ratings import skipped (include_ratings=False)", lib_name,
        )

    # Managed users only - the owner block was already restored
    # above via the role lookup. Filter against owner_handle (the
    # JSON key) AND any explicit role marker so a payload with two
    # owner rows (shouldn't happen, but defensive) doesn't accidentally
    # treat the second one as managed.
    users_data = {
        h: ub for h, ub in users_map.items()
        if h != owner_handle
        and isinstance(ub, dict)
        and ub.get("role") != "owner"
    }
    if users_data and not home_users:
        logger.info(
            f"Export contains data for {len(users_data)} home user(s), but no "
            f"home users are available on this server (requires Plex.tv account). "
            f"Per-user data will be skipped."
        )

    user_lookup: Dict[str, Tuple[str, PlexServer]] = {
        uname: (utok, usrv) for uname, utok, usrv in (home_users or [])
    }

    def _restore_user(username: str, user_data: dict) -> str:
        """Import one home user's watch history, playlists, and ratings."""
        if username not in user_lookup:
            logger.info(
                f"Home user '{username}' is in the export but not on this server - "
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
                f"Library '{lib_name}' not accessible to home user '{username}' - skipped"
            )
            return "skipped"

        logger.info(
            f"Importing home user '{username}' - "
            f"library '{lib_name}': "
            f"{len(user_data.get('watch_history', []))} watch history, "
            f"{len(user_data.get('playlists', []))} playlist(s), "
            f"{len(user_data.get('collections', []))} personal collection(s), "
            f"{len(user_data.get('ratings', []))} rating(s)"
        )

        # v0.9.6 Feature 1: tag the header with this user for the
        # duration of their block. Restored to the owner's identifier
        # (or None) on exit so subsequent libraries' owner phases
        # don't show this user.
        # v0.9.7 Item 4: gated by ``_current_user_visible``. When
        # False the header field stays null for the whole run.
        prev_user = state.get_dashboard().current_user if state.get_dashboard() else None
        if state.get_dashboard() and state._current_user_visible:
            state.get_dashboard().set_current_user(username)
        # v0.9.7 Item 5: register this orchestrator thread under
        # "home_user" so the Thread Pool panel shows live counts
        # during the per-user phase. Inner pools spawned by the
        # import_* primitives still register under their own
        # categories (watched / playlists / collections / ratings).
        thread_ctx = _thread_category("home_user")
        thread_ctx.__enter__()
        try:
            if include_watch_history:
                restore_watch_history(
                    user_server, user_section, user_data.get("watch_history", []),
                    user_token, base_url, logger, log_dir, remap, strict_match,
                    scan_cache, scan_lock, user=username,
                    stop_event=stop_event,
                    mode=mode,
                    merge_watch_strategy=merge_watch_strategy,
                )
            if include_playlists:
                restore_playlists(
                    user_server, user_section, user_data.get("playlists", []),
                    logger, log_dir, remap, strict_match,
                    scan_cache, scan_lock,
                    existing_playlists=None,
                    stop_event=stop_event,
                    mode=mode,
                )
            # v0.9.7 Item 9: restore this user's personal collections
            # (Plex Pass feature - collections that live in their
            # profile, not at the library level). Older exports
            # without the field map to an empty list via .get's
            # default, making this a no-op for pre-v0.9.7 snapshots.
            if include_collections:
                restore_collections(
                    user_server, user_section, user_data.get("collections", []),
                    logger, log_dir, remap, strict_match,
                    scan_cache, scan_lock,
                    stop_event=stop_event,
                    mode=mode,
                )
            if include_ratings:
                restore_ratings(
                    user_server, user_section, user_data.get("ratings", []),
                    user_token, base_url, logger, remap, strict_match,
                    scan_cache, scan_lock, user=username,
                    stop_event=stop_event,
                    mode=mode,
                )
        finally:
            # Restore the prior current_user (owner email or None) so
            # the next user's block has a clean starting point. Safe
            # to run even when gating is off - the prior value was
            # also null in that case, so this is a no-op.
            if state.get_dashboard() and state._current_user_visible:
                state.get_dashboard().set_current_user(prev_user)
            # v0.9.7 Item 5: unregister the "home_user" thread tag.
            thread_ctx.__exit__(None, None, None)
        return "imported"

    if users_data:
        imported_users: List[str] = []
        skipped_users: List[str] = []
        n_user_workers = min(4, len(users_data))
        with concurrent.futures.ThreadPoolExecutor(max_workers=n_user_workers) as user_pool:
            futs = {
                submit_with_context(user_pool, _restore_user, uname, udata): uname
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
                f"Home user import complete - {len(imported_users)} imported: "
                f"{sorted(imported_users)}"
            )
        if skipped_users:
            logger.info(
                f"Home user import - {len(skipped_users)} skipped (not on target server): "
                f"{sorted(skipped_users)}. "
                f"Add them to Plex Home and re-run to import their data."
            )


def run_restore(
    server: PlexServer,
    export_files: List[str],
    token: str,
    base_url: str,
    logger: logging.Logger,
    log_dir: str,
    remap: Optional[Tuple[str, str]],
    strict_match: bool,
    include_playlists: bool = True,
    # PR-3 / Phase D - additional include_* flags.
    include_watch_history: bool = True,
    include_ratings: bool = True,
    include_collections: bool = True,
    # v0.13.x: restore mode. "merge" is the default additive behaviour;
    # "replace" is the opt-in point-in-time overwrite. The job worker
    # in server/jobs.py forwards the user's request body value and
    # runs the pre-Replace auto-capture safety belt before calling this
    # function when mode == "replace".
    mode: str = "merge",
    # v0.13.x: Merge-mode watch-count math sub-strategy. Forwarded to
    # restore_export_file → restore_watch_history. Ignored when
    # mode == "replace".
    merge_watch_strategy: str = "higher",
) -> None:
    """
    Runs the full import pipeline for all selected export files.

    Processes multiple export files concurrently (up to 3 at once), then
    writes the troubleshooting and unresolved logs once all are done.

    Stop semantics: pressing [Q] (CLI) or POSTing /api/job/stop (server)
    flips ``stop_event``. The dashboard loop stops dispatching new
    library imports and best-effort cancels queued futures; mid-import
    library work is not interrupted. ThreadPoolExecutor's ``f.cancel()``
    only succeeds on futures that have not yet started running.

    Args:
        server (PlexServer): Active connection to the target server.
        export_files (List[str]): Filesystem paths to .plexexport.json files.
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

    # One-line summary of which data types this run will import. The
    # per-library skip notices are DEBUG; this is the only INFO line
    # confirming the operator's filter choices for the import side.
    _included = [
        n for n, v in (
            ("watch_history", include_watch_history),
            ("ratings",       include_ratings),
            ("playlists",     include_playlists),
            ("collections",   include_collections),
        ) if v
    ]
    logger.info("Import data types: %s", ", ".join(_included) if _included else "(none)")
    # v0.13.x: restore-mode header. Visible at INFO so the runtime.log
    # forensic trail makes it obvious whether a given run was the safe
    # additive default (merge) or the destructive point-in-time variant
    # (replace). For merge, also surface the sub-strategy.
    if mode == "replace":
        logger.info("Restore mode: REPLACE (point-in-time overwrite - destructive).")
    else:
        if merge_watch_strategy == "sum":
            logger.info(
                "Restore mode: merge (additive, watch counts COMBINE current+stored)."
            )
        else:
            logger.info(
                "Restore mode: merge (additive, watch counts keep HIGHER of stored/current)."
            )

    home_users = get_home_users(server, base_url, logger)
    home_user_names: Set[str] = {n for n, _, _ in home_users}
    if home_user_names:
        logger.info(
            f"Target server home users available for import ({len(home_user_names)}): "
            f"{sorted(home_user_names)}"
        )
    else:
        logger.info("No home users available on target server - only owner data will be imported")

    # PR-1 / Phase B (skip-playlists end-to-end): defer the destination
    # playlists prefetch until we know any payload actually carries
    # playlists AND the operator hasn't disabled the playlist phase.
    # Skipping unnecessarily was previously responsible for a wasted
    # ``server.playlists()`` round-trip on every import - visible in
    # the Network panel even when ``skip_playlists=True`` was set at
    # snapshot time and the export files contained no playlists at all.
    all_playlists: Dict[str, Any] = {}

    # One-shot section enumeration for the whole run. Defensive retry:
    # a Plex server warming up from idle, or one mid-refresh on a
    # library, can return an empty (or partial) section list on the
    # first call - which would silently strand every subsequent
    # library lookup as "not found." Retry once after a short delay,
    # then hard-fail with a clear message rather than the misleading
    # per-library error a downstream lookup would emit.
    sections_by_name: Dict[str, Any] = {s.title: s for s in server.library.sections()}
    if not sections_by_name:
        logger.warning(
            "Destination returned no libraries on first fetch - server may "
            "be warming up from idle. Retrying in 2 seconds..."
        )
        time.sleep(2)
        sections_by_name = {s.title: s for s in server.library.sections()}
        if not sections_by_name:
            raise RuntimeError(
                "Destination server returned no libraries after retry. "
                "Verify the server is awake, the token has library access, "
                "and at least one library is published. Re-run the job once "
                "the destination is fully responsive."
            )
        logger.info(
            "Section list refetched - %d libraries now visible: %s",
            len(sections_by_name), sorted(sections_by_name),
        )

    # P1-2: don't hold every export file in memory at once. Peek each
    # file just long enough to extract its library name, item totals,
    # and user set; then let it go out of scope. restore_export_file
    # will re-open and load the file when its turn comes (it supports
    # this path natively via preloaded_data=None). Peak resident set
    # is now ~n_lib_workers files instead of len(export_files) files.
    def _peek_metadata(data: dict) -> Tuple[str, int, Set[str], bool]:
        # v0.13.0: unified users map. Walk every user and accumulate
        # totals; the owner (role='owner') is always counted (we always
        # restore the owner block), managed users only count when the
        # target server actually has them.
        users_map = data.get("users") or {}
        total = 0
        has_playlists = False
        managed_keys: Set[str] = set()
        for handle, udata in users_map.items():
            if not isinstance(udata, dict):
                continue
            role = udata.get("role") or ("owner" if handle == "" else "managed")
            if udata.get("playlists"):
                has_playlists = True
            if role == "owner":
                total += len(udata.get("watch_history", []))
                total += sum(len(pl.get("items", [])) for pl in udata.get("playlists", []))
                total += len(udata.get("playlists", []))
                total += sum(len(c.get("items", [])) for c in udata.get("collections", []))
                total += len(udata.get("collections", []))
                total += len(udata.get("ratings", []))
            else:
                managed_keys.add(handle)
                if handle in home_user_names:
                    total += len(udata.get("watch_history", []))
                    total += sum(len(pl.get("items", [])) for pl in udata.get("playlists", []))
                    total += len(udata.get("playlists", []))
                    total += len(udata.get("ratings", []))
        return data.get("library", ""), total, managed_keys, has_playlists

    # Task = one (export_file, section_override) pair. Non-reconstructed
    # payloads produce one task with override=None (uses the file's own
    # ``library`` field). Reconstructed payloads (from snapshot .db) produce
    # one task per target section so the resolver can find items in
    # whichever real library they live in.
    ImportTask = Tuple[str, Optional[str]]
    lib_names: Dict[ImportTask, str] = {}
    lib_totals: Dict[ImportTask, int] = {}
    export_users: Set[str] = set()
    readable_tasks: List[ImportTask] = []
    any_payload_has_playlists = False
    # Cache the section titles once - hitting the live API again per
    # export file would slow startup linearly.
    _target_section_titles: Optional[List[str]] = None

    for bf in export_files:
        try:
            with open(bf, encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception as e:
            logger.error(f"Could not read export file {bf}: {e}")
            continue
        name, total, users, has_playlists = _peek_metadata(data)
        export_users.update(users)
        if has_playlists:
            any_payload_has_playlists = True

        reconstructed = bool(
            (data.get("snapshot_meta") or {}).get("reconstructed_from_db")
        )
        if reconstructed:
            if _target_section_titles is None:
                try:
                    _target_section_titles = [
                        s.title for s in server.library.sections()
                    ]
                except Exception as e:
                    logger.error(
                        f"Could not enumerate target server libraries: {e}; "
                        "skipping reconstructed payload."
                    )
                    _target_section_titles = []
            if not _target_section_titles:
                logger.error(
                    f"Reconstructed payload {bf}: target server has no "
                    "visible libraries; nothing to import into."
                )
                continue

            # Scope the fan-out to ONLY the source libraries the
            # snapshot was captured from. The reconstructed .db
            # carries that list in ``snapshot_meta.libraries`` (added
            # by the post-Steps-1-3 capture pipeline); we match those
            # names against the destination's section list.
            #
            # Older snapshots that pre-date the meta table fall back
            # to "fan out to every destination section" so a legacy
            # archive still imports (with the same wasted work the
            # pre-fix code did - acceptable for the migration tail).
            source_libs = (data.get("snapshot_meta") or {}).get("libraries") or []
            if isinstance(source_libs, list) and source_libs:
                # Case-sensitive match against destination section
                # titles. The operator's library name is the contract;
                # mismatches mean they renamed it on one side and
                # should fix that explicitly rather than silently
                # cross-match on case folding.
                scoped_titles = [
                    t for t in _target_section_titles if t in set(source_libs)
                ]
                missing = [n for n in source_libs if n not in _target_section_titles]
                if not scoped_titles:
                    logger.error(
                        "Reconstructed payload %s: source libraries %r have no "
                        "matching section on the destination (destination has: "
                        "%r). Skipping this payload. Rename the destination "
                        "library to match, or restore into a different server.",
                        bf, source_libs, _target_section_titles,
                    )
                    continue
                if missing:
                    logger.warning(
                        "Reconstructed payload %s: source libraries %r have no "
                        "destination counterpart and will be skipped. Matched: %r.",
                        bf, missing, scoped_titles,
                    )
                target_titles_for_payload = scoped_titles
                logger.info(
                    "Reconstructed payload %s scoped to source libraries %r "
                    "(destination sections: %r).",
                    bf, source_libs, scoped_titles,
                )
            else:
                # Legacy snapshot without snapshot_meta.libraries. Fan
                # out to every section as before.
                target_titles_for_payload = list(_target_section_titles)
                logger.info(
                    "Reconstructed payload %s has no snapshot_meta.libraries "
                    "(legacy snapshot); fanning out to all %d destination "
                    "sections: %r.",
                    bf, len(target_titles_for_payload), target_titles_for_payload,
                )

            # Split the per-payload total across the targeted sections
            # so the dashboard's per-library ETA isn't N x inflated.
            # Approximate; the resolver will only succeed for items
            # that actually live in each section.
            per_section_total = max(
                1, total // max(1, len(target_titles_for_payload))
            )
            for title in target_titles_for_payload:
                task: ImportTask = (bf, title)
                lib_names[task] = title
                lib_totals[task] = per_section_total
                readable_tasks.append(task)
        else:
            task = (bf, None)
            lib_names[task] = name or bf
            lib_totals[task] = total
            readable_tasks.append(task)
        # `data` falls out of scope here and is collectable.

    # Now gate the destination-side playlist prefetch on both signals.
    if include_playlists and any_payload_has_playlists:
        all_playlists = {pl.title: pl for pl in server.playlists()}
        logger.info(
            f"Fetched {len(all_playlists)} existing playlist(s) from target server"
        )
    elif not include_playlists:
        logger.info(
            "Skipping target-server playlist prefetch - playlist import disabled."
        )
    else:
        logger.info(
            "Skipping target-server playlist prefetch - no playlists in any export payload."
        )

    if not readable_tasks:
        logger.error("No readable export files. Aborting import.")
        return

    # Timing engine: zero-init the rolling tracker. Totals are
    # discovered as each per-library restore phase enumerates its
    # real work (discover-don't-predict) - there is no pre-run
    # estimate.
    _dash = state.get_dashboard()
    if _dash is not None:
        _dash.init_etr_tracker()

    if export_users:
        missing = sorted(export_users - home_user_names)
        logger.info(
            f"Export contains data for {len(export_users)} user(s): "
            f"{sorted(export_users)}"
        )
        if missing:
            logger.info(
                f"These export users are not on the target server and will be skipped: "
                f"{missing}"
            )

    n_lib_workers = min(3, len(readable_tasks))

    # Stop coordination - hoisted before the if/else so both branches share
    # one event. _keyboard_thread in CLI mode reads [Q]; the server-mode
    # stub stashes the event for /api/job/stop to flip.
    stop_event = threading.Event()
    kb = threading.Thread(
        target=_keyboard_thread, args=(log_dir, logger, stop_event), daemon=True
    )
    kb.start()

    def _submit_all(lib_pool):
        fmap: Dict[Any, str] = {}
        for task in readable_tasks:
            bf, section_override = task
            # Skip submissions queued after stop was requested.
            # Already-running futures continue until their
            # restore_export_file's per-phase stop check fires.
            if stop_event.is_set():
                logger.info(f"Stop requested - not submitting '{lib_names[task]}'.")
                continue
            fut = submit_with_context(
                lib_pool,
                restore_export_file,
                server, bf, token, base_url, logger, log_dir, remap, strict_match,
                home_users, all_playlists, sections_by_name, None,  # preloaded_data=None: load on demand
                stop_event,
                include_playlists,
                include_watch_history,
                include_ratings,
                include_collections,
                section_override,
                mode,
                merge_watch_strategy,
            )
            fmap[fut] = lib_names[task]
        return fmap

    if _check_terminal_size():
        # ── Small-terminal fallback: Rich Progress bars ────────────────────────
        state._live_progress = _make_progress()
        for task in readable_tasks:
            lib_name = lib_names[task]
            # add_task is keyed by lib_name; for reconstructed payloads
            # with multiple sections the same key may already exist if
            # another export file already added it. Skip silently in
            # that case - the existing task's total still bounds it.
            if lib_name not in state._lib_task_ids:
                state._lib_task_ids[lib_name] = state._live_progress.add_task(
                    lib_name, total=max(lib_totals[task], 1), completed=0,
                    fields={"phase": "Queued"},
                )
        overall_task = state._live_progress.add_task(
            "Overall", total=len(readable_tasks), completed=0, fields={"phase": ""},
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
        if state.get_dashboard() is None:
            state._dashboard = DashboardState(log_dir=log_dir)
        else:
            state.get_dashboard().log_dir = log_dir
        # Owner + every home user we connected to = total users this run covers.
        state.get_dashboard().set_user_count(1 + len(home_users))
        _added_libs: Set[str] = set()
        for task in readable_tasks:
            lib_name = lib_names[task]
            # Reconstructed payload may add the same library name once
            # per task; the dashboard's add_library is keyed by name
            # and a duplicate add overwrites the total. Skip duplicates.
            if lib_name in _added_libs:
                continue
            state.get_dashboard().add_library(lib_name, total=max(lib_totals[task], 1))
            _added_libs.add(lib_name)

        try:
            with Live(console=console, refresh_per_second=4) as live:
                state._live_instance = live
                with concurrent.futures.ThreadPoolExecutor(max_workers=n_lib_workers) as lib_pool:
                    futs_map = _submit_all(lib_pool)
                    pending = set(futs_map.keys())
                    while pending and not stop_event.is_set():
                        try:
                            live.update(_build_dashboard(state.get_dashboard().to_dashboard_frame(), mode="IMPORT"))
                        except Exception:
                            pass
                        done, pending = concurrent.futures.wait(pending, timeout=0.25)
                        for fut in done:
                            lib_name = futs_map[fut]
                            exc = fut.exception()
                            if exc:
                                logger.error(f"Library import failed: {exc}")
                                state.get_dashboard().finish_library(lib_name, error=True)
                                state.get_dashboard().push_activity("error", lib_name, "Import failed")
                            else:
                                state.get_dashboard().finish_library(lib_name)
                                state.get_dashboard().push_activity("done", lib_name, "Import complete")
                    if stop_event.is_set():
                        for f in pending:
                            f.cancel()
                if not stop_event.is_set():
                    try:
                        live.update(_build_dashboard(state.get_dashboard().to_dashboard_frame(), mode="IMPORT"))
                    except Exception:
                        pass
                    time.sleep(3)
        except Exception as render_err:
            logger.warning(
                f"Dashboard rendering unavailable ({render_err!r}). "
                f"Running without display - see {log_dir}/ for full details."
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
            # v0.13.x: do NOT clear state._dashboard here. The job worker
            # in server/jobs.py runs post-engine finalization
            # (``write_troubleshoot_log`` below already needs the
            # dashboard alive for any cleanup hooks; the worker's
            # ``set_finalizing("finalizing run")`` writes to it too).
            # Worker's finally block nulls it once everything completes.

    write_troubleshoot_log(log_dir)
    write_unresolved_log(log_dir)

    successes = state._lib_successes or {}
    failures = state._lib_failures or {}
    total_success = sum(len(v) for v in successes.values())
    total_fail = sum(len(v) for v in failures.values())

    console.print(f"\n[bold green]Import complete.[/bold green]")
    console.print(f"  Succeeded: {total_success}")
    console.print(f"  Failed:    {total_fail}")

    if total_fail:
        console.print(f"  [yellow]See {log_dir}/ for troubleshooting details.[/yellow]")
    console.print()
