"""
Playlist restore for the Plex-direct engine.

Owns ``restore_playlists`` plus the playlist-type classification helpers and
the chunked create/add wrappers that work around Plex's URL-length cap. The
helpers are still importable from the package facade (``services.restore.plex_native``)
because the test suite and ``services.restore.mixed_media`` reference them by that
public path.
"""

import concurrent.futures
import logging
import threading
import time
from typing import Any, Dict, List, Optional, Set, Tuple

from plexapi.playlist import Playlist
from plexapi.server import PlexServer

import services.state as state
from services.dashboard import (
    _advance_lib,
    _thread_category,
    submit_with_context,
)
from services.logging_ops import (
    _record_failure,
    _record_success,
    _tz_now,
)
from services.resolver import resolve_item


# ── Chunked playlist helpers (v0.11.1) ───────────────────────────────────────
#
# Plex's embedded HTTP server caps request URL length around 8 KB. The
# ``Playlist.create(items=…)`` path in python-plexapi joins every item's
# ratingKey into the ``uri=`` query parameter - for a static playlist with
# thousands of members, the URL blows past the cap and Plex returns 400
# ``bad_request`` without creating anything. ``addItems`` has the same shape
# and the same failure mode at scale.
#
# Fix: do the create with a small first batch, then call ``addItems`` in
# further small batches until the entire member list lands. Each individual
# call stays well under the URL limit.
#
# 100 items per batch is conservative. A typical Plex ratingKey is 5–6
# digits + ``%2C`` separator (4 chars URL-encoded), so 100 items is
# ≈900 bytes in the ``uri=`` parameter - leaves ~7 KB of headroom for the
# rest of the URL (base, query keys, machineIdentifier, etc.).
_PLAYLIST_CHUNK_SIZE = 100


# Plex's playlistType enum is one of "audio" / "video" / "photo".
# Map each leaf item type from our snapshot serialiser to the Plex
# playlist family it'd belong in. Used by the append-path conflict
# check below.
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

    # Skip-playlists fast path: if there are no playlists in the
    # payload AND the caller didn't hand us a pre-fetched destination
    # map, return without ever calling ``server.playlists()``. A
    # payload with zero playlists must not trigger a full destination
    # prefetch as a side-effect of the lazy default - that wastes an
    # API round-trip on every library with no playlists to import.
    if not playlists and existing_playlists is None:
        return

    # Pre-restore transform.
    # Reads the run-level mixed-media config from state (set by
    # jobs.py before invoking the engine); applies the end user's
    # skip/dominant/split strategy on rows whose source items span
    # multiple Plex playlist-type families. Non-mixed rows pass
    # through unchanged. Legacy snapshots whose items lack a `type`
    # field classify as non-mixed and bypass the transform.
    try:
        from services.restore import mixed_media as _mm
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
                        # Remove one member per call (plexapi's
                        # removeItems may not chunk; one-per-item keeps
                        # URL length bounded), counting only the calls
                        # that actually succeed. A single try/except
                        # around the whole loop would abort on the
                        # first failure yet still let the code below
                        # report every intended removal as done.
                        removed_ok = 0
                        for it in items_to_remove:
                            try:
                                existing_pl.removeItems([it])
                                removed_ok += 1
                            except Exception as exc:
                                logger.warning(
                                    "[%s] Replace: removeItems failed on %r "
                                    "(member ratingKey=%s): %s. Continuing; "
                                    "pre-Replace snapshot is the recovery point.",
                                    lib_name, pl_name,
                                    getattr(it, "ratingKey", "?"), exc,
                                )
                        if items_to_add:
                            _add_to_playlist_chunked(existing_pl, items_to_add)
                        _removed_str = (
                            str(removed_ok)
                            if removed_ok == len(items_to_remove)
                            else f"{removed_ok} of {len(items_to_remove)} (partial)"
                        )
                        _record_success(lib_name, {
                            "ts": ts, "tier": "N/A", "title": pl_name,
                            "type": "playlist",
                            "action": (
                                f"[REPLACED] +{len(items_to_add)} / -{_removed_str} "
                                f"(snapshot had {len(resolved_items)}, "
                                f"destination had {len(existing_items)})"
                            ),
                        })
                        logger.info(
                            f"Replaced playlist '{pl_name}': "
                            f"+{len(items_to_add)} added, -{_removed_str} removed"
                        )
                        _rlog = getattr(state, "_restoration_log", None)
                        if _rlog is not None:
                            if items_to_add or removed_ok:
                                _rlog.restored(
                                    library=lib_name,
                                    item_title=pl_name,
                                    user=user,
                                    metric="playlist_member",
                                    before=len(existing_items),
                                    after=len(existing_items) + len(items_to_add) - removed_ok,
                                    duration_ms=int((time.monotonic() - _pl_t0) * 1000),
                                )
                            elif items_to_remove:
                                # Stale members existed but every
                                # removeItems call failed and nothing
                                # was added: a failure, not a no-op.
                                _rlog.failed(
                                    library=lib_name,
                                    item_title=pl_name,
                                    user=user,
                                    metric="playlist_member",
                                    reason=(
                                        f"removeItems failed for all "
                                        f"{len(items_to_remove)} stale member(s)"
                                    ),
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
