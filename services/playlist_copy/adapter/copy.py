"""
Playlist Management copy orchestrator.

Drives the per-playlist copy from source server / source user to
destination server / destination user. Built on top of the
backend-agnostic adapter ABC so it works for any
{plex, jellyfin, emby} -> {plex, jellyfin, emby} pair.

Public surface:

* :func:`list_user_playlists` - cache-aware list for the UI.
* :func:`get_playlist_detail` - cache-aware single-playlist read.
* :func:`copy_playlist` - the end user-facing copy action.
* :func:`refresh_user_cache` - force-refresh one user's cache.
* :func:`refresh_server_cache` - force-refresh every user on a server.

Typed errors defined here surface as structured codes in
``server/app.py`` REST handlers. Cache lookups fall back to live
silently on errors; live failures bubble up.
"""

from __future__ import annotations

import concurrent.futures
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from server import playlist_cache_db, server_registry
from services.adapters import (
    ItemRef,
    MediaServerAdapter,
    PlaylistSpec as AdapterPlaylistSpec,
    UserContext,
    UserSpec,
)
from services.tunables import (
    playlist_cache_enabled,
    playlist_cache_max_age_seconds,
    playlist_cache_refresh_interval_seconds,
    playlist_cache_snapshot_threshold_seconds,
    playlist_mgmt_batch_per_source_workers,
    playlist_mgmt_fuzzy_ambiguous_behavior,
    playlist_mgmt_item_resolve_workers,
    playlist_mgmt_plex_home_auth_mode,
    playlist_mgmt_same_user_behavior,
    strict_identity_resolution,
)

from services.playlist_copy.adapter.item_resolver import resolve_item_to_dest
from services.playlist_copy.adapter.batch_runner import (
    _per_source_semaphore_for,
    _reset_per_source_semaphores_for_tests,
    acquire_with_cancel,
)


# ── Re-exported from services.playlist_copy.adapter.user_auth ─────────────────────────────
# The user-resolution + per-user-auth cluster moved to its own module.
# Re-exported here so this module's import surface is unchanged: external
# code still does ``from services.playlist_copy import _find_user`` etc.
from services.playlist_copy.adapter.user_auth import (
    _find_user,
    _identity_kit,
    _lookup_plex_home_token,  # noqa: F401
    _obtain_per_user_token_via_pin,  # noqa: F401
    _resolve_app_user_uuid_for_lookup,
    _same_logical_user,
    _user_context_for,
)


log = logging.getLogger("plexmigrate.services.playlist_copy")


# ── Typed errors ────────────────────────────────────────────────────────────


class PlaylistCopyError(Exception):
    """Base type for all orchestrator errors. ``code`` is the stable
    string the REST layer maps to an HTTP response. ``http_status`` is
    the suggested response code; the handler may override."""

    code: str = "PLAYLIST_COPY_FAILED"
    http_status: int = 500


class SourceUnreachable(PlaylistCopyError):
    code = "SOURCE_UNREACHABLE"
    http_status = 502


class DestUnreachable(PlaylistCopyError):
    code = "DEST_UNREACHABLE"
    http_status = 502


class PlaylistNotFound(PlaylistCopyError):
    code = "PLAYLIST_NOT_FOUND"
    http_status = 404


class SmartPlaylistNotPortable(PlaylistCopyError):
    code = "SMART_PLAYLIST_NOT_PORTABLE"
    http_status = 422


class DestUserNotFound(PlaylistCopyError):
    code = "DEST_USER_NOT_FOUND"
    http_status = 404


class DestWriteFailed(PlaylistCopyError):
    code = "DEST_WRITE_FAILED"
    http_status = 502


class DestUserTokenMissing(PlaylistCopyError):
    """Raised when the end user chose ``per_user_token`` auth mode but
    the destination user has no saved Plex Home token in
    ``managed_users.auth_token_enc``. The UI surfaces this with a
    pointer to the
    ``POST /api/managed-users/{server_id}/{username}/plex-home-token``
    endpoint."""
    code = "DEST_USER_TOKEN_MISSING"
    http_status = 412


class PlaylistCopyCancelled(PlaylistCopyError):
    """Raised inside copy_playlist when the per-item cancel_event
    fires mid-copy. Distinct from a generic failure so the batch
    result counts cancellations separately from failures + skips.
    The operator-set cancel_event is checked between phases; an item
    already mid-write completes, since aborting would leave partial
    state on the destination."""
    code = "ITEM_CANCELLED"
    http_status = 499  # client-closed-request convention


# ── Helpers ─────────────────────────────────────────────────────────────────


def _connect(server_id: str, *, on_fail: type) -> Any:
    """Resolve the prefixed UID + open the adapter connection. Wraps
    every connect-time failure in the caller-specified typed error so
    the orchestrator never leaks transport-level exceptions to the
    REST layer."""
    try:
        return server_registry.connect_registered_server(server_id, log)
    except ValueError as exc:
        # No such server registered. Treat as unreachable from the
        # caller's perspective; the end user typed a stale id.
        raise on_fail(f"server {server_id!r} not registered: {exc}") from exc
    except ConnectionError as exc:
        raise on_fail(f"server {server_id!r} unreachable: {exc}") from exc


