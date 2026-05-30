"""User resolution and per-user authentication for Playlist Management."""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Optional

from services.adapters import MediaServerAdapter, UserContext, UserSpec
from services.tunables import (
    playlist_mgmt_plex_home_auth_mode,
    strict_identity_resolution,
)


# The logger name is kept identical to playlist_copy's on purpose: the
# log-message text below still reads "playlist_copy: ...", and the audit
# channel should not move just because the code did.
log = logging.getLogger("plexmigrate.services.playlist_copy")


def _user_context_for(
    conn: Any,
    user_spec: UserSpec,
    *,
    role: str = "source",
    server_id: Optional[str] = None,
    progress_cb: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> UserContext:
    """Build a UserContext using the server's admin token + the target
    user's backend_user_id by default.

    When ``role='dest'`` AND the destination service_type is Plex AND
    the end user set ``playlist_mgmt_plex_home_auth_mode='per_user_token'``,
    the orchestrator instead looks up the user's saved Plex Home
    token in ``managed_users.auth_token_enc`` and threads it through
    so :class:`services.adapters.plex.PlexAdapter._server_for` can
    create a per-user PlexServer instance for the write.

    Missing-token behaviour is gated on the
    ``strict_identity_resolution`` tunable (developer's identity-audit
    follow-up):

    * strict=True  -> refuse to write as someone else; raise
      :class:`DestUserTokenMissing`. The UI surfaces the typed 412
      with a pointer to the per-user-token save endpoint.
    * strict=False -> silently fall back to the admin/owner context
      for this copy. The new playlist lands under the owner instead
      of under the Plex Home user, but the operation succeeds. The
      Playlist Management dest picker uses the same gate to decide
      whether to hide vs offer a user with no saved token.

    Plex per-user-token writes are required when the end user wants
    new playlists / collections / scrobbles to appear UNDER that
    Plex Home user instead of under the owner; the owner_token
    mode (default) creates everything under the admin account.

    The per-user auth attempt applies to BOTH ``role='source'`` and
    ``role='dest'`` because Plex's admin token only sees the OWNER's
    playlists; reading a managed user's playlists requires per-user
    auth too. Resolution chain runs unconditionally for non-owner Plex
    users:
      1. Saved per-user token from ``managed_users.auth_token_enc``.
      2. PIN-derived token via ``signInHomeUser`` when a Plex Home PIN
         is stored on the row.
      3. Admin/owner-token fallback (with logging) when neither works.

    The ``playlist_mgmt_plex_home_auth_mode`` + ``strict_identity_resolution``
    tunables together gate ONLY the dest-role admin fallback. Source
    reads always fall back silently because admin can return *some*
    data (the owner's view), and even if that view is empty for the
    target user, an empty cache row is more useful than a hard error.
    """
    from services.playlist_copy import log as _audit
    from services.playlist_copy import DestUserTokenMissing  # local: avoids an import cycle

    # Mirror every auth-chain decision to the progress callback so the
    # dashboard activity feed surfaces the actual reason a copy
    # did/didn't authenticate as the target user. Without this,
    # operators see "Item N: failed" with no clue which step
    # (saved_token / pin / admin_fallback) tripped.
    def _trace_auth(step: str, outcome: str, detail: str = "") -> None:
        # ALWAYS file the audit row (existing behavior).
        _audit.log_auth_chain_step(
            server_id=server_id or "", user_id=user_spec.username,
            step=step, outcome=outcome, role=role,
            detail=detail,
        )
        # Also surface to the live progress stream for batch jobs.
        if progress_cb is None:
            return
        try:
            progress_cb({
                "event": "auth-chain",
                "step": step,
                "outcome": outcome,
                "role": role,
                "user": user_spec.username,
                "server_id": server_id or "",
                "detail": detail,
            })
        except Exception:
            log.exception(
                "playlist_copy _user_context_for auth-chain emit failed",
            )

    auth_token = conn.token
    is_admin = True
    is_plex = (conn.service_type or "plex").lower() == "plex"
    # Multi-signal gate for owner detection (any one signal trips owner-mode):
    #   1. ``user_spec.role == 'owner'`` — the explicit declaration.
    #   2. ``user_spec.is_admin == True`` — the admin/owner flag from
    #      both managed_users (kind='owner') and the live adapter's
    #      ``list_users()``.
    #   3. Positive match against the dest server's ``myPlexAccount``
    #      email / username. Catches the rare case where neither of
    #      the above signals fired but the target is provably the
    #      Plex owner of THIS server (e.g. operator passed the owner's
    #      email as ``dest_user_id`` and ``_find_user`` fell through
    #      to a stale row).
    is_owner_target = (
        (getattr(user_spec, "role", "") or "").lower() == "owner"
        or bool(getattr(user_spec, "is_admin", False))
        or _user_is_plex_owner(conn, user_spec)
    )
    if is_plex and is_owner_target and server_id:
        _trace_auth(
            "admin_owner", "used",
            "target is Plex owner; admin token applied",
        )

    admin_fallback_used = False  # CONSOLE-07: set on admin-token fallback
    if is_plex and not is_owner_target and server_id:
        # Step 1: try a saved per-user token.
        per_user_token = _lookup_plex_home_token(server_id, user_spec.username)
        _trace_auth(
            "saved_token", "hit" if per_user_token else "miss",
        )
        # Step 2: try a PIN-derived token when no saved token exists.
        if not per_user_token:
            per_user_token = _obtain_per_user_token_via_pin(
                conn, server_id, user_spec.username, role=role,
            )
        if per_user_token:
            auth_token = per_user_token
            is_admin = False
        else:
            # Step 3: admin fallback. Strict-mode gate applies for DEST
            # writes — the operator may have configured the system to
            # refuse writes that would land under the owner rather than
            # the intended user. Source reads always fall back silently
            # because we want at least the admin-visible view.
            mode = (playlist_mgmt_plex_home_auth_mode() or "owner_token").lower()
            if (
                role == "dest"
                and mode == "per_user_token"
                and strict_identity_resolution()
            ):
                _trace_auth(
                    "admin_fallback", "refused",
                    "strict-mode dest + per_user_token",
                )
                raise DestUserTokenMissing(
                    f"Per-user-token auth mode is enabled but no usable "
                    f"per-user credentials are stored for user "
                    f"{user_spec.username!r} on server {server_id!r}. "
                    f"Save a Plex Home token via POST "
                    f"/api/managed-users/{server_id}/{user_spec.username}/"
                    f"plex-home-token, OR save a Plex Home PIN under "
                    f"User Management."
                )
            admin_fallback_used = True  # CONSOLE-07
            _trace_auth(
                "admin_fallback", "used",
                "no saved token, no usable PIN",
            )
            log.info(
                "playlist_copy: per-user auth unavailable for user %r on "
                "server %r (no saved token, no usable PIN); falling back "
                "to admin/owner context. The admin token only sees the "
                "owner's view, so this user's actual playlists / data "
                "may not be visible. Save a Plex Home token or PIN to "
                "unlock this user's data.",
                user_spec.username, server_id,
            )
    return UserContext(
        backend_user_id=user_spec.backend_user_id or "",
        username=user_spec.username,
        auth_token=auth_token,
        is_admin=is_admin,
        admin_fallback=admin_fallback_used,
    )


def _same_logical_user(
    server_id: str, a: UserSpec, b: UserSpec,
) -> bool:
    """Decide whether two UserSpecs refer to the same logical user on
    ``server_id``. Used by ``copy_playlist`` to short-circuit a
    source==destination copy as a no-op.

    Priority chain:
      1. app_user_uuid (canonical handle from managed_users.v12). The
         strongest signal — survives backend_user_id / username drift.
      2. backend_user_id exact (after normalising blank-vs-None).
      3. Case-insensitive username (last-resort handle).

    Each signal is checked only when BOTH sides have a non-empty value
    for it; we never declare equality on "both unset" (that would
    collapse all rows with an empty backend_user_id, e.g. owner rows
    pre-share-state-refresh). Returns False when no signal could be
    evaluated.
    """
    a_uuid: Optional[str] = None
    b_uuid: Optional[str] = None
    if server_id:
        try:
            a_uuid = _resolve_app_user_uuid_for_lookup(
                server_id, a.backend_user_id or a.username,
            )
            b_uuid = _resolve_app_user_uuid_for_lookup(
                server_id, b.backend_user_id or b.username,
            )
        except Exception:
            a_uuid, b_uuid = None, None
    if a_uuid and b_uuid:
        return a_uuid == b_uuid
    a_bid = (a.backend_user_id or "").strip()
    b_bid = (b.backend_user_id or "").strip()
    if a_bid and b_bid:
        return a_bid == b_bid
    a_name = (a.username or "").strip().lower()
    b_name = (b.username or "").strip().lower()
    if a_name and b_name:
        return a_name == b_name
    return False


def _user_is_plex_owner(conn: Any, user_spec: UserSpec) -> bool:
    """Positive identification of the Plex owner on ``conn``'s server.
    Matches ``user_spec.username`` and ``user_spec.backend_user_id``
    against ``myPlexAccount``'s email + username + id. Any match
    returns True.

    Used by :func:`_user_context_for` as a fallback owner signal when
    neither ``role`` nor ``is_admin`` flag the user as owner — covers
    stale managed_users rows + identity-map drift where the canonical
    handle is correct but the role bookkeeping isn't.

    Returns False silently on any error so callers can fall through
    to the per-user chain rather than blocking on a transient
    plex.tv lookup hiccup. Non-Plex backends always return False.
    """
    if (getattr(conn, "service_type", "plex") or "plex").lower() != "plex":
        return False
    username = (getattr(user_spec, "username", "") or "").strip().lower()
    backend_id = (getattr(user_spec, "backend_user_id", "") or "").strip()
    if not username and not backend_id:
        return False
    try:
        adapter = getattr(conn, "adapter", None)
        plex_server = getattr(adapter, "_server", None)
        if plex_server is None:
            return False
        account = plex_server.myPlexAccount()
        owner_email = (getattr(account, "email", "") or "").strip().lower()
        owner_username = (getattr(account, "username", "") or "").strip().lower()
        owner_id = str(getattr(account, "id", "") or "").strip()
        if username and (username == owner_email or username == owner_username):
            return True
        if backend_id and owner_id and backend_id == owner_id:
            return True
    except Exception:
        # Plex.tv hiccup, missing account, etc. The per-user chain
        # below catches the dest_token_missing case; do NOT block here.
        return False
    return False


def _lookup_plex_home_token(server_id: str, username: str) -> Optional[str]:
    """Pull the Fernet-decrypted token off ``managed_users.auth_token_enc``
    via :func:`server.media_db.get_managed_user_credential`. Returns
    None on missing row, missing column, or decrypt failure.
    """
    from server import media_db
    try:
        return media_db.get_managed_user_credential(
            server_id, username, kind="auth_token",
        )
    except Exception:
        log.exception(
            "playlist_copy: per-user-token lookup failed for "
            "(%s, %s); treating as missing.",
            server_id, username,
        )
        return None


def _obtain_per_user_token_via_pin(
    conn: Any, server_id: str, username: str, *, role: str = "source",
) -> Optional[str]:
    """When a saved per-user token isn't stored but a Plex Home PIN
    IS, sign in as the home user with the PIN to obtain a fresh per-user token.
    Mirrors the PIN-auth pattern already used by :func:`services.auth.get_home_users`
    for snapshot/restore runs.

    Returns the obtained token string on success; None when no PIN is
    stored, no matching home-user object exists, plexapi's build doesn't
    expose ``signInHomeUser`` / ``switchHomeUser``, or the sign-in fails.
    Errors are caught and logged so callers can fall through to admin
    auth or the strict-mode raise.

    Doesn't persist the token. The caller can save it to
    managed_users.auth_token_enc separately if they want it cached for
    subsequent runs; we treat the obtained token as a one-shot.
    """
    from server import media_db
    from services.playlist_copy import log as _audit
    try:
        stored_pin = media_db.get_managed_user_credential(
            server_id, username, kind="plex_home_pin",
        )
    except Exception:
        log.exception(
            "playlist_copy: PIN lookup failed for (%s, %s).",
            server_id, username,
        )
        _audit.log_auth_chain_step(
            server_id=server_id, user_id=username,
            step="pin_token", outcome="miss", role=role,
            detail="PIN lookup raised",
        )
        return None
    if not stored_pin:
        _audit.log_auth_chain_step(
            server_id=server_id, user_id=username,
            step="pin_token", outcome="miss", role=role,
            detail="no PIN stored",
        )
        return None
    plex_server = getattr(getattr(conn, "adapter", None), "_server", None)
    if plex_server is None:
        _audit.log_auth_chain_step(
            server_id=server_id, user_id=username,
            step="pin_token", outcome="miss", role=role,
            detail="no adapter._server on conn",
        )
        return None
    try:
        account = plex_server.myPlexAccount()
        target_user = None
        for u in (account.users() or []):
            if (getattr(u, "title", "") or "").lower() == username.lower():
                target_user = u
                break
        if target_user is None:
            log.info(
                "playlist_copy: no matching home-user object for %r on "
                "server %r; PIN-derived token unavailable.",
                username, server_id,
            )
            _audit.log_auth_chain_step(
                server_id=server_id, user_id=username,
                step="pin_token", outcome="miss", role=role,
                detail="no matching home-user object",
            )
            return None
        switch_method = (
            getattr(account, "signInHomeUser", None)
            or getattr(account, "switchHomeUser", None)
        )
        if switch_method is None:
            log.warning(
                "playlist_copy: plexapi build does not expose a home-user "
                "sign-in helper; PIN-derived token unavailable.",
            )
            _audit.log_auth_chain_step(
                server_id=server_id, user_id=username,
                step="pin_token", outcome="miss", role=role,
                detail="plexapi exposes no signInHomeUser/switchHomeUser",
            )
            return None
        try:
            impersonated = switch_method(target_user, pin=stored_pin)
        except TypeError:
            # Positional pin signature on older plexapi builds.
            impersonated = switch_method(target_user, stored_pin)
        user_token = (
            getattr(impersonated, "authToken", None)
            or getattr(impersonated, "_token", None)
        )
        if not user_token:
            log.warning(
                "playlist_copy: PIN-authenticated account for %r exposed "
                "no token; falling back.", username,
            )
            _audit.log_auth_chain_step(
                server_id=server_id, user_id=username,
                step="pin_token", outcome="miss", role=role,
                detail="signInHomeUser returned no token",
            )
            return None
        _audit.log_auth_chain_step(
            server_id=server_id, user_id=username,
            step="pin_token", outcome="hit", role=role,
        )
        return str(user_token)
    except Exception as exc:
        log.exception(
            "playlist_copy: PIN-based home-user sign-in failed for "
            "(%s, %s); falling back to admin auth.",
            server_id, username,
        )
        _audit.log_auth_chain_step(
            server_id=server_id, user_id=username,
            step="pin_token", outcome="miss", role=role,
            detail=f"signInHomeUser raised: {type(exc).__name__}",
        )
        return None


def _identity_kit(
    server_id: str, user_spec: UserSpec, ctx: Optional["UserContext"] = None,
) -> Dict[str, Any]:
    """Resolve the identity / auth metadata that gets tagged onto each
    playlist_cache row. Returns ``{app_user_uuid, auth_kind, role_flags}``.

    The cache schema carries these columns so cache hits are robust
    against backend_user_id-vs-username drift in ``user_id``. Callers
    pass the result through to ``upsert_playlist`` / ``record_refresh``.

    Fields:
      * ``app_user_uuid`` — canonical handle from managed_users. ``None``
        when the user has no managed_users row yet (live-only cold-start);
        cache rows still get written, just without the uuid tag.
      * ``auth_kind`` — ``'admin'`` when the orchestrator used the admin
        / owner token (default + per_user_token fallback path), or
        ``'per_user'`` when a Plex Home per-user token was used. ``None``
        when no ctx was passed.
      * ``role_flags`` — bitmask. bit 0 = is_admin, bit 1 = is_owner.
        Captures J/E's dual-flag case where one user can be both.
    """
    from server import media_db
    app_uuid: Optional[str] = None
    try:
        app_uuid = media_db.get_managed_user_app_uuid(
            server_id, user_spec.username,
        )
    except Exception:
        log.exception(
            "playlist_copy._identity_kit: app_user_uuid lookup failed "
            "for (%s, %s); cache row will be untagged.",
            server_id, user_spec.username,
        )
    auth_kind: Optional[str] = None
    if ctx is not None:
        auth_kind = "admin" if ctx.is_admin else "per_user"
    role_flags = 0
    if bool(getattr(user_spec, "is_admin", False)):
        role_flags |= 1
    if (getattr(user_spec, "role", "") or "").lower() == "owner":
        role_flags |= 2
    return {
        "app_user_uuid": app_uuid,
        "auth_kind": auth_kind,
        "role_flags": role_flags,
    }


def _managed_users_row(
    server_id: str, needle: str,
) -> Optional[Dict[str, Any]]:
    """Resolve a managed_users row by a flexible ``needle`` (any of
    ``app_user_uuid`` / ``backend_user_id`` / case-insensitive
    ``username``).

    Returns the raw row dict so callers can use whatever fields they
    need: :func:`_managed_users_lookup` extracts a ``UserSpec``;
    :func:`_resolve_app_user_uuid_for_lookup` pulls just the uuid for
    cache key resolution.

    Match order:
      1. app_user_uuid exact (canonical, post-v12 stable identifier)
      2. backend_user_id exact
      3. case-insensitive username
    """
    # Route through services.user_management.activity_filter.list_active_users so
    # playlist copy honours the same tombstone + auth-health filter as
    # every other backend-touching path.
    from services.user_management.activity_filter import list_active_users
    if not server_id or not needle:
        return None
    try:
        rows = list_active_users(server_id) or []
    except Exception:
        log.exception(
            "playlist_copy._managed_users_row: list_active_users "
            "failed for server %r.",
            server_id,
        )
        return None
    n = needle.lower()
    for r in rows:
        if (r.get("app_user_uuid") or "") == needle:
            return r
    for r in rows:
        if (r.get("backend_user_id") or "") == needle:
            return r
    for r in rows:
        if (r.get("username") or "").lower() == n:
            return r
    return None


def _managed_users_lookup(
    server_id: str, needle: str,
) -> Optional[UserSpec]:
    """Resolve a user from the local ``managed_users`` table without
    touching the live Plex / Jellyfin / Emby API.

    For user IDENTIFICATION this trusts the local ``managed_users``
    cache rather than a live ``adapter.list_users()`` call, which is
    brittle when the owner's myPlexAccount lookup fails, Plex.tv is
    rate-limiting, or the live ID-mapping has drifted. Live calls only
    need to happen for the actual playlist data fetch.

    Returns a synthesized ``UserSpec`` when a row is found; ``None``
    when no row matches (caller falls back to the live adapter).
    """
    chosen = _managed_users_row(server_id, needle)
    if chosen is None:
        return None
    kind = (chosen.get("kind") or "managed").lower()
    is_admin = kind == "owner"
    return UserSpec(
        backend_user_id=chosen.get("backend_user_id") or "",
        username=chosen.get("username") or "",
        display_name=chosen.get("display_name") or chosen.get("username") or "",
        role="owner" if is_admin else "managed",
        is_admin=is_admin,
    )


def _resolve_app_user_uuid_for_lookup(
    server_id: str, user_id: str,
) -> Optional[str]:
    """Get the canonical ``app_user_uuid`` for a (server_id, user_id)
    pair where ``user_id`` may be the canonical uuid itself, a
    backend_user_id, or a username. Used by cache READ paths to look
    up rows by uuid even when the caller passed a legacy identifier.
    """
    row = _managed_users_row(server_id, user_id)
    if row is None:
        return None
    return row.get("app_user_uuid")


def _find_user(
    adapter: MediaServerAdapter,
    user_id: str,
    *,
    on_missing: type,
    this_server_id: Optional[str] = None,
    peer_server_id: Optional[str] = None,
) -> UserSpec:
    """Lookup a UserSpec.

    Resolution order:

      0. Local ``managed_users`` cache (when ``this_server_id`` is set).
         Trusts the DB as the source of truth for user identification;
         avoids brittle live ``adapter.list_users()`` calls. See
         :func:`_managed_users_lookup` for the rationale.
      1. Live adapter ``list_users()`` exact ``backend_user_id`` match.
      2. Live adapter case-insensitive ``username`` match (legacy).
      3. USER-MGMT-IDENTITY-AUDIT R-4: user_identity_map walk. When
         ``this_server_id`` and ``peer_server_id`` are both provided,
         interpret ``user_id`` as a handle on the PEER server and
         look it up in identity_map; if a row links to a handle on
         ``this_server_id``, retry the direct match using the
         resolved handle.

    Raises ``on_missing`` if every step fails.
    """
    needle = (user_id or "").strip()
    if not needle:
        raise on_missing("user_id is required")
    # Step 0: local DB. Cheap, robust, and the end user's source of
    # truth for who exists. Live adapter fallback below covers the
    # legacy case where managed_users has not been populated yet
    # (the route-level cold-start sync handles initial seeding).
    if this_server_id:
        cached = _managed_users_lookup(this_server_id, needle)
        if cached is not None:
            return cached
    users = adapter.list_users() or []
    for u in users:
        if (u.backend_user_id or "") == needle:
            return u
    # Fallback: case-insensitive username match.
    for u in users:
        if (u.username or "").lower() == needle.lower():
            return u
    # USER-MGMT-IDENTITY-AUDIT R-4: walk identity_map for a peer-
    # server -> this-server link when both server ids are known.
    if this_server_id and peer_server_id:
        try:
            from server.media_db import get_identity_maps_for_user
            for link in get_identity_maps_for_user(
                peer_server_id, needle,
            ) or []:
                if link.get("other_server_id") != this_server_id:
                    continue
                resolved = (link.get("other_user_handle") or "").strip()
                if not resolved:
                    continue
                for u in users:
                    if (u.username or "").lower() == resolved.lower():
                        return u
        except Exception:  # pragma: no cover (defensive)
            pass
    raise on_missing(f"user {user_id!r} not found on destination server")