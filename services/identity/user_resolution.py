"""
Cross-server user resolution: pick the destination user that should
receive a source user's per-user payload during snapshot/restore,
direct transfer, fan-out, and playlist migration.

Background
----------
Before this module landed, the priority chain lived only inside the
adapter restorer (``services/restorer_adapter.py:_resolve_destination_user``)
and was Jellyfin / Emby-only. The Plex-to-Plex restorer and the playlist
orchestrator used case-insensitive username matching as their primary
mechanism and ignored ``user_identity_map`` entirely. The
USER-MGMT-IDENTITY-AUDIT Finding flagged this; the end user agreed to
re-key identity_map on the new ``app_user_uuid`` anchor and to wire a
shared resolver into every engine path.

The five-step priority chain
----------------------------

  0. Per-job override        (end user picked Map in the preflight modal
                              WITHOUT ticking persist_as_identity_map).
                              Wins over every other path so end user
                              decisions ride through even when no map
                              row exists.

  1. user_identity_map       (authoritative, populated manually OR by
                              :func:`server.media_db.auto_link_identity_map_by_backend_user_id`)

  2. backend_user_id direct  (safety net for transient cases where the
                              auto-link helper has not yet processed a
                              just-synced server; scoped by service_type
                              so a Plex ID never false-links to Jellyfin)

  3. Direct case-insensitive (legacy username match; can be disabled
                              via the ``strict_identity_resolution``
                              tunable for end users who want zero
                              implicit routing)

  4. Single-admin owner      (only when source role is owner AND the
                              destination has exactly one admin; never
                              guesses on a multi-admin destination)

  5. None                    (caller logs + skips with an actionable
                              message)

The previous chain in restorer_adapter.py corresponds to steps
0 + 1 + 3 + 4 of this one. This module adds step 2 (backend_user_id
direct match) AND swaps step 1 from handle-based to UUID-based lookup.
The semantic differences:

  * Step 1 now lifts identity_map rows that key on app_user_uuid (the
    v12 schema). Resolution succeeds even when handles differ across
    servers as long as the underlying UUIDs are linked. The lookup is
    via :func:`server.media_db.get_identity_maps_for_user` which still
    accepts (server_id, user_handle) input - we resolve the source's
    UUID internally and walk the bidirectional map.

  * Step 2 fires when no identity_map row exists yet (e.g. a server
    just added; the auto-link helper has not yet run). The source row
    and a destination row carrying the same (service_type,
    backend_user_id) ARE the same human by the backend's own
    definition; the safety net catches the case without waiting for
    the next sync.

  * Step 3 is skippable. Setting the tunable
    ``strict_identity_resolution=true`` makes the resolver return None
    after step 2; the caller surfaces the unresolved user explicitly
    instead of silently routing via name match.

Public API
----------
:func:`resolve_destination_user` is the entry point every engine
should call. The adapter restorer's existing
``_resolve_destination_user`` becomes a thin compatibility shim over
this helper so its callers and tests stay unchanged.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional


# ── Strict mode toggle ─────────────────────────────────────────────────────

def _strict_mode() -> bool:
    """Read the ``strict_identity_resolution`` tunable; default False.

    Best-effort: any failure reading the tunable (module not loaded,
    settings file missing, etc.) returns False so the resolver falls
    back to its lenient default instead of silently refusing to
    resolve."""
    try:
        from services.tunables import strict_identity_resolution
        return bool(strict_identity_resolution())
    except Exception:
        return False


# ── Resolution chain ──────────────────────────────────────────────────────

def resolve_destination_user(
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
    """Pick the destination user for ``source_username``'s payload.

    ``dest_by_username`` is the destination's user roster keyed by
    lower-cased username; values are ``UserSpec``-like (anything with
    ``username``, ``backend_user_id``, ``role``). ``dest_admins`` is
    the subset whose ``role == 'admin'`` (or ``'owner'`` on Plex).

    ``source_backend_user_id`` and ``source_service_type`` enable
    step 2 (backend_user_id direct match within service_type). If
    omitted, step 2 short-circuits and the chain proceeds to step 3
    as the legacy path did.

    Returns the matched destination user OR ``None`` to indicate the
    caller should log + skip the source user's payload with an
    actionable message.
    """
    # 0. Per-job override (end user Map decision without persistence).
    if per_job_overrides:
        normalised = (source_username or "").strip().lower()
        if normalised in per_job_overrides:
            target_uid = per_job_overrides[normalised]
            for u in dest_by_username.values():
                if (getattr(u, "backend_user_id", "") or "") == target_uid:
                    logger.info(
                        "user resolution: %r resolved to destination "
                        "user_id %r via per-job override.",
                        source_username, target_uid,
                    )
                    return u
            logger.warning(
                "user resolution: per-job override for %r points at "
                "dest_user_id %r which is not in the destination "
                "roster; falling through to standard resolution.",
                source_username, target_uid,
            )

    # 1. user_identity_map lookup (authoritative).
    if source_server_id and dest_server_id:
        try:
            from server.media_db import get_identity_maps_for_user
            for link in get_identity_maps_for_user(
                source_server_id, source_username,
            ) or []:
                if link.get("other_server_id") != dest_server_id:
                    continue
                target_handle = (link.get("other_user_handle") or "").strip().lower()
                if target_handle and target_handle in dest_by_username:
                    logger.info(
                        "user resolution: %r resolved to destination %r "
                        "via identity_map (source=%s).",
                        source_username, target_handle, link.get("source"),
                    )
                    return dest_by_username[target_handle]
        except Exception as exc:
            logger.debug(
                "user resolution: identity_map lookup for %r failed: %s",
                source_username, exc,
            )

    # 2. backend_user_id direct match within same service_type.
    # Fires when no identity_map row exists yet (e.g. just-added
    # server before auto-link has run). Scoped by service_type so a
    # Plex.tv numeric userID can never false-link to a Jellyfin GUID
    # that happens to coerce to the same string.
    if source_backend_user_id and source_service_type:
        bk = str(source_backend_user_id).strip()
        svc = str(source_service_type).strip().lower()
        if bk:
            for u in dest_by_username.values():
                dest_bk = (getattr(u, "backend_user_id", "") or "").strip()
                dest_svc = (
                    getattr(u, "service_type", "")
                    or getattr(u, "backend", "")
                    or ""
                ).strip().lower()
                if dest_bk == bk and dest_svc == svc:
                    logger.info(
                        "user resolution: %r resolved to destination %r "
                        "via backend_user_id direct match "
                        "(service=%s, id=%s; auto-link helper has not "
                        "yet written an identity_map row).",
                        source_username, u.username, svc, bk,
                    )
                    return u

    # Strict mode short-circuits before the username + owner fallbacks.
    if _strict_mode():
        logger.info(
            "user resolution: %r unresolved after identity_map + "
            "backend_user_id (strict_identity_resolution=true; no "
            "fallback to username match).",
            source_username,
        )
        return None

    # 3. Direct case-insensitive match (legacy fallback).
    normalised = (source_username or "").strip().lower()
    if normalised in dest_by_username:
        return dest_by_username[normalised]

    # 4. Owner-role single-admin fallback. Only fires when the source
    #    user is THE plex owner and the destination has exactly one
    #    admin - any ambiguity (multi-admin) forces the end user to
    #    map explicitly so we never silently write to the wrong admin.
    if (source_role or "").strip().lower() == "owner" and len(dest_admins) == 1:
        only_admin = dest_admins[0]
        logger.info(
            "user resolution: source owner %r resolved to destination "
            "admin %r (single-admin convention; add an identity_map "
            "entry to lock this in).",
            source_username, only_admin.username,
        )
        return only_admin

    # 5. None - caller logs + skips.
    return None