# ── Re-exported from services.playlist_copy.adapter.cache_api ─────────────────────────────
# The cache-aware list / detail / refresh surface (plus the snapshot-path
# freshness check) moved to its own module. Re-exported so external callers
# (server/app.py, services/sync_worker.py,
# services/playlist_cache_refresher.py) are unaffected.
from services.playlist_copy.adapter.cache_api import (
    _ListResult,  # noqa: F401
    _live_list_and_cache,  # noqa: F401
    get_playlist_detail,
    list_user_playlists,
    refresh_server_cache,
    refresh_user_cache,
    snapshot_should_use_cache,  # noqa: F401
)


# ── Public copy API ────────────────────────────────────────────────────────


def copy_playlist(
    *,
    source_server_id: str,
    source_user_id: str,
    source_playlist_id: str,
    dest_server_id: str,
    dest_user_id: str,
    dest_playlist_name: Optional[str] = None,
    progress_cb: Optional[Any] = None,
    cancel_event: Optional[threading.Event] = None,
    on_existing: str = "create",
    materialize_smart_as_static: bool = False,
    logger: Optional[logging.Logger] = None,
) -> Dict[str, Any]:
    """Copy one playlist from source -> destination.

    Returns a dict matching :class:`server.models.PlaylistCopyResult`.
    Raises one of the typed errors above on any failure the REST layer
    should surface as a structured error (not 500).

    ``progress_cb`` is an optional callable invoked with structured
    progress events as the copy proceeds. Event payload shapes:
      * ``{"event": "started"}``
      * ``{"event": "users-resolved", "source_username", "dest_username"}``
      * ``{"event": "source-loaded", "playlist_name", "is_smart", "item_count"}``
      * ``{"event": "resolving", "completed", "total"}`` — fired
        roughly every 10 items so the dashboard counter ticks.
      * ``{"event": "writing", "name", "resolved_count"}``
      * ``{"event": "done", "items_written", "items_skipped_no_match"}``
    The worker passes a callback that pushes activity + counter
    updates to the live DashboardState so the Dashboard tab can render
    the same per-phase progress it shows for snapshot/restore/direct.
    Any callback exception is caught + swallowed so the copy itself
    never fails on observer plumbing.

    ``logger`` overrides the module-level logger for this call.
    Manual / operator-initiated copies (Playlist Management,
    restore-mode playlists) leave this None so log records
    propagate to ``plexmigrate`` and land in the active job's
    runtime.log. The sync engine passes
    :func:`services.mirror_sync.log.get_sync_logger` so its activity is
    written to sync.log instead, never bleeding into a running
    job's runtime.log.
    """
    started = time.perf_counter()
    # ``log`` is the module-level logger; rebind it locally so the
    # body uses the caller-supplied logger when provided. Existing
    # code inside the function references ``log`` heavily; this is
    # the smallest-touch way to honour the logger override without
    # rewriting every call site below.
    log = logger if logger is not None else logging.getLogger(
        "plexmigrate.services.playlist_copy"
    )

    def _emit(event: str, **fields: Any) -> None:
        if progress_cb is None:
            return
        try:
            progress_cb({"event": event, **fields})
        except Exception:
            log.exception("copy_playlist progress_cb raised on event %r", event)

    def _check_cancel(phase: str) -> None:
        """Cancellation checkpoint. cancel_event is set externally
        (per-item Cancel button OR whole-batch Stop). Checked at phase
        boundaries so an in-flight HTTP call isn't interrupted
        mid-flight but no new work starts after the operator clicks
        Cancel."""
        if cancel_event is not None and cancel_event.is_set():
            raise PlaylistCopyCancelled(
                f"Copy cancelled at phase {phase!r} by operator request."
            )

    _emit("started")
    _check_cancel("start")

    # 1. Connect both ends.
    src_conn = _connect(source_server_id, on_fail=SourceUnreachable)
    dst_conn = _connect(dest_server_id, on_fail=DestUnreachable)

    src_adapter: MediaServerAdapter = src_conn.adapter
    dst_adapter: MediaServerAdapter = dst_conn.adapter

    # 2. Resolve users on each end. Source must exist; destination
    # must exist (no inline-create on the copy path; the end user
    # uses the preflight inline-create endpoint for that).
    # Source resolution does not need identity_map (the user_id is
    # already on the source server). Destination resolution does:
    # end users may pick a dest user by source-side handle when the
    # destination uses a different name; identity_map then walks
    # source -> dest. The two server_ids are passed only on the dest
    # call so the source lookup keeps its narrow behaviour.
    src_user = _find_user(
        src_adapter, source_user_id, on_missing=PlaylistNotFound,
        this_server_id=source_server_id,
    )
    dst_user = _find_user(
        dst_adapter, dest_user_id, on_missing=DestUserNotFound,
        this_server_id=dest_server_id,
        peer_server_id=source_server_id,
    )

    # Same-user no-op short-circuit.
    # When the resolved source + destination are the same (server,
    # user) pair, the copy would either no-op or silently produce a
    # duplicate-named playlist under the same account. Skip by default;
    # the ``playlist_mgmt_same_user_behavior`` tunable lets end users
    # opt into the duplicate-creation behaviour when they want to
    # fork a playlist for editing.
    #
    # Canonical-identity comparison (in priority order): app_user_uuid
    # (post-schema-v12 stable handle), backend_user_id, case-insensitive
    # username. Falls back to whatever identifiers are populated; treats
    # blank-on-both-sides as a match for that signal.
    if source_server_id == dest_server_id:
        same_user = _same_logical_user(
            source_server_id, src_user, dst_user,
        )
        if same_user and playlist_mgmt_same_user_behavior() == "skip":
            elapsed = time.perf_counter() - started
            reason = (
                f"Source and destination resolve to the same user "
                f"({src_user.username!r}) on server "
                f"{source_server_id!r}. Skipped — change the "
                f"'playlist_mgmt_same_user_behavior' tunable to "
                f"'duplicate' if you want to fork the playlist."
            )
            _emit(
                "done", items_written=0, items_skipped_no_match=0,
                skipped=True, skip_reason=reason,
            )
            return {
                "success": True,
                "new_playlist_id": None,
                "items_written": 0,
                "items_skipped_no_match": 0,
                "items_failed": 0,
                "errors": [],
                "elapsed_seconds": elapsed,
                "skipped": True,
                "skip_reason": reason,
            }

    src_ctx = _user_context_for(
        src_conn, src_user, role="source", server_id=source_server_id,
        progress_cb=progress_cb,
    )
    dst_ctx = _user_context_for(
        dst_conn, dst_user, role="dest", server_id=dest_server_id,
        progress_cb=progress_cb,
    )
    _emit(
        "users-resolved",
        source_username=src_user.username,
        dest_username=dst_user.username,
    )
    _check_cancel("after_users_resolved")

    # 3. Read the source playlist's name + smart flag + items.
    #
    # The ``get_playlist`` adapter method fetches ONE playlist in a
    # single round-trip and returns name + is_smart + items together.
    # Preferred over the ``list_playlists`` + ``get_playlist_items``
    # path, which eagerly fetches items for EVERY playlist the user
    # has (hundreds of round-trips). Falls back to that slow two-walk
    # path on adapters that don't implement ``get_playlist``.
    src_spec: Optional[AdapterPlaylistSpec] = None
    _get_playlist = getattr(src_adapter, "get_playlist", None)
    if callable(_get_playlist):
        try:
            src_spec = _get_playlist(source_playlist_id, user_context=src_ctx)
        except Exception as exc:
            raise SourceUnreachable(
                f"source get_playlist failed: {exc}"
            ) from exc
    if src_spec is None:
        # Fallback: backends that haven't implemented get_playlist.
        try:
            src_specs = list(src_adapter.list_playlists(src_ctx) or [])
        except Exception as exc:
            raise SourceUnreachable(
                f"source list_playlists failed: {exc}"
            ) from exc
        src_spec = next(
            (s for s in src_specs if s.playlist_id == source_playlist_id),
            None,
        )
    if src_spec is None:
        raise PlaylistNotFound(
            f"playlist {source_playlist_id!r} not found for user "
            f"{source_user_id!r} on source server"
        )
    if src_spec.is_smart and not materialize_smart_as_static:
        raise SmartPlaylistNotPortable(
            f"playlist {src_spec.name!r} is smart; criteria do not port "
            f"across backends"
        )
    # When the caller asked to
    # materialize a smart playlist as static (the destination is
    # Jellyfin / Emby, which have no smart-playlist concept), fall
    # through. ``src_spec.items`` is empty for a smart playlist, so
    # the item block below fetches the current resolved members via
    # ``get_playlist_items`` - a point-in-time static snapshot.

    # Items: prefer the ones the get_playlist call already returned;
    # otherwise fetch them now (fallback path only).
    if src_spec.items:
        src_items: Tuple[ItemRef, ...] = tuple(src_spec.items)
    else:
        try:
            src_items = src_adapter.get_playlist_items(
                source_playlist_id, user_context=src_ctx,
            )
        except Exception as exc:
            raise SourceUnreachable(
                f"source get_playlist_items failed: {exc}"
            ) from exc
    _emit(
        "source-loaded",
        playlist_name=src_spec.name,
        is_smart=bool(src_spec.is_smart),
        item_count=len(src_items),
    )

    # 4. Resolve each source item to a destination backend_item_id.
    #
    # Resolution chain (multi-tier):
    #   1. Same-server passthrough — when src_server == dst_server, the
    #      source's ratingKey IS the dest's ratingKey. No live API call.
    #   2. GUID match — try every GUID the source carries against the
    #      destination's library via plexapi's getByGuid. Modern Plex
    #      installs match here cleanly for movies / TV / audiobooks
    #      with public-database identifiers.
    #   3. Path-tail match — last N components of the file path (default
    #      3 → artist/album/song.ext). Root-agnostic so D:\\Music\\X →
    #      /mnt/plex/Music/X matches by the trailing tail. Catches the
    #      music-tracks-without-GUIDs case the end user's Jade.TV →
    #      Jade.Music copy hit.
    #
    # Per-item failures land in ``per_item_misses`` with the list of
    # methods attempted, so the end user can see exactly what was tried.
    same_server = (
        bool(source_server_id)
        and bool(dest_server_id)
        and source_server_id == dest_server_id
    )
    resolved_refs: List[ItemRef] = []
    skipped_no_match = 0
    # CONSOLE-05: items whose resolution raised an exception and stayed
    # unresolved - a real failure, distinct from a clean no-match skip.
    resolve_failed = 0
    errors: List[str] = []
    # CONSOLE-07: a non-owner destination context that fell back to the
    # admin token writes the playlist under the OWNER, not the requested
    # user. Surface that in the result so callers / UI do not report a
    # clean success for a misdirected copy.
    if dst_ctx.admin_fallback:
        errors.append(
            f"Destination user {dst_ctx.username!r}: per-user "
            f"credentials unavailable - copied under the server "
            f"owner's admin token, so the playlist lands under the "
            f"OWNER, not {dst_ctx.username!r}."
        )
    total_items = len(src_items)
    PROGRESS_STEP = max(1, total_items // 50) if total_items > 0 else 1

    # Each item's tier walk (GUID -> full-path -> path-tail -> fuzzy)
    # is independent once the dest adapter's path-tail index is built.
    # Run them through a ThreadPoolExecutor to overlap the Plex
    # round-trips. Input order is preserved by
    # writing to a pre-sized slot list, then concatenating the
    # per-item ItemRef sub-lists at the end.
    parallelism = max(1, int(playlist_mgmt_item_resolve_workers()))
    if total_items < 2:
        parallelism = 1

    # Per-slot output: each input item produces 0 (miss) or 1+ refs
    # (1 for unique match, N when fuzzy 'all' policy expanded).
    slot_refs: List[List[ItemRef]] = [list() for _ in range(total_items)]
    counters_lock = threading.Lock()
    progress_state = {"completed": 0, "resolved": 0, "skipped": 0}

    def _resolve_one(idx: int, ref: ItemRef) -> None:
        """Per-item resolution worker. Delegates the tier walk to
        :func:`resolve_item_to_dest` and applies the neutral result
        to the shared slot list + thread-safe counters."""
        # CONSOLE-04: per-item silent drop when the cancel event
        # fires during the resolution phase. This is asymmetric with
        # the phase-boundary _check_cancel (which raises
        # PlaylistCopyCancelled) - the silent drop is by design so
        # the resolver pool drains promptly without a per-item raise.
        if cancel_event is not None and cancel_event.is_set():
            return
        result = resolve_item_to_dest(
            dst_adapter, ref,
            same_server=same_server,
            fuzzy_ambiguous_behavior=playlist_mgmt_fuzzy_ambiguous_behavior(),
            logger=log,
        )
        if result.errors:
            with counters_lock:
                errors.extend(result.errors)
        if not result.dest_refs:
            # Empty dest_refs -> a clean no-match miss, or a
            # tier-raised failure. CONSOLE-05: distinguish the two
            # in the counters so items_failed stays accurate.
            _bump_progress(
                failed=result.tier_raised,
                skipped=not result.tier_raised,
            )
            return
        slot_refs[idx].extend(result.dest_refs)
        _bump_progress(resolved=True, count=len(result.dest_refs))
        if result.tier_hit and progress_cb is not None:
            try:
                progress_cb({
                    "event": "tier-hit",
                    "tier": result.tier_hit,
                    "title": ref.title,
                    "expanded": (
                        len(result.dest_refs)
                        if len(result.dest_refs) > 1 else 1
                    ),
                })
            except Exception:
                log.exception("tier-hit progress emit failed")

    def _bump_progress(
        *,
        resolved: bool = False,
        skipped: bool = False,
        failed: bool = False,
        count: int = 1,
    ) -> None:
        """Thread-safe progress counter update + emit on
        PROGRESS_STEP boundaries. Called by each _resolve_one
        worker as items finish."""
        nonlocal skipped_no_match, resolve_failed
        with counters_lock:
            progress_state["completed"] += 1
            if resolved:
                progress_state["resolved"] += count
            if skipped:
                progress_state["skipped"] += 1
                skipped_no_match += 1
            if failed:
                # CONSOLE-05: shown in the progress bar's skipped total
                # (the item did not resolve) but tracked separately so
                # the result dict's items_failed is accurate.
                progress_state["skipped"] += 1
                resolve_failed += 1
            completed = progress_state["completed"]
            resolved_total = progress_state["resolved"]
            skipped_total = progress_state["skipped"]
        if completed % PROGRESS_STEP == 0 or completed == total_items:
            _emit(
                "resolving",
                completed=completed,
                total=total_items,
                resolved=resolved_total,
                skipped=skipped_total,
            )

    if parallelism == 1 or total_items <= 1:
        for idx, ref in enumerate(src_items):
            _resolve_one(idx, ref)
    else:
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=parallelism,
            thread_name_prefix="pl-resolve",
        ) as pool:
            futures = [
                pool.submit(_resolve_one, idx, ref)
                for idx, ref in enumerate(src_items)
            ]
            # Drain futures so any exceptions surface (defensive —
            # _resolve_one catches its own + records the error in the
            # shared errors list, so this should be quick).
            for fut in concurrent.futures.as_completed(futures):
                try:
                    fut.result()
                except Exception:
                    log.exception(
                        "pl-resolve worker raised unexpectedly; "
                        "the item's miss should still be recorded.",
                    )

    # Flatten the pre-sized slots into the final resolved_refs list,
    # preserving input order.
    for slot in slot_refs:
        resolved_refs.extend(slot)

    # Final post-loop tick so the dashboard sees the end state even
    # when the last item didn't land on a PROGRESS_STEP boundary.
    _emit(
        "resolving",
        completed=total_items,
        total=total_items,
        resolved=len(resolved_refs),
        skipped=skipped_no_match,
    )

    if not resolved_refs:
        # End user-visible "nothing landed" outcome; not a 500.
        return {
            "success": False,
            "new_playlist_id": None,
            "items_written": 0,
            "items_skipped_no_match": skipped_no_match,
            "items_failed": resolve_failed,
            "errors": errors or [
                "No source items resolved on the destination server (zero GUID matches)."
            ],
            "elapsed_seconds": time.perf_counter() - started,
        }

    # 5. Land the playlist on the destination. ``on_existing`` chooses
    # the policy when a playlist with the same name already lives on
    # the destination under the same user:
    #
    #   * "create" (default) — always call create_playlist. May produce
    #     duplicate-named playlists. Preserves historical behaviour for
    #     the manual Playlist Management + restore paths that built
    #     against this function before merge mode existed.
    #   * "merge" — used by the sync engine. Looks up an existing
    #     playlist with the same case-insensitive name, dedups
    #     resolved_refs against its current items by backend_item_id,
    #     and appends only the missing entries via add_to_playlist.
    #     Returns the existing playlist_id so the caller can record
    #     the persistent identity.
    #   * "skip" — same lookup as merge, but on hit returns success
    #     without touching the dest. Useful for "only fire on first
    #     run, never replay" semantics.
    name = (dest_playlist_name or src_spec.name or "").strip()
    if not name:
        name = "Untitled"
    on_existing_norm = (on_existing or "create").strip().lower()
    if on_existing_norm not in ("create", "merge", "skip"):
        on_existing_norm = "create"

    existing_pl = None
    if on_existing_norm in ("merge", "skip"):
        try:
            dst_playlists = dst_adapter.list_playlists(dst_ctx)
        except Exception as exc:
            log.warning(
                "copy_playlist: list_playlists for %s lookup failed; "
                "falling through to create: %s",
                on_existing_norm, exc,
            )
            dst_playlists = []
        target_lc = name.lower()
        for pl in dst_playlists:
            if (pl.name or "").strip().lower() == target_lc:
                existing_pl = pl
                break

    # Last cancel-checkpoint BEFORE the destination write fires.
    # Per Plan section 8a Q5: in-flight items mid-write don't abort
    # (would leave partial state); they complete normally. The
    # check here is the last chance to drop the work before any
    # write side-effect lands.

    if existing_pl is not None and on_existing_norm == "skip":
        # Operator-requested no-op. Report success so the caller's
        # cycle accounting marks this as resolved, not failed.
        _emit("done", items_written=0, items_skipped_no_match=skipped_no_match)
        return {
            "success":                  True,
            "new_playlist_id":          existing_pl.playlist_id or None,
            "items_written":            0,
            "items_already_present":    len(resolved_refs),
            "items_skipped_no_match":   skipped_no_match,
            "items_failed":             resolve_failed,
            "errors":                   errors,
            "merged_into_existing":     True,
            "existing_playlist_name":   existing_pl.name,
            "skipped_create":           True,
            "elapsed_seconds":          time.perf_counter() - started,
        }

    if existing_pl is not None and on_existing_norm == "merge":
        # Pull the existing playlist's items so we can dedup new
        # resolved_refs against what's already on the destination.
        # Dedup key is the destination-side backend_item_id (rating
        # key on Plex, GUID on Jellyfin / Emby). Items in
        # resolved_refs without a backend_item_id (resolver shouldn't
        # produce these, but defensive) are appended unconditionally.
        try:
            existing_items = dst_adapter.get_playlist_items(
                existing_pl.playlist_id, user_context=dst_ctx,
            )
        except Exception as exc:
            raise DestWriteFailed(
                f"get_playlist_items({existing_pl.playlist_id!r}) "
                f"failed during merge: {exc}"
            ) from exc
        existing_keys = {
            str(it.backend_item_id) for it in (existing_items or [])
            if getattr(it, "backend_item_id", None)
        }
        new_refs: List[ItemRef] = []
        already_present = 0
        for r in resolved_refs:
            key = str(getattr(r, "backend_item_id", "") or "")
            if key and key in existing_keys:
                already_present += 1
                continue
            new_refs.append(r)

        if not new_refs:
            # Source and destination already agree; nothing to write.
            _emit(
                "done", items_written=0,
                items_skipped_no_match=skipped_no_match,
            )
            return {
                "success":                  True,
                "new_playlist_id":          existing_pl.playlist_id or None,
                "items_written":            0,
                "items_already_present":    already_present,
                "items_skipped_no_match":   skipped_no_match,
                "items_failed":             resolve_failed,
                "errors":                   errors,
                "merged_into_existing":     True,
                "existing_playlist_name":   existing_pl.name,
                "elapsed_seconds":          time.perf_counter() - started,
            }

        _check_cancel("before_write")
        _emit("writing", name=name, resolved_count=len(new_refs))
        try:
            added = dst_adapter.add_to_playlist(
                existing_pl.playlist_id, new_refs,
                user_context=dst_ctx,
            )
        except Exception as exc:
            raise DestWriteFailed(
                f"add_to_playlist({existing_pl.playlist_id!r}) "
                f"failed during merge: {exc}"
            ) from exc
        added_int = int(added or 0)
        _emit(
            "done", items_written=added_int,
            items_skipped_no_match=skipped_no_match,
        )
        return {
            "success":                  True,
            "new_playlist_id":          existing_pl.playlist_id or None,
            "items_written":            added_int,
            "items_already_present":    already_present,
            "items_skipped_no_match":   skipped_no_match,
            "items_failed":             resolve_failed,
            "errors":                   errors,
            "merged_into_existing":     True,
            "existing_playlist_name":   existing_pl.name,
            "elapsed_seconds":          time.perf_counter() - started,
        }

    # Default path: create a fresh playlist on the destination.
    _check_cancel("before_write")
    _emit("writing", name=name, resolved_count=len(resolved_refs))
    try:
        new_id = dst_adapter.create_playlist(name, resolved_refs, user_context=dst_ctx)
    except Exception as exc:
        raise DestWriteFailed(f"create_playlist failed: {exc}") from exc

    _emit(
        "done",
        items_written=len(resolved_refs),
        items_skipped_no_match=skipped_no_match,
    )

    return {
        "success":                  True,
        "new_playlist_id":          new_id or None,
        "items_written":            len(resolved_refs),
        "items_skipped_no_match":   skipped_no_match,
        "items_failed":             resolve_failed,
        "errors":                   errors,
        "merged_into_existing":     False,
        "elapsed_seconds":          time.perf_counter() - started,
    }


