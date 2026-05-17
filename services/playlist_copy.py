"""
Playlist Management copy orchestrator (Plan[PLAYLIST-MANAGEMENT]-2026-05-16,
section 3.6).

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

import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

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
    playlist_mgmt_plex_home_auth_mode,
    playlist_mgmt_same_user_behavior,
    strict_identity_resolution,
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


def _user_context_for(
    conn: Any,
    user_spec: UserSpec,
    *,
    role: str = "source",
    server_id: Optional[str] = None,
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

    2026-05-17 (operator request — combined auth chain): the per-user
    auth attempt now applies to BOTH ``role='source'`` and
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
    from services import playlist_cache_log as _audit  # local: keep import light

    auth_token = conn.token
    is_admin = True
    is_plex = (conn.service_type or "plex").lower() == "plex"
    # Owner detection 2026-05-17 (operator bug — managed user copying
    # a playlist TO the Plex owner: orchestrator reported success but
    # the playlist never appeared on the owner's account). Root cause:
    # the prior gate only checked ``role == 'owner'``; if the resolved
    # ``user_spec`` had ``is_admin=True`` but a non-owner role (cached
    # row drift, identity-map walk landing on a managed-flavor row,
    # etc.) the per-user auth chain ran and the create_playlist call
    # got routed through a per-user PlexServer with the WRONG token —
    # the write succeeded on that user's view but never showed up
    # under the owner.
    #
    # Multi-signal gate (any one signal trips owner-mode):
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
    #
    # Plex Home + fan-out: the operator wants the owner ALWAYS routed
    # through the admin token even when fan-out is on for other users.
    # This gate is the single chokepoint that enforces it.
    is_owner_target = (
        (getattr(user_spec, "role", "") or "").lower() == "owner"
        or bool(getattr(user_spec, "is_admin", False))
        or _user_is_plex_owner(conn, user_spec)
    )
    if is_plex and is_owner_target and server_id:
        _audit.log_auth_chain_step(
            server_id=server_id, user_id=user_spec.username,
            step="admin_owner", outcome="used", role=role,
            detail="target is Plex owner; admin token applied",
        )

    # Owner case: admin token IS the owner's token. No per-user lookup
    # needed; bare admin auth returns the owner's view, which is the
    # correct view of the owner's own data.
    if is_plex and not is_owner_target and server_id:
        # Step 1: try a saved per-user token.
        per_user_token = _lookup_plex_home_token(server_id, user_spec.username)
        _audit.log_auth_chain_step(
            server_id=server_id, user_id=user_spec.username,
            step="saved_token",
            outcome="hit" if per_user_token else "miss",
            role=role,
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
                _audit.log_auth_chain_step(
                    server_id=server_id, user_id=user_spec.username,
                    step="admin_fallback", outcome="refused", role=role,
                    detail="strict-mode dest + per_user_token",
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
            _audit.log_auth_chain_step(
                server_id=server_id, user_id=user_spec.username,
                step="admin_fallback", outcome="used", role=role,
                detail="no saved token, no usable PIN",
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
    # The Plex adapter's ``list_playlists`` does its own per-user
    # filter via the username→local-SystemAccount-id map; UserContext
    # just carries the (backend_user_id, username, token, is_admin)
    # tuple. No need to nullify backend_user_id here.
    return UserContext(
        backend_user_id=user_spec.backend_user_id or "",
        username=user_spec.username,
        auth_token=auth_token,
        is_admin=is_admin,
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
    None on missing row, missing column, or decrypt failure (the
    helper itself logs and returns None on bad ciphertext)."""
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
    """2026-05-17 (operator request, combined-auth chain): when a saved
    per-user token isn't stored but a Plex Home PIN IS, sign in as the
    home user with the PIN to obtain a fresh per-user token. Mirrors the
    PIN-auth pattern already used by :func:`services.auth.get_home_users`
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
    from services import playlist_cache_log as _audit
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
        # account.users() is the home-user roster. Match by case-insensitive
        # title (Plex Home username).
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

    2026-05-16 (end user request): the cache schema now carries these
    columns so cache hits are robust against backend_user_id-vs-username
    drift in ``user_id`` (the prior cache key). Callers pass the result
    through to ``upsert_playlist`` / ``record_refresh``.

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
    from server import media_db
    if not server_id or not needle:
        return None
    try:
        rows = media_db.list_managed_users(
            server_id, include_hidden=False,
        ) or []
    except Exception:
        log.exception(
            "playlist_copy._managed_users_row: list_managed_users "
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

    2026-05-16 (end user request): the prior _find_user always called
    ``adapter.list_users()`` live and matched on its result, which was
    brittle when the owner's myPlexAccount lookup failed, Plex.tv was
    rate-limiting, or the live ID-mapping had drifted. We already have
    everything we need in managed_users; for user IDENTIFICATION trust
    the local cache. Live calls only need to happen for the actual
    playlist data fetch.

    Returns a synthesized ``UserSpec`` when a row is found; ``None``
    when no row matches (caller falls back to the live adapter)."""
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
    up rows by uuid even when the caller passed a legacy identifier."""
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


# ── Public read API ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _ListResult:
    playlists: List[Dict[str, Any]]
    from_cache: bool
    fetched_at: float


def list_user_playlists(
    *,
    server_id: str,
    user_id: str,
    force_refresh: bool = False,
) -> _ListResult:
    """List a user's playlists. Reads from cache when fresh enough
    (``playlist_cache_refresh_interval_seconds`` tunable) unless the
    caller forces a refresh. Cache failures fall through to live."""
    if not server_id or not user_id:
        return _ListResult(playlists=[], from_cache=False, fetched_at=time.time())

    cache_on = playlist_cache_enabled()
    if cache_on and not force_refresh:
        try:
            interval = playlist_cache_refresh_interval_seconds()
            # Resolve the canonical app_user_uuid early so the cache
            # lookup matches by uuid even when ``user_id`` here is an
            # alias (backend_user_id vs username) that doesn't agree
            # with what the bulk refresh stored. Falls back to the
            # legacy user_id key when no uuid resolves.
            app_uuid = _resolve_app_user_uuid_for_lookup(server_id, user_id)
            marker = playlist_cache_db.get_refresh_marker(
                server_id, user_id, app_user_uuid=app_uuid,
            )
            if marker and not marker.get("error"):
                age = time.time() - float(marker["last_refreshed_at"])
                if age <= float(interval):
                    rows = playlist_cache_db.list_cached_playlists(
                        server_id, user_id, app_user_uuid=app_uuid,
                    )
                    return _ListResult(
                        playlists=rows,
                        from_cache=True,
                        fetched_at=float(marker["last_refreshed_at"]),
                    )
        except Exception:
            log.exception(
                "list_user_playlists cache read failed for (%s,%s); "
                "falling through to live.",
                server_id, user_id,
            )

    # Live fetch + cache write.
    return _live_list_and_cache(server_id, user_id)


def _live_list_and_cache(server_id: str, user_id: str) -> _ListResult:
    """Connect to ``server_id``, list playlists for ``user_id`` via
    the adapter, write the rosters to cache, return the result."""
    start = time.perf_counter()
    try:
        conn = _connect(server_id, on_fail=SourceUnreachable)
    except PlaylistCopyError as exc:
        # Connection failed before we could resolve a user_spec. Try a
        # best-effort uuid lookup so the error marker still carries the
        # canonical identity tag; falls back to untagged when no row.
        early_uuid = _resolve_app_user_uuid_for_lookup(server_id, user_id)
        try:
            playlist_cache_db.record_refresh(
                server_id=server_id, user_id=user_id,
                last_refresh_ms=int((time.perf_counter() - start) * 1000),
                error=str(exc),
                app_user_uuid=early_uuid,
            )
        except Exception:
            pass
        raise
    adapter = conn.adapter
    user_spec = _find_user(
        adapter, user_id, on_missing=DestUserNotFound,
        this_server_id=server_id,
    )
    # 2026-05-17 bug fix (operator report — combined auth chain
    # appeared to have no effect): pass server_id through so the
    # per-user auth chain inside _user_context_for activates. The
    # prior bare call defaulted server_id=None which skipped the
    # saved-token + PIN-sign-in steps entirely and silently fell
    # back to admin auth (returning the admin's view, filtered to 0
    # rows for managed users).
    ctx = _user_context_for(conn, user_spec, server_id=server_id)
    # Identity / auth tags written onto every cache row this call
    # produces. Resolved once here so all per-playlist + the refresh
    # marker stay in sync (the canonical uuid + auth kind + role flags
    # let cache reads match by app_user_uuid regardless of which legacy
    # user_id alias the caller passed in).
    kit = _identity_kit(server_id, user_spec, ctx)
    try:
        specs: List[AdapterPlaylistSpec] = list(adapter.list_playlists(ctx) or [])
    except Exception as exc:
        try:
            playlist_cache_db.record_refresh(
                server_id=server_id, user_id=user_id,
                last_refresh_ms=int((time.perf_counter() - start) * 1000),
                error=f"list_playlists failed: {exc}",
                **kit,
            )
        except Exception:
            pass
        raise SourceUnreachable(f"list_playlists failed: {exc}") from exc

    out_rows: List[Dict[str, Any]] = []
    cache_uid = user_spec.backend_user_id or user_id
    cache_enabled = playlist_cache_enabled()
    # 2026-05-17 (operator bug report — cross-user contamination):
    # wipe every existing row for this user before writing the fresh
    # set. Per-playlist upsert only overwrites matching playlist_ids,
    # so without this delete, stale rows from a prior (incorrectly-
    # tagged) refresh would survive forever. Matches both the legacy
    # user_id key + the canonical app_user_uuid key so pre- and post-
    # schema-v2 rows are both cleared.
    if cache_enabled:
        try:
            playlist_cache_db.clear_user_cache(
                server_id=server_id,
                user_id=cache_uid,
                app_user_uuid=kit.get("app_user_uuid"),
            )
        except Exception:
            log.exception(
                "playlist_cache clear_user_cache failed for (%s,%s); "
                "stale rows may survive into the next read.",
                server_id, cache_uid,
            )
    for spec in specs:
        # Cache write per playlist; surface as the end user-facing
        # row regardless of cache outcome.
        items_for_cache = [
            {
                "title": ref.title,
                "guids": list(ref.guids),
                "type": "",
            }
            for ref in (spec.items or ())
        ]
        if cache_enabled:
            try:
                playlist_cache_db.upsert_playlist(
                    server_id=server_id,
                    user_id=cache_uid,
                    playlist_id=spec.playlist_id,
                    name=spec.name,
                    is_smart=bool(spec.is_smart),
                    items=items_for_cache,
                    **kit,
                )
            except Exception:
                log.exception(
                    "playlist_cache upsert failed for (%s,%s,%s); continuing.",
                    server_id, cache_uid, spec.playlist_id,
                )
        out_rows.append({
            "playlist_id": spec.playlist_id,
            "name": spec.name,
            "is_smart": bool(spec.is_smart),
            "item_count": len(spec.items or ()),
            "fetched_at": time.time(),
        })

    refreshed_at = time.time()
    if cache_enabled:
        try:
            playlist_cache_db.record_refresh(
                server_id=server_id, user_id=cache_uid,
                last_refreshed_at=refreshed_at,
                last_refresh_ms=int((time.perf_counter() - start) * 1000),
                error=None,
                **kit,
            )
        except Exception:
            log.exception(
                "playlist_cache refresh marker write failed for (%s,%s); continuing.",
                server_id, cache_uid,
            )
    return _ListResult(playlists=out_rows, from_cache=False, fetched_at=refreshed_at)


def get_playlist_detail(
    *,
    server_id: str,
    user_id: str,
    playlist_id: str,
    force_refresh: bool = False,
) -> Dict[str, Any]:
    """Return one playlist's items. Cache-aware in the same way as
    :func:`list_user_playlists`. Returns a dict shape that maps
    directly onto :class:`server.models.PlaylistDetail`."""
    if not server_id or not user_id or not playlist_id:
        raise PlaylistNotFound("server_id, user_id, playlist_id are all required")

    cache_on = playlist_cache_enabled()
    if cache_on and not force_refresh:
        try:
            interval = playlist_cache_refresh_interval_seconds()
            app_uuid = _resolve_app_user_uuid_for_lookup(server_id, user_id)
            marker = playlist_cache_db.get_refresh_marker(
                server_id, user_id, app_user_uuid=app_uuid,
            )
            cached = playlist_cache_db.get_cached_playlist_items(
                server_id, user_id, playlist_id,
            )
            if cached and marker and not marker.get("error"):
                age = time.time() - float(marker["last_refreshed_at"])
                if age <= float(interval):
                    return {
                        "playlist_id": playlist_id,
                        "name": cached["name"],
                        "is_smart": cached["is_smart"],
                        "items": cached["items"],
                        "fetched_at": float(cached["fetched_at"]),
                        "from_cache": True,
                    }
        except Exception:
            log.exception(
                "get_playlist_detail cache read failed for (%s,%s,%s); "
                "falling through to live.",
                server_id, user_id, playlist_id,
            )

    # Live fetch.
    conn = _connect(server_id, on_fail=SourceUnreachable)
    adapter = conn.adapter
    user_spec = _find_user(
        adapter, user_id, on_missing=DestUserNotFound,
        this_server_id=server_id,
    )
    # Pass server_id so the per-user auth chain (saved token → PIN
    # sign-in → admin fallback) activates; without it the call
    # short-circuits to admin auth.
    ctx = _user_context_for(conn, user_spec, server_id=server_id)
    cache_uid = user_spec.backend_user_id or user_id
    kit = _identity_kit(server_id, user_spec, ctx)

    # Resolve the playlist's name / is_smart flag via list_playlists
    # (single source of truth on the adapter); items via the dedicated
    # get_playlist_items helper.
    name = ""
    is_smart = False
    try:
        specs = list(adapter.list_playlists(ctx) or [])
        match = next((s for s in specs if s.playlist_id == playlist_id), None)
    except Exception as exc:
        raise SourceUnreachable(f"list_playlists failed: {exc}") from exc
    if match is None:
        raise PlaylistNotFound(
            f"playlist {playlist_id!r} not found for user {user_id!r}"
        )
    name = match.name
    is_smart = bool(match.is_smart)

    try:
        items_tuple: Tuple[ItemRef, ...] = adapter.get_playlist_items(
            playlist_id, user_context=ctx,
        )
    except Exception as exc:
        raise SourceUnreachable(f"get_playlist_items failed: {exc}") from exc

    items_out = [
        {
            "title": ref.title,
            "guids": list(ref.guids),
            "type": "",
            "duration_ms": None,
        }
        for ref in (items_tuple or ())
    ]

    # Cache-write side effect.
    if playlist_cache_enabled():
        try:
            playlist_cache_db.upsert_playlist(
                server_id=server_id,
                user_id=cache_uid,
                playlist_id=playlist_id,
                name=name,
                is_smart=is_smart,
                items=items_out,
                **kit,
            )
        except Exception:
            log.exception("playlist_cache upsert failed; continuing.")

    return {
        "playlist_id": playlist_id,
        "name": name,
        "is_smart": is_smart,
        "items": items_out,
        "fetched_at": time.time(),
        "from_cache": False,
    }


# ── Public refresh API ──────────────────────────────────────────────────────


def refresh_user_cache(
    *,
    server_id: str,
    user_id: str,
) -> Dict[str, Any]:
    """Force a live re-fetch + cache rewrite for one user. Returns a
    dict shape matching :class:`server.models.PlaylistCacheRefreshResult`."""
    from services import playlist_cache_log
    start = time.perf_counter()
    try:
        result = _live_list_and_cache(server_id, user_id)
    except PlaylistCopyError as exc:
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        playlist_cache_log.log_user_refresh(
            server_id=server_id, user_id=user_id, ok=False,
            elapsed_ms=elapsed_ms, error=f"{exc.code}: {exc}",
        )
        return {
            "server_id": server_id,
            "user_id": user_id,
            "refreshed_at": time.time(),
            "playlists_count": 0,
            "items_count": 0,
            "duration_ms": elapsed_ms,
            "error": f"{exc.code}: {exc}",
        }
    items_count = sum(int(r.get("item_count") or 0) for r in result.playlists)
    elapsed_ms = int((time.perf_counter() - start) * 1000)
    playlist_cache_log.log_user_refresh(
        server_id=server_id, user_id=user_id, ok=True,
        elapsed_ms=elapsed_ms,
        playlists=len(result.playlists), items=items_count,
    )
    return {
        "server_id": server_id,
        "user_id": user_id,
        "refreshed_at": result.fetched_at,
        "playlists_count": len(result.playlists),
        "items_count": items_count,
        "duration_ms": elapsed_ms,
        "error": None,
    }


def refresh_server_cache(
    *,
    server_id: str,
    source: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Force-refresh every user on ``server_id``. Returns a list of
    :class:`PlaylistCacheRefreshResult`-shaped dicts (one per user)
    plus a final aggregate row with ``user_id=None``.

    ``source`` is an optional free-form tag for the audit log (e.g.
    ``"servers-refresh"`` or ``"playlist-mgmt-panel"``) so the end user
    can tell which UI surface triggered each entry."""
    from services import playlist_cache_log
    try:
        conn = _connect(server_id, on_fail=SourceUnreachable)
    except PlaylistCopyError as exc:
        playlist_cache_log.log_bulk_refresh_end(
            server_id=server_id, ok=0, errors=1, elapsed_s=0.0, source=source,
        )
        return [{
            "server_id": server_id, "user_id": None,
            "refreshed_at": time.time(),
            "playlists_count": 0, "items_count": 0, "duration_ms": 0,
            "error": f"{exc.code}: {exc}",
        }]
    try:
        users = conn.adapter.list_users() or []
    except Exception as exc:
        playlist_cache_log.log_bulk_refresh_end(
            server_id=server_id, ok=0, errors=1, elapsed_s=0.0, source=source,
        )
        return [{
            "server_id": server_id, "user_id": None,
            "refreshed_at": time.time(),
            "playlists_count": 0, "items_count": 0, "duration_ms": 0,
            "error": f"list_users failed: {exc}",
        }]

    playlist_cache_log.log_bulk_refresh_start(
        server_id=server_id, user_count=len(users), source=source,
    )
    out: List[Dict[str, Any]] = []
    total_playlists = 0
    total_items = 0
    error_count = 0
    aggregate_start = time.perf_counter()
    for user in users:
        uid = user.backend_user_id or user.username
        if not uid:
            continue
        row = refresh_user_cache(server_id=server_id, user_id=uid)
        out.append(row)
        total_playlists += int(row.get("playlists_count") or 0)
        total_items += int(row.get("items_count") or 0)
        if row.get("error"):
            error_count += 1
    elapsed_s = time.perf_counter() - aggregate_start
    playlist_cache_log.log_bulk_refresh_end(
        server_id=server_id,
        ok=len(out) - error_count,
        errors=error_count,
        elapsed_s=elapsed_s,
        source=source,
    )
    out.append({
        "server_id": server_id, "user_id": None,
        "refreshed_at": time.time(),
        "playlists_count": total_playlists,
        "items_count": total_items,
        "duration_ms": int(elapsed_s * 1000),
        "error": None,
    })
    return out


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
    """
    started = time.perf_counter()

    def _emit(event: str, **fields: Any) -> None:
        if progress_cb is None:
            return
        try:
            progress_cb({"event": event, **fields})
        except Exception:
            log.exception("copy_playlist progress_cb raised on event %r", event)

    _emit("started")

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

    # 2026-05-17 (operator request): same-user no-op short-circuit.
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
    )
    dst_ctx = _user_context_for(
        dst_conn, dst_user, role="dest", server_id=dest_server_id,
    )
    _emit(
        "users-resolved",
        source_username=src_user.username,
        dest_username=dst_user.username,
    )

    # 3. Read the source playlist's name + smart flag + items.
    try:
        src_specs = list(src_adapter.list_playlists(src_ctx) or [])
    except Exception as exc:
        raise SourceUnreachable(f"source list_playlists failed: {exc}") from exc
    src_spec = next(
        (s for s in src_specs if s.playlist_id == source_playlist_id),
        None,
    )
    if src_spec is None:
        raise PlaylistNotFound(
            f"playlist {source_playlist_id!r} not found for user "
            f"{source_user_id!r} on source server"
        )
    if src_spec.is_smart:
        raise SmartPlaylistNotPortable(
            f"playlist {src_spec.name!r} is smart; criteria do not port "
            f"across backends"
        )

    try:
        src_items: Tuple[ItemRef, ...] = src_adapter.get_playlist_items(
            source_playlist_id, user_context=src_ctx,
        )
    except Exception as exc:
        raise SourceUnreachable(f"source get_playlist_items failed: {exc}") from exc
    _emit(
        "source-loaded",
        playlist_name=src_spec.name,
        is_smart=bool(src_spec.is_smart),
        item_count=len(src_items),
    )

    # 4. Resolve each source item to a destination backend_item_id.
    #
    # Resolution chain (2026-05-17 multi-tier):
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
    errors: List[str] = []
    total_items = len(src_items)
    # Emit a "resolving" progress event every PROGRESS_STEP items so
    # the dashboard counter ticks without overwhelming the WS payload.
    # 1 keeps small playlists (a few items) responsive; for large
    # cross-server runs the per-item live calls dominate latency
    # anyway, so emitting per-item is fine.
    PROGRESS_STEP = max(1, total_items // 50) if total_items > 0 else 1
    items_processed = 0
    for ref in src_items:
        items_processed += 1
        # Method 1: same-server passthrough.
        if same_server:
            if not ref.backend_item_id:
                skipped_no_match += 1
            else:
                resolved_refs.append(ItemRef(
                    backend_item_id=ref.backend_item_id,
                    guids=tuple(ref.guids or ()),
                    title=ref.title,
                    file_path=ref.file_path,
                ))
            if items_processed % PROGRESS_STEP == 0:
                _emit(
                    "resolving",
                    completed=items_processed,
                    total=total_items,
                    resolved=len(resolved_refs),
                    skipped=skipped_no_match,
                )
            continue
        # Cross-server: layered resolution. Track which methods were
        # actually attempted (not just which were available) so the
        # per-item miss message can list them honestly.
        methods_tried: List[str] = []
        dest_id: Optional[str] = None
        guids = tuple(ref.guids or ())
        # Method 2: GUID resolution.
        if guids:
            methods_tried.append("GUID")
            try:
                dest_id = dst_adapter.resolve_by_guids(guids)
            except Exception as exc:
                errors.append(f"resolve {ref.title!r} via GUID: {exc}")
                dest_id = None
        # Method 3: path-tail fallback (last N components, default 3).
        if not dest_id and ref.file_path:
            _resolver = getattr(dst_adapter, "resolve_by_path_tail", None)
            if callable(_resolver):
                methods_tried.append("path-tail")
                try:
                    dest_id = _resolver(ref.file_path)
                except Exception as exc:
                    errors.append(
                        f"resolve {ref.title!r} via path-tail: {exc}",
                    )
                    dest_id = None
        if not dest_id:
            methods_label = (
                ", ".join(methods_tried) if methods_tried
                else "none (item has no GUIDs or file_path)"
            )
            errors.append(
                f"{ref.title!r}: no destination match — tried {methods_label}"
            )
            skipped_no_match += 1
            continue
        resolved_refs.append(ItemRef(
            backend_item_id=dest_id,
            guids=guids,
            title=ref.title,
            file_path=ref.file_path,
        ))
        if items_processed % PROGRESS_STEP == 0:
            _emit(
                "resolving",
                completed=items_processed,
                total=total_items,
                resolved=len(resolved_refs),
                skipped=skipped_no_match,
            )

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
            "items_failed": 0,
            "errors": errors or [
                "No source items resolved on the destination server (zero GUID matches)."
            ],
            "elapsed_seconds": time.perf_counter() - started,
        }

    # 5. Create the destination playlist using the destination user's
    # context so it appears under their user on the destination.
    name = (dest_playlist_name or src_spec.name or "").strip()
    if not name:
        name = "Untitled"
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
        "success": True,
        "new_playlist_id": new_id or None,
        "items_written": len(resolved_refs),
        "items_skipped_no_match": skipped_no_match,
        "items_failed": 0,
        "errors": errors,
        "elapsed_seconds": time.perf_counter() - started,
    }


# ── Cache freshness helper (for snapshot path integration) ──────────────────


def snapshot_should_use_cache(
    server_id: str,
    user_id: str,
) -> bool:
    """Return True if the snapshot path should consult the playlist
    cache instead of hitting the live API. Reads
    ``playlist_cache_snapshot_threshold_seconds`` and short-circuits to
    False when the cache is disabled or the marker is stale / errored.

    Lives here (not in ``playlist_cache_db``) because the threshold +
    enabled check are tunable-side concerns, not DB-side concerns."""
    if not playlist_cache_enabled():
        return False
    try:
        threshold = playlist_cache_snapshot_threshold_seconds()
    except Exception:
        return False
    return playlist_cache_db.is_fresh(
        server_id, user_id, threshold_seconds=float(threshold),
    )
