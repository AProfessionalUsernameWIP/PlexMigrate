"""
Collection restore for the Plex-direct engine.

Owns ``restore_collections`` and the chunked Collection create/add wrappers
that work around Plex's URL-length cap. Re-uses the chunk-size constant from
``services.restorer.playlists`` because the two write paths share the same
HTTP limit and the value must stay in lockstep.
"""

import concurrent.futures
import logging
import threading
import time
from typing import Any, Dict, List, Optional, Set, Tuple

from plexapi.collection import Collection
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
from services.restore_replace_sweep import compute_dest_only_titles
from services.restorer.playlists import _PLAYLIST_CHUNK_SIZE


def _create_collection_chunked(server, name: str, section: Any, items: List[Any]) -> Any:
    """
    Same chunking strategy as ``_create_playlist_chunked`` for the
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
                    # Remove one member per call, counting only the
                    # calls that actually succeed - a single try/except
                    # around the whole loop would abort on the first
                    # failure yet still report every intended removal
                    # as done.
                    removed_ok = 0
                    for it in items_to_remove:
                        try:
                            existing_coll.removeItems([it])
                            removed_ok += 1
                        except Exception as exc:
                            logger.warning(
                                "[%s] Replace: removeItems failed on collection %r "
                                "(member ratingKey=%s): %s. Continuing; pre-Replace "
                                "snapshot is the recovery point.",
                                lib_name, coll_name,
                                getattr(it, "ratingKey", "?"), exc,
                            )
                    if items_to_add:
                        _add_to_collection_chunked(existing_coll, items_to_add)
                    _removed_str = (
                        str(removed_ok)
                        if removed_ok == len(items_to_remove)
                        else f"{removed_ok} of {len(items_to_remove)} (partial)"
                    )
                    _record_success(lib_name, {
                        "ts": ts, "tier": "N/A", "title": coll_name,
                        "type": "collection",
                        "action": (
                            f"[REPLACED] +{len(items_to_add)} / -{_removed_str} "
                            f"(snapshot had {len(resolved_items)}, "
                            f"destination had {len(existing_items)})"
                        ),
                    })
                    logger.info(
                        f"Replaced collection '{coll_name}': "
                        f"+{len(items_to_add)} added, -{_removed_str} removed"
                    )
                    _rlog = getattr(state, "_restoration_log", None)
                    if _rlog is not None:
                        if items_to_add or removed_ok:
                            _rlog.restored(
                                library=lib_name,
                                item_title=coll_name,
                                user=user,
                                metric="collection_member",
                                before=len(existing_items),
                                after=len(existing_items) + len(items_to_add) - removed_ok,
                                duration_ms=int((time.monotonic() - _coll_t0) * 1000),
                            )
                        elif items_to_remove:
                            # Stale members existed but every
                            # removeItems call failed and nothing was
                            # added: a failure, not a no-op.
                            _rlog.failed(
                                library=lib_name,
                                item_title=coll_name,
                                user=user,
                                metric="collection_member",
                                reason=(
                                    f"removeItems failed for all "
                                    f"{len(items_to_remove)} stale member(s)"
                                ),
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

    # Replace-mode dest-only sweep: a collection on the destination
    # that's absent from the source snapshot is a row the end user has
    # asked us to overwrite away (Replace semantics = exact mirror).
    # Collections are library-scoped on Plex so this per-section sweep
    # is safe — we only touch collections under ``section``. Merge mode
    # is additive and never deletes; the sweep is a no-op there.
    if mode == "replace":
        source_names = {c.get("name") for c in collections if c.get("name")}
        to_delete = compute_dest_only_titles(
            source_names, list(existing_collections.keys()),
        )
        for title in to_delete:
            try:
                existing_collections[title].delete()
                _record_success(lib_name, {
                    "ts": _tz_now(), "tier": "N/A", "title": title,
                    "type": "collection",
                    "action": "[DELETED] dest-only (Replace mode mirror)",
                })
                logger.info(
                    "[%s] Replace: deleted destination-only collection %r "
                    "(absent from source snapshot)",
                    lib_name, title,
                )
                _rlog = getattr(state, "_restoration_log", None)
                if _rlog is not None:
                    _rlog.restored(
                        library=lib_name,
                        item_title=title,
                        user=user,
                        metric="collection_member",
                        before=1,
                        after=0,
                        duration_ms=0,
                    )
            except Exception as exc:
                logger.warning(
                    "[%s] Replace: delete of dest-only collection %r failed: %s",
                    lib_name, title, exc,
                )