# ── Batch copy ───────────────────────────────────────────────────────────────


def copy_playlist_batch(
    items: List[Dict[str, Any]],
    *,
    parallelism: int = 8,
    stop_event: Optional[threading.Event] = None,
    per_item_cancel_events: Optional[Dict[int, threading.Event]] = None,
    progress_cb: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> Dict[str, Any]:
    """Run N playlist copies in parallel via ThreadPoolExecutor.

    Contract:

    * ``items`` is a list of dicts, each carrying the kwargs that
      :func:`copy_playlist` accepts (``source_server_id``,
      ``source_user_id``, ``source_playlist_id``, ``dest_server_id``,
      ``dest_user_id``, ``dest_playlist_name``).
    * ``parallelism`` caps the worker pool size (operator-tunable;
      caller supplies the resolved value from the per-submit override
      or ``playlist_mgmt_batch_workers``).
    * ``stop_event`` is the whole-batch Stop signal — when set, the
      helper drains pending items without starting them; in-flight
      items respect their own per-item cancel_event.
    * ``per_item_cancel_events`` maps item INDEX (0-based) to its own
      cancel_event. The per-item Cancel button on the active-deploys
      panel sets the matching event; the worker for that item checks
      it between phases (see ``copy_playlist``'s ``_check_cancel``).
      Missing index = no cancel possible for that item (treated as
      "not cancelled").
    * ``progress_cb`` receives structured per-item progress events
      decorated with the item's ``index`` so the caller can correlate
      with the original batch positions.

    Returns ``{total, succeeded, failed, skipped, cancelled,
    elapsed_seconds, results: [...]}``. The ``results`` list is
    1:1 with the input ``items`` and ordered by original index;
    each entry mirrors :func:`copy_playlist`'s return shape plus
    a per-item ``index`` field and an ``error_code`` when the item
    failed (one of the PlaylistCopyError ``code`` values).

    Per-item failures DO NOT abort the batch; the result row carries
    the typed error code and the batch continues."""
    started = time.perf_counter()
    total = len(items)

    if total == 0:
        return {
            "total": 0, "succeeded": 0, "failed": 0,
            "skipped": 0, "cancelled": 0,
            "elapsed_seconds": 0.0, "results": [],
        }

    # Pre-size results so per-item slots maintain input ordering
    # regardless of which thread finishes first.
    results: List[Optional[Dict[str, Any]]] = [None] * total

    def _batch_emit(event: str, **fields: Any) -> None:
        if progress_cb is None:
            return
        try:
            progress_cb({"event": event, **fields})
        except Exception:
            log.exception(
                "copy_playlist_batch progress_cb raised on event %r", event,
            )

    _batch_emit("batch-started", total=total, parallelism=parallelism)

    def _per_item_emit_factory(
        index: int, item_for_thread: Dict[str, Any],
    ) -> Callable[[Dict[str, Any]], None]:
        """Wrap the per-item progress events with the batch index so
        the caller can correlate to the original input slot. Also
        updates the dashboard's per-thread CurrentItem phase as the
        inner copy progresses, so the Currently Processing panel
        shows live phase transitions."""
        # Map copy_playlist event names to a short phase label for
        # the dashboard's CurrentItem row.
        _PHASE_FOR_EVENT = {
            "started": "starting",
            "auth-chain": "authenticating",
            "users-resolved": "resolving users",
            "source-loaded": "reading source",
            "resolving": "resolving items",
            "writing": "writing to destination",
            "done": "done",
        }
        # Stash details for the dashboard CurrentItem row.
        _src_sid = str(item_for_thread.get("source_server_id") or "")
        _src_pid = str(item_for_thread.get("source_playlist_id") or "")

        def _per_item(ev: Dict[str, Any]) -> None:
            try:
                if progress_cb is not None:
                    progress_cb({**ev, "index": index})
            except Exception:
                log.exception(
                    "copy_playlist_batch per-item progress emit failed "
                    "for index %d", index,
                )
            # Update the per-thread dashboard row as we move
            # through phases. Best-effort; failures don't affect the
            # copy.
            phase = _PHASE_FOR_EVENT.get(str(ev.get("event") or ""))
            if not phase:
                return
            # Use the playlist NAME if the event surfaced it (e.g.
            # 'source-loaded' carries playlist_name); otherwise fall
            # back to the id we captured at item start.
            title = ev.get("playlist_name") or _src_pid or "(playlist)"
            try:
                from services import state as _state
                d = _state.get_dashboard()
                if d is not None:
                    d.set_current_item(
                        library=_src_sid or "(source server)",
                        item_type="playlist",
                        title=str(title),
                        phase=phase,
                    )
            except Exception:
                log.exception(
                    "copy_playlist_batch dashboard phase update failed",
                )
        return _per_item

    def _run_one(index: int, item: Dict[str, Any]) -> Dict[str, Any]:
        """Execute one copy_playlist; return its result dict
        decorated with the batch index. Per-item structured errors
        land here as ``{success: False, error_code: <code>}`` rather
        than bubbling out — the batch never aborts on one bad item."""
        per_item_evt = (
            per_item_cancel_events.get(index)
            if per_item_cancel_events is not None else None
        )
        # If the whole-batch stop_event is set BEFORE we start this
        # item, drop it as cancelled. The cancel_event check inside
        # copy_playlist handles mid-flight cases; this is the pre-
        # start gate.
        if stop_event is not None and stop_event.is_set():
            return {
                "index": index,
                "success": False,
                "cancelled": True,
                "error_code": PlaylistCopyCancelled.code,
                "errors": ["Batch stop was requested before this item started."],
                "items_written": 0,
                "items_skipped_no_match": 0,
                "items_failed": 0,
                "elapsed_seconds": 0.0,
                "new_playlist_id": None,
            }
        # Bridge the per-item event into copy_playlist; ALSO wire the
        # whole-batch stop_event so a global Stop short-circuits any
        # in-flight item (matching the operator's "Cancel All"
        # semantic from Q5).
        bridged_evt: Optional[threading.Event] = None
        if per_item_evt is not None and stop_event is not None:
            # Both supplied — bridge into a single event the inner
            # copy_playlist checks. Cheap one-shot daemon thread
            # forwards the global stop into the per-item event when
            # it fires.
            bridged_evt = per_item_evt
            def _forward_global_stop() -> None:
                stop_event.wait()
                bridged_evt.set()
            threading.Thread(
                target=_forward_global_stop, daemon=True,
            ).start()
        else:
            bridged_evt = per_item_evt if per_item_evt is not None else stop_event

        # Per-source backpressure (Plan section 8a Q7): at most N
        # in-flight copy_playlist calls per source server, regardless of
        # total batch parallelism. Acquire the source-keyed semaphore
        # AFTER the pre-start cancel gate (cheap) and BEFORE
        # copy_playlist starts any work. While waiting on the semaphore
        # we still respect the cancel events with a short-timeout poll
        # so the operator's Stop / per-item Cancel doesn't block on a
        # busy source.
        source_server_id = str(item.get("source_server_id") or "")
        sem = _per_source_semaphore_for(source_server_id)
        if not acquire_with_cancel(sem, (stop_event, per_item_evt)):
            return {
                "index": index, "success": False, "cancelled": True,
                "error_code": PlaylistCopyCancelled.code,
                "errors": [
                    "Cancelled while waiting for a per-source slot."
                ],
                "items_written": 0, "items_skipped_no_match": 0,
                "items_failed": 0, "elapsed_seconds": 0.0,
                "new_playlist_id": None,
            }
        # ── Light up the dashboard "Currently Processing"
        # panel for the duration of this item. set_current_item is
        # keyed by threading.get_ident(), and since each batch worker
        # runs in its own thread (from the ThreadPoolExecutor), each
        # in-flight item gets its own row on the panel. clear_current_item
        # in the finally block below removes it once the item finishes.
        try:
            from services import state as _state
            _dash = _state.get_dashboard()
        except Exception:
            _dash = None
        if _dash is not None:
            try:
                _dash.set_current_item(
                    library=source_server_id or "(source server)",
                    item_type="playlist",
                    title=str(item.get("source_playlist_id") or "(playlist)"),
                    phase="starting",
                )
            except Exception:
                log.exception(
                    "playlist_copy_batch: set_current_item failed at start",
                )
        # Fire a per-item "started" event AFTER the semaphore acquire
        # so the dashboard activity feed shows when each item actually
        # leaves the source-throttle queue and begins work. Without
        # this, operators see only completions and assume the batch is
        # running sequentially when it's actually being throttled by
        # the per-source semaphore.
        _batch_emit_started = progress_cb
        if _batch_emit_started is not None:
            try:
                _batch_emit_started({
                    "event": "batch-item-running",
                    "index": index,
                    "source_server_id": source_server_id,
                    "source_user_id": str(item.get("source_user_id") or ""),
                    "source_playlist_id": str(item.get("source_playlist_id") or ""),
                    "dest_server_id": str(item.get("dest_server_id") or ""),
                    "dest_user_id": str(item.get("dest_user_id") or ""),
                })
            except Exception:
                log.exception(
                    "copy_playlist_batch batch-item-running emit failed "
                    "for index %d", index,
                )
        try:
            result = copy_playlist(
                source_server_id=source_server_id,
                source_user_id=str(item.get("source_user_id") or ""),
                source_playlist_id=str(item.get("source_playlist_id") or ""),
                dest_server_id=str(item.get("dest_server_id") or ""),
                dest_user_id=str(item.get("dest_user_id") or ""),
                dest_playlist_name=item.get("dest_playlist_name"),
                progress_cb=_per_item_emit_factory(index, item),
                cancel_event=bridged_evt,
            )
            decorated = dict(result)
            decorated["index"] = index
            # Mark cancelled-status explicitly when the inner copy
            # short-circuited via PlaylistCopyCancelled (it raises
            # there; we shouldn't see it here, but defensive).
            if not decorated.get("cancelled"):
                decorated["cancelled"] = False
            decorated["error_code"] = None
            return decorated
        except PlaylistCopyCancelled as exc:
            return {
                "index": index,
                "success": False,
                "cancelled": True,
                "error_code": exc.code,
                "errors": [str(exc)],
                "items_written": 0,
                "items_skipped_no_match": 0,
                "items_failed": 0,
                "elapsed_seconds": 0.0,
                "new_playlist_id": None,
            }
        except PlaylistCopyError as exc:
            # Typed structured error (smart-playlist, dest-not-found,
            # token-missing, etc.) — record the code + message and
            # let the batch continue.
            return {
                "index": index,
                "success": False,
                "cancelled": False,
                "error_code": exc.code,
                "errors": [f"{exc.code}: {exc}"],
                "items_written": 0,
                "items_skipped_no_match": 0,
                "items_failed": 0,
                "elapsed_seconds": 0.0,
                "new_playlist_id": None,
            }
        except Exception as exc:
            # Unexpected exception — log + record as a generic
            # failure so the batch keeps going. The operator's
            # forensic trail (run-level log) still has the full
            # traceback.
            log.exception(
                "copy_playlist_batch: item index %d raised an unexpected "
                "exception; recording as failed and continuing.",
                index,
            )
            return {
                "index": index,
                "success": False,
                "cancelled": False,
                "error_code": "PLAYLIST_COPY_FAILED",
                "errors": [f"unexpected: {exc}"],
                "items_written": 0,
                "items_skipped_no_match": 0,
                "items_failed": 0,
                "elapsed_seconds": 0.0,
                "new_playlist_id": None,
            }
        finally:
            # Always release the per-source slot, even when the item
            # raises or returns early. Without this a single permanent
            # source failure would leak the semaphore depth and
            # deadlock subsequent batches against the same source.
            sem.release()
            # Clear the dashboard's per-thread current_item so
            # the Currently Processing panel drops this row when the
            # item finishes. Best-effort; never blocks return.
            if _dash is not None:
                try:
                    _dash.clear_current_item()
                except Exception:
                    log.exception(
                        "playlist_copy_batch: clear_current_item failed",
                    )

    parallelism = max(1, int(parallelism))
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=parallelism,
        thread_name_prefix="pl-batch",
    ) as pool:
        future_to_index = {
            pool.submit(_run_one, idx, item): idx
            for idx, item in enumerate(items)
        }
        for future in concurrent.futures.as_completed(future_to_index):
            idx = future_to_index[future]
            try:
                row = future.result()
            except Exception as exc:
                # _run_one catches its own exceptions; this branch is
                # a defensive belt for executor-internal failures.
                log.exception(
                    "copy_playlist_batch: future for index %d raised "
                    "unexpectedly; recording as failed.", idx,
                )
                row = {
                    "index": idx, "success": False, "cancelled": False,
                    "error_code": "PLAYLIST_COPY_FAILED",
                    "errors": [f"executor: {exc}"],
                    "items_written": 0, "items_skipped_no_match": 0,
                    "items_failed": 0, "elapsed_seconds": 0.0,
                    "new_playlist_id": None,
                }
            results[idx] = row
            # Surface the first error message + items_written/skipped to
            # the progress callback so the dashboard activity feed can
            # render rich per-item context (Plan section 8a follow-up:
            # operator reported "all 16 failed with no detail").
            _batch_emit(
                "batch-item-done",
                index=idx,
                success=bool(row.get("success")),
                cancelled=bool(row.get("cancelled")),
                error_code=row.get("error_code"),
                error_message=((row.get("errors") or [None])[0]),
                items_written=int(row.get("items_written") or 0),
                items_skipped_no_match=int(row.get("items_skipped_no_match") or 0),
                items_failed=int(row.get("items_failed") or 0),
            )

    # Tally outcomes. ``skipped`` covers the same-user no-op +
    # SmartPlaylistNotPortable paths (both surface as success=True
    # with a skipped flag from copy_playlist).
    succeeded = sum(
        1 for r in results
        if r and r.get("success") and not r.get("skipped") and not r.get("cancelled")
    )
    skipped = sum(
        1 for r in results
        if r and (r.get("skipped") or r.get("error_code") == "SMART_PLAYLIST_NOT_PORTABLE")
    )
    cancelled = sum(1 for r in results if r and r.get("cancelled"))
    failed = sum(
        1 for r in results
        if r and not r.get("success") and not r.get("cancelled")
        and r.get("error_code") != "SMART_PLAYLIST_NOT_PORTABLE"
    )

    elapsed = time.perf_counter() - started
    _batch_emit(
        "batch-done", total=total, succeeded=succeeded,
        failed=failed, skipped=skipped, cancelled=cancelled,
        elapsed_seconds=elapsed,
    )

    return {
        "total": total,
        "succeeded": succeeded,
        "failed": failed,
        "skipped": skipped,
        "cancelled": cancelled,
        "elapsed_seconds": elapsed,
        "results": [r if r is not None else {} for r in results],
    }
