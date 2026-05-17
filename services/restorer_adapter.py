"""
Backend-agnostic restore engine. Used for Jellyfin / Emby
destinations; Plex still routes through ``services/restorer.py`` to
keep its plexapi-specific code paths untouched.

Scope (MVP for PR-Backends Phase 1)
-----------------------------------
Per-library watch_history + ratings restore. The engine consumes a
snapshot payload produced by either ``services/snapshotter.py``
(Plex source) or ``services/snapshotter_adapter.py`` (Jellyfin / Emby
source) - both produce the same per-library dict shape so this
restore code is source-agnostic. Cross-backend (Plex source ->
Jellyfin destination etc.) is the Phase 2 deliverable in
PR-CrossPolish; this MVP works for same-backend or trivially
matching content where GUID resolution succeeds.

Out of scope for MVP (deferred to PR-CrossPolish):
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


log = logging.getLogger("plexmigrate.services.restorer_adapter")


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
    # PR-Phase-3: per-user fan-out on restore. When True (default),
    # the engine walks each library's ``users`` map and replays each
    # source user's data against the matching destination user.
    # End user-supplied ``user_filter`` narrows to a subset; missing
    # destination users are logged and skipped (no auto-create -
    # that's the D-OWNER flow end user-confirms via the modal).
    include_managed_users: bool = True,
    user_filter: Optional[List[str]] = None,
    # PR-CrossPolish D-RATE (locked in Plan section 11.5): when source
    # rating is >= ``favorite_threshold``, also write IsFavorite=true
    # on backends that expose it. Plex's set_favorite returns
    # not_supported and the restorer counts those separately so the
    # end user isn't surprised by "skipped" rows.
    favorite_threshold: float = 5.0,
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
    # Plan[CROSS-PLATFORM-PREFLIGHT] step 6: belt-and-braces
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
    # Plan[CROSS-PLATFORM-PREFLIGHT] follow-up: end user-authored per-job
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

    # Plan[MIXED-MEDIA-PLAYLISTS]-2026-05-16: pick up the run-level
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
    # Plan[IDENTITY-UTILIZATION-AUDIT]-2026-05-17 R-3: ALSO pull the
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
            )
        if include_ratings:
            _apply_ratings(
                adapter, lib_name, lib.get("ratings") or [],
                user_context, result.ratings, stop_event, log_,
                favorite_threshold=favorite_threshold,
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

        # Per-user fan-out (Phase 3). The library payload's ``users``
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
                include_playlists=include_playlists,
                include_collections=include_collections,
                apply_col_scope_prefix=apply_col_scope_prefix,
                source_server_id=source_server_id,
                dest_server_id=dest_server_id,
                cross_platform_resolutions=cross_platform_resolutions,
                source_service_type=source_service_type,
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
) -> None:
    for row in rows:
        if stop_event is not None and stop_event.is_set():
            return
        view_count = int(row.get("view_count") or 0)
        if view_count <= 0:
            counts.skipped_zero_state += 1
            continue
        guids = tuple(row.get("guids") or ())
        if not guids:
            counts.skipped_no_match += 1
            continue
        dest_item_id = _resolve_destination_item(adapter, guids)
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
        result = adapter.set_watched(
            item_ref,
            view_count=view_count,
            last_viewed_at=last_viewed_epoch,
            user_context=user_context,
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
) -> None:
    for row in rows:
        if stop_event is not None and stop_event.is_set():
            return
        rating = row.get("user_rating")
        if rating is None or float(rating) <= 0:
            counts.skipped_zero_state += 1
            continue
        guids = tuple(row.get("guids") or ())
        if not guids:
            counts.skipped_no_match += 1
            continue
        dest_item_id = _resolve_destination_item(adapter, guids)
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
        result = adapter.set_rating(
            item_ref,
            float(rating),
            user_context=user_context,
        )
        _bump_from_result(counts, result, library_name, row, logger, "rating")

        # D-RATE: when the source rating clears the favorite threshold,
        # also flip IsFavorite on the destination. On backends without
        # a per-user favorite (Plex) set_favorite returns
        # ``not_supported`` and we silently skip it; the numeric
        # Rating write above is the only path that landed data.
        if float(rating) >= favorite_threshold:
            fav_result = adapter.set_favorite(
                item_ref, True, user_context=user_context,
            )
            if fav_result.success:
                # Don't double-count writes - the rating write already
                # bumped ``written``. The favorite is a paired write,
                # not a separate row.
                pass
            elif fav_result.unsupported:
                # Expected on Plex; record once per-library to avoid
                # spamming errors with the same message.
                msg = (
                    f"[{library_name}] favorite skipped: "
                    f"{fav_result.detail or 'backend has no per-user favorite'}"
                )
                if msg not in counts.errors:
                    counts.errors.append(msg)
            else:
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

    Thin shim over :func:`services.user_resolution.resolve_destination_user`
    (the USER-MGMT-IDENTITY-AUDIT shared helper). Signature is
    preserved EXACTLY for backward compatibility with every existing
    caller in this module + developer's tests; the two new keyword args
    (``source_backend_user_id``, ``source_service_type``) are optional
    and default to None.

    See the user_resolution module docstring for the full 5-step
    chain and the ``strict_identity_resolution`` tunable that gates
    the username fallback.
    """
    from services.user_resolution import resolve_destination_user
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
    include_playlists: bool = False,
    include_collections: bool = False,
    apply_col_scope_prefix: bool = True,
    source_server_id: str = "",
    dest_server_id: str = "",
    cross_platform_resolutions: Optional[Dict[str, Any]] = None,
    # Plan[IDENTITY-UTILIZATION-AUDIT]-2026-05-17 R-3: source service
    # type from snapshot_meta.backend so the resolver chain step 2
    # (backend_user_id direct match within service_type) can fire.
    # None when the snapshot doesn't carry the backend field.
    source_service_type: Optional[str] = None,
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
    # Tombstone write-side filter (Plan[CROSS-PLATFORM-PREFLIGHT] Q-4):
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
        # Plan[IDENTITY-UTILIZATION-AUDIT]-2026-05-17 R-3: forward the
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
        # appear here in Phase 3 - per-user fan-out for Plex is
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
            )
        if include_ratings:
            _apply_ratings(
                adapter, library_name,
                list(rows_block.get("ratings") or []),
                per_user_ctx, result.ratings, stop_event, logger,
                favorite_threshold=favorite_threshold,
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

    Mixed-media handling (Plan[MIXED-MEDIA-PLAYLISTS]-2026-05-16):
    when ``mixed_media_config`` is provided, the rows are run through
    the strategy driver before write so mixed-source playlists are
    skipped / split / collapsed-to-dominant per the end user's choice
    BEFORE the destination resolution runs.

    No idempotency check today - re-running creates duplicates. That
    matches the Plex restorer's behaviour and is on the end user's
    list for a future "deduplicate at write" tunable."""
    if mixed_media_config is not None:
        from services import mixed_media as _mm
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
            dest_id = _resolve_destination_item(adapter, guids)
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
            dest_id = _resolve_destination_item(adapter, guids)
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


def _resolve_destination_item(
    adapter: MediaServerAdapter, guids,
) -> Optional[str]:
    """Wrap ``adapter.resolve_by_guids`` so a backend that hasn't
    implemented it (Plex's PlexAdapter today) returns None cleanly."""
    try:
        result = adapter.resolve_by_guids(tuple(guids))
        return result if result else None
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


# ── Preflight dry-run (Plan[CROSS-PLATFORM-PREFLIGHT] step 1) ────────────────
#
# Pure resolution dry-run. Walks the payload's per-user blocks across
# every library and computes what _apply_per_user_block would do at
# write time, without touching the destination. Powers the
# POST /api/jobs/cross-platform-preflight + schedules variant endpoints.

@dataclass(frozen=True)
class DestUserOption:
    backend_user_id: str
    username: str
    role: str             # 'owner' | 'admin' | 'managed'
    is_tombstoned: bool


@dataclass(frozen=True)
class UserRowCounts:
    watch_history: int
    ratings: int
    playlists: int
    collections: int


@dataclass(frozen=True)
class UserResolutionRecord:
    source_username: str
    source_role: str               # 'owner' | 'admin' | 'managed'
    source_row_counts: UserRowCounts
    proposed_resolution: str       # one of the 8 PROPOSED_RESOLUTION_VALUES
    proposed_dest_user_id: Optional[str]
    proposed_dest_username: Optional[str]
    proposed_dest_role: Optional[str]
    needs_ack: bool
    blocks_submit: bool
    warnings: List[str]
    available_dest_users: List[DestUserOption]


@dataclass(frozen=True)
class LibraryTypeNote:
    source_library: str
    source_type: str
    dest_type_used: str
    message: str


@dataclass(frozen=True)
class TombstoneNote:
    dest_username: str
    reason: str


@dataclass(frozen=True)
class ZeroRowSkip:
    source_username: str
    empty_signals: List[str]
    filter_flags_in_effect: List[str]
    message: str


@dataclass(frozen=True)
class DryRunReport:
    source_kind: str               # 'plex' | 'jellyfin' | 'emby' | 'unknown'
    dest_kind: str
    source_server_id: str
    dest_server_id: str
    is_cross_platform: bool
    source_admin_count: int
    dest_admin_count: int
    resolutions: List[UserResolutionRecord]
    smart_playlists_skipped: int
    smart_playlist_names: List[str]
    library_type_notes: List[LibraryTypeNote]
    tombstoned_users_excluded: List[TombstoneNote]
    zero_row_skipped: List[ZeroRowSkip]
    overall_verdict: str           # 'ok' | 'ack_required' | 'blocked'
    blocking_reasons: List[str]


PROPOSED_RESOLUTION_VALUES = frozenset({
    "identity_map",
    "direct_match",
    "single_admin_fallback",
    "role_flip_ack",
    "tombstone_blocked",
    "zero_row_skip",
    "no_match",
    "multi_admin_collapse",
})


def _normalise_dest_role(user_spec: Any, dest_kind: str) -> str:
    """Translate a UserSpec's role/is_admin into the end user-facing
    three-tier label. Plex admins surface as 'owner' (Plex has one
    owner per server); J/E admins surface as 'admin' (multi-admin
    capable). Non-admins surface as 'managed' regardless of backend.
    """
    if not getattr(user_spec, "is_admin", False):
        return "managed"
    return "owner" if (dest_kind or "").lower() == "plex" else "admin"


def _load_tombstoned_dest_usernames(
    dest_server_id: str, logger: logging.Logger,
) -> set:
    """Read the tombstoned-or-globally-hidden subset of managed_users
    for the destination server. Returns lowercased usernames.

    Defensive: empty set on empty server_id or any failure. Tombstone
    enforcement is best-effort - if the lookup fails the engine still
    works against the live roster, it just doesn't filter."""
    sid = (dest_server_id or "").strip()
    if not sid:
        return set()
    try:
        from server.media_db import list_managed_users
        rows = list_managed_users(sid, include_hidden=True) or []
    except Exception as exc:
        logger.debug("tombstone lookup failed: %s", exc)
        return set()
    return {
        (r.get("username") or "").strip().lower()
        for r in rows
        if r.get("hidden_scope") != "none"
    }


def dry_run_resolve_users(
    payload: Dict[str, Any],
    adapter: MediaServerAdapter,
    *,
    source_server_id: str = "",
    dest_server_id: str = "",
    include_watch_history: bool = True,
    include_ratings: bool = True,
    include_playlists: bool = True,
    include_collections: bool = True,
    include_managed_users: bool = True,
    user_filter: Optional[List[str]] = None,
    cross_platform_resolutions: Optional[Dict[str, Any]] = None,
    logger: Optional[logging.Logger] = None,
) -> DryRunReport:
    """Compute every per-user resolution the restore engine would
    fire for this (payload, destination) pair, without writing.

    The result powers the cross-platform preflight modal: end user
    sees what would happen to each source user before any destructive
    operation. See Plan[CROSS-PLATFORM-PREFLIGHT]-2026-05-16.md for
    the verdict semantics and Plan[UI-FOR-PREFLIGHT]-2026-05-16.md
    for the data shape this maps to on the wire."""
    log_ = logger or log

    # Auto-derive source_server_id from snapshot_meta when caller
    # didn't supply one. Current-shape .db snapshots always populate
    # snapshot_meta.server_id at capture time; the auto-derive is the
    # convenience path for callers (CLI, tests) that have a payload
    # but no separate id to hand in.
    meta: Dict[str, Any] = {}
    if isinstance(payload, dict):
        m = payload.get("snapshot_meta") or {}
        if isinstance(m, dict):
            meta = m
    if not source_server_id:
        source_server_id = str(meta.get("server_id") or "")

    source_kind = (str(meta.get("backend") or "").strip().lower()) or "unknown"
    dest_kind = ((getattr(adapter, "backend", "") or "").strip().lower()) or "unknown"
    is_cross_platform = (
        source_kind in ("plex", "jellyfin", "emby")
        and dest_kind in ("plex", "jellyfin", "emby")
        and source_kind != dest_kind
    )

    # Destination roster, split into active vs tombstoned subsets.
    try:
        dest_users_raw = adapter.list_users() or []
    except Exception as exc:
        log_.warning("dry_run_resolve_users: list_users failed: %s", exc)
        dest_users_raw = []

    tombstoned_set = _load_tombstoned_dest_usernames(dest_server_id, log_)
    dest_users_active = [
        u for u in dest_users_raw
        if (u.username or "").strip().lower() not in tombstoned_set
    ]
    dest_admins = [u for u in dest_users_active if getattr(u, "is_admin", False)]
    dest_by_username = {
        (u.username or "").strip().lower(): u
        for u in dest_users_active
        if u.username
    }
    dest_tombstoned_by_username = {
        (u.username or "").strip().lower(): u
        for u in dest_users_raw
        if u.username and (u.username or "").strip().lower() in tombstoned_set
    }

    available_dest_users = [
        DestUserOption(
            backend_user_id=u.backend_user_id or "",
            username=u.username,
            role=_normalise_dest_role(u, dest_kind),
            is_tombstoned=False,
        )
        for u in dest_users_active
    ]

    # Walk libraries; aggregate per-source-user row counts across
    # them and collect smart-playlist names.
    libraries_iter = []
    if isinstance(payload, dict) and "libraries" in payload:
        libraries_iter = payload.get("libraries") or []
    elif isinstance(payload, dict):
        libraries_iter = [payload]

    per_user_counts: Dict[str, Dict[str, int]] = {}
    per_user_role: Dict[str, str] = {}
    # Per-source-user backend_user_id (when present in the payload).
    # Plan[IDENTITY-UTILIZATION-AUDIT]-2026-05-17 R-3: forward this to
    # the resolver so step 2 of the resolution chain (backend_user_id
    # direct match) can fire on cross-server restores.
    per_user_backend_user_id: Dict[str, str] = {}
    smart_playlist_names: List[str] = []

    for lib in libraries_iter:
        if not isinstance(lib, dict):
            continue
        for pl in (lib.get("playlists") or []):
            if isinstance(pl, dict) and pl.get("is_smart"):
                name = str(pl.get("name") or "")
                if name and name not in smart_playlist_names:
                    smart_playlist_names.append(name)
        users_block = lib.get("users") or {}
        if not isinstance(users_block, dict):
            continue
        for u_name, u_payload in users_block.items():
            if not isinstance(u_name, str) or not u_name.strip():
                continue
            if not isinstance(u_payload, dict):
                u_payload = {}
            counts = per_user_counts.setdefault(u_name, {
                "watch_history": 0, "ratings": 0,
                "playlists": 0, "collections": 0,
            })
            counts["watch_history"] += len(u_payload.get("watch_history") or [])
            counts["ratings"] += len(u_payload.get("ratings") or [])
            counts["playlists"] += len(u_payload.get("playlists") or [])
            counts["collections"] += len(u_payload.get("collections") or [])
            role = str(u_payload.get("role") or "").strip().lower()
            if role and u_name not in per_user_role:
                per_user_role[u_name] = role
            # Capture backend_user_id once per user (first non-empty
            # value wins). The snapshot serializer emits this field
            # in every per-user block when known; legacy payloads
            # without it leave the dict empty and step 2 short-
            # circuits naturally inside the resolver.
            buid = str(u_payload.get("backend_user_id") or "").strip()
            if buid and u_name not in per_user_backend_user_id:
                per_user_backend_user_id[u_name] = buid
            for pl in (u_payload.get("playlists") or []):
                if isinstance(pl, dict) and pl.get("is_smart"):
                    name = str(pl.get("name") or "")
                    if name and name not in smart_playlist_names:
                        smart_playlist_names.append(name)

    source_admin_count_seen = sum(
        1 for r in per_user_role.values()
        if r in ("owner", "admin")
    )

    # Per-job end user decisions: same shape and semantics the engine
    # applies at write time. Drop decisions skip the user; Map
    # decisions feed the resolver as priority-0 overrides so the
    # verdict reflects what would actually happen.
    dropped_usernames, per_job_overrides = _extract_per_job_overrides(
        cross_platform_resolutions, dest_server_id,
    )

    filter_set: Optional[set] = None
    if user_filter is not None:
        filter_set = {
            u.strip().lower() for u in user_filter
            if isinstance(u, str) and u.strip()
        }
    if dropped_usernames:
        if filter_set is None:
            filter_set = {
                (k or "").strip().lower()
                for k in per_user_counts.keys()
                if isinstance(k, str) and k.strip()
            } - dropped_usernames
        else:
            filter_set = filter_set - dropped_usernames

    # When fan-out is disabled the per-user resolution doesn't apply.
    # Modal still wants the smart-playlist + library-type info.
    if not include_managed_users:
        return DryRunReport(
            source_kind=source_kind,
            dest_kind=dest_kind,
            source_server_id=source_server_id,
            dest_server_id=dest_server_id,
            is_cross_platform=is_cross_platform,
            source_admin_count=source_admin_count_seen,
            dest_admin_count=len(dest_admins),
            resolutions=[],
            smart_playlists_skipped=len(smart_playlist_names),
            smart_playlist_names=smart_playlist_names,
            library_type_notes=[],
            tombstoned_users_excluded=[],
            zero_row_skipped=[],
            overall_verdict="ok",
            blocking_reasons=[],
        )

    resolutions: List[UserResolutionRecord] = []
    tombstoned_users_excluded: List[TombstoneNote] = []
    zero_row_skipped: List[ZeroRowSkip] = []
    blocking_reasons: List[str] = []
    # Track multi-admin collapse: dest_user_id -> [source usernames].
    admin_resolution_targets: Dict[str, List[str]] = {}

    for source_username, counts in per_user_counts.items():
        normalised = source_username.strip().lower()
        if filter_set is not None and normalised not in filter_set:
            continue
        source_role = per_user_role.get(source_username, "managed")

        # Effective row counts AFTER the end user's filter flags.
        eff = {
            "watch_history": counts["watch_history"] if include_watch_history else 0,
            "ratings": counts["ratings"] if include_ratings else 0,
            "playlists": counts["playlists"] if include_playlists else 0,
            "collections": counts["collections"] if include_collections else 0,
        }
        if (eff["watch_history"] + eff["ratings"]
                + eff["playlists"] + eff["collections"]) == 0:
            empty_signals = [
                k for k, v in counts.items() if v == 0
            ]
            filter_flags = []
            if not include_watch_history:
                filter_flags.append("include_watch_history=false")
            if not include_ratings:
                filter_flags.append("include_ratings=false")
            if not include_playlists:
                filter_flags.append("include_playlists=false")
            if not include_collections:
                filter_flags.append("include_collections=false")
            zero_row_skipped.append(ZeroRowSkip(
                source_username=source_username,
                empty_signals=empty_signals,
                filter_flags_in_effect=filter_flags,
                message=(
                    f"Skipped {source_username!r}: no rows"
                    + (f" ({', '.join(filter_flags)})" if filter_flags else "")
                ),
            ))
            continue

        # Plan[IDENTITY-UTILIZATION-AUDIT]-2026-05-17 R-3: forward the
        # source user's backend_user_id + the snapshot's source service
        # type so step 2 of the resolution chain (backend_user_id direct
        # match within service_type) can fire. Missing values short-
        # circuit the step naturally inside the resolver.
        dest_user = _resolve_destination_user(
            source_username=source_username,
            source_role=source_role,
            dest_by_username=dest_by_username,
            dest_admins=dest_admins,
            source_server_id=source_server_id,
            dest_server_id=dest_server_id,
            logger=log_,
            per_job_overrides=per_job_overrides,
            source_backend_user_id=per_user_backend_user_id.get(source_username) or None,
            source_service_type=source_kind if source_kind != "unknown" else None,
        )

        warnings: List[str] = []
        proposed_resolution = "no_match"
        proposed_dest_user_id: Optional[str] = None
        proposed_dest_username: Optional[str] = None
        proposed_dest_role: Optional[str] = None

        if dest_user is None and normalised in dest_tombstoned_by_username:
            tomb = dest_tombstoned_by_username[normalised]
            proposed_resolution = "tombstone_blocked"
            tombstoned_users_excluded.append(TombstoneNote(
                dest_username=tomb.username,
                reason=(
                    f"Source user {source_username!r} would map to tombstoned "
                    f"destination user {tomb.username!r}. Unhide under "
                    f"Servers > User Management to enable writes."
                ),
            ))
        elif dest_user is not None:
            proposed_dest_user_id = dest_user.backend_user_id or ""
            proposed_dest_username = dest_user.username
            proposed_dest_role = _normalise_dest_role(dest_user, dest_kind)
            # Classify which resolution path fired.
            # Priority-0: per-job override (end user picked Map in the
            # modal without persist_as_identity_map). Surface as
            # identity_map (semantically equivalent - explicit end user
            # decision) with a warning naming the non-persisted nature.
            hit_via_per_job_override = (
                normalised in per_job_overrides
                and per_job_overrides[normalised] == proposed_dest_user_id
            )
            hit_via_map = False
            if not hit_via_per_job_override and source_server_id and dest_server_id:
                try:
                    from server.media_db import get_identity_maps_for_user
                    for link in get_identity_maps_for_user(
                        source_server_id, source_username,
                    ) or []:
                        if link.get("other_server_id") != dest_server_id:
                            continue
                        target_handle = (link.get("other_user_handle") or "").strip().lower()
                        if target_handle == (dest_user.username or "").strip().lower():
                            hit_via_map = True
                            break
                except Exception:
                    pass
            if hit_via_per_job_override:
                proposed_resolution = "identity_map"
                warnings.append(
                    f"Applied via per-job override (operator Map decision "
                    f"not persisted as identity-map entry). Tick "
                    f"'Save my decisions as identity-map entries' next "
                    f"run to skip the prompt."
                )
            elif hit_via_map:
                proposed_resolution = "identity_map"
            elif normalised == (dest_user.username or "").strip().lower():
                # Direct name match. Role flip check.
                source_admin_tier = source_role in ("owner", "admin")
                dest_admin_tier = proposed_dest_role in ("owner", "admin")
                if source_admin_tier != dest_admin_tier:
                    proposed_resolution = "role_flip_ack"
                    warnings.append(
                        f"Direct name match but role differs: source role "
                        f"{source_role!r}, destination role {proposed_dest_role!r}. "
                        f"Confirm intent before writing."
                    )
                else:
                    proposed_resolution = "direct_match"
            elif source_role in ("owner", "admin") and len(dest_admins) == 1:
                proposed_resolution = "single_admin_fallback"
                warnings.append(
                    f"Resolved via single-admin convention. Add an identity-map "
                    f"entry to lock this in and skip the prompt next run."
                )

            if source_role in ("owner", "admin") and proposed_dest_user_id:
                admin_resolution_targets.setdefault(
                    proposed_dest_user_id, []
                ).append(source_username)

        # Verdict per row.
        needs_ack = False
        blocks_submit = False
        if proposed_resolution in ("identity_map", "direct_match"):
            pass
        elif proposed_resolution in (
            "single_admin_fallback", "role_flip_ack", "tombstone_blocked"
        ):
            needs_ack = True
        elif proposed_resolution == "no_match":
            if source_role in ("owner", "admin"):
                blocks_submit = True
                blocking_reasons.append(
                    f"Source {source_role} {source_username!r} has no "
                    f"destination resolution (no identity map, no direct name "
                    f"match, no single-admin fallback). Add a mapping, create "
                    f"on the destination, or drop the user."
                )
            else:
                needs_ack = True

        resolutions.append(UserResolutionRecord(
            source_username=source_username,
            source_role=source_role,
            source_row_counts=UserRowCounts(
                watch_history=counts["watch_history"],
                ratings=counts["ratings"],
                playlists=counts["playlists"],
                collections=counts["collections"],
            ),
            proposed_resolution=proposed_resolution,
            proposed_dest_user_id=proposed_dest_user_id,
            proposed_dest_username=proposed_dest_username,
            proposed_dest_role=proposed_dest_role,
            needs_ack=needs_ack,
            blocks_submit=blocks_submit,
            warnings=warnings,
            available_dest_users=available_dest_users,
        ))

    # Multi-admin collapse second pass: when multiple source admins
    # resolve to the same destination user, surface as an ack-class
    # collapse warning so the end user can choose to refine.
    for dest_uid, src_names in admin_resolution_targets.items():
        if len(src_names) <= 1:
            continue
        collapse_msg = (
            f"Multi-admin collapse: source admins "
            f"{', '.join(repr(n) for n in src_names)} all map to one "
            f"destination user; their artifacts will share one account."
        )
        for idx, r in enumerate(resolutions):
            if (r.source_username in src_names
                    and r.proposed_dest_user_id == dest_uid):
                new_warnings = list(r.warnings) + [collapse_msg]
                resolutions[idx] = UserResolutionRecord(
                    source_username=r.source_username,
                    source_role=r.source_role,
                    source_row_counts=r.source_row_counts,
                    proposed_resolution="multi_admin_collapse",
                    proposed_dest_user_id=r.proposed_dest_user_id,
                    proposed_dest_username=r.proposed_dest_username,
                    proposed_dest_role=r.proposed_dest_role,
                    needs_ack=True,
                    blocks_submit=False,
                    warnings=new_warnings,
                    available_dest_users=r.available_dest_users,
                )

    overall_verdict = "ok"
    if any(r.blocks_submit for r in resolutions):
        overall_verdict = "blocked"
    elif any(r.needs_ack for r in resolutions):
        overall_verdict = "ack_required"

    return DryRunReport(
        source_kind=source_kind,
        dest_kind=dest_kind,
        source_server_id=source_server_id,
        dest_server_id=dest_server_id,
        is_cross_platform=is_cross_platform,
        source_admin_count=source_admin_count_seen,
        dest_admin_count=len(dest_admins),
        resolutions=resolutions,
        smart_playlists_skipped=len(smart_playlist_names),
        smart_playlist_names=smart_playlist_names,
        library_type_notes=[],  # TODO: per-library compatibility checks
        tombstoned_users_excluded=tombstoned_users_excluded,
        zero_row_skipped=zero_row_skipped,
        overall_verdict=overall_verdict,
        blocking_reasons=blocking_reasons,
    )


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
