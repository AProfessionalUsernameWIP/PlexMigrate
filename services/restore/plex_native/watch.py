"""
Watch-history restore for the Plex-direct engine.
"""

import concurrent.futures
import logging
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from plexapi.server import PlexServer

import services.state as state
from services.dashboard import (
    _advance_lib,
    _thread_category,
    submit_with_context,
)
from services.logging_ops import (
    _fmt_media_line,
    _record_failure,
    _record_success,
    _tz_now,
    write_library_logs,
)
from services.resolver import (
    _category_for_failure,
    resolve_item,
)


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
    from services import tunables
    resp = state._session.get(
        url, params=params, headers={"X-Plex-Token": token},
        timeout=tunables.scrobble_get_timeout(),
    )
    # A 4xx/5xx here means the scrobble did NOT land. Surface it so the
    # per-item restore loop records the item as failed rather than OK.
    resp.raise_for_status()


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
    from services import tunables
    resp = state._session.get(
        url, params=params, headers={"X-Plex-Token": token},
        timeout=tunables.scrobble_get_timeout(),
    )
    # Surface a 4xx/5xx so a failed resume-position write is recorded
    # as a failure rather than silently passing.
    resp.raise_for_status()


def _facade_scrobble(base_url: str, rating_key: int, token: str) -> None:
    """
    Dispatch to ``services.restore.plex_native._scrobble`` so existing test
    monkeypatches against the public-facade name continue to win at the
    call site even though the implementation lives in this submodule.
    The package's ``__init__`` re-exports the real ``_scrobble`` so the
    fallback path is identical to a direct call when no patch is set.
    """
    import services.restore.plex_native as _facade
    _facade._scrobble(base_url, rating_key, token)


def _facade_set_resume_position(
    base_url: str, rating_key: int, offset_ms: int, token: str,
) -> None:
    """Facade-aware counterpart to ``_facade_scrobble`` for resume offsets."""
    import services.restore.plex_native as _facade
    _facade._set_resume_position(base_url, rating_key, offset_ms, token)


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
    # Register every worker thread under the right category so the
    # dashboard's Thread Pool panel shows live counts during imports.
    # Music sections use ``play_count``; everything else uses ``watched``.
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
        # panel. Phase advances from "resolving" ? "scrobbling" so the
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

            # Replace mode: set viewCount to exactly the stored value.
            # If the destination is currently HIGHER than the snapshot,
            # reset to 0 first via markUnplayed so the scrobble loop
            # below can bring it back up to the stored value.
            if mode == "replace":
                if current_view_count > stored_view_count:
                    try:
                        live_item.markUnplayed()
                        current_view_count = 0
                    except Exception as exc:
                        # Replace promises an exact point-in-time mirror.
                        # The destination viewCount is HIGHER than the
                        # snapshot and we could not reset it, so the
                        # scrobble path below cannot reach the stored
                        # target - it would leave the count too high and
                        # then record that wrong end state as a success.
                        # Record a failure and skip this item instead.
                        logger.warning(
                            "[%s] Replace: could not reset viewCount on %r "
                            "(destination=%d > stored=%d): %s. Recording as "
                            "failed - exact watch count not reached.",
                            lib_name, stored.get("title", "?"),
                            current_view_count, stored_view_count, exc,
                        )
                        _record_failure(lib_name, {
                            "ts": ts, "tier": tier,
                            "title": stored["title"],
                            "guid": (stored.get("guids") or [""])[0],
                            "filepath": stored.get("filepath", ""),
                            "reason": f"markUnplayed_failed: {exc}",
                            "library": lib_name,
                            "type": stored.get("type", "?"),
                        }, "api_error")
                        _rlog = getattr(state, "_restoration_log", None)
                        if _rlog is not None:
                            _rlog.failed(
                                library=lib_name,
                                item_title=stored.get("title", "?"),
                                user=user,
                                metric="view_count",
                                reason=f"markUnplayed_failed: {exc}",
                            )
                        return
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
                            _facade_set_resume_position(
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
                    _facade_scrobble(base_url, live_item.ratingKey, token)

                # Merge: only set offset when target has no progress.
                # Replace: always overwrite (end user wants point-in-time).
                if set_offset_unconditionally:
                    if stored_view_offset != current_view_offset:
                        _facade_set_resume_position(base_url, live_item.ratingKey, stored_view_offset, token)
                elif current_view_offset == 0 and stored_view_offset > 0:
                    _facade_set_resume_position(base_url, live_item.ratingKey, stored_view_offset, token)

            if capped:
                action = (
                    f"added {actual_views} view(s) - capped at {state.VIEWCOUNT_INCREMENT_CAP} "
                    f"(stored {stored_view_count}, target was {current_view_count}; "
                    f"re-run import to advance further)"
                )
                plays_str = f"+{actual_views}/{views_to_add} (capped)"
            else:
                action = f"added {actual_views} view(s) (total now ?{stored_view_count})"
                plays_str = f"+{actual_views} ? ?{stored_view_count}"

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
