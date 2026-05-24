"""
Rating restore for the Plex-direct engine.

Owns ``restore_ratings`` and the Plex HTTP ``_rate_item`` write helper.
Mirrors the layout of ``services.restorer.watch``: the ``_rate_item``
function lives here, the package facade re-exports it, and the importer
calls go through ``_facade_rate_item`` so existing test monkeypatches
against ``services.restorer._rate_item`` still win.
"""

import concurrent.futures
import logging
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from plexapi.server import PlexServer

import services.state as state
from services.backend_translation import translate_affinity
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
)
from services.resolver import (
    _category_for_failure,
    resolve_item,
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
    from services import tunables
    resp = state._session.put(
        url, params=params, headers={"X-Plex-Token": token},
        timeout=tunables.scrobble_put_timeout(),
    )
    # Surface a 4xx/5xx so a failed rating write is recorded as a
    # failure rather than silently passing.
    resp.raise_for_status()


def _facade_rate_item(
    base_url: str, rating_key: int, rating: float, token: str,
) -> None:
    """
    Dispatch to ``services.restorer._rate_item`` so existing test
    monkeypatches against the public-facade name continue to win at the
    call site even though the implementation lives in this submodule.
    """
    import services.restorer as _facade
    _facade._rate_item(base_url, rating_key, rating, token)


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

            # Export-side rating value. The current snapshot serializer
            # writes a ``rating`` key on each row; the destination here
            # is always Plex (this is the Plex-native restore path).
            # Translate the neutral affinity so a Jellyfin / Emby
            # snapshot's favorite-only row (no numeric rating of its
            # own) lands as a numeric rating instead of being written
            # as 0. A row that already carries a real rating passes
            # through unchanged.
            _src_rating_raw = stored.get("rating")
            try:
                _src_rating = (
                    None if _src_rating_raw is None
                    else float(_src_rating_raw)
                )
            except (TypeError, ValueError):
                _src_rating = None
            _src_fav_raw = stored.get("is_favorite")
            try:
                from services import tunables as _tun
                _fav_as_rating = _tun.favorite_as_rating_value()
            except Exception:
                _fav_as_rating = 10.0
            export_rating = translate_affinity(
                source_rating=_src_rating,
                source_is_favorite=(
                    None if _src_fav_raw is None else bool(_src_fav_raw)
                ),
                dest_backend="plex",
                favorite_as_rating_value=_fav_as_rating,
            ).rating

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
                        "carries no 'rating' value; skipped."
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
                        _facade_rate_item(base_url, live_item.ratingKey, export_rating, token)
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
