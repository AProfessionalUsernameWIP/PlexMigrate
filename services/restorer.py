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

    Plex playlists are strictly single-type - a mixed member list must
    never reach a create/append call, or Plex rejects the whole batch
    with "Can not mix media types when building a playlist". A mixed
    list at the write boundary is an *upstream bug* (a wrong-type
    resolver match, or a snapshot read with mixed members), not a
    normal case, so the caller logs the drop as an error and surfaces
    it on the per-container result.

    Items whose ``.type`` is unrecognised are kept (consistent with
    ``_plex_playlist_type_for_items`` choosing to proceed on unknown
    future Plex types rather than false-positive). ``family`` is ``""``
    only when no item carries a recognisable type - caller leaves the
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


# ── Restoration math (pure helpers - v0.13.x) ────────────────────────────────

def _compute_merge_views_to_add(
    *, stored: int, current: int, strategy: str,
) -> int:
    """
    Merge-mode watch-count math. Returns how many scrobble calls the
    engine should fire against a single item to apply Merge semantics.

    Two strategies:
      * ``"higher"`` (default, legacy) - bring destination up to
        ``max(stored, current)``. Idempotent across re-runs: a second
        run with the same snapshot adds zero (because current already
        equals or exceeds stored). Computed as ``max(0, stored - current)``.
      * ``"sum"`` - add stored on top of current; destination ends at
        ``current + stored``. NOT idempotent: re-running the same
        snapshot doubles the destination count. End user opt-in for
        cases where the snapshot represents real plays on a different
        server that should contribute alongside, not replace.

    Both strategies still satisfy the "Merge never reduces a count"
    contract; they differ only in the upper bound. The Replace branch
    does its own math and does NOT call this helper - Replace is an
    overwrite, not a merge.
    """
    if strategy == "sum":
        return max(0, stored)
    # Default / "higher".
    return max(0, stored - current)


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
    #       same job will double-count. End user opt-in.
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
              ends at current + stored. End user opt-in; NOT idempotent.
        - Resume position: only set if the target item has NO saved progress.

    Replace (opt-in, destructive):
        - View count: set to exactly the stored value. If the destination
          has more plays than the snapshot, ``markUnplayed`` resets to 0
          first, then we scrobble up to the stored count (capped by
          VIEWCOUNT_INCREMENT_CAP).
        - Resume position: set to the stored value unconditionally.
        - Used for true point-in-time recovery. End user-gated by the
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
                _rlog = getattr(state, "_restoration_log", None)
                if _rlog is not None:
                    _rlog.failed(
                        library=lib_name,
                        item_title=stored.get("title", "?"),
                        user=user,
                        metric="view_count",
                        reason=f"resolver_no_match: {reason}" if reason else "resolver_no_match",
                    )
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
            # snapshot (the end user watched something after the
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
                        # perfect, but the end user was warned.
                views_to_add = max(0, stored_view_count - current_view_count)
                set_offset_unconditionally = True
            else:
                # Merge mode. The strategy decision (higher vs sum) is
                # in _compute_merge_views_to_add so the policy is unit-
                # testable in isolation from the HTTP / dashboard /
                # threading machinery wrapped around this call.
                views_to_add = _compute_merge_views_to_add(
                    stored=stored_view_count,
                    current=current_view_count,
                    strategy=merge_watch_strategy,
                )
                set_offset_unconditionally = False

            if views_to_add == 0:
                # Replace mode still needs to write the resume offset
                # even when view counts already match. Merge mode keeps
                # the legacy skip-with-no-work behaviour.
                if mode == "replace" and stored_view_offset != current_view_offset:
                    _rlog = getattr(state, "_restoration_log", None)
                    _t0 = time.monotonic()
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
                        if _rlog is not None:
                            _rlog.restored(
                                library=lib_name,
                                item_title=stored.get("title", "?"),
                                user=user,
                                metric="view_offset",
                                before=current_view_offset,
                                after=stored_view_offset,
                                duration_ms=int((time.monotonic() - _t0) * 1000),
                            )
                    except Exception as exc:
                        logger.warning(
                            "[%s] Replace: failed to set view_offset on %r: %s",
                            lib_name, stored.get("title", "?"), exc,
                        )
                        if _rlog is not None:
                            _rlog.failed(
                                library=lib_name,
                                item_title=stored.get("title", "?"),
                                user=user,
                                metric="view_offset",
                                reason=f"set_resume_position: {exc}",
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
                _rlog = getattr(state, "_restoration_log", None)
                if _rlog is not None:
                    _rlog.noop(
                        library=lib_name,
                        item_title=stored.get("title", "?"),
                        user=user,
                        metric="view_count",
                        before=current_view_count,
                        after=current_view_count,
                        reason="already_matched",
                    )
                return

            # Bound the per-item scrobble burst to VIEWCOUNT_INCREMENT_CAP so
            # a runaway export (e.g. stale viewCount of 99999) cannot hammer
            # Plex with thousands of writes for one track. The log line below
            # reports the *actual* number fired, plus the stored target - the
            # delta tells the user whether a re-run is needed to advance further.
            actual_views = min(views_to_add, state.VIEWCOUNT_INCREMENT_CAP)
            capped = actual_views < views_to_add

            _scrobble_t0 = time.monotonic()
            with scrobble_sem:
                for _ in range(actual_views):
                    _scrobble(base_url, live_item.ratingKey, token)

                # Merge: only set offset when target has no progress.
                # Replace: always overwrite (end user wants point-in-time).
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
            _rlog = getattr(state, "_restoration_log", None)
            if _rlog is not None:
                _rlog.restored(
                    library=lib_name,
                    item_title=stored.get("title", "?"),
                    user=user,
                    metric="view_count",
                    before=current_view_count,
                    after=current_view_count + actual_views,
                    duration_ms=int((time.monotonic() - _scrobble_t0) * 1000),
                )

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
            _rlog = getattr(state, "_restoration_log", None)
            if _rlog is not None:
                _rlog.failed(
                    library=lib_name,
                    item_title=stored.get("title", "?"),
                    user=user,
                    metric="view_count",
                    reason=f"api_error: {e}",
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
    user: str = "Plex Owner",
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

    # Plan[MIXED-MEDIA-PLAYLISTS]-2026-05-16: pre-restore transform.
    # Reads the run-level mixed-media config from state (set by
    # jobs.py before invoking the engine); applies the end user's
    # skip/dominant/split strategy on rows whose source items span
    # multiple Plex playlist-type families. Non-mixed rows pass
    # through unchanged. Legacy snapshots whose items lack a `type`
    # field classify as non-mixed and bypass the transform.
    try:
        from services import mixed_media as _mm
        _mm_cfg = state.get_mixed_media_config()
        if _mm_cfg is not None:
            playlists = _mm.transform_playlists_for_restore(
                playlists,
                config=_mm_cfg,
                per_user_configs=state.get_mixed_media_per_user_configs(),
                logger=logger,
            )
            if not playlists:
                # Everything got filtered out; nothing left to restore.
                return
    except Exception:
        logger.exception(
            "[%s] mixed-media pre-restore transform failed; falling back "
            "to raw payload (existing playlist write semantics).",
            lib_name,
        )

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
                # Rule 5: surface the end user-facing reason at INFO so
                # the run log line is unambiguous and survives without
                # log-level filtering.
                logger.info(
                    "[%s] %s - smart playlist, requires manual recreation on destination "
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
                        reason="Smart playlist - requires manual recreation on destination",
                    )
                _rlog = getattr(state, "_restoration_log", None)
                if _rlog is not None:
                    _rlog.skipped(
                        library=lib_name,
                        item_title=pl_name,
                        user=user,
                        metric="playlist_member",
                        reason="smart_playlist",
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
            # create/append call - Plex rejects the whole batch with
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
                    "[%s] %s - %d off-type item(s) dropped before write "
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
                            f"off-type for {_family} playlist - dropped to "
                            f"avoid Plex media-type mix rejection"
                        ),
                    }
                    for d in _dropped_offtype
                ]

            if not resolved_items:
                logger.warning(
                    "[%s] %s - 0/%d items restored (no members resolved on destination)",
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
                _rlog = getattr(state, "_restoration_log", None)
                if _rlog is not None:
                    _rlog.failed(
                        library=lib_name,
                        item_title=pl_name,
                        user=user,
                        metric="playlist_member",
                        reason=f"no_members_resolved (0/{total_members})",
                    )
                continue

            if pl_name not in existing_playlists:
                _pl_t0 = time.monotonic()
                try:
                    _create_playlist_chunked(server, pl_name, resolved_items)
                    _record_success(lib_name, {
                        "ts": ts, "tier": "N/A", "title": pl_name,
                        "type": "playlist",
                        "action": f"[CREATED] with {len(resolved_items)} items",
                    })
                    logger.info(f"Created playlist '{pl_name}' with {len(resolved_items)} items")
                    _rlog = getattr(state, "_restoration_log", None)
                    if _rlog is not None:
                        _rlog.restored(
                            library=lib_name,
                            item_title=pl_name,
                            user=user,
                            metric="playlist_member",
                            before=0,
                            after=len(resolved_items),
                            duration_ms=int((time.monotonic() - _pl_t0) * 1000),
                        )
                except Exception as e:
                    _record_failure(lib_name, {
                        "ts": ts, "tier": "none", "title": pl_name,
                        "reason": str(e), "library": lib_name, "type": "playlist",
                        "guid": "", "filepath": "",
                    }, "api_error")
                    logger.error(f"Failed to create playlist '{pl_name}': {e}")
                    _rlog = getattr(state, "_restoration_log", None)
                    if _rlog is not None:
                        _rlog.failed(
                            library=lib_name,
                            item_title=pl_name,
                            user=user,
                            metric="playlist_member",
                            reason=f"create_failed: {e}",
                        )

            else:
                existing_pl = existing_playlists[pl_name]
                # Plex enforces a single media type per playlist
                # ("audio" / "video" / "photo"). Trying to addItems
                # across that boundary fails with a generic API error.
                # Catch it up front so the end user sees a clear
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
                    # Dashboard's restore table shows the end user
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
                    _rlog = getattr(state, "_restoration_log", None)
                    if _rlog is not None:
                        _rlog.skipped(
                            library=lib_name,
                            item_title=pl_name,
                            user=user,
                            metric="playlist_member",
                            reason=(
                                f"type_conflict (destination={existing_pl_type!r}, "
                                f"snapshot={expected_pl_type!r})"
                            ),
                        )
                    continue
                _pl_t0 = time.monotonic()
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
                        _rlog = getattr(state, "_restoration_log", None)
                        if _rlog is not None:
                            if items_to_add or items_to_remove:
                                _rlog.restored(
                                    library=lib_name,
                                    item_title=pl_name,
                                    user=user,
                                    metric="playlist_member",
                                    before=len(existing_items),
                                    after=len(existing_items) + len(items_to_add) - len(items_to_remove),
                                    duration_ms=int((time.monotonic() - _pl_t0) * 1000),
                                )
                            else:
                                _rlog.noop(
                                    library=lib_name,
                                    item_title=pl_name,
                                    user=user,
                                    metric="playlist_member",
                                    before=len(existing_items),
                                    after=len(existing_items),
                                    reason="replace_target_already_matched",
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

                        _rlog = getattr(state, "_restoration_log", None)
                        if _rlog is not None:
                            if items_to_add:
                                _rlog.restored(
                                    library=lib_name,
                                    item_title=pl_name,
                                    user=user,
                                    metric="playlist_member",
                                    before=len(existing_items),
                                    after=len(existing_items) + len(items_to_add),
                                    duration_ms=int((time.monotonic() - _pl_t0) * 1000),
                                )
                            else:
                                _rlog.noop(
                                    library=lib_name,
                                    item_title=pl_name,
                                    user=user,
                                    metric="playlist_member",
                                    before=len(existing_items),
                                    after=len(existing_items),
                                    reason="already_present",
                                )

                except Exception as e:
                    _record_failure(lib_name, {
                        "ts": ts, "tier": "none", "title": pl_name,
                        "reason": str(e), "library": lib_name, "type": "playlist",
                        "guid": "", "filepath": "",
                    }, "api_error")
                    logger.error(f"Failed to update playlist '{pl_name}': {e}")
                    _rlog = getattr(state, "_restoration_log", None)
                    if _rlog is not None:
                        _rlog.failed(
                            library=lib_name,
                            item_title=pl_name,
                            user=user,
                            metric="playlist_member",
                            reason=f"update_failed: {e}",
                        )

            # Rule 4: emit the per-container result for the Dashboard
            # summary + JobRecord.summary. Reached on the create-or-
            # merge success path as well as the API-error path so every
            # non-smart playlist with at least one resolved member
            # contributes a row.
            restored_count = len(resolved_items)
            if total_members != restored_count:
                logger.info(
                    "[%s] %s - %d/%d items restored (%d not available on destination)",
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
    user: str = "Plex Owner",
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
                "[%s] %s - 0/%d members restored (no members resolved on destination)",
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
            _rlog = getattr(state, "_restoration_log", None)
            if _rlog is not None:
                _rlog.failed(
                    library=lib_name,
                    item_title=coll_name,
                    user=user,
                    metric="collection_member",
                    reason=f"no_members_resolved (0/{total_members})",
                )
            continue

        if coll_name not in existing_collections:
            _coll_t0 = time.monotonic()
            try:
                _create_collection_chunked(server, coll_name, section, resolved_items)
                _record_success(lib_name, {
                    "ts": ts, "tier": "N/A", "title": coll_name,
                    "type": "collection",
                    "action": f"[CREATED] with {len(resolved_items)} member(s)",
                })
                logger.info(f"Created collection '{coll_name}' with {len(resolved_items)} items")
                _rlog = getattr(state, "_restoration_log", None)
                if _rlog is not None:
                    _rlog.restored(
                        library=lib_name,
                        item_title=coll_name,
                        user=user,
                        metric="collection_member",
                        before=0,
                        after=len(resolved_items),
                        duration_ms=int((time.monotonic() - _coll_t0) * 1000),
                    )
            except Exception as e:
                _record_failure(lib_name, {
                    "ts": ts, "tier": "none", "title": coll_name,
                    "reason": str(e), "library": lib_name, "type": "collection",
                    "guid": "", "filepath": "",
                }, "api_error")
                logger.error(f"Failed to create collection '{coll_name}': {e}")
                _rlog = getattr(state, "_restoration_log", None)
                if _rlog is not None:
                    _rlog.failed(
                        library=lib_name,
                        item_title=coll_name,
                        user=user,
                        metric="collection_member",
                        reason=f"create_failed: {e}",
                    )

        else:
            existing_coll = existing_collections[coll_name]
            _coll_t0 = time.monotonic()
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
                    _rlog = getattr(state, "_restoration_log", None)
                    if _rlog is not None:
                        if items_to_add or items_to_remove:
                            _rlog.restored(
                                library=lib_name,
                                item_title=coll_name,
                                user=user,
                                metric="collection_member",
                                before=len(existing_items),
                                after=len(existing_items) + len(items_to_add) - len(items_to_remove),
                                duration_ms=int((time.monotonic() - _coll_t0) * 1000),
                            )
                        else:
                            _rlog.noop(
                                library=lib_name,
                                item_title=coll_name,
                                user=user,
                                metric="collection_member",
                                before=len(existing_items),
                                after=len(existing_items),
                                reason="replace_target_already_matched",
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

                    _rlog = getattr(state, "_restoration_log", None)
                    if _rlog is not None:
                        if items_to_add:
                            _rlog.restored(
                                library=lib_name,
                                item_title=coll_name,
                                user=user,
                                metric="collection_member",
                                before=len(existing_items),
                                after=len(existing_items) + len(items_to_add),
                                duration_ms=int((time.monotonic() - _coll_t0) * 1000),
                            )
                        else:
                            _rlog.noop(
                                library=lib_name,
                                item_title=coll_name,
                                user=user,
                                metric="collection_member",
                                before=len(existing_items),
                                after=len(existing_items),
                                reason="already_present",
                            )

            except Exception as e:
                _record_failure(lib_name, {
                    "ts": ts, "tier": "none", "title": coll_name,
                    "reason": str(e), "library": lib_name, "type": "collection",
                    "guid": "", "filepath": "",
                }, "api_error")
                logger.error(f"Failed to update collection '{coll_name}': {e}")
                _rlog = getattr(state, "_restoration_log", None)
                if _rlog is not None:
                    _rlog.failed(
                        library=lib_name,
                        item_title=coll_name,
                        user=user,
                        metric="collection_member",
                        reason=f"update_failed: {e}",
                    )

        # Rule 4: per-container result for the Dashboard summary +
        # JobRecord.summary.
        restored_count = len(resolved_items)
        if total_members != restored_count:
            logger.info(
                "[%s] %s - %d/%d members restored (%d not available on destination)",
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
                _rlog = getattr(state, "_restoration_log", None)
                if _rlog is not None:
                    _rlog.failed(
                        library=lib_name,
                        item_title=stored.get("title", "?"),
                        user=user,
                        metric="rating",
                        reason=f"resolver_no_match: {reason}" if reason else "resolver_no_match",
                    )
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
                _rlog = getattr(state, "_restoration_log", None)
                if _rlog is not None:
                    _rlog.noop(
                        library=lib_name,
                        item_title=stored.get("title", "?"),
                        user=user,
                        metric="rating",
                        before=current_rating,
                        after=current_rating,
                        reason="merge_target_already_rated",
                    )
            else:
                if export_rating is None:
                    logger.warning(
                        f"[{lib_name}] Rating record for '{stored.get('title','?')}' "
                        "carries no value (neither 'rating' nor 'user_rating'); skipped."
                    )
                    _rlog = getattr(state, "_restoration_log", None)
                    if _rlog is not None:
                        _rlog.skipped(
                            library=lib_name,
                            item_title=stored.get("title", "?"),
                            user=user,
                            metric="rating",
                            reason="export_record_missing_value",
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
                _rate_t0 = time.monotonic()
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
                    _rlog = getattr(state, "_restoration_log", None)
                    if _rlog is not None:
                        if already_matches:
                            # Replace mode: the destination already had
                            # the exact stored value, so no API call
                            # fired. From the end user's perspective
                            # nothing changed - that's a NOOP, not a
                            # RESTORED, regardless of action_tag.
                            _rlog.noop(
                                library=lib_name,
                                item_title=stored.get("title", "?"),
                                user=user,
                                metric="rating",
                                before=current_rating,
                                after=current_rating,
                                reason="replace_target_already_matched",
                            )
                        else:
                            _rlog.restored(
                                library=lib_name,
                                item_title=stored.get("title", "?"),
                                user=user,
                                metric="rating",
                                before=current_rating,
                                after=export_rating,
                                duration_ms=int((time.monotonic() - _rate_t0) * 1000),
                            )
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
                    _rlog = getattr(state, "_restoration_log", None)
                    if _rlog is not None:
                        _rlog.failed(
                            library=lib_name,
                            item_title=stored.get("title", "?"),
                            user=user,
                            metric="rating",
                            reason=f"api_error: {e}",
                        )
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
    # Phase C (admin-management follow-up, 2026-05-15): per-library
    # metric map. When provided AND this library has an entry, the
    # entry's flags override the include_* booleans for this
    # library only. Keys are library names. Same pattern as
    # ``services.snapshotter.snapshot_library``.
    library_metrics: Optional[Dict[str, Dict[str, bool]]] = None,
    # v0.15: per-library section selector for reconstructed payloads.
    # A reconstructed payload (from ``snapshot_serializer.build_payload_from_db``)
    # carries a top-level ``libraries`` array - one entry per library
    # section the snapshot captured, each shaped like a normal flat
    # per-library export. When ``library_section_id`` is supplied,
    # ``restore_export_file`` finds the matching entry by
    # ``library_section_id`` and uses it as the per-library payload for
    # the rest of the function. ``run_restore`` emits one task per
    # library entry with its real section_id and resolved target name.
    #
    # Legacy single-library files (``library`` + ``users`` at the top
    # level, no ``libraries`` array) ignore this argument and use the
    # flat shape directly.
    library_section_id: Optional[int] = None,
    # Optional target-name override. When supplied, takes precedence
    # over the picked entry's ``library`` field for destination
    # routing - the end user may have renamed the library on the
    # destination. ``run_restore`` resolves this against the live
    # destination section list before submitting the task.
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
    # v0.14 - per-job user filter. None = import every user the
    # payload carries that also exists on the destination (historical
    # default). When supplied (set of Plex identifiers - owner email
    # + managed usernames), the importer drops payload users whose
    # handle isn't in the set BEFORE running their per-user restore.
    user_filter: Optional[List[str]] = None,
    # USER-MGMT-IDENTITY-AUDIT R-1: server identity for the
    # cross-server user resolution chain. When BOTH are provided, the
    # per-user fan-out below routes each source user via
    # ``services.user_resolution.resolve_destination_user`` (the
    # 5-step chain: per-job override -> identity_map -> backend_user_id
    # direct -> case-insensitive name -> single-admin owner). When
    # either is None or empty, the per-user fan-out preserves the
    # legacy direct-name-match behaviour. Defaults preserve every
    # existing caller.
    source_server_id: Optional[str] = None,
    dest_server_id: Optional[str] = None,
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

    # USER-MGMT-IDENTITY-AUDIT R-1: when the caller didn't supply
    # source_server_id explicitly (the common case for snapshot-file
    # restores; jobs.py usually only knows the destination), peek at
    # the payload's snapshot_meta to recover the source. Best-effort:
    # legacy payloads without snapshot_meta leave source_server_id at
    # None and the resolver chain falls through to backend_user_id /
    # username matching (legacy behaviour).
    if not source_server_id:
        meta = data.get("snapshot_meta") or data.get("meta") or {}
        if isinstance(meta, dict):
            inferred = (meta.get("server_id") or meta.get("source_server_id") or "").strip()
            if inferred:
                source_server_id = inferred

    # v0.15: if the payload is a reconstructed multi-library wrapper
    # (``libraries`` array at the top level), select the entry matching
    # the supplied ``library_section_id`` and treat THAT as ``data`` for
    # the rest of the function. The entry's shape is identical to a
    # legacy flat per-library export, so every downstream read
    # (``data["users"]`` etc.) continues to work without changes.
    wrapper_libraries = data.get("libraries")
    if isinstance(wrapper_libraries, list) and library_section_id is not None:
        picked: Optional[Dict[str, Any]] = None
        for entry in wrapper_libraries:
            if not isinstance(entry, dict):
                continue
            try:
                if int(entry.get("library_section_id") or 0) == int(library_section_id):
                    picked = entry
                    break
            except (TypeError, ValueError):
                continue
        if picked is None:
            logger.error(
                "restore_export_file: wrapper payload %s has no entry with "
                "library_section_id=%s; aborting this task.",
                export_path, library_section_id,
            )
            return
        data = picked

    if target_section_name_override:
        lib_name = target_section_name_override
        logger.info(
            f"Importing from {export_path} → reconstructed payload "
            f"(section_id={library_section_id}), target library: {lib_name}"
        )
    else:
        lib_name = data.get("library", "Unknown")
        logger.info(f"Importing from {export_path} → library: {lib_name}")

    # Phase C (admin-management follow-up, 2026-05-15): if a per-library
    # metric map was passed in AND this library has an entry, override
    # the include_* booleans for this library only. Every downstream
    # restore_* call inside this function reads from the local
    # include_* names; only this rebind point changes.
    if library_metrics and lib_name in library_metrics:
        _lm_row = library_metrics[lib_name]
        if isinstance(_lm_row, dict):
            include_watch_history = bool(_lm_row.get("watch_history", include_watch_history))
            include_ratings = bool(_lm_row.get("ratings", include_ratings))
            include_playlists = bool(_lm_row.get("playlists", include_playlists))
            include_collections = bool(_lm_row.get("collections", include_collections))
            logger.info(
                "Per-library metric override (restore) for %r: "
                "watch_history=%s ratings=%s playlists=%s collections=%s",
                lib_name, include_watch_history, include_ratings, include_playlists, include_collections,
            )

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
        # normal-operations output that confirms the end user's
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
    # v0.14 - per-job user filter. When the end user picked a subset
    # of users on the Restore form, drop everyone else here BEFORE the
    # per-user fan-out. The owner row was already handled by the role
    # lookup above; the matching filter for owner is the email
    # check in run_restore (managed_filter setup) - at this point the
    # filter list only matters for managed users.
    if user_filter is not None:
        filter_set = {str(s).strip() for s in user_filter if str(s).strip()}
        before = len(users_data)
        users_data = {h: ub for h, ub in users_data.items() if h in filter_set}
        if before != len(users_data):
            logger.info(
                "[%s] user_filter applied: %d managed user(s) in payload → %d "
                "selected for restore",
                lib_name, before, len(users_data),
            )
    if users_data and not home_users:
        logger.info(
            f"Export contains data for {len(users_data)} home user(s), but no "
            f"home users are available on this server (requires Plex.tv account). "
            f"Per-user data will be skipped."
        )

    user_lookup: Dict[str, Tuple[str, PlexServer]] = {
        uname: (utok, usrv) for uname, utok, usrv in (home_users or [])
    }
    # USER-MGMT-IDENTITY-AUDIT R-1: build a UserSpec-like view of the
    # destination's available home users so resolve_destination_user
    # can apply the 5-step priority chain (per-job override ->
    # identity_map -> backend_user_id direct -> case-insensitive name
    # -> single-admin owner). When source_server_id + dest_server_id
    # are BOTH provided, the resolver runs; otherwise this stays in
    # legacy direct-name-match mode.
    class _UserView:
        """Adapter shim: resolve_destination_user reads ``.username``,
        ``.role``, ``.backend_user_id``, and ``.service_type``. The
        Plex home_users list only carries usernames, so the latter
        three default to safe values that let steps 0/1/4 work while
        step 2 (backend_user_id direct match) naturally short-circuits
        because Plex home users don't expose backend_user_id here."""
        __slots__ = ("username", "role", "backend_user_id", "service_type")
        def __init__(self, username: str):
            self.username = username
            self.role = "managed"
            self.backend_user_id = ""
            self.service_type = "plex"
    _resolver_enabled = bool(source_server_id and dest_server_id)
    _dest_by_username_lc: Dict[str, _UserView] = (
        {n.lower(): _UserView(n) for n in user_lookup}
        if _resolver_enabled else {}
    )

    def _restore_user(username: str, user_data: dict) -> str:
        """Import one home user's watch history, playlists, and ratings."""
        # Identity resolution: when the 5-step resolver is enabled,
        # route the source username through it and use the matched
        # destination handle to look up the (token, server) tuple.
        # Legacy mode (resolver disabled) is identical to the prior
        # ``if username not in user_lookup`` skip.
        resolved_dest = username
        if _resolver_enabled:
            try:
                from services.user_resolution import resolve_destination_user
                match = resolve_destination_user(
                    source_username=username,
                    source_role="managed",
                    dest_by_username=_dest_by_username_lc,
                    dest_admins=[],
                    source_server_id=source_server_id or "",
                    dest_server_id=dest_server_id or "",
                    logger=logger,
                )
            except Exception as exc:
                logger.debug(
                    "user_resolution call failed for %r; falling back to "
                    "direct-name match. cause: %s", username, exc,
                )
                match = _dest_by_username_lc.get(username.lower())
            if match is None:
                from services.user_display import display_for_logging
                logger.info(
                    f"Home user "
                    f"'{display_for_logging(source_server_id, username)}' "
                    f"did not resolve to any user on the destination "
                    f"(no identity_map row, no name match) - skipped."
                )
                return "skipped"
            resolved_dest = match.username
        if resolved_dest not in user_lookup:
            from services.user_display import display_for_logging
            logger.info(
                f"Home user "
                f"'{display_for_logging(source_server_id, username)}' is in "
                f"the export but not on this server - skipped. Add them to "
                f"Plex Home and re-run to import their data."
            )
            return "skipped"

        user_token, user_server = user_lookup[resolved_dest]

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
                    user=username,
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
                    user=username,
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
    # v0.14 - per-job user filter (intersection of snapshot users and
    # destination users). None = restore every user from the payload
    # that also exists on the destination (historical default). When
    # supplied, only Plex identifiers (owner email + managed
    # usernames) in this list survive. Read at the per-user iteration
    # boundary in restore_library; users not in the list are skipped
    # silently with an info log.
    user_filter: Optional[List[str]] = None,
    # v0.13.x: library-level concurrency cap. Was a hardcoded
    # ``min(3, len)``; now end user-tunable via settings. The final
    # pool size is ``min(library_workers, len(libraries))`` so the
    # tunable is a ceiling, never a floor. Lower when Plex rate-limits
    # multi-library bursts during a restore.
    library_workers: int = 3,
    # Phase C (admin-management follow-up, 2026-05-15): per-library
    # metric map. Forwarded to each ``restore_export_file`` call so
    # the restore engine applies the end user's per-library choice.
    library_metrics: Optional[Dict[str, Dict[str, bool]]] = None,
    # USER-MGMT-IDENTITY-AUDIT R-1 production wire-up. Optional
    # source / destination server ids forwarded down to
    # restore_export_file so the per-user fan-out's identity_map
    # resolver actually fires in production. Defaults to None preserve
    # backward compat: when both are None the resolver short-circuits
    # to the legacy case-insensitive username match (identical to
    # pre-R-1 behaviour). ``source_server_id`` defaults to the value
    # encoded in the snapshot's snapshot_meta when not supplied at
    # this layer (restore_export_file picks it up automatically).
    source_server_id: Optional[str] = None,
    dest_server_id: Optional[str] = None,
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

    # Phase 2: per-run restoration log. Open before any metric handler
    # runs so emissions go to a single file across all libraries. The
    # null-writer shim covers the "run logging disabled" path so the
    # rest of the engine can call writer.restored() etc. unconditionally.
    # The finally-block at the bottom closes it with a summary.
    from services.restoration_log import open_restoration_log
    # Forward dest_server_id so the writer can apply the
    # log_use_display_name substitution (USER-MGMT-IDENTITY-AUDIT
    # cosmetic toggle, off by default). Legacy callers that didn't
    # plumb the kwarg through still see raw usernames in logs.
    state._restoration_log = open_restoration_log(
        log_dir, logger=logger, dest_server_id=dest_server_id or None,
    )

    # One-line summary of which data types this run will import. The
    # per-library skip notices are DEBUG; this is the only INFO line
    # confirming the end user's filter choices for the import side.
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

    # 2026-05-17 (operator request): pre-filter the home-user auth
    # burst so we only authenticate users that the operator actually
    # selected for this restore. Pre-fix every Plex Home user got an
    # auth round-trip on every restore — wasteful + confusing in the
    # activity feed when only one user is being restored. The
    # downstream per-user gate still runs as a belt-and-braces check.
    home_users = get_home_users(
        server, base_url, logger,
        user_filter=user_filter,
    )
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
    # playlists AND the end user hasn't disabled the playlist phase.
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
    def _peek_one_library(udata_map: Dict[str, Any]) -> Tuple[int, Set[str], bool]:
        # v0.13.0: unified users map. Walk every user and accumulate
        # totals; the owner (role='owner') is always counted (we always
        # restore the owner block), managed users only count when the
        # target server actually has them.
        total = 0
        has_playlists = False
        managed_keys: Set[str] = set()
        for handle, udata in udata_map.items():
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
        return total, managed_keys, has_playlists

    # v0.15: Task = (export_file, library_section_id, lib_name).
    #   * Legacy / engine flat payload (one library per file):
    #       section_id=None, lib_name=data["library"]
    #   * Reconstructed wrapper payload (v0.15 multi-library):
    #       one task per entry in data["libraries"], with the entry's
    #       library_section_id and resolved destination lib_name.
    # The fan-out + divide-by-N hack the pre-v15 code used for
    # reconstructed payloads is gone - every task carries the EXACT
    # per-library total computed from that library's own users block.
    ImportTask = Tuple[str, Optional[int], str]
    lib_names: Dict[ImportTask, str] = {}
    lib_totals: Dict[ImportTask, int] = {}
    export_users: Set[str] = set()
    readable_tasks: List[ImportTask] = []
    any_payload_has_playlists = False
    # Cache the destination section titles once - hitting the live API
    # again per export file would slow startup linearly.
    _target_section_titles: Optional[Set[str]] = None

    def _dest_section_titles() -> Set[str]:
        nonlocal _target_section_titles
        if _target_section_titles is None:
            try:
                _target_section_titles = {
                    s.title for s in server.library.sections()
                }
            except Exception as exc:
                logger.error(
                    f"Could not enumerate target server libraries: {exc}; "
                    "reconstructed payloads will be skipped."
                )
                _target_section_titles = set()
        return _target_section_titles

    for bf in export_files:
        try:
            with open(bf, encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception as e:
            logger.error(f"Could not read export file {bf}: {e}")
            continue

        wrapper_libraries = data.get("libraries")
        is_wrapper = isinstance(wrapper_libraries, list) and bool(wrapper_libraries)

        if is_wrapper:
            # v0.15 reconstructed payload. Emit one task per entry,
            # each with its real per-library total. The destination
            # must have a section with the same title; case-sensitive
            # (renaming on either side is the end user's contract).
            dest_titles = _dest_section_titles()
            if not dest_titles:
                logger.error(
                    f"Reconstructed payload {bf}: target server has no "
                    "visible libraries; nothing to import into."
                )
                continue

            for entry in wrapper_libraries:
                if not isinstance(entry, dict):
                    continue
                entry_name = str(entry.get("library") or "")
                try:
                    sec_id = int(entry.get("library_section_id") or 0)
                except (TypeError, ValueError):
                    sec_id = 0
                if not entry_name or sec_id <= 0:
                    logger.error(
                        "Reconstructed payload %s contains a malformed entry "
                        "(library=%r, library_section_id=%r); skipping.",
                        bf, entry.get("library"), entry.get("library_section_id"),
                    )
                    continue
                if entry_name not in dest_titles:
                    logger.warning(
                        "Reconstructed payload %s: source library %r has no "
                        "destination counterpart (destination has: %r). "
                        "Skipping this library; rename it on the destination "
                        "to match, or restore into a different server.",
                        bf, entry_name, sorted(dest_titles),
                    )
                    continue

                entry_total, entry_users, entry_has_pl = _peek_one_library(
                    entry.get("users") or {}
                )
                export_users.update(entry_users)
                if entry_has_pl:
                    any_payload_has_playlists = True

                task: ImportTask = (bf, sec_id, entry_name)
                lib_names[task] = entry_name
                lib_totals[task] = entry_total
                readable_tasks.append(task)
        else:
            # Legacy / engine flat per-library file. One library per file.
            lib_name = str(data.get("library") or "")
            entry_total, entry_users, entry_has_pl = _peek_one_library(
                data.get("users") or {}
            )
            export_users.update(entry_users)
            if entry_has_pl:
                any_payload_has_playlists = True

            task = (bf, None, lib_name or bf)
            lib_names[task] = lib_name or bf
            lib_totals[task] = entry_total
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

    # v0.13.x: library-level concurrency is end user-tunable via the
    # ``restore_library_workers`` setting. Clamped at 1 (a 0/negative
    # value would silently disable the pool) and capped at the actual
    # library count so a high setting doesn't spawn idle workers.
    n_lib_workers = max(1, min(int(library_workers), len(readable_tasks)))

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
            bf, section_id, target_name = task
            # For wrapper-shape (v0.15 reconstructed) payloads, both
            # library_section_id (picks the entry) and the target name
            # (the destination section title) are required. For legacy
            # flat per-library files, both stay None and the function
            # uses data["library"] directly.
            override_name = target_name if section_id is not None else None
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
                # Phase C: per-library metric map. restore_export_file
                # overrides the include_* booleans for any library
                # listed in this map.
                library_metrics,
                section_id,        # library_section_id
                override_name,     # target_section_name_override
                mode,
                merge_watch_strategy,
                user_filter,
                source_server_id,  # USER-MGMT-IDENTITY-AUDIT R-1 (None-safe)
                dest_server_id,    # USER-MGMT-IDENTITY-AUDIT R-1 (None-safe)
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

    # Phase 2: close the per-run restoration log with its summary
    # block. Guarded because the engine has many early-return paths
    # above; we want the summary written regardless. Phase 4: stash
    # the affected-user list on state before close so the jobs.py
    # finalisation can populate run_history.users_affected_list.
    try:
        rlog = getattr(state, "_restoration_log", None)
        if rlog is not None:
            try:
                state._restoration_log_affected_users = list(
                    rlog.affected_user_list() or []
                )
            except Exception:
                state._restoration_log_affected_users = []
            rlog.close_with_summary()
    finally:
        state._restoration_log = None

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
