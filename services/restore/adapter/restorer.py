"""
Backend-agnostic restore engine. Used for Jellyfin / Emby
destinations; Plex still routes through ``services/restorer.py`` to
keep its plexapi-specific code paths untouched.

Scope
-----
Per-library watch_history + ratings restore. The engine consumes a
snapshot payload produced by either ``services/snapshotter.py``
(Plex source) or ``services/snapshotter_adapter.py`` (Jellyfin / Emby
source) - both produce the same per-library dict shape so this
restore code is source-agnostic. Full cross-backend restore (Plex
source -> Jellyfin destination etc.) is not complete; the engine
currently works for same-backend or trivially matching content
where GUID resolution succeeds.

Not yet supported:
- Playlist + collection restore
- D-RATE numeric+favorite mapping
- D-COL-SCOPE library-prefixed collection names
- Per-user fan-out beyond the owner

Flow
----
1. Caller resolves the destination ``ServerConnection``.
2. ``restore_payload_adapter(connection, payload, ...)`` iterates the
   payload's libraries.
3. For each library's watch_history / ratings rows, the engine
   resolves the destination's ``backend_item_id`` via
   ``adapter.resolve_by_guids(guids)`` and issues
   ``adapter.set_watched`` / ``adapter.set_rating`` calls.
4. Per-row outcomes are accumulated into a ``RestoreResult`` so the
   run log + dashboard summary show "12 watch events written, 3
   skipped (no destination match), 1 unsupported (set_favorite on
   Plex)".
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from services.adapters import (
    ItemRef,
    MediaServerAdapter,
    UserContext,
    WriteResult,
)
from services.translation.backend_translation import translate_affinity
from services.media_state_writer import resolve_view_count_target


# ── Re-exported from services.restore.adapter.preflight ──────────────────────────────
# The preflight dry-run subsystem moved to its own module. Re-exported
# here so the import surface is unchanged: app.py still does
# ``from services.restore.adapter.restorer import dry_run_resolve_users`` etc.
from services.restore.adapter.preflight import (
    DestUserOption,
    DryRunReport,
    LibraryTypeNote,
    PROPOSED_RESOLUTION_VALUES,
    TombstoneNote,
    UserResolutionRecord,
    UserRowCounts,
    ZeroRowSkip,
    _load_tombstoned_dest_usernames,
    _normalise_dest_role,  # noqa: F401
    dry_run_resolve_users,
)


log = logging.getLogger("plexmigrate.services.restore.adapter.restorer")


@dataclass
class RestoreCounts:
    """Per-data-kind outcome counters for one restore run.

    Mirrors what the Plex restorer surfaces on its run summary so the
    end user UI can present uniform numbers regardless of which engine
    actually drove the writes."""
    written: int = 0
    skipped_no_match: int = 0
    skipped_zero_state: int = 0
    failed: int = 0
    unsupported: int = 0
    errors: List[str] = field(default_factory=list)


@dataclass
class RestoreResult:
    watch_history: RestoreCounts = field(default_factory=RestoreCounts)
    ratings: RestoreCounts = field(default_factory=RestoreCounts)
    playlists: RestoreCounts = field(default_factory=RestoreCounts)
    collections: RestoreCounts = field(default_factory=RestoreCounts)
    libraries_processed: int = 0
    started_at: float = 0.0
    finished_at: float = 0.0

    def as_dict(self) -> Dict[str, Any]:
        return {
            "watch_history": _counts_as_dict(self.watch_history),
            "ratings": _counts_as_dict(self.ratings),
            "playlists": _counts_as_dict(self.playlists),
            "collections": _counts_as_dict(self.collections),
            "libraries_processed": self.libraries_processed,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


def _counts_as_dict(c: RestoreCounts) -> Dict[str, Any]:
    return {
        "written": c.written,
        "skipped_no_match": c.skipped_no_match,
        "skipped_zero_state": c.skipped_zero_state,
        "failed": c.failed,
        "unsupported": c.unsupported,
        "errors": list(c.errors),
    }


def restore_payload_adapter(
    adapter: MediaServerAdapter,
    *,
    payload: Dict[str, Any],
    user_context: UserContext,
    logger: Optional[logging.Logger] = None,
    stop_event: Optional[threading.Event] = None,
    include_watch_history: bool = True,
    include_ratings: bool = True,
    include_playlists: bool = True,
    include_collections: bool = True,
    # Parity with services.restore.plex_native.run_restore: "merge"
    # (default, additive) or "replace" (destructive point-in-time
    # mirror). Replace mode adds a post-loop sweep that deletes
    # destination playlists / collections absent from the source so
    # the destination ends up as an exact mirror — closing the
    # adapter-path gap operator hit on the Plex-direct path.
    mode: str = "merge",
    # RESTORE-04: watch-count merge strategy. "higher" raises the
    # destination to max(stored, current); "sum" adds stored on top of
    # current. Mirrors services.restore.plex_native.run_restore's parameter of the
    # same name. Ignored in Replace mode (Replace is an overwrite).
    merge_watch_strategy: str = "higher",
    # PR-Phase-3: per-user fan-out on restore. When True (default),
    # the engine walks each library's ``users`` map and replays each
    # source user's data against the matching destination user.
    # End user-supplied ``user_filter`` narrows to a subset; missing
    # destination users are logged and skipped (no auto-create -
    # that's the D-OWNER flow end user-confirms via the modal).
    include_managed_users: bool = True,
    user_filter: Optional[List[str]] = None,
    # Affinity translation knobs.
    # ``favorite_threshold`` is the rating-to-favorite cutoff (a
    # source rating >= this writes IsFavorite=true on Jellyfin /
    # Emby). ``favorite_as_rating_value`` is the reverse: the numeric
    # rating written when a favorited item is restored onto a
    # rating-only backend (Plex). Both feed services.translation.backend_translation
    # .translate_affinity, the single chokepoint for the conversion.
    favorite_threshold: float = 5.0,
    favorite_as_rating_value: float = 10.0,
    # PR-CrossPolish D-COL-SCOPE (locked in Plan section 11.5): when
    # writing a library-scoped collection (Plex source) to a server-
    # wide BoxSet (Jellyfin / Emby destination), prefix the
    # collection name with the source library to prevent same-named
    # collections from different libraries merging on the destination.
    apply_col_scope_prefix: bool = True,
    # Identity-map resolution. When both IDs are non-empty, the
    # per-user fan-out consults media_db.user_identity_map before
    # falling back to direct username match + the owner-role
    # single-admin convention. Empty strings disable the lookup (the
    # end user's legacy .plexexport.json files carry no source
    # server_id, for example).
    source_server_id: str = "",
    dest_server_id: str = "",
    # Belt-and-braces
    # enforcement of block-class verdicts. When True (default), the
    # engine runs dry_run_resolve_users at the top of this function
    # and refuses to write if any source admin has no resolution path
    # (no identity map, no direct name match, no single-admin
    # fallback). The end user should have addressed the issue via the
    # cross-platform preflight modal before submit; if they bypassed
    # it (CLI / direct API call), we still won't silently drop their
    # data. Pass False as a CLI escape hatch when the end user
    # explicitly knows the consequences.
    enforce_preflight: bool = True,
    # End user-authored per-job
    # resolutions from the preflight modal (Map / Drop). Keyed by
    # destination_server_id; each value is a CrossPlatformPreflightAckIn
    # dict. Drop decisions skip the user; Map decisions act as priority-0
    # overrides in the resolver. Create + accept_proposed need no per-job
    # action (Create lands via /api/jobs/inline-create-user before
    # submit; accept_proposed re-derives the same proposal).
    cross_platform_resolutions: Optional[Dict[str, Any]] = None,
) -> RestoreResult:
    """Apply a snapshot payload to ``adapter``'s server.

    ``payload`` is the per-server snapshot dict produced by either
    snapshotter engine. The function walks the ``libraries`` array
    and applies each library's watch_history + ratings rows to the
    destination via the adapter.

    Returns a :class:`RestoreResult` summarising what happened. Never
    raises - per-row failures are accumulated into the result so the
    end user can review the run summary."""
    log_ = logger or log
    result = RestoreResult(started_at=time.time())

    # Pick up the run-level
    # mixed-media config jobs.py stashed on state. None means the
    # caller didn't go through the job dispatch (CLI direct, tests);
    # the engine treats every playlist row as pass-through in that
    # case (legacy behaviour).
    from services import state as _state
    _mm_cfg = _state.get_mixed_media_config()
    _mm_per_user = _state.get_mixed_media_per_user_configs()

    # When the caller didn't supply source_server_id explicitly, pull
    # it off the payload's snapshot_meta. Current-shape .db snapshots
    # populate snapshot_meta.server_id at capture time.
    # ALSO pull the
    # source service_type (snapshot_meta.backend) so the per-user
    # fan-out can forward it to the resolver chain's step 2.
    source_service_type: Optional[str] = None
    if isinstance(payload, dict):
        meta = payload.get("snapshot_meta") or {}
        if isinstance(meta, dict):
            if not source_server_id:
                source_server_id = str(meta.get("server_id") or "")
            backend_str = str(meta.get("backend") or "").strip().lower()
            if backend_str in ("plex", "jellyfin", "emby"):
                source_service_type = backend_str

    # Belt-and-braces preflight enforcement. Run the dry-run early;
    # if any source admin has no resolution path, refuse to write.
    # The end user should have run the preflight modal before submit.
    # Note: this runs the dry-run AGAIN even when the API endpoint
    # already called it pre-submit. That's intentional - cheap
    # (read-only adapter calls) and bullet-proof: no submit path
    # (CLI, direct API, schedule fire) bypasses it.
    if enforce_preflight and include_managed_users:
        dry_report = dry_run_resolve_users(
            payload, adapter,
            source_server_id=source_server_id,
            dest_server_id=dest_server_id,
            include_watch_history=include_watch_history,
            include_ratings=include_ratings,
            include_playlists=include_playlists,
            include_collections=include_collections,
            include_managed_users=include_managed_users,
            user_filter=user_filter,
            cross_platform_resolutions=cross_platform_resolutions,
            logger=log_,
        )
        if dry_report.overall_verdict == "blocked":
            reasons = "; ".join(
                dry_report.blocking_reasons or ["unresolved source admins"]
            )
            raise ValueError(
                f"Cross-platform restore refused (preflight-blocked): "
                f"{reasons}. Run the preflight modal on Run Job, apply "
                f"Map / Create / Drop decisions for the unresolved "
                f"source admins, then retry. The decisions persist when "
                f"the operator ticks 'Save my decisions as identity-map "
                f"entries' in the modal. CLI override: pass "
                f"enforce_preflight=False."
            )

    # Prefer the per-library array when the payload includes one
    # (even when empty). Fall back to "the payload IS a single
    # library" only when the libraries key is absent entirely - that
    # matches the legacy / cross-engine flat shape.
    if isinstance(payload, dict) and "libraries" in payload:
        libraries = payload.get("libraries") or []
    elif isinstance(payload, dict):
        libraries = [payload]
    else:
        libraries = []

    for lib_idx, lib in enumerate(libraries or []):
        if stop_event is not None and stop_event.is_set():
            log_.info(
                "Restore: stop requested after %d libraries.",
                result.libraries_processed,
            )
            break
        if not isinstance(lib, dict):
            continue
        lib_name = lib.get("library") or f"library-{lib_idx}"
        result.libraries_processed += 1

        if include_watch_history:
            _apply_watch_history(
                adapter, lib_name, lib.get("watch_history") or [],
                user_context, result.watch_history, stop_event, log_,
                mode=mode, merge_watch_strategy=merge_watch_strategy,
            )
        if include_ratings:
            _apply_ratings(
                adapter, lib_name, lib.get("ratings") or [],
                user_context, result.ratings, stop_event, log_,
                favorite_threshold=favorite_threshold,
                favorite_as_rating_value=favorite_as_rating_value,
            )
        if include_playlists:
            _apply_playlists(
                adapter, lib_name, lib.get("playlists") or [],
                user_context, result.playlists, stop_event, log_,
                mixed_media_config=_mm_cfg,
                per_user_mixed_media_configs=_mm_per_user,
            )
        if include_collections:
            _apply_collections(
                adapter, lib_name, lib.get("collections") or [],
                user_context, result.collections, stop_event, log_,
                apply_col_scope_prefix=apply_col_scope_prefix,
            )

        # Per-user fan-out. The library payload's ``users``
        # dict carries per-managed-user watch_history + ratings
        # captured at snapshot time. Resolve each source username to
        # a destination user (matched by username, case-insensitive)
        # and replay their state. Missing destination users are
        # logged and skipped - auto-creation is the end user-
        # confirmed D-OWNER flow which lives in the JobFormPanel UI,
        # not the engine.
        if include_managed_users:
            _apply_per_user_block(
                adapter,
                library_name=lib_name,
                users_block=lib.get("users") or {},
                user_filter=user_filter,
                admin_token=user_context.auth_token,
                logger=log_,
                stop_event=stop_event,
                result=result,
                include_watch_history=include_watch_history,
                include_ratings=include_ratings,
                favorite_threshold=favorite_threshold,
                favorite_as_rating_value=favorite_as_rating_value,
                include_playlists=include_playlists,
                include_collections=include_collections,
                apply_col_scope_prefix=apply_col_scope_prefix,
                source_server_id=source_server_id,
                dest_server_id=dest_server_id,
                cross_platform_resolutions=cross_platform_resolutions,
                source_service_type=source_service_type,
                mode=mode,
                merge_watch_strategy=merge_watch_strategy,
            )

    # Replace-mode dest-only sweep: a destination playlist /
    # collection absent from the source snapshot is a row the operator
    # has chosen to overwrite (Replace = exact mirror). Sweep runs
    # once after every library has been processed so a multi-library
    # restore sees the full source-side title set before deciding
    # what to delete. Merge mode is additive and leaves dest-only
    # rows alone. Adapter delete_playlist / delete_collection return
    # ``unsupported`` on backends that don't expose deletion (the
    # default base-class behaviour); counts go into the result's
    # ``unsupported`` bucket so the run summary surfaces the gap.
    if mode == "replace":
        _replace_dest_only_sweep(
            adapter,
            libraries=libraries or [],
            user_context=user_context,
            result=result,
            include_playlists=include_playlists,
            include_collections=include_collections,
            apply_col_scope_prefix=apply_col_scope_prefix,
            logger=log_,
        )

    result.finished_at = time.time()
    log_.info(
        "Restore complete: %d library/ies; watch=%d written / %d skipped(no match) / %d failed; "
        "ratings=%d written / %d skipped(no match) / %d failed / %d unsupported.",
        result.libraries_processed,
        result.watch_history.written,
        result.watch_history.skipped_no_match,
        result.watch_history.failed,
        result.ratings.written,
        result.ratings.skipped_no_match,
        result.ratings.failed,
        result.ratings.unsupported,
    )
    return result


def _apply_watch_history(
    adapter: MediaServerAdapter,
    library_name: str,
    rows: List[Dict[str, Any]],
    user_context: UserContext,
    counts: RestoreCounts,
    stop_event: Optional[threading.Event],
    logger: logging.Logger,
    *,
    mode: str = "merge",
    merge_watch_strategy: str = "higher",
) -> None:
    """Apply watch_history rows to the destination via ``adapter``.

    Exact-target wiring (Replace mode):
      * Replace mode interprets the snapshot's ``view_count`` as the
        ABSOLUTE target the destination should end at. The function
        reads the destination's current count via
        ``adapter.get_current_view_count`` and passes both to
        ``adapter.set_watched(view_count=N, current_view_count=C)``.
        Plex uses C to choose unscrobble + scrobble math so the
        destination lands exactly on N; Jellyfin/Emby ignore C
        (UserData writes exact PlayCount in one call).
      * Merge mode honours ``merge_watch_strategy`` (RESTORE-04):
        ``higher`` brings the destination up to
        ``max(stored, current)``; ``sum`` lands it at
        ``current + stored``. Both read the destination's current
        count; when that read is unavailable (the Jellyfin / Emby
        base default) the target falls back to the stored count,
        which is the pre-RESTORE-04 behaviour for those backends.
    """
    is_replace = str(mode or "merge").lower() == "replace"
    for row in rows:
        if stop_event is not None and stop_event.is_set():
            return
        view_count = int(row.get("view_count") or 0)
        if view_count <= 0 and not is_replace:
            # Merge mode: zero contributes nothing; skip.
            # Replace mode: zero IS the target — we still need to fire
            # the write so a destination row with current > 0 gets
            # cleared.
            counts.skipped_zero_state += 1
            continue
        guids = tuple(row.get("guids") or ())
        if not guids:
            counts.skipped_no_match += 1
            continue
        dest_item_id = _resolve_destination_item(adapter, guids, row)
        if not dest_item_id:
            counts.skipped_no_match += 1
            logger.debug(
                "[%s] watch: no destination match for %r (guids=%s)",
                library_name, row.get("title"), guids,
            )
            continue
        item_ref = ItemRef(
            backend_item_id=dest_item_id,
            guids=guids,
            title=str(row.get("title") or ""),
        )
        last_viewed = row.get("last_viewed_at")
        last_viewed_epoch: Optional[float] = None
        if isinstance(last_viewed, (int, float)):
            last_viewed_epoch = float(last_viewed)
        # Read the destination's current count whenever it affects the
        # write. Replace mode always needs it (exact-target math).
        # Merge mode needs it to honour merge_watch_strategy
        # (RESTORE-04). The adapter returns None when the read isn't
        # available (the Jellyfin / Emby base default); set_watched is
        # then passed current_view_count=None and the merge target
        # falls back to the stored count.
        current_view_count: Optional[int] = None
        try:
            current_view_count = adapter.get_current_view_count(
                item_ref, user_context=user_context,
            )
        except Exception as exc:
            logger.debug(
                "[%s] watch: get_current_view_count failed for "
                "%r: %s; falling back to delta-mode.",
                library_name, row.get("title"), exc,
            )
            current_view_count = None
        target_view_count = resolve_view_count_target(
            stored_view_count=view_count,
            current_view_count=current_view_count,
            mode=mode,
            strategy=merge_watch_strategy,
        )
        result = adapter.set_watched(
            item_ref,
            view_count=target_view_count,
            last_viewed_at=last_viewed_epoch,
            user_context=user_context,
            current_view_count=current_view_count,
        )
        _bump_from_result(counts, result, library_name, row, logger, "watch")


def _apply_ratings(
    adapter: MediaServerAdapter,
    library_name: str,
    rows: List[Dict[str, Any]],
    user_context: UserContext,
    counts: RestoreCounts,
    stop_event: Optional[threading.Event],
    logger: logging.Logger,
    *,
    favorite_threshold: float = 5.0,
    favorite_as_rating_value: float = 10.0,
) -> None:
    # A ratings row carries the
    # neutral per-user affinity (numeric rating + favorite face).
    # translate_affinity converts it into what the destination
    # backend can store: a numeric rating for Plex, a favorite flag
    # (plus optional numeric rating) for Jellyfin / Emby.
    dest_backend = str(getattr(adapter, "backend", "") or "")
    for row in rows:
        if stop_event is not None and stop_event.is_set():
            return
        # Read the neutral affinity. ``user_rating`` is the engine
        # shape; ``rating`` is the snapshot-serializer shape - accept
        # both so this works for live payloads and round-tripped .db
        # snapshots.
        _rating_raw = row.get("user_rating")
        if _rating_raw is None:
            _rating_raw = row.get("rating")
        try:
            src_rating = None if _rating_raw is None else float(_rating_raw)
        except (TypeError, ValueError):
            src_rating = None
        _fav_raw = row.get("is_favorite")
        src_fav = None if _fav_raw is None else bool(_fav_raw)

        spec = translate_affinity(
            source_rating=src_rating,
            source_is_favorite=src_fav,
            dest_backend=dest_backend,
            favorite_threshold=favorite_threshold,
            favorite_as_rating_value=favorite_as_rating_value,
        )
        if not spec.wrote_anything:
            counts.skipped_zero_state += 1
            continue

        guids = tuple(row.get("guids") or ())
        if not guids:
            counts.skipped_no_match += 1
            continue
        dest_item_id = _resolve_destination_item(adapter, guids, row)
        if not dest_item_id:
            counts.skipped_no_match += 1
            logger.debug(
                "[%s] rating: no destination match for %r (guids=%s)",
                library_name, row.get("title"), guids,
            )
            continue
        item_ref = ItemRef(
            backend_item_id=dest_item_id,
            guids=guids,
            title=str(row.get("title") or ""),
        )

        # The numeric rating write is the primary, counted write when
        # present. A favorite-only row (no rating to land) promotes
        # the favorite write into that role so the row still counts.
        rating_written = False
        if spec.rating is not None:
            result = adapter.set_rating(
                item_ref, float(spec.rating), user_context=user_context,
            )
            _bump_from_result(
                counts, result, library_name, row, logger, "rating",
            )
            rating_written = True

        if spec.is_favorite is not None:
            fav_result = adapter.set_favorite(
                item_ref, bool(spec.is_favorite), user_context=user_context,
            )
            if not rating_written:
                # Favorite-only row: this write IS the row's outcome.
                _bump_from_result(
                    counts, fav_result, library_name, row, logger,
                    "favorite",
                )
            elif fav_result.unsupported:
                # Paired write; the rating already counted. Expected
                # on Plex - record once per library, don't spam.
                msg = (
                    f"[{library_name}] favorite skipped: "
                    f"{fav_result.detail or 'backend has no per-user favorite'}"
                )
                if msg not in counts.errors:
                    counts.errors.append(msg)
            elif not fav_result.success:
                logger.warning(
                    "[%s] favorite write failed for %r: %s",
                    library_name, row.get("title"), fav_result.detail,
                )


def _extract_per_job_overrides(
    cross_platform_resolutions: Optional[Dict[str, Any]],
    dest_server_id: str,
) -> "tuple":
    """Pull out per-job Drop + Map decisions from the end user's
    preflight modal payload, scoped to one destination.

    ``cross_platform_resolutions`` is keyed by destination_server_id;
    each value is a ``CrossPlatformPreflightAckIn`` dict with a
    ``resolutions`` list of ``UserResolutionDecisionIn`` dicts.

    Returns ``(dropped_usernames, override_map)``:
      * dropped_usernames: set of lowercased source usernames the
        end user chose to drop. The caller subtracts these from
        user_filter so the resolver never processes them.
      * override_map: dict of lowercased source username -> dest
        backend_user_id. Map decisions land here. The resolver
        consults this BEFORE the identity_map / direct-match / owner
        fallback chain so per-job end user picks win.

    Returns ``(set(), {})`` when no resolutions apply (empty input
    or no entry for this destination).

    Create + accept_proposed decisions need no per-job action:
    Create decisions land via POST /api/jobs/inline-create-user
    before submit so the user already exists on the destination by
    the time this code runs; accept_proposed means the resolver
    re-derives the same proposal at write time. Both are no-ops here.
    """
    if not cross_platform_resolutions or not (dest_server_id or "").strip():
        return set(), {}
    ack = cross_platform_resolutions.get(dest_server_id)
    if not isinstance(ack, dict):
        return set(), {}
    decisions = ack.get("resolutions") or []
    if not isinstance(decisions, list):
        return set(), {}
    dropped: set = set()
    overrides: Dict[str, str] = {}
    for dec in decisions:
        if not isinstance(dec, dict):
            continue
        source_username = (dec.get("source_username") or "").strip().lower()
        if not source_username:
            continue
        action = (dec.get("action") or "").strip().lower()
        if action == "drop":
            dropped.add(source_username)
        elif action == "map":
            target = dec.get("dest_user_id")
            if target:
                overrides[source_username] = str(target)
    return dropped, overrides


def _resolve_destination_user(
    *,
    source_username: str,
    source_role: str,
    dest_by_username: Dict[str, Any],
    dest_admins: List[Any],
    source_server_id: str,
    dest_server_id: str,
    logger: logging.Logger,
    per_job_overrides: Optional[Dict[str, str]] = None,
    source_backend_user_id: Optional[str] = None,
    source_service_type: Optional[str] = None,
) -> Optional[Any]:
    """Resolve the destination user for a source user's per-user payload.

    Thin shim over :func:`services.identity.user_resolution.resolve_destination_user`
    (the USER-MGMT-IDENTITY-AUDIT shared helper). Signature is
    preserved EXACTLY for backward compatibility with every existing
    caller in this module + developer's tests; the two new keyword args
    (``source_backend_user_id``, ``source_service_type``) are optional
    and default to None.

    See the user_resolution module docstring for the full 5-step
    chain and the ``strict_identity_resolution`` tunable that gates
    the username fallback.
    """
    from services.identity.user_resolution import resolve_destination_user
    return resolve_destination_user(
        source_username=source_username,
        source_role=source_role,
        dest_by_username=dest_by_username,
        dest_admins=dest_admins,
        source_server_id=source_server_id,
        dest_server_id=dest_server_id,
        logger=logger,
        per_job_overrides=per_job_overrides,
        source_backend_user_id=source_backend_user_id,
        source_service_type=source_service_type,
    )


def _apply_per_user_block(
    adapter: MediaServerAdapter,
    *,
    library_name: str,
    users_block: Dict[str, Any],
    user_filter: Optional[List[str]],
    admin_token: str,
    logger: logging.Logger,
    stop_event: Optional[threading.Event],
    result: "RestoreResult",
    include_watch_history: bool,
    include_ratings: bool,
    favorite_threshold: float,
    favorite_as_rating_value: float = 10.0,
    include_playlists: bool = False,
    include_collections: bool = False,
    apply_col_scope_prefix: bool = True,
    source_server_id: str = "",
    dest_server_id: str = "",
    cross_platform_resolutions: Optional[Dict[str, Any]] = None,
    # Source service
    # type from snapshot_meta.backend so the resolver chain step 2
    # (backend_user_id direct match within service_type) can fire.
    # None when the snapshot doesn't carry the backend field.
    source_service_type: Optional[str] = None,
    # Forwarded to _apply_watch_history so Replace mode
    # per-user fan-out hits exact targets on Plex destinations.
    mode: str = "merge",
    # RESTORE-04: forwarded to _apply_watch_history so per-user
    # fan-out honours the watch-count merge strategy.
    merge_watch_strategy: str = "higher",
) -> None:
    """Walk the per-user payload for one library and replay each
    source user's state to the matching destination user.

    User matching is done by username (case-insensitive). Source
    users not present on the destination are logged and skipped; the
    end user can create them via the D-OWNER modal in the UI and
    re-run.

    ``user_filter`` (when supplied) restricts the fan-out to a
    specific subset of source usernames - mirrors the
    ``snapshot_adapter`` fan-out's filter so a paired snapshot +
    restore round-trip behaves consistently."""
    if not users_block:
        return
    # Per-job end user decisions: pull Drop + Map decisions out of
    # cross_platform_resolutions scoped to this destination. Drop
    # decisions augment user_filter to skip the user entirely; Map
    # decisions feed _resolve_destination_user as priority-0
    # overrides.
    dropped_usernames, per_job_overrides = _extract_per_job_overrides(
        cross_platform_resolutions, dest_server_id,
    )
    filter_set: Optional[set] = None
    if user_filter is not None:
        filter_set = {
            u.strip().lower() for u in user_filter
            if isinstance(u, str) and u.strip()
        }
    # End user-Dropped users always skipped, regardless of whether a
    # user_filter was supplied. If filter_set is None we start with an
    # all-inclusive view and just subtract; if filter_set is supplied
    # we subtract from it.
    if dropped_usernames:
        if filter_set is None:
            # Build an all-inclusive filter from the users_block keys
            # MINUS the dropped set; this way the loop below applies the
            # drop without otherwise narrowing.
            filter_set = {
                (k or "").strip().lower()
                for k in users_block.keys()
                if isinstance(k, str) and k.strip()
            } - dropped_usernames
        else:
            filter_set = filter_set - dropped_usernames
    # Build the destination's user roster once per library so we can
    # match without N round-trips per user. Match by lowercased
    # username so "Alice" / "alice" don't both fail.
    try:
        dest_users = adapter.list_users()
    except Exception as exc:
        logger.warning(
            "[%s] per-user fan-out: destination list_users failed: %s",
            library_name, exc,
        )
        return
    # Tombstone write-side filter:
    # the end user-marked-hidden destination users are excluded from
    # dest_by_username + dest_admins before any resolution fires. The
    # preflight surfaces the exclusion as a TombstoneNote so the
    # end user sees what was filtered; here at write time we just
    # honour the decision silently. Best-effort: lookup failure leaves
    # the roster un-filtered (the engine still works against the live
    # roster; preflight is the end user-facing surface for this).
    tombstoned_set = _load_tombstoned_dest_usernames(dest_server_id, logger)
    if tombstoned_set:
        dest_users = [
            u for u in (dest_users or [])
            if (u.username or "").strip().lower() not in tombstoned_set
        ]
    dest_by_username = {
        (u.username or "").strip().lower(): u
        for u in (dest_users or [])
        if u.username
    }
    # Pre-compute the admin subset once. Used by the owner-role
    # single-admin fallback in _resolve_destination_user; Plex has one
    # owner per server but Jellyfin / Emby can have many admins, so
    # the resolver only auto-picks when exactly one exists.
    dest_admins = [u for u in (dest_users or []) if getattr(u, "is_admin", False)]
    for source_username, source_payload in users_block.items():
        if stop_event is not None and stop_event.is_set():
            return
        if not isinstance(source_username, str) or not source_username.strip():
            continue
        normalised = source_username.strip().lower()
        if filter_set is not None and normalised not in filter_set:
            continue
        source_role = str((source_payload or {}).get("role") or "").strip().lower()
        # Forward the
        # source user's backend_user_id (carried in the per-user
        # payload block by the snapshot serializer) + the snapshot's
        # source service_type so step 2 of the resolution chain
        # (backend_user_id direct match within service_type) can fire
        # without an identity_map row. Both fields short-circuit
        # naturally inside the resolver when None.
        source_buid = str((source_payload or {}).get("backend_user_id") or "").strip()
        dest_user = _resolve_destination_user(
            source_username=source_username,
            source_role=source_role,
            dest_by_username=dest_by_username,
            dest_admins=dest_admins,
            source_server_id=source_server_id,
            dest_server_id=dest_server_id,
            logger=logger,
            per_job_overrides=per_job_overrides,
            source_backend_user_id=source_buid or None,
            source_service_type=source_service_type,
        )
        if dest_user is None:
            # Multi-admin destinations with an unmapped source owner
            # land here. Surface the ambiguity explicitly so the
            # end user knows to add an identity map.
            if (source_role == "owner"
                    and len(dest_admins) > 1
                    and source_username.strip().lower()
                    not in dest_by_username):
                logger.info(
                    "[%s] per-user fan-out: source owner %r could not "
                    "be resolved on the destination (%d admins, no "
                    "matching username, no identity map). Add a "
                    "user_identity_map entry to route owner-authored "
                    "data to the correct destination admin.",
                    library_name, source_username, len(dest_admins),
                )
            else:
                logger.info(
                    "[%s] per-user fan-out: source user %r not present on "
                    "destination; skipping. Create via Servers > User "
                    "Management or via the destination's admin UI and re-run.",
                    library_name, source_username,
                )
            # Count as skipped on every metric that had rows for the user.
            wh_rows = (source_payload or {}).get("watch_history") or []
            r_rows = (source_payload or {}).get("ratings") or []
            result.watch_history.skipped_no_match += len(wh_rows)
            result.ratings.skipped_no_match += len(r_rows)
            continue
        # Per-user UserContext retains the admin token because
        # Jellyfin / Emby admin tokens can write any user's
        # state via the UserId in the URL. Plex (which doesn't
        # appear here - per-user fan-out for Plex is
        # the existing get_home_users path) would need real per-
        # user tokens; the adapter's set_watched falls back to
        # the admin token when auth_token is empty.
        per_user_ctx = UserContext(
            backend_user_id=dest_user.backend_user_id or "",
            username=dest_user.username,
            auth_token=admin_token,
            is_admin=True,
        )
        rows_block = source_payload or {}
        if include_watch_history:
            _apply_watch_history(
                adapter, library_name,
                list(rows_block.get("watch_history") or []),
                per_user_ctx, result.watch_history, stop_event, logger,
                mode=mode, merge_watch_strategy=merge_watch_strategy,
            )
        if include_ratings:
            _apply_ratings(
                adapter, library_name,
                list(rows_block.get("ratings") or []),
                per_user_ctx, result.ratings, stop_event, logger,
                favorite_threshold=favorite_threshold,
                favorite_as_rating_value=favorite_as_rating_value,
            )
        # Legacy .plexexport.json files store playlists + collections
        # under each user (because Plex Home users authored them);
        # modern adapter snapshots put both at the library top-level
        # (owner-phase) instead. Reading them here closes the gap on
        # legacy payloads without double-writing for modern ones,
        # since modern payloads never populate users.<name>.playlists.
        if include_playlists:
            from services import state as _state
            _apply_playlists(
                adapter, library_name,
                list(rows_block.get("playlists") or []),
                per_user_ctx, result.playlists, stop_event, logger,
                mixed_media_config=_state.get_mixed_media_config(),
                per_user_mixed_media_configs=_state.get_mixed_media_per_user_configs(),
            )
        if include_collections:
            _apply_collections(
                adapter, library_name,
                list(rows_block.get("collections") or []),
                per_user_ctx, result.collections, stop_event, logger,
                apply_col_scope_prefix=apply_col_scope_prefix,
            )


def _apply_playlists(
    adapter: MediaServerAdapter,
    library_name: str,
    rows: List[Dict[str, Any]],
    user_context: UserContext,
    counts: RestoreCounts,
    stop_event: Optional[threading.Event],
    logger: logging.Logger,
    mixed_media_config: Optional["mixed_media.MixedMediaConfig"] = None,
    per_user_mixed_media_configs: Optional[Dict[str, "mixed_media.MixedMediaConfig"]] = None,
) -> None:
    """Create / append playlists on the destination.

    Each source playlist row has ``name``, ``is_smart``, ``items``
    (list of {title, guids, rating_key}). For each row:
      * Smart playlists: skipped with a log line (criteria don't port).
      * Resolve each item's destination_id via ``resolve_by_guids``;
        rows with zero resolvable items count as ``skipped_no_match``.
      * Create new playlist via ``adapter.create_playlist``; ratings
        and watch state on the items were applied by the earlier
        passes.

    Mixed-media handling:
    when ``mixed_media_config`` is provided, the rows are run through
    the strategy driver before write so mixed-source playlists are
    skipped / split / collapsed-to-dominant per the end user's choice
    BEFORE the destination resolution runs.

    No idempotency check today - re-running creates duplicates. That
    matches the Plex restorer's behaviour and is on the end user's
    list for a future "deduplicate at write" tunable."""
    if mixed_media_config is not None:
        from services.restore import mixed_media as _mm
        rows = _mm.transform_playlists_for_restore(
            rows,
            config=mixed_media_config,
            per_user_configs=per_user_mixed_media_configs,
            logger=logger,
        )
    for row in rows:
        if stop_event is not None and stop_event.is_set():
            return
        if row.get("is_smart"):
            counts.skipped_zero_state += 1
            logger.info(
                "[%s] playlist %r is smart; skipped (criteria preserved in payload).",
                library_name, row.get("name"),
            )
            continue
        items_raw = row.get("items") or []
        item_refs: List[ItemRef] = []
        for item in items_raw:
            guids = tuple(item.get("guids") or ())
            if not guids:
                continue
            dest_id = _resolve_destination_item(adapter, guids, item)
            if not dest_id:
                continue
            item_refs.append(ItemRef(
                backend_item_id=dest_id,
                guids=guids,
                title=str(item.get("title") or ""),
            ))
        if not item_refs:
            counts.skipped_no_match += 1
            logger.debug(
                "[%s] playlist %r: no destination items matched.",
                library_name, row.get("name"),
            )
            continue
        try:
            adapter.create_playlist(
                str(row.get("name") or ""),
                item_refs,
                user_context=user_context,
            )
            counts.written += 1
        except Exception as exc:
            counts.failed += 1
            counts.errors.append(
                f"[{library_name}] playlist {row.get('name')!r} failed: {exc}"
            )
            logger.warning(
                "[%s] playlist %r create failed: %s",
                library_name, row.get("name"), exc,
            )


def _apply_collections(
    adapter: MediaServerAdapter,
    library_name: str,
    rows: List[Dict[str, Any]],
    user_context: UserContext,
    counts: RestoreCounts,
    stop_event: Optional[threading.Event],
    logger: logging.Logger,
    *,
    apply_col_scope_prefix: bool = True,
) -> None:
    """Create collections on the destination.

    D-COL-SCOPE: when ``apply_col_scope_prefix`` is on (default), the
    destination collection name is prefixed with the source library
    name (``"Movies / Marvel"`` instead of just ``"Marvel"``). This
    prevents same-named collections from different source libraries
    merging into one BoxSet on a Jellyfin / Emby destination where
    BoxSets are server-wide.

    Same-backend transfer (P->P, J->J) doesn't strictly need the
    prefix because the destination respects the source's scope; the
    flag stays on by default to keep behaviour uniform across
    backends. End user can disable per-job if they want raw names."""
    target_backend = (adapter.backend or "plex").lower()
    needs_prefix = apply_col_scope_prefix and target_backend in ("jellyfin", "emby")
    for row in rows:
        if stop_event is not None and stop_event.is_set():
            return
        source_lib = str(row.get("source_library") or "")
        raw_name = str(row.get("name") or "")
        if needs_prefix and source_lib and raw_name and not raw_name.startswith(source_lib + " /"):
            dest_name = f"{source_lib} / {raw_name}"
        else:
            dest_name = raw_name
        items_raw = row.get("items") or []
        item_refs: List[ItemRef] = []
        for item in items_raw:
            guids = tuple(item.get("guids") or ())
            if not guids:
                continue
            dest_id = _resolve_destination_item(adapter, guids, item)
            if not dest_id:
                continue
            item_refs.append(ItemRef(
                backend_item_id=dest_id,
                guids=guids,
                title=str(item.get("title") or ""),
            ))
        if not item_refs:
            counts.skipped_no_match += 1
            logger.debug(
                "[%s] collection %r: no destination items matched.",
                library_name, raw_name,
            )
            continue
        try:
            # library_id is passed through when known; Plex requires
            # it (collections are library-scoped), Jellyfin / Emby
            # accept it and ignore.
            adapter.create_collection(
                dest_name, item_refs,
                library_id=row.get("library_id"),
            )
            counts.written += 1
        except Exception as exc:
            counts.failed += 1
            counts.errors.append(
                f"[{library_name}] collection {dest_name!r} failed: {exc}"
            )
            logger.warning(
                "[%s] collection %r create failed: %s",
                library_name, dest_name, exc,
            )


def _replace_dest_only_sweep(
    adapter: MediaServerAdapter,
    *,
    libraries: List[Dict[str, Any]],
    user_context: UserContext,
    result: RestoreResult,
    include_playlists: bool,
    include_collections: bool,
    apply_col_scope_prefix: bool,
    logger: logging.Logger,
) -> None:
    """Replace-mode finale: delete destination playlists / collections
    absent from the source. Mirrors the orchestration-level sweep that
    lives in :mod:`services.restore.plex_native` for the Plex-direct path so a
    Jellyfin / Emby destination ends up an exact mirror in Replace mode
    too.

    Playlists: identified by ``name``. Source titles are collected
    across every library in this restore so a multi-library run sees
    one consolidated set before deciding what to delete on the dest.

    Collections: identified by the *destination-side* name, which on
    Jellyfin / Emby is prefixed by source library (D-COL-SCOPE) to
    prevent BoxSet name collisions across libraries. The sweep applies
    the same prefix so the comparison stays apples-to-apples — without
    it, a collection whose source name was ``Marvel`` would compare
    against a dest name ``Movies / Marvel`` and always look orphaned.
    """
    target_backend = (adapter.backend or "plex").lower()
    needs_collection_prefix = (
        apply_col_scope_prefix and target_backend in ("jellyfin", "emby")
    )

    # The set of destination library_ids that participated in this
    # restore. Used to scope playlist + collection deletion so an
    # unrelated dest-only library's containers are never touched.
    # Without this, a Replace run on source "Music" against a
    # destination that also has a separate "Audio Files" library
    # would wipe out the latter's playlists, because scoping by
    # Plex's broad playlistType (audio) rather than the actual
    # library id is too coarse.
    #
    # Cross-backend mapping note: the payload carries source-side
    # library identifiers but the dest sweep must check against
    # dest-side identifiers. We bridge that by matching the source
    # library NAME (which is what the engine already uses to pick
    # destination libraries everywhere else) to the dest's
    # ``LibrarySpec.library_id``.
    restored_library_ids: set = set()
    try:
        _dest_libs_by_name = {
            (lib.name or ""): lib for lib in (adapter.list_libraries() or [])
        }
    except Exception as exc:
        logger.warning(
            "Replace sweep: list_libraries failed (%s); skipping sweep "
            "(scope undeterminable).", exc,
        )
        _dest_libs_by_name = {}
    for lib in libraries:
        src_name = str(lib.get("library") or "")
        if src_name and src_name in _dest_libs_by_name:
            restored_library_ids.add(
                str(_dest_libs_by_name[src_name].library_id)
            )
        else:
            # Fall back to the payload's own library_id when name
            # match fails — same-backend transfers carry matching ids.
            lib_id = lib.get("library_id") or lib.get("library_section_id")
            if lib_id is not None and str(lib_id):
                restored_library_ids.add(str(lib_id))

    # ── Playlist sweep (admin context + per-managed-user) ───────────────
    if include_playlists and restored_library_ids:
        source_playlist_titles: set = set()
        for lib in libraries:
            for pl in (lib.get("playlists") or []):
                name = (pl.get("name") or "").strip()
                if name:
                    source_playlist_titles.add(name)
            # Per-user playlists count toward the same dest-only
            # consideration set so a managed user's dest-only playlist
            # also gets pruned in Replace mode.
            for udata in (lib.get("users") or {}).values():
                if not isinstance(udata, dict):
                    continue
                for pl in (udata.get("playlists") or []):
                    name = (pl.get("name") or "").strip()
                    if name:
                        source_playlist_titles.add(name)

        # Build the contexts we need to sweep. The admin / owner
        # context catches owner-scope playlists; per-managed-user
        # contexts catch user-private playlists on backends where
        # ``list_playlists(owner_ctx)`` doesn't see them (Jellyfin /
        # Emby per-user playlists). On Plex, ``list_playlists`` is
        # server-wide so the extra per-user passes are mostly no-ops
        # but the same delete is idempotent (returns failure for a
        # missing id).
        contexts_to_sweep: List[UserContext] = [user_context]
        try:
            for u in adapter.list_users() or []:
                if u.role == "owner":
                    continue
                if u.backend_user_id and u.backend_user_id != user_context.backend_user_id:
                    contexts_to_sweep.append(UserContext(
                        backend_user_id=u.backend_user_id,
                        username=u.username,
                        auth_token=user_context.auth_token,
                    ))
        except Exception as exc:
            logger.debug(
                "Replace sweep: list_users failed (%s); per-user pass "
                "skipped.", exc,
            )

        seen_playlist_ids: set = set()
        for ctx in contexts_to_sweep:
            try:
                dest_playlists = adapter.list_playlists(ctx)
            except Exception as exc:
                logger.warning(
                    "Replace sweep: list_playlists failed for context %r (%s); "
                    "skipping this context.", ctx.backend_user_id, exc,
                )
                continue
            for spec in dest_playlists:
                if spec.playlist_id in seen_playlist_ids:
                    continue
                title = (spec.name or "").strip()
                if not title or title in source_playlist_titles:
                    continue
                # Library-scope check: the playlist's items must all
                # live inside libraries we restored. An empty playlist
                # or one with items missing ``library_id`` is skipped
                # conservatively — we can't classify its scope so we
                # don't risk deleting it.
                item_libs = {
                    str(it.library_id) for it in spec.items
                    if getattr(it, "library_id", None)
                }
                if not item_libs:
                    logger.debug(
                        "Replace sweep: playlist %r has no resolvable "
                        "library_id on items; skipping (conservative).",
                        title,
                    )
                    continue
                if not item_libs.issubset(restored_library_ids):
                    logger.debug(
                        "Replace sweep: playlist %r has items outside "
                        "restored library scope (item libs=%s, "
                        "restored=%s); skipping.",
                        title, sorted(item_libs),
                        sorted(restored_library_ids),
                    )
                    continue
                wr = adapter.delete_playlist(
                    spec.playlist_id, user_context=ctx,
                )
                seen_playlist_ids.add(spec.playlist_id)
                if wr.success:
                    result.playlists.written += 1
                    logger.info(
                        "Replace: deleted destination-only playlist %r (id=%s).",
                        title, spec.playlist_id,
                    )
                elif wr.unsupported:
                    result.playlists.unsupported += 1
                    if wr.detail and wr.detail not in result.playlists.errors:
                        result.playlists.errors.append(wr.detail)
                else:
                    result.playlists.failed += 1
                    detail = (
                        f"replace-sweep: delete playlist {title!r} failed: "
                        f"{wr.detail or 'unknown'}"
                    )
                    result.playlists.errors.append(detail)
                    logger.warning(detail)

    # ── Collection sweep ────────────────────────────────────────────────
    if include_collections and restored_library_ids:
        source_collection_titles: set = set()
        for lib in libraries:
            source_lib = str(lib.get("library") or "")
            for c in (lib.get("collections") or []):
                raw_name = (c.get("name") or "").strip()
                if not raw_name:
                    continue
                if (
                    needs_collection_prefix
                    and source_lib
                    and not raw_name.startswith(source_lib + " /")
                ):
                    source_collection_titles.add(f"{source_lib} / {raw_name}")
                else:
                    source_collection_titles.add(raw_name)
        try:
            dest_collections = adapter.list_collections()
        except Exception as exc:
            logger.warning(
                "Replace sweep: list_collections failed (%s); skipping collection sweep.",
                exc,
            )
            dest_collections = []
        for spec in dest_collections:
            title = (spec.name or "").strip()
            if not title or title in source_collection_titles:
                continue
            # Collections on Plex are inherently library-scoped via
            # ``CollectionSpec.library_id``. On Jellyfin / Emby
            # BoxSets are server-wide and ``library_id`` is None; for
            # those backends we fall back to scoping by the items'
            # library_id (same approach as the playlist sweep). If a
            # collection has no items AND no library_id, the scope is
            # indeterminate — skip conservatively.
            scope_lib: Optional[str] = (
                str(spec.library_id) if spec.library_id is not None else None
            )
            item_libs = {
                str(it.library_id) for it in spec.items
                if getattr(it, "library_id", None)
            }
            if scope_lib is not None:
                if scope_lib not in restored_library_ids:
                    continue
            elif item_libs:
                if not item_libs.issubset(restored_library_ids):
                    continue
            else:
                # No library info at all — don't delete.
                continue
            wr = adapter.delete_collection(spec.collection_id)
            if wr.success:
                result.collections.written += 1
                logger.info(
                    "Replace: deleted destination-only collection %r (id=%s).",
                    title, spec.collection_id,
                )
            elif wr.unsupported:
                result.collections.unsupported += 1
                if wr.detail and wr.detail not in result.collections.errors:
                    result.collections.errors.append(wr.detail)
            else:
                result.collections.failed += 1
                detail = (
                    f"replace-sweep: delete collection {title!r} failed: "
                    f"{wr.detail or 'unknown'}"
                )
                result.collections.errors.append(detail)
                logger.warning(detail)


def _resolve_destination_item(
    adapter: MediaServerAdapter, guids,
    row: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """Resolve a source row to a destination ``backend_item_id``.

    Tier 1 — GUID: ``adapter.resolve_by_guids``. A backend that
    hasn't implemented it (Plex's PlexAdapter today) returns None
    cleanly.

    Tier 2 - hierarchy: when the GUID match misses AND ``row`` carries
    hierarchy fields (show_title / parent_index / episode_index for
    episodes; artist / album for tracks) AND the adapter exposes
    ``resolve_by_hierarchy``, fall back to a hierarchy match. This
    gives cross-backend Plex->Jellyfin/Emby restore a second axis
    when the two servers' metadata agents produced no shared GUID.
    Best-effort: any failure returns None and the caller logs the
    skip exactly as before."""
    try:
        result = adapter.resolve_by_guids(tuple(guids))
        if result:
            return result
    except Exception:
        pass
    # Tier 2 — hierarchy fallback.
    if row is None:
        return None
    item_type = str(row.get("type") or "")
    if item_type not in ("episode", "track"):
        return None
    resolve_hier = getattr(adapter, "resolve_by_hierarchy", None)
    if not callable(resolve_hier):
        return None

    def _int_or_none(v: Any) -> Optional[int]:
        if v in (None, ""):
            return None
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    try:
        return resolve_hier(
            item_type=item_type,
            title=str(row.get("title") or ""),
            show_title=str(row.get("show_title") or ""),
            season_number=_int_or_none(row.get("parent_index")),
            episode_number=_int_or_none(row.get("episode_index")),
            artist=str(row.get("artist") or ""),
            album=str(row.get("album") or ""),
        ) or None
    except Exception:
        return None


def _bump_from_result(
    counts: RestoreCounts,
    result: WriteResult,
    library_name: str,
    row: Dict[str, Any],
    logger: logging.Logger,
    kind: str,
) -> None:
    if result.success:
        counts.written += 1
        return
    if result.unsupported:
        counts.unsupported += 1
        if result.detail and result.detail not in counts.errors:
            counts.errors.append(result.detail)
        return
    counts.failed += 1
    detail = (
        f"[{library_name}] {kind} write failed for {row.get('title')!r}: "
        f"{result.detail or 'unknown error'}"
    )
    counts.errors.append(detail)
    logger.warning(detail)


__all__ = [
    "RestoreCounts",
    "RestoreResult",
    "restore_payload_adapter",
    "dry_run_resolve_users",
    "DryRunReport",
    "UserResolutionRecord",
    "UserRowCounts",
    "DestUserOption",
    "LibraryTypeNote",
    "TombstoneNote",
    "ZeroRowSkip",
    "PROPOSED_RESOLUTION_VALUES",
]
