"""Cross-platform restore preflight - the resolution dry-run.

A pure, write-free pass over a snapshot payload that computes what the
adapter restore engine (``services/restorer_adapter.py``) WOULD do to
each source user, without touching the destination. It walks the
payload's per-user blocks across every library and resolves each
source user to a destination user the same way the write engine's
``_apply_per_user_block`` does.

The :class:`DryRunReport` it returns powers the cross-platform
preflight modal (``POST /api/jobs/cross-platform-preflight`` and the
schedules variant): the end user sees every per-user resolution, the
ack-required and submit-blocking cases, smart-playlist skips, and
tombstoned-user exclusions before any destructive write.

Extracted from ``restorer_adapter.py``, which re-exports the public
names so existing
``from services.restorer_adapter import dry_run_resolve_users`` (etc.)
importers are unaffected. The two resolution helpers the dry-run shares
with the write engine - ``_extract_per_job_overrides`` and
``_resolve_destination_user`` - stay in ``restorer_adapter``; the
dry-run imports them locally to avoid an import cycle.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from services.adapters import MediaServerAdapter

log = logging.getLogger("plexmigrate.services.restore_preflight")


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
    operation."""
    log_ = logger or log
    # local import: restorer_adapter imports this module, so a
    # top-level import would cycle. These two resolution helpers are
    # shared with the write engine and stay in restorer_adapter.
    from services.restorer_adapter import (
        _extract_per_job_overrides,
        _resolve_destination_user,
    )

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
    # Forward this to
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

        # Forward the
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
