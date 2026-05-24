"""
Multi-user JWT authentication for the Hestia-MediaManager web UI.

Always on
---------
Auth is active on every install. The first time the app boots with
no users the ``/api/auth/status`` endpoint reports
``setup_needed=true`` and the frontend renders ``<SetupPage />``
which calls ``POST /api/auth/setup`` to create the root admin. Every
subsequent boot goes straight to ``<LoginPage />``.

Role hierarchy:
  viewer < end user < manager < root_admin  (+ db_admin non-login)

Role enforcement strategy (immediacy contract from §8.7 of
multiuserauth.md): every protected endpoint re-reads the user's
CURRENT role from the database via ``auth_db.get_user()`` rather than
trusting the JWT's ``role`` claim. Cost: one DB read per request.
Benefit: a ``PATCH /api/auth/users/{u}`` role change is visible to the
affected user on their very next request without re-login.

Why no localStorage on the client?
----------------------------------
The access token is held in React component state plus optional
sessionStorage / localStorage on the client side (the ``Remember me``
toggle). Closing the browser tab ends the session in the default
mode. The roadmap calls this out explicitly: the tool is locally
hosted and the threat model favours "no persisted creds in the
browser" over the marginal UX win of "stay logged in across
reloads."
"""

from __future__ import annotations

import logging
import os
import secrets as _secrets
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from server import auth_db
from server.persistence import get_data_dir


log = logging.getLogger("plexmigrate.server.auth_router")


# ── Secret-key management ────────────────────────────────────────────────────
#
# JWTs are signed with a 32-byte hex secret kept at
# ``server_data/.auth_secret``. Same atomic-write pattern as
# ``server/secrets.py``'s ``.keyfile`` so a first-boot race between
# two workers produces exactly one secret. End users who already
# manage their own secrets can override via ``PLEXMIGRATE_AUTH_SECRET``
# (raw 64-char hex string) - that path skips the file entirely.

_SECRET_FILE = ".auth_secret"
_secret_lock = threading.Lock()
_cached_secret: Optional[str] = None


def _secret_path() -> Path:
    return get_data_dir() / _SECRET_FILE


def _load_or_create_secret() -> str:
    """
    Return the active JWT secret. Env-var override wins; otherwise
    read or create ``.auth_secret``. Result is a 64-character hex
    string (32 raw bytes).
    """
    global _cached_secret
    if _cached_secret is not None:
        return _cached_secret
    with _secret_lock:
        if _cached_secret is not None:
            return _cached_secret
        env = (os.environ.get("PLEXMIGRATE_AUTH_SECRET") or "").strip()
        if env:
            # Sanity-check the end user-provided value: hex, even length.
            try:
                bytes.fromhex(env)
            except ValueError as exc:
                raise RuntimeError(
                    "PLEXMIGRATE_AUTH_SECRET must be a hex-encoded string "
                    "(e.g. 64 chars for 32 bytes)."
                ) from exc
            _cached_secret = env
            return _cached_secret

        path = _secret_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            # O_EXCL means exactly one process wins the creation race;
            # the loser sees FileExistsError and falls through to read.
            # M16: the 0o600 mode is POSIX-only - Windows ignores it and
            # the .auth_secret inherits the data-directory ACL, which is
            # the actual security boundary on that platform and must be
            # locked down to the service account.
            fd = os.open(str(path), os.O_EXCL | os.O_CREAT | os.O_WRONLY, 0o600)
        except FileExistsError:
            _cached_secret = path.read_text(encoding="ascii").strip()
            return _cached_secret

        try:
            new = _secrets.token_hex(32)
            os.write(fd, new.encode("ascii"))
        finally:
            os.close(fd)

        # AUTH-07: the 0o600 mode above is POSIX-only. On Windows the
        # file inherits the data-directory ACL; strip the broad-access
        # groups so the JWT signing secret is not readable by other
        # local accounts. Best-effort, never fatal.
        try:
            from server.secrets import harden_secret_file
            harden_secret_file(path)
        except Exception:
            log.warning("could not harden ACL on %s", path, exc_info=True)

        log.warning(
            "Generated new JWT signing secret at %s. Existing tokens "
            "(if any) are now invalid; users must log in again.",
            path,
        )
        _cached_secret = new
        return _cached_secret


# ── JWT issue / validate ─────────────────────────────────────────────────────

# Short-lived access token (30 minutes). Pairs with the long-lived
# refresh-token cookie below: when the access JWT expires, the
# frontend silently calls /api/auth/refresh to mint a new one,
# extending the session without a re-login as long as the refresh
# cookie is still valid.
#
# Hot-reload: the TTL is read from
# ``services.tunables.jwt_access_token_ttl_seconds`` at each token
# mint, so a save to the tunable takes effect on the next login /
# refresh. The constant below is the fallback when the tunables
# module isn't importable.
_JWT_TTL_FALLBACK = 30 * 60
JWT_ALG = "HS256"


def _jwt_ttl_seconds() -> int:
    try:
        from services.tunables import jwt_access_token_ttl_seconds
        return int(jwt_access_token_ttl_seconds())
    except Exception:
        return _JWT_TTL_FALLBACK


# 7-day refresh-token lifetime. ``auth_db`` is authoritative; this
# value is mirrored here only so the cookie ``Max-Age`` and the DB
# row expiry agree. Hot-reload via
# ``services.tunables.refresh_token_ttl_seconds``.
_REFRESH_TTL_FALLBACK = 7 * 24 * 60 * 60


def _refresh_ttl_seconds() -> int:
    try:
        from services.tunables import refresh_token_ttl_seconds
        return int(refresh_token_ttl_seconds())
    except Exception:
        return _REFRESH_TTL_FALLBACK

# Cookie name + scope for the refresh token. ``Path=/api/auth`` so
# every auth endpoint receives the cookie (refresh, logout, me,
# etc.) but nothing else does. ``HttpOnly`` keeps it out of
# JavaScript reach. ``SameSite=Strict`` blocks cross-site delivery
# (CSRF mitigation). ``Secure`` (M15) is set per-request when the
# connection scheme is https, so a TLS-terminated deployment never
# leaks the cookie over a plain-HTTP hop; a plain-HTTP local-network
# deployment still works because the flag is simply omitted there.
REFRESH_COOKIE_NAME = "refresh_token"
REFRESH_COOKIE_PATH = "/api/auth"


def verify_db_admin(username: str, password: str) -> None:
    """
    Re-validate db_admin credentials. Raises 401 on any failure path -
    "no such user", "wrong password", and "username exists but is not
    a db_admin row" all collapse to one response so an attacker cannot
    probe for db_admin existence.

    AUTH-05: this is the single shared gate. Every destructive
    endpoint that takes a {db_admin_username, db_admin_password}
    two-factor body funnels through here (directly, or via
    :func:`verify_db_admin_from_body`) instead of re-implementing the
    check inline, so a future change to the gate lands in one place.
    """
    if not username or not password:
        raise HTTPException(status_code=401, detail="Database admin credentials are required.")
    verified = auth_db.verify_password(username, password)
    if verified is None or verified.get("role") != "db_admin":
        raise HTTPException(status_code=401, detail="Database admin credentials are invalid.")


def verify_db_admin_from_body(body: Dict[str, Any]) -> None:
    """
    Pull ``db_admin_username`` / ``db_admin_password`` out of a
    destructive endpoint's request body and run them through
    :func:`verify_db_admin`. Missing keys become empty strings, which
    :func:`verify_db_admin` rejects with the same 401 as a wrong
    password.
    """
    body = body or {}
    verify_db_admin(
        str(body.get("db_admin_username") or ""),
        str(body.get("db_admin_password") or ""),
    )


def issue_token(
    username: str,
    role: str,
    display_name: Optional[str] = None,
    sid: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Mint a JWT carrying ``{sub, role, display_name, jti, sid, exp, iat}``.
    Returned dict matches the OAuth2 password-flow vocabulary the
    frontend expects.

    ``display_name`` is included so the topbar can show the end user's
    chosen display name without a separate ``/api/auth/me`` round-trip
    on first paint. The authoritative value still lives in the DB -
    every protected endpoint re-reads it via ``auth_db.get_user()`` so
    a mid-session display-name change reflects on the next request.

    ``jti`` is a freshly-generated UUID4 hex - unique per access token,
    used for token-identity purposes (not for session keying).

    ``sid`` is the *session identifier*: the same value across every
    access token minted from the same refresh-token cookie. The View
    Mode session table keys off ``sid`` so an end user's view-mode
    override survives /refresh (new access JWT, same sid) and clears
    on logout (refresh token revoked, no future JWT carries that sid).
    Passing ``sid=None`` is supported for transitional /me / verify
    requests that shouldn't tie to a session, but /login, /setup, and
    /refresh always pass the refresh-token id explicitly.
    """
    import jwt as _jwt

    now = int(time.time())
    ttl = _jwt_ttl_seconds()
    payload: Dict[str, Any] = {
        "sub": username,
        "role": role,
        "display_name": display_name,
        "exp": now + ttl,
        "iat": now,
        "jti": uuid.uuid4().hex,
    }
    if sid is not None:
        payload["sid"] = sid
    token = _jwt.encode(payload, _load_or_create_secret(), algorithm=JWT_ALG)
    # PyJWT 2.x returns a str; older 1.x returned bytes. Normalise.
    if isinstance(token, bytes):
        token = token.decode("ascii")
    return {
        "access_token": token,
        "token_type": "bearer",
        "expires_in": ttl,
    }


def _set_refresh_cookie(
    response: Response, token_id: str, *, secure: bool = False,
) -> None:
    """
    Attach the refresh-token cookie to an outgoing response. Used by
    /setup, /login, and /refresh. The cookie is httponly + samesite
    strict. M15: ``secure`` is set by the caller from the request
    scheme - True over https so the cookie never travels in clear,
    omitted over plain HTTP so local-network deployments still work.
    """
    response.set_cookie(
        key=REFRESH_COOKIE_NAME,
        value=token_id,
        max_age=_refresh_ttl_seconds(),
        path=REFRESH_COOKIE_PATH,
        httponly=True,
        samesite="strict",
        secure=secure,
    )


def _clear_refresh_cookie(response: Response) -> None:
    """
    Remove the refresh-token cookie by setting it to an empty value
    with Max-Age=0. Path must match the original Set-Cookie scope or
    the browser keeps the prior cookie alive.
    """
    response.delete_cookie(
        key=REFRESH_COOKIE_NAME,
        path=REFRESH_COOKIE_PATH,
    )


def decode_token(token: str) -> Optional[Dict[str, Any]]:
    """
    Verify and decode a JWT. Returns the payload on success, ``None``
    on any failure (expired, bad signature, malformed). Callers should
    surface a generic 401 on ``None`` rather than the specific reason
    - the distinction has no operational value here.
    """
    import jwt as _jwt

    if not token:
        return None
    try:
        return _jwt.decode(token, _load_or_create_secret(), algorithms=[JWT_ALG])
    except Exception:
        return None


# ── Role hierarchy & permission map ─────────────────────────────────────────
#
# Single source of truth for the four login roles, their ordering, and
# the permission set each one carries. The frontend duplicates this map
# (in ``src/contexts/AuthContext.tsx``) so it can gate UI without
# round-trips, but the backend remains authoritative - every protected
# endpoint goes through ``require_role()`` and validates the caller's
# CURRENT role from the database.

_ROLE_RANK = {
    "viewer": 0,
    "operator": 1,
    "manager": 2,
    "admin": 3,        # sudo-root: full perms BUT cannot modify root_admin
    "root_admin": 4,
}

ALL_PERMISSIONS = (
    "dashboard.view",
    "servers.view",
    "servers.edit",
    "jobs.start",
    "jobs.stop",
    "schedules.view",
    "schedules.edit",
    "logs.view",
    "exports.view",
    "settings.edit",
    # ``settings.tunables`` gates the System Tunables page
    # (infrastructure knobs). Held by root_admin ONLY - even ``admin``
    # can't toggle JWT TTLs, HTTP pool sizes, SQLite busy timeouts,
    # etc., because those values can lock every user out of the
    # system if set wrong. The frontend hides the Tunables sub-tab
    # when this perm is absent; the backend gates the PATCH route on
    # the same perm.
    "settings.tunables",
    "users.manage",
    "db_admin.access",
    "sync.view",
    "sync.edit",
)

# Permission bundle for ``admin`` - everything except settings.tunables.
# Built once at module import; if you add a new entry to ALL_PERMISSIONS
# and it should be admin-visible too, no code change here is needed.
_ADMIN_PERMS: List[str] = [p for p in ALL_PERMISSIONS if p != "settings.tunables"]


def effective_permissions_for(username: str, role: str) -> List[str]:
    """
    Resolve the effective permission set for ``username`` at ``role``.

    Baseline = ``ROLE_PERMISSIONS[role]``. The user's per-row
    ``extra_permissions`` adds permissions on top; ``revoked_permissions``
    removes them.

    Safety rules:
      * ``root_admin`` is **immune to revokes** - the role always
        resolves to the full ``ALL_PERMISSIONS`` set so an accidental
        revoke can't lock the only restore path out of the system.
      * Unknown permissions in either list are ignored silently
        (they couldn't have effect anyway).
      * Empty / missing username → role baseline only.
      * ``effective_role`` (post View Mode drop) is what the caller
        usually passes, so a root_admin in viewer view-mode resolves
        to viewer's baseline - view-mode trumps grants.
    """
    if role == "root_admin":
        # Always full; ignore revokes so root admin can't be
        # accidentally locked out via PATCH.
        return list(ALL_PERMISSIONS)
    baseline = list(ROLE_PERMISSIONS.get(role, []))
    if not username:
        return baseline
    try:
        grants = auth_db.get_user_permission_grants(username)
    except Exception:
        log.exception(
            "effective_permissions_for: grants lookup failed for %r; "
            "falling back to the role baseline (per-user grants and "
            "revokes are NOT applied this call).",
            username,
        )
        return baseline
    extras = [p for p in (grants.get("extra") or []) if p in ALL_PERMISSIONS]
    revoked = set(p for p in (grants.get("revoked") or []) if p in ALL_PERMISSIONS)
    result: List[str] = []
    for p in baseline:
        if p not in revoked:
            result.append(p)
    for p in extras:
        if p not in result:
            result.append(p)
    return result

ROLE_PERMISSIONS: Dict[str, List[str]] = {
    "viewer":     ["dashboard.view", "servers.view"],
    "operator":   ["dashboard.view", "servers.view", "logs.view", "exports.view",
                   "jobs.start", "schedules.view"],
    "manager":    ["dashboard.view", "servers.view", "logs.view", "exports.view",
                   "jobs.start", "jobs.stop", "schedules.view", "schedules.edit",
                   "sync.view"],
    # ``admin`` carries every permission EXCEPT settings.tunables - the
    # infrastructure-knob bundle is root_admin-exclusive (see
    # ALL_PERMISSIONS comment). Per-row guards via ``can_modify_user``
    # still prevent admin from touching the root_admin row.
    "admin":      list(_ADMIN_PERMS),
    "root_admin": list(ALL_PERMISSIONS),
}


def role_at_least(actual: str, minimum: str) -> bool:
    """True iff ``actual`` sits at or above ``minimum`` in the
    hierarchy. ``db_admin`` is NOT a login role and always returns
    False here - login-protected routes never accept a db_admin
    token."""
    if actual not in _ROLE_RANK or minimum not in _ROLE_RANK:
        return False
    return _ROLE_RANK[actual] >= _ROLE_RANK[minimum]


# ── View Mode (server-side privilege drop) ──────────────────────────────────
#
# End users with at least one valid drop target (anyone except viewer)
# can preview the UI as a lesser role. The override is server-enforced:
# require_role() below looks up the caller's session id (``sid`` claim,
# = the refresh-token cookie value) in _VIEW_MODE_SESSIONS before
# deciding whether to admit the request, so every protected endpoint
# actually honours the reduced privilege.
#
# Why key off ``sid`` and not ``jti``?
#   * ``jti`` is unique per access token. Keying off it would erase
#     the end user's override on every /refresh, which fires every
#     ~30 minutes and on every page reload.
#   * ``sid`` is the refresh-token id. It stays constant across every
#     access JWT minted from the same refresh cookie, so the override
#     survives /refresh and survives page reloads (which themselves
#     drive a /refresh). The override clears when:
#       (a) the end user hits /view-mode/exit explicitly, or
#       (b) /logout revokes the refresh row, taking the sid with it,
#       (c) a password change / user delete revokes the refresh row,
#       (d) the container restarts (in-memory dict wiped).
#
# Lock-guarded for thread safety. In-memory only - cases (a)-(c) all
# clear deterministically; case (d) is acceptable per spec.

# Per-role drop tables. The dropdown shown in the UI matches the
# entries here; the backend re-validates on every /view-mode/enter
# call so a hand-crafted request that names a role outside its real
# role's drop set is rejected with 403.
_VIEW_MODE_DROP_TARGETS: Dict[str, tuple] = {
    "viewer":     (),
    "operator":   ("viewer",),
    "manager":    ("operator", "viewer"),
    "admin":      ("manager", "operator", "viewer"),
    "root_admin": ("manager", "operator", "viewer"),
}

# { sid: {"real_role": str, "view_role": str, "username": str} }
_VIEW_MODE_SESSIONS: Dict[str, Dict[str, str]] = {}
_VIEW_MODE_LOCK = threading.Lock()


# ── Sudo-style elevation session table ─────────────────────────────────────────
#
# Logging in as a root_admin is the everyday-work session. Performing a
# root-level action (creating users, granting root, migrating PINs)
# requires the caller to RE-AUTH with their own password. The
# re-auth grants a time-limited elevation (default 10 minutes,
# tunable). The elevation is keyed on the JWT's ``sid`` so it
# survives /refresh but clears on /logout and on container restart.
#
# This is intentionally separate from View Mode (which DROPS privilege
# for the end user to safely click through a lower-tier view) and from
# the general role-rank gate (which the regular session JWT satisfies
# for non-elevated paths). Together: role-rank says "you are allowed to
# call this endpoint", elevation says "you have proven possession of
# the password recently enough that we trust this destructive write".

_ELEVATION_SESSIONS: Dict[str, float] = {}
_ELEVATION_LOCK = threading.Lock()

# Default cached-elevation TTL in seconds. Picked to match common
# sudo defaults (10 minutes). The value can be overridden via the
# ``elevation_ttl_seconds`` setting; floor 60 seconds, ceiling 3600.
_DEFAULT_ELEVATION_TTL = 600


def _elevation_ttl_seconds() -> int:
    """
    Read the elevation TTL from settings, clamped to a sane range.
    Floor of 60 keeps elevation a real proof; ceiling of 3600 keeps
    it from drifting into "the end user left their session open all
    day" territory.
    """
    try:
        from server.persistence import load_settings
        v = (load_settings() or {}).get("elevation_ttl_seconds")
        if v is None:
            v = _DEFAULT_ELEVATION_TTL
        return max(60, min(3600, int(v)))
    except Exception:
        return _DEFAULT_ELEVATION_TTL


def _elevation_stamp(sid: str) -> float:
    """Record (or refresh) an elevation for this session. Returns the
    new expiry timestamp in unix seconds."""
    expires_at = time.time() + _elevation_ttl_seconds()
    with _ELEVATION_LOCK:
        _ELEVATION_SESSIONS[sid] = expires_at
    return expires_at


def _elevation_valid(sid: Optional[str]) -> bool:
    """Return True iff this session has an unexpired elevation entry.
    Expired entries are purged on read so the table doesn't grow."""
    if not sid:
        return False
    now = time.time()
    with _ELEVATION_LOCK:
        exp = _ELEVATION_SESSIONS.get(sid)
        if exp is None:
            return False
        if exp < now:
            _ELEVATION_SESSIONS.pop(sid, None)
            return False
        return True


def _elevation_expires_at(sid: Optional[str]) -> Optional[float]:
    """Return the unix timestamp this session's elevation expires at,
    or None if no valid elevation exists."""
    if not sid:
        return None
    now = time.time()
    with _ELEVATION_LOCK:
        exp = _ELEVATION_SESSIONS.get(sid)
        if exp is None or exp < now:
            if exp is not None:
                _ELEVATION_SESSIONS.pop(sid, None)
            return None
        return exp


def _elevation_clear(sid: str) -> None:
    """Drop a session's elevation. Idempotent. Called on logout and
    when the end user hits the 'drop elevation' button."""
    with _ELEVATION_LOCK:
        _ELEVATION_SESSIONS.pop(sid, None)


def _elevation_reset_for_tests() -> None:
    """Wipe the elevation table. Not called from production code."""
    with _ELEVATION_LOCK:
        _ELEVATION_SESSIONS.clear()


def require_elevation():
    """
    FastAPI dependency factory: gate an endpoint on a valid elevation
    for the caller's session id. The caller must ALSO be a root_admin
    (real role, not view-mode role) - elevation without root_admin
    role is meaningless and rejected so we fail loudly rather than
    quietly succeed.

    Returned dict matches require_role()'s shape so handlers reading
    ``user['username']`` etc. don't need to special-case the elevated
    path.
    """
    base = require_role("root_admin")

    def _dep(request: Request) -> Dict[str, Any]:
        user = base(request)
        ctx = getattr(request.state, "auth", None) or {}
        sid = ctx.get("sid") if isinstance(ctx, dict) else None
        if not _elevation_valid(sid):
            raise HTTPException(
                status_code=403,
                detail=(
                    "This action requires a recent password re-confirmation. "
                    "Re-authenticate via POST /api/auth/elevate."
                ),
            )
        return user

    return _dep


def _view_mode_lookup(sid: Optional[str]) -> Optional[Dict[str, str]]:
    """Return the view-mode entry for this session id or None."""
    if not sid:
        return None
    with _VIEW_MODE_LOCK:
        entry = _VIEW_MODE_SESSIONS.get(sid)
        # Return a copy so the caller can't mutate the live dict.
        return dict(entry) if entry else None


def _view_mode_set(sid: str, real_role: str, view_role: str, username: str) -> None:
    """Insert / replace a view-mode entry for this session id."""
    with _VIEW_MODE_LOCK:
        _VIEW_MODE_SESSIONS[sid] = {
            "real_role": real_role,
            "view_role": view_role,
            "username": username,
        }


def _view_mode_clear(sid: str) -> None:
    """Remove a view-mode entry. Idempotent."""
    with _VIEW_MODE_LOCK:
        _VIEW_MODE_SESSIONS.pop(sid, None)


def _view_mode_migrate(old_sid: str, new_sid: str) -> None:
    """Move a view-mode entry from one sid to another. Idempotent.

    AUTH-08 rotates the refresh token on every /refresh, and the JWT
    ``sid`` IS the refresh-token id - so the sid changes each refresh.
    Without this migration an active View Mode override would be
    orphaned on the old sid and silently lost on the next refresh (the
    admin would pop back to their real role mid-session). Moving the
    entry preserves the documented "view mode survives /refresh"
    contract.
    """
    if not old_sid or not new_sid or old_sid == new_sid:
        return
    with _VIEW_MODE_LOCK:
        entry = _VIEW_MODE_SESSIONS.pop(old_sid, None)
        if entry is not None:
            _VIEW_MODE_SESSIONS[new_sid] = entry


def _effective_role_for(payload: Dict[str, Any], real_role: str) -> str:
    """
    Resolve the effective role for an authenticated request. If the
    JWT's sid is present in _VIEW_MODE_SESSIONS AND the recorded
    real_role still matches the live DB role (the end user wasn't
    demoted out from under their override), the view_role is returned.
    Otherwise the real role is returned and any stale entry is purged.
    """
    sid = payload.get("sid") if payload else None
    entry = _view_mode_lookup(sid) if isinstance(sid, str) else None
    if entry and entry.get("real_role") == real_role:
        return entry.get("view_role") or real_role
    # Stale entry (role changed underneath us) - drop it.
    if entry and isinstance(sid, str):
        _view_mode_clear(sid)
    return real_role


def can_modify_user(caller: Dict[str, Any], target: Dict[str, Any]) -> bool:
    """
    Per-row protection rule for the user-management routes. ``caller``
    and ``target`` are user rows; each carries ``role`` + ``username``.

      * ``admin`` has every user-management permission EXCEPT it can
        never modify or delete a ``root_admin`` row - the sudo-root
        constraint.
      * A ``root_admin`` may always modify a non-root row.
      * Root-on-root is restricted: a ``root_admin`` may modify
        another ``root_admin`` only when the caller is the PRIMARY
        root (the lowest-id root_admin row, i.e. the first-run
        account) or the caller IS the target (self). This stops a
        secondary root_admin from demoting a peer or the primary
        root; a secondary root may still demote only itself.
      * Roles below ``admin`` do not manage users at all.

    The primary root is resolved via ``auth_db.get_root_admin()``,
    which returns the first-created (lowest-id) root_admin row.
    """
    caller_role = caller.get("role")
    target_role = target.get("role")
    if caller_role == "admin":
        return target_role != "root_admin"
    if caller_role == "root_admin":
        if target_role != "root_admin":
            return True
        caller_name = caller.get("username")
        target_name = target.get("username")
        if caller_name and caller_name == target_name:
            return True  # a root_admin may always modify itself
        primary = auth_db.get_root_admin() or {}
        primary_name = primary.get("username")
        return bool(primary_name and caller_name == primary_name)
    return False


def require_permission(permission: str):
    """
    FastAPI dependency factory that gates an endpoint on a SPECIFIC
    permission string rather than a role rank. Built on top of
    :func:`require_role` so the View Mode + immediacy guarantees still
    apply, but the final admit check uses
    :func:`effective_permissions_for` so per-user grants and revokes
    are honoured.

    Use this on endpoints whose access should follow a granted
    permission rather than the caller's role rank. ``require_role``
    stays the right choice for endpoints whose access tracks the
    role hierarchy as a whole (e.g. "anyone manager or up can stop
    jobs"). Tunables / Access Control endpoints use this so a viewer
    with a granted ``settings.tunables`` permission really can edit
    tunables backend-side, not just see the UI.
    """
    if permission not in ALL_PERMISSIONS:
        raise ValueError(f"Invalid permission {permission!r}")

    def _dep(request: Request) -> Dict[str, Any]:
        ctx = getattr(request.state, "auth", None) or {}
        username = ctx.get("sub")
        if not username:
            raise HTTPException(status_code=401, detail="Not authenticated.")
        user = auth_db.get_user(username)
        if user is None:
            raise HTTPException(
                status_code=401,
                detail="Account no longer exists; please log in again.",
            )
        real_role = user["role"]
        effective_role = _effective_role_for(ctx, real_role)
        effective_perms = effective_permissions_for(username, effective_role)
        if permission not in effective_perms:
            raise HTTPException(
                status_code=403,
                detail=f"This action requires the {permission!r} permission.",
            )
        return {
            "username": user["username"],
            "role": effective_role,
            "real_role": real_role,
            "effective_role": effective_role,
            "display_name": user.get("display_name"),
        }

    return _dep


def require_role(minimum: str):
    """
    FastAPI dependency factory. Returns a dependency that:

      1. Reads the JWT payload from ``request.state.auth`` (set by
         the middleware in ``server/app.py`` on every authenticated
         request).
      2. Re-reads the CURRENT role for that user from auth_db. This
         is the immediacy guarantee from multiuserauth.md §8.7 - a
         role change via ``PATCH /api/auth/users/{u}`` is visible to
         the affected user on their next request, no re-login.
      3. Resolves the EFFECTIVE role: if the JWT's jti is in the
         View Mode session map, the recorded view_role applies
         instead of the real role. This is what makes View Mode a
         real privilege drop, not a UI-only filter.
      4. Rejects with 403 if the effective role is below ``minimum``.
      5. Returns a dict
         ``{username, role, real_role, effective_role, display_name}``
         for the handler to use. ``role`` is kept as an alias for
         ``effective_role`` so existing handlers that read ``role``
         get the gated value automatically.

    Cost: one DB read + one dict lookup per protected request.
    """
    if minimum not in _ROLE_RANK:
        raise ValueError(f"Invalid minimum role {minimum!r}")

    def _dep(request: Request) -> Dict[str, Any]:
        ctx = getattr(request.state, "auth", None) or {}
        username = ctx.get("sub")
        if not username:
            raise HTTPException(status_code=401, detail="Not authenticated.")
        # Re-read live role from DB (immediacy guarantee).
        user = auth_db.get_user(username)
        if user is None:
            raise HTTPException(
                status_code=401,
                detail="Account no longer exists; please log in again.",
            )
        real_role = user["role"]
        effective_role = _effective_role_for(ctx, real_role)
        if not role_at_least(effective_role, minimum):
            raise HTTPException(
                status_code=403,
                detail=f"This action requires the {minimum!r} role or higher.",
            )
        return {
            "username": user["username"],
            "role": effective_role,           # alias: existing handlers read this
            "real_role": real_role,
            "effective_role": effective_role,
            "display_name": user.get("display_name"),
        }

    return _dep


# ── Pydantic request models ──────────────────────────────────────────────────

class SetupIn(BaseModel):
    username: str = Field(description="Root admin username (any string).")
    password: str = Field(description="Root admin password (>= 8 chars).")
    display_name: Optional[str] = Field(
        default=None,
        description="Optional display name shown in the UI instead of the username.",
    )


class LoginIn(BaseModel):
    username: str
    password: str


class VerifyPasswordIn(BaseModel):
    """
    Server-side verification for the Switch View Mode flow.
    Body carries only the password; the username is read from the
    caller's JWT to prevent end users from verifying anyone else's
    credentials.
    """
    password: str


class CreateUserIn(BaseModel):
    username: str
    password: str
    role: str = Field(
        default="operator",
        description="'viewer', 'operator', or 'manager'. root_admin cannot be created here.",
    )
    display_name: Optional[str] = None


class PatchUserIn(BaseModel):
    """
    Both fields optional - supply only what's changing. Setting
    ``display_name`` to an empty string clears it.
    """
    role: Optional[str] = Field(default=None)
    display_name: Optional[str] = Field(default=None)
    # PATCH-with-empty-string semantics for clearing display_name need
    # an explicit flag because Pydantic's default-coercion can't
    # distinguish "field omitted" from "field present and empty."
    clear_display_name: bool = Field(default=False)


class ResetPasswordIn(BaseModel):
    new_password: str = Field(description=">= 8 chars; same rules as create_user.")


class DisplayNameIn(BaseModel):
    display_name: str = Field(default="", description="Empty string clears the display name.")


# Database Admin Account request bodies. The admin account these
# endpoints manage is the per-write gate for User Management. Each
# mutating call carries its own admin password as the request-level
# credential - no JWT involved.

class AdminSetupIn(BaseModel):
    username: str = Field(description="Admin username to create.")
    password: str = Field(description=">= 8 chars; same rules as auth_db.create_user.")


class AdminUpdateIn(BaseModel):
    current_password: str = Field(description="Current admin password - required to authorise the change.")
    new_username: Optional[str] = Field(default=None, description="New username; omit to keep existing.")
    new_password: Optional[str] = Field(default=None, description="New password; omit to keep existing.")


class AdminVerifyIn(BaseModel):
    username: str
    password: str


# ── Router ───────────────────────────────────────────────────────────────────

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.get("/status")
def auth_status() -> Dict[str, Any]:
    """
    Public probe used by the frontend on first load. Always returns
    ``auth_enabled: true`` (auth is not optional).

    Fields:

      * ``setup_needed`` - True when no user rows exist yet; the
        frontend renders ``<SetupPage />`` (the two-step variant)
        instead of ``<LoginPage />``.
    """
    return {
        "auth_enabled": True,
        "setup_needed": not auth_db.has_any_users(),
    }


@router.post("/setup")
def auth_setup(body: SetupIn, request: Request, response: Response) -> Dict[str, Any]:
    """
    Create the root admin account on first boot.

    Self-locking: once any user exists in ``app_users`` this endpoint
    returns 403 on every subsequent call. The frontend's setup screen
    re-checks ``/auth/status`` after each attempt so a race between
    two end users on first boot resolves cleanly - the loser sees a
    "Setup already completed" error and is redirected to the login
    screen.

    Mints both halves of the auth pair on success: a 30-minute access
    JWT in the body and a 7-day refresh-token cookie. The user is
    fully signed in when this call returns.
    """
    if auth_db.has_any_users():
        raise HTTPException(
            status_code=403,
            detail="Setup already completed - log in instead.",
        )
    try:
        user = auth_db.create_user(
            body.username, body.password,
            role="root_admin",
            display_name=body.display_name,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    # Issue a token immediately so the frontend can hop straight into
    # the main UI without a second round-trip through the login form.
    # Create the refresh row first so we can stamp its id into the JWT
    # as the session id - any view-mode override the end user sets in
    # this session then survives /refresh.
    auth_db.update_last_login(user["username"])
    refresh_id = auth_db.create_refresh_token(user["username"])
    _set_refresh_cookie(response, refresh_id, secure=request.url.scheme == "https")
    token = issue_token(
        user["username"], user["role"], user.get("display_name"),
        sid=refresh_id,
    )
    return {
        "user": {
            "username": user["username"],
            "role": user["role"],
            "display_name": user.get("display_name"),
        },
        **token,
    }


# ── Item 1: two-step setup (admin + root) ───────────────────────────────────

class SetupV2In(BaseModel):
    """
    Two-step setup: the end user creates their day-to-day ``admin``
    account AND a separate ``root_admin`` account in a single atomic
    request. The frontend collects both credential pairs in its
    wizard and submits them together so a half-complete setup can't
    leave the install with only one account.
    """
    admin_username: str
    admin_password: str
    admin_display_name: Optional[str] = None
    root_username: str
    root_password: str
    root_display_name: Optional[str] = None


class GrantRevokeRootIn(BaseModel):
    """Body for revoke-root: the role the demoted user should drop to.
    grant-root takes no body (the username comes from the URL path)."""
    new_role: str = Field(
        default="admin",
        description=(
            "Role to demote the user to. One of: viewer, operator, "
            "manager, admin. db_admin is rejected. The last-root-admin "
            "floor in auth_db prevents revoking the only root."
        ),
    )


@router.post("/setup-v2")
def auth_setup_v2(
    body: SetupV2In, request: Request, response: Response,
) -> Dict[str, Any]:
    """
    Item 1: atomic two-step first-boot setup. Creates one ``admin``
    account (the end user's day-to-day) AND one ``root_admin``
    account (the dedicated root credential for privileged actions),
    then signs the end user in as their admin account.

    Self-locking on ``has_any_users``: a second call against an
    initialised install returns 403.

    Validation:

      * Both usernames must differ; otherwise the second create call
        would fail with a unique-constraint error and leave the first
        account dangling.
      * Both passwords obey the bcrypt-safe length rules already
        enforced by ``auth_db.create_user``.
      * On any failure after the first user is created, the partial
        admin row is rolled back so the install stays "needs setup".
    """
    if auth_db.has_any_users():
        raise HTTPException(
            status_code=403,
            detail="Setup already completed - log in instead.",
        )
    if body.admin_username.strip() == body.root_username.strip():
        raise HTTPException(
            status_code=400,
            detail="Admin and root accounts must have different usernames.",
        )
    try:
        admin_user = auth_db.create_user(
            body.admin_username, body.admin_password,
            role="admin",
            display_name=body.admin_display_name,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"admin: {exc}")
    try:
        auth_db.create_user(
            body.root_username, body.root_password,
            role="root_admin",
            display_name=body.root_display_name,
        )
    except ValueError as exc:
        # Roll back the admin row so the install is still "needs setup".
        try:
            auth_db.delete_user(admin_user["username"])
        except Exception:  # pragma: no cover (defensive)
            log.exception(
                "setup-v2 rollback of admin row failed for %r",
                admin_user["username"],
            )
        raise HTTPException(status_code=400, detail=f"root: {exc}")
    # Sign the end user in as the admin account they just created.
    auth_db.update_last_login(admin_user["username"])
    refresh_id = auth_db.create_refresh_token(admin_user["username"])
    _set_refresh_cookie(response, refresh_id, secure=request.url.scheme == "https")
    token = issue_token(
        admin_user["username"], admin_user["role"], admin_user.get("display_name"),
        sid=refresh_id,
    )
    log.info(
        "setup-v2: created admin=%r + root=%r, signed admin in",
        admin_user["username"], body.root_username,
    )
    return {
        "user": {
            "username": admin_user["username"],
            "role": admin_user["role"],
            "display_name": admin_user.get("display_name"),
        },
        **token,
    }


@router.post("/users/{username}/grant-root")
def auth_grant_root(
    username: str,
    request: Request,
    user: Dict[str, Any] = Depends(require_elevation()),
) -> Dict[str, Any]:
    """
    Item 1: promote a user to root_admin. Requires a fresh sudo-style
    elevation (caller proved password within the last 10 minutes via
    /api/auth/elevate). Bypasses the PATCH /users/{u} "no promotion
    to root_admin" guard intentionally - this is the dedicated
    promotion path.
    """
    target = auth_db.get_user(username)
    if target is None:
        raise HTTPException(status_code=404, detail=f"No user named {username!r}.")
    if target["role"] == "root_admin":
        return {"username": username, "role": "root_admin", "noop": True}
    if target["role"] == "db_admin":
        raise HTTPException(
            status_code=400,
            detail="Cannot promote a db_admin row to root_admin.",
        )
    if not can_modify_user(user, target):
        raise HTTPException(
            status_code=403,
            detail="You are not permitted to modify this account.",
        )
    try:
        auth_db.update_role(username, "root_admin")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    log.info("grant-root: %r promoted to root_admin", username)
    return {"username": username, "role": "root_admin", "noop": False}


@router.post("/users/{username}/revoke-root")
def auth_revoke_root(
    username: str,
    body: GrantRevokeRootIn,
    request: Request,
    user: Dict[str, Any] = Depends(require_elevation()),
) -> Dict[str, Any]:
    """
    Item 1: demote a root_admin user to a lower role. Requires a
    fresh sudo-style elevation. The last-root-admin floor in
    ``auth_db.update_role`` independently protects against removing
    the only root.

    Root-on-root protection: a root_admin may demote only itself;
    demoting a DIFFERENT root_admin requires the caller to be the
    primary root (the first-run, lowest-id root_admin account).
    """
    if body.new_role == "root_admin":
        raise HTTPException(
            status_code=400,
            detail="Use grant-root to promote; revoke-root must demote.",
        )
    target = auth_db.get_user(username)
    if target is None:
        raise HTTPException(status_code=404, detail=f"No user named {username!r}.")
    if target["role"] != "root_admin":
        raise HTTPException(
            status_code=400,
            detail=f"User {username!r} is not currently a root_admin.",
        )
    if not can_modify_user(user, target):
        raise HTTPException(
            status_code=403,
            detail=(
                "A root_admin can only be demoted by itself or by the "
                "primary root admin (the first-run root account)."
            ),
        )
    try:
        auth_db.update_role(username, body.new_role)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    log.info("revoke-root: %r demoted to %r", username, body.new_role)
    return {"username": username, "role": body.new_role}


@router.post("/login")
def auth_login(body: LoginIn, request: Request, response: Response) -> Dict[str, Any]:
    """
    Validate ``(username, password)`` and return an access token.

    Failure cases (no such user, wrong password, or matching a non-
    login role like db_admin) all return 401 with the same generic
    message - no oracle for username enumeration.

    On success the response also carries a 7-day ``refresh_token``
    HttpOnly cookie at ``Path=/api/auth``. The frontend uses it
    silently via /api/auth/refresh to extend the session past the
    30-minute access-JWT expiry.
    """
    user = auth_db.verify_password(body.username, body.password)
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid username or password.")
    # db_admin is not a login role - reject silently with the same
    # 401 so an attacker can't probe for its existence.
    if user.get("role") not in _ROLE_RANK:
        raise HTTPException(status_code=401, detail="Invalid username or password.")
    # Best-effort: stamp last_login. Failure here must not block the
    # response - the end user already authenticated successfully.
    try:
        auth_db.update_last_login(user["username"])
    except Exception:  # pragma: no cover (defensive)
        log.exception("update_last_login failed for %r", user["username"])
    # Refresh row first so its id becomes the JWT's session id.
    refresh_id = auth_db.create_refresh_token(user["username"])
    _set_refresh_cookie(response, refresh_id, secure=request.url.scheme == "https")
    token = issue_token(
        user["username"], user["role"], user.get("display_name"),
        sid=refresh_id,
    )
    return {
        "user": {
            "username": user["username"],
            "role": user["role"],
            "display_name": user.get("display_name"),
        },
        **token,
    }


@router.post("/refresh")
def auth_refresh(
    request: Request,
    response: Response,
    refresh_token: Optional[str] = Cookie(default=None, alias=REFRESH_COOKIE_NAME),
) -> Dict[str, Any]:
    """
    Mint a new access JWT from a valid refresh-token cookie, ROTATING
    the refresh token in the process (AUTH-08).

    Public endpoint: no Authorization header required. The cookie is
    the sole credential, validated against the refresh_tokens table.
    Returns 401 if the cookie is absent, expired, revoked, replayed,
    or points at a user that no longer exists.

    Rotation: every call consumes the presented refresh token and
    issues a fresh one (new cookie value + new JWT ``sid``). A token
    that was already rotated, presented again, is treated as a replay
    - ``auth_db.rotate_refresh_token`` then revokes every refresh
    token for that user. This shrinks a stolen cookie's usefulness
    from the full TTL window to a single refresh cycle.

    The frontend calls this:
      * Once on page mount, before rendering anything, to detect
        whether the previous session can be silently resumed.
      * Transparently on any mid-session 401 from a protected route
        (access JWT expired while the tab was idle), then retries
        the original call with the new access token.
    """
    if not refresh_token:
        raise HTTPException(status_code=401, detail="No refresh token.")
    # Rotate: consume the presented token and obtain its replacement.
    # rotate_refresh_token returns None for an unknown / expired /
    # revoked / replayed token (and, for a replay, has already revoked
    # every token for that user) - all collapse to one generic 401.
    rotated = auth_db.rotate_refresh_token(refresh_token)
    if rotated is None:
        _view_mode_clear(refresh_token)
        _clear_refresh_cookie(response)
        raise HTTPException(status_code=401, detail="Refresh token invalid or expired.")
    username, new_refresh_token = rotated
    user = auth_db.get_user(username)
    if user is None or user.get("role") not in _ROLE_RANK:
        # User was deleted between issuance and now. The old token is
        # already consumed by the rotation above; revoke the
        # just-issued replacement too and drop any view-mode entry.
        _view_mode_clear(refresh_token)
        auth_db.revoke_refresh_token(new_refresh_token)
        _clear_refresh_cookie(response)
        raise HTTPException(status_code=401, detail="Account no longer exists.")
    # Rotation changed the refresh-token id, and the JWT sid IS that
    # id - carry any active View Mode override across to the new sid,
    # otherwise it would be orphaned and the admin would silently pop
    # back to their real role on this very refresh.
    _view_mode_migrate(refresh_token, new_refresh_token)
    _set_refresh_cookie(
        response, new_refresh_token,
        secure=request.url.scheme == "https",
    )
    token = issue_token(
        user["username"], user["role"], user.get("display_name"),
        sid=new_refresh_token,
    )
    return {
        "user": {
            "username": user["username"],
            "role": user["role"],
            "display_name": user.get("display_name"),
        },
        **token,
    }


@router.post("/logout")
def auth_logout(
    response: Response,
    refresh_token: Optional[str] = Cookie(default=None, alias=REFRESH_COOKIE_NAME),
) -> Dict[str, Any]:
    """
    Revoke the caller's refresh token and clear the cookie. The
    access JWT is bearer-only and held in client memory - the frontend
    drops its in-memory copy on logout. Combined the two halves make
    the session unusable: the browser can't mint new access tokens
    (cookie gone + DB row revoked) and the discarded in-memory token
    will hit 401 on its first protected call anyway.

    Also drops the in-memory View Mode entry keyed by this session's
    refresh-token id. That's the single supported path for clearing
    a view-mode override - a page refresh deliberately preserves it
    because the same refresh cookie still backs the new access token.
    """
    if refresh_token:
        try:
            _view_mode_clear(refresh_token)
            auth_db.revoke_refresh_token(refresh_token)
        except Exception:  # pragma: no cover (defensive)
            log.exception("revoke_refresh_token failed on logout")
    _clear_refresh_cookie(response)
    return {"logged_out": True}


@router.get("/me")
def auth_me(user: Dict[str, Any] = Depends(require_role("viewer"))) -> Dict[str, Any]:
    """
    Return the calling user's identity, effective role, and computed
    permission set. Frontend hits this once on mount (after the
    silent /refresh) and again after every View Mode enter/exit to
    rebuild its auth context.

    Response shape:
      * ``real_role``      - the user's DB role.
      * ``effective_role`` - the role driving permission gating.
        Equals ``real_role`` unless an active View Mode session
        applies, in which case it's the dropped role.
      * ``in_view_mode``   - bool: real_role != effective_role.
      * ``permissions``    - derived from effective_role, so a
        viewer-dropped admin sees only viewer perms.
      * ``created_at`` / ``last_login`` - kept on the response so
        the Account Settings panel can render them without a second
        round-trip.

    Every login role (viewer through root_admin) can call this.
    """
    real_role = user["real_role"]
    effective_role = user["effective_role"]
    # Layer per-user grants + revokes on top of the effective role's
    # baseline. ``effective_permissions_for`` reads the per-user grant
    # list from auth.db and resolves the final set. Root admin is
    # immune to revokes (always full set).
    perms = effective_permissions_for(user["username"], effective_role)
    full = auth_db.get_user(user["username"]) or {}
    return {
        "username": user["username"],
        "display_name": user.get("display_name"),
        "real_role": real_role,
        "effective_role": effective_role,
        "in_view_mode": real_role != effective_role,
        "permissions": perms,
        "created_at": full.get("created_at"),
        "last_login": full.get("last_login"),
    }


@router.post("/verify-password")
def auth_verify_password(
    body: VerifyPasswordIn,
    user: Dict[str, Any] = Depends(require_role("viewer")),
) -> Dict[str, Any]:
    """
    Verify the calling user's password without issuing a new token.
    Used by the change-password forms to confirm the current password
    before applying changes.

    Username is read from the JWT - the end user can never verify
    anyone else's credentials through this endpoint. The role check
    compares against ``real_role`` (the DB row) rather than
    ``effective_role`` so a user currently in View Mode can still
    verify their own password.
    """
    verified = auth_db.verify_password(user["username"], body.password)
    valid = (
        verified is not None
        and verified.get("role") == user["real_role"]
    )
    return {"valid": bool(valid)}


# ── Sudo-style elevation (Item 1 of admin-management plan) ──────────────────

class ElevateIn(BaseModel):
    password: str = Field(
        description=(
            "Caller's own current password. Verified against the DB; "
            "on success, an elevation flag is stamped on the caller's "
            "session for the configured TTL (default 10 minutes)."
        ),
    )


@router.post("/elevate")
def auth_elevate(
    body: ElevateIn,
    request: Request,
    user: Dict[str, Any] = Depends(require_role("root_admin")),
) -> Dict[str, Any]:
    """
    Prove possession of the caller's password and grant a time-limited
    elevation flag on the current session. Elevated sessions can call
    endpoints gated by :func:`require_elevation` (root_admin-only
    destructive writes that touch user accounts).

    Requires the caller's real role to be ``root_admin`` (View Mode
    drops do not elevate the lower view). Failure cases (wrong
    password, View Mode active) return 401 / 403 respectively without
    stamping the elevation.

    Returns ``{elevated_until}`` as a unix timestamp so the frontend
    can render a countdown or pre-warn the end user before expiry.
    """
    if user["real_role"] != "root_admin":
        raise HTTPException(
            status_code=403,
            detail="Only the root_admin role can elevate.",
        )
    # The password check uses the live DB row, like /verify-password.
    verified = auth_db.verify_password(user["username"], body.password)
    if verified is None or verified.get("role") != "root_admin":
        # Same generic 401 as /login so we don't leak any oracle.
        raise HTTPException(status_code=401, detail="Invalid password.")
    ctx = getattr(request.state, "auth", None) or {}
    sid = ctx.get("sid") if isinstance(ctx, dict) else None
    if not isinstance(sid, str) or not sid:
        # Defensive: a JWT without a sid can't be elevation-tracked.
        # Force the end user to log in again so a real sid is minted.
        raise HTTPException(
            status_code=400,
            detail="Session has no id; log out and back in, then retry.",
        )
    expires_at = _elevation_stamp(sid)
    log.info(
        "Elevation granted to %r (sid=%s, expires_at=%.0f)",
        user["username"], sid, expires_at,
    )
    return {"elevated_until": expires_at}


@router.get("/elevation/status")
def auth_elevation_status(
    request: Request,
    user: Dict[str, Any] = Depends(require_role("root_admin")),
) -> Dict[str, Any]:
    """
    Report whether the caller's session currently holds a valid
    elevation, and if so when it expires. Used by the frontend to
    decide whether to show the ElevationModal before a root action
    or proceed directly.
    """
    ctx = getattr(request.state, "auth", None) or {}
    sid = ctx.get("sid") if isinstance(ctx, dict) else None
    expires_at = _elevation_expires_at(sid if isinstance(sid, str) else None)
    return {
        "elevated": expires_at is not None,
        "elevated_until": expires_at,
        "ttl_seconds": _elevation_ttl_seconds(),
    }


@router.post("/elevation/clear")
def auth_elevation_clear(
    request: Request,
    user: Dict[str, Any] = Depends(require_role("root_admin")),
) -> Dict[str, Any]:
    """
    Drop the caller's elevation immediately. Equivalent to ``sudo -k``.
    Idempotent: succeeds whether or not an elevation was active.
    """
    ctx = getattr(request.state, "auth", None) or {}
    sid = ctx.get("sid") if isinstance(ctx, dict) else None
    if isinstance(sid, str) and sid:
        _elevation_clear(sid)
    return {"ok": True}


# ── View Mode entry / exit ──────────────────────────────────────────────────

class ViewModeEnterIn(BaseModel):
    target_role: str = Field(description="Role to view the UI as. Must be strictly lower than real role.")
    # Optional because dropping to a lower role does not require a
    # password - only raising the visible role does. Frontend passes
    # an empty string in the down-direction case.
    password: str = Field(default="", description="Caller's current password. Required only when raising the visible role.")


class ViewModeExitIn(BaseModel):
    password: str = Field(description="Caller's current password - always required, since exit always raises the visible role.")


@router.post("/view-mode/enter")
def view_mode_enter(
    body: ViewModeEnterIn,
    request: Request,
    user: Dict[str, Any] = Depends(require_role("viewer")),
) -> Dict[str, Any]:
    """
    Open or update a View Mode session for the caller. Both entering
    fresh AND switching between dropped roles go through this endpoint;
    the recorded view_role is overwritten if an entry already exists.

    Direction-aware password rule:
      * If the new ``target_role`` ranks *above* the caller's current
        effective role, the password is required (re-verified against
        the DB).
      * If the new target ranks at or below the effective role, no
        password check runs. The end user already has the higher
        privilege, so dropping further is free.

    Other validation:
      * ``target_role`` must be in the caller's drop set
        (end user/manager/admin/root_admin only; viewer has no
        targets and is rejected outright).

    The session entry is keyed by the JWT's ``sid`` (= refresh-token
    id) so it survives /refresh and page reloads. It clears on
    /view-mode/exit, /logout, password change / user delete (refresh
    rows revoked), or container restart.
    """
    real_role = user["real_role"]
    effective_role = user["effective_role"]
    allowed = _VIEW_MODE_DROP_TARGETS.get(real_role, ())
    if not allowed:
        raise HTTPException(
            status_code=403,
            detail="Your role has no valid View Mode targets.",
        )
    if body.target_role not in allowed:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Target role {body.target_role!r} is not a valid drop for "
                f"{real_role!r}. Allowed: {', '.join(allowed)}."
            ),
        )

    target_rank = _ROLE_RANK.get(body.target_role, -1)
    effective_rank = _ROLE_RANK.get(effective_role, -1)
    raising = target_rank > effective_rank
    if raising:
        if not body.password:
            raise HTTPException(
                status_code=400,
                detail="Password is required to raise the visible role.",
            )
        verified = auth_db.verify_password(user["username"], body.password)
        if verified is None or verified.get("role") != real_role:
            raise HTTPException(status_code=401, detail="Password incorrect.")

    ctx = getattr(request.state, "auth", None) or {}
    sid = ctx.get("sid")
    if not isinstance(sid, str) or not sid:
        # JWTs minted before the sid claim landed won't have one.
        # Tell the end user to log out and back in so a fresh JWT
        # with a real session id is issued.
        raise HTTPException(
            status_code=409,
            detail="Stale token format; log out and back in to enable View Mode.",
        )
    # Entering or changing View Mode is a privilege-context change, so
    # any banked sudo elevation must be dropped (AUTH-04): otherwise an
    # elevation minted as root_admin would survive the drop to a lower
    # view role and still pass require_elevation() gates.
    _elevation_clear(sid)
    _view_mode_set(sid, real_role=real_role, view_role=body.target_role, username=user["username"])
    return {
        "in_view_mode": True,
        "real_role": real_role,
        "effective_role": body.target_role,
    }


@router.post("/view-mode/exit")
def view_mode_exit(
    body: ViewModeExitIn,
    request: Request,
    user: Dict[str, Any] = Depends(require_role("viewer")),
) -> Dict[str, Any]:
    """
    Close the caller's active View Mode session, restoring full real-
    role access. Password is always required - exit always raises the
    visible role, and a hostile page in another tab must not be able
    to silently elevate the end user out of their dropped view.
    Idempotent: if there is no entry to clear, returns 200 anyway.
    """
    if not body.password:
        raise HTTPException(
            status_code=400,
            detail="Password is required to restore full access.",
        )
    verified = auth_db.verify_password(user["username"], body.password)
    if verified is None or verified.get("role") != user["real_role"]:
        raise HTTPException(status_code=401, detail="Password incorrect.")
    ctx = getattr(request.state, "auth", None) or {}
    sid = ctx.get("sid")
    if isinstance(sid, str) and sid:
        _view_mode_clear(sid)
    return {
        "in_view_mode": False,
        "real_role": user["real_role"],
        "effective_role": user["real_role"],
    }


# ── User CRUD (admin + root_admin) ──────────────────────────────────────────
#
# Both ``admin`` and ``root_admin`` can manage other users. The
# difference is enforced via ``can_modify_user``:
#
#   * ``admin`` cannot touch the ``root_admin`` row (no role change,
#     no rename, no password reset, no delete).
#   * ``root_admin`` can touch any row.
#
# Neither role can promote a user to ``root_admin`` via this surface -
# the only way to create a root_admin row is the one-time first-boot
# ``/setup`` flow. The new ``admin`` role IS creatable here, however,
# so a root_admin can grant sudo-root access at runtime.

@router.get("/users")
def auth_list_users(
    user: Dict[str, Any] = Depends(require_role("admin")),
) -> Dict[str, Any]:
    """Return every login user (db_admin excluded). admin + root_admin."""
    return {"users": auth_db.list_users()}


@router.post("/users")
def auth_create_user(
    body: CreateUserIn,
    user: Dict[str, Any] = Depends(require_elevation()),
) -> Dict[str, Any]:
    """
    Create a viewer / end user / manager / admin account. The
    ``root_admin`` role cannot be created here - the only way to mint
    one is the one-time ``/setup`` flow on first boot.
    """
    if body.role not in ("viewer", "operator", "manager", "admin"):
        raise HTTPException(
            status_code=400,
            detail="Role must be one of: viewer, operator, manager, admin. "
                   "root_admin cannot be created here; db_admin is created "
                   "via Settings → Accounts → Database Admin Account.",
        )
    try:
        new_user = auth_db.create_user(
            body.username, body.password, body.role,
            display_name=body.display_name,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {
        "username": new_user["username"],
        "role": new_user["role"],
        "display_name": new_user.get("display_name"),
        "id": new_user["id"],
    }


@router.patch("/users/{username}")
def auth_update_user(
    username: str,
    body: PatchUserIn,
    user: Dict[str, Any] = Depends(require_elevation()),
) -> Dict[str, Any]:
    """
    Update role and/or display_name for a user. admin + root_admin.

    Per-row protection (sudo-root constraint):
      * admin cannot modify a root_admin row - 403.
      * Nobody can change a root_admin's role via this endpoint (would
        risk locking root out if the only one is demoted).
      * Nobody can promote a user to root_admin via this endpoint.
      * Nobody can set role to db_admin - auth_db.update_role rejects
        that internally with a clear ValueError.
    """
    target = auth_db.get_user(username)
    if target is None:
        raise HTTPException(status_code=404, detail=f"No user named {username!r}.")
    if not can_modify_user(user, target):
        raise HTTPException(
            status_code=403,
            detail=(
                "A root_admin row can only be modified by the primary "
                "root admin or by itself; the admin role cannot modify "
                "a root_admin row at all."
            ),
        )
    if body.role is not None:
        if target["role"] == "root_admin":
            raise HTTPException(
                status_code=400,
                detail="Cannot change a root_admin's role from this endpoint.",
            )
        if body.role == "root_admin":
            raise HTTPException(
                status_code=400,
                detail="Cannot promote a user to root_admin from this endpoint.",
            )
        try:
            auth_db.update_role(username, body.role)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
    if body.clear_display_name:
        try:
            auth_db.update_display_name(username, None)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
    elif body.display_name is not None:
        try:
            auth_db.update_display_name(username, body.display_name)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
    updated = auth_db.get_user(username) or {}
    return {
        "username": updated.get("username"),
        "role": updated.get("role"),
        "display_name": updated.get("display_name"),
    }


@router.post("/users/{username}/reset-password")
def auth_reset_password(
    username: str,
    body: ResetPasswordIn,
    user: Dict[str, Any] = Depends(require_elevation()),
) -> Dict[str, Any]:
    """
    Set a user's password. admin + root_admin. admin cannot reset
    a root_admin row's password (sudo-root constraint).

    Side effect: revokes every refresh token belonging to the target
    user. Any active session they had on other devices stops being
    able to mint new access tokens the moment its current one expires.
    """
    target = auth_db.get_user(username)
    if target is None:
        raise HTTPException(status_code=404, detail=f"No user named {username!r}.")
    if not can_modify_user(user, target):
        raise HTTPException(
            status_code=403,
            detail=(
                "A root_admin row's password can only be reset by the "
                "primary root admin or by itself; the admin role cannot "
                "reset a root_admin row's password."
            ),
        )
    try:
        auth_db.update_password(username, body.new_password)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    try:
        auth_db.revoke_all_for_user(username)
    except Exception:  # pragma: no cover (defensive)
        log.exception("revoke_all_for_user failed after password reset for %r", username)
    return {"username": username, "reset": True}


@router.delete("/users/{username}")
def auth_delete_user(
    username: str,
    user: Dict[str, Any] = Depends(require_elevation()),
) -> Dict[str, Any]:
    """
    Delete a user. admin + root_admin. Refuses to delete any
    root_admin row (otherwise the last root could be removed,
    locking everyone out). admin cannot delete a root_admin row.
    """
    target = auth_db.get_user(username)
    if target is None:
        raise HTTPException(status_code=404, detail=f"No user named {username!r}.")
    if target["role"] == "root_admin":
        raise HTTPException(
            status_code=400,
            detail="Cannot delete a root_admin row.",
        )
    if not can_modify_user(user, target):
        raise HTTPException(
            status_code=403,
            detail="The admin role cannot delete the root_admin row.",
        )
    try:
        auth_db.delete_user(username)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    try:
        auth_db.revoke_all_for_user(username)
    except Exception:  # pragma: no cover (defensive)
        log.exception("revoke_all_for_user failed after delete for %r", username)
    return {"deleted": username}


@router.get("/users/{username}/permissions")
def auth_get_user_permissions(
    username: str,
    user: Dict[str, Any] = Depends(require_role("root_admin")),
) -> Dict[str, Any]:
    """
    Return the per-user grant + revoke layer for ``username`` plus the
    role baseline so the Access Control UI can render each permission's
    current state without a second round-trip.

    Root-admin only - admin can't view or edit these.
    """
    target = auth_db.get_user(username)
    if target is None:
        raise HTTPException(status_code=404, detail=f"No user named {username!r}.")
    if target["role"] == "db_admin":
        raise HTTPException(
            status_code=400,
            detail="db_admin is a non-login credential row; no per-user permissions.",
        )
    grants = auth_db.get_user_permission_grants(username)
    role = target["role"]
    baseline = list(ROLE_PERMISSIONS.get(role, []))
    effective = effective_permissions_for(username, role)
    return {
        "username": username,
        "role": role,
        "baseline": baseline,
        "extra": grants.get("extra") or [],
        "revoked": grants.get("revoked") or [],
        "effective": effective,
        "all_permissions": list(ALL_PERMISSIONS),
        "root_admin_immune_to_revokes": role == "root_admin",
    }


class UserPermissionsPatchIn(BaseModel):
    extra: List[str] = Field(
        default_factory=list,
        description="Permissions granted on top of the role baseline.",
    )
    revoked: List[str] = Field(
        default_factory=list,
        description="Permissions removed from the role baseline.",
    )


@router.patch("/users/{username}/permissions")
def auth_set_user_permissions(
    username: str,
    body: UserPermissionsPatchIn,
    user: Dict[str, Any] = Depends(require_elevation()),
) -> Dict[str, Any]:
    """
    Replace the per-user grant + revoke lists for ``username``.

    Root-admin only. Both lists are validated against
    ``ALL_PERMISSIONS``; unknown strings raise 400. The root_admin
    role is immune to revokes server-side (resolver always returns
    the full set) but we also reject revokes against a root_admin
    row up front so the UI doesn't pretend the value stuck.

    A grant or revoke that would be a no-op (granting a baseline
    permission, revoking a non-baseline one) is accepted silently -
    the effective set is what matters, and the UI may surface those
    as "redundant" badges later.
    """
    target = auth_db.get_user(username)
    if target is None:
        raise HTTPException(status_code=404, detail=f"No user named {username!r}.")
    if target["role"] == "db_admin":
        raise HTTPException(
            status_code=400,
            detail="db_admin is a non-login credential row; cannot edit permissions.",
        )
    # Validate each permission string is known.
    unknown = [p for p in (body.extra + body.revoked) if p not in ALL_PERMISSIONS]
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown permission(s): {sorted(set(unknown))!r}",
        )
    # Revokes against a root_admin row are pointless (the resolver
    # ignores them) and would be confusing - reject explicitly.
    if target["role"] == "root_admin" and body.revoked:
        raise HTTPException(
            status_code=400,
            detail=(
                "Root admin is immune to revokes (always full permissions). "
                "Clear the revoked list to save."
            ),
        )
    try:
        auth_db.set_user_permission_grants(username, body.extra, body.revoked)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    log.info(
        "Permissions updated for user %r by %r (extra=%d, revoked=%d, "
        "elevate_confirmed=True)",
        username, user.get("username"), len(body.extra), len(body.revoked),
    )
    # Explicitly audit-log the grant via db_access_log so the entry
    # lands in db_access.log alongside other privilege-relevant
    # writes. The elevate-confirmed
    # flag is unconditionally True at this point because the endpoint
    # is gated on ``require_elevation()`` - we cannot reach this line
    # without a fresh password re-confirmation within the elevation
    # TTL window. Capturing both lists explicitly lets a future audit
    # query trace WHICH permissions changed, not just that something
    # changed.
    try:
        from services import db_access_log
        db_access_log.log_event(
            "PERMISSIONS_GRANT grantor=%r grantee=%r extra=%r revoked=%r "
            "elevate_confirmed=true",
            user.get("username"),
            username,
            list(body.extra),
            list(body.revoked),
        )
    except Exception:
        # Audit log is best-effort - never fail the grant on a
        # logging hiccup. The application logger above still carries
        # the same information for forensic recovery.
        log.exception("permissions-grant audit_log write failed")
    grants = auth_db.get_user_permission_grants(username)
    effective = effective_permissions_for(username, target["role"])
    return {
        "username": username,
        "role": target["role"],
        "baseline": list(ROLE_PERMISSIONS.get(target["role"], [])),
        "extra": grants.get("extra") or [],
        "revoked": grants.get("revoked") or [],
        "effective": effective,
        "all_permissions": list(ALL_PERMISSIONS),
    }


@router.post("/users/me/display-name")
def auth_update_own_display_name(
    body: DisplayNameIn,
    user: Dict[str, Any] = Depends(require_role("viewer")),
) -> Dict[str, Any]:
    """
    Update the caller's own display name. Any logged-in role can call
    this - it operates only on the caller's row.
    """
    try:
        auth_db.update_display_name(user["username"], body.display_name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"username": user["username"], "display_name": body.display_name or None}


class OwnPasswordIn(BaseModel):
    current_password: str = Field(description="Caller's current password (per-call gate).")
    new_password: str = Field(description=">= 8 chars; bcrypt rules from auth_db.create_user.")


@router.post("/users/me/password")
def auth_change_own_password(
    body: OwnPasswordIn,
    user: Dict[str, Any] = Depends(require_role("viewer")),
) -> Dict[str, Any]:
    """
    Change the caller's own password. Any logged-in role can call this
    - it operates only on the caller's row. The current password is
    re-verified server-side before the change applies.

    The caller's existing JWT remains valid after the change (we don't
    have a denylist). Subsequent logins will require the new password.
    """
    verified = auth_db.verify_password(user["username"], body.current_password)
    if verified is None:
        raise HTTPException(
            status_code=401,
            detail="Current password is incorrect.",
        )
    try:
        auth_db.update_password(user["username"], body.new_password)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    # Revoke every refresh token for this user. The caller's current
    # access JWT remains valid until its own 30-minute expiry; their
    # browser will then fail to refresh and bounce to login. Other
    # devices behave the same way. Intentional - a password change
    # should not leave dormant sessions usable elsewhere.
    try:
        auth_db.revoke_all_for_user(user["username"])
    except Exception:  # pragma: no cover (defensive)
        log.exception(
            "revoke_all_for_user failed after self password change for %r",
            user["username"],
        )
    return {"username": user["username"], "changed": True}


# ── Database Admin account (role='db_admin') ───────────────────────────────────
#
# A SEPARATE row from the root_admin login row. Decoupled so the
# end user can authorise destructive User Management writes with a
# credential they don't use for everyday login.
#
# Every endpoint here requires a root_admin JWT. The current-password
# requirement on /admin/update is an additional defence-in-depth
# layer.

@router.get("/admin/status")
def admin_status(
    user: Dict[str, Any] = Depends(require_role("admin")),
) -> Dict[str, Any]:
    """Does the db-admin row exist? Returns ``{has_admin, username}``."""
    admin = auth_db.get_db_admin()
    return {
        "has_admin": admin is not None,
        "username": admin["username"] if admin else None,
    }


@router.post("/admin/setup")
def admin_setup(
    body: AdminSetupIn,
    user: Dict[str, Any] = Depends(require_role("admin")),
) -> Dict[str, Any]:
    """
    Create the db-admin account (``role='db_admin'``). Self-locking
    on existence of any db-admin row.
    """
    if auth_db.get_db_admin() is not None:
        raise HTTPException(
            status_code=403,
            detail="Database admin already exists. Use /admin/update to change credentials.",
        )
    try:
        new_user = auth_db.create_user(body.username, body.password, role="db_admin")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"username": new_user["username"], "role": new_user["role"]}


@router.post("/admin/update")
def admin_update(
    body: AdminUpdateIn,
    user: Dict[str, Any] = Depends(require_role("admin")),
) -> Dict[str, Any]:
    """
    Update the db-admin's username and/or password. Caller must be
    admin or root_admin (JWT) AND supply the current db-admin
    password (request body) as defence-in-depth.
    """
    admin = auth_db.get_db_admin()
    if admin is None:
        raise HTTPException(
            status_code=404,
            detail="No database admin exists yet. Use /admin/setup first.",
        )
    verified = auth_db.verify_password(admin["username"], body.current_password)
    if verified is None or verified.get("role") != "db_admin":
        raise HTTPException(
            status_code=401,
            detail="Current database admin password is incorrect.",
        )

    effective_username = admin["username"]
    if body.new_username and body.new_username.strip() != admin["username"]:
        try:
            auth_db.update_username(admin["username"], body.new_username.strip())
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        effective_username = body.new_username.strip()

    if body.new_password:
        try:
            auth_db.update_password(effective_username, body.new_password)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    return {"username": effective_username, "role": "db_admin"}


@router.post("/admin/verify")
def admin_verify(
    body: AdminVerifyIn,
    user: Dict[str, Any] = Depends(require_role("admin")),
) -> Dict[str, Any]:
    """
    Validate a db-admin username + password without issuing a token.
    Used by the User Management write gate. Caller must be admin or
    root_admin (JWT); the body is the db-admin credential to verify.
    """
    verified = auth_db.verify_password(body.username, body.password)
    valid = verified is not None and verified.get("role") == "db_admin"
    return {"valid": bool(valid)}


# ── Root admin login account management ────────────────────────────────────────
#
# Updates the row with ``role='root_admin'``. Access requires a
# root_admin JWT; the current-password requirement is an additional
# defence-in-depth layer. The Account Settings panel offers the same
# surface as a self-service alternative for the logged-in end user.

@router.get("/login-account/status")
def login_account_status(
    user: Dict[str, Any] = Depends(require_role("root_admin")),
) -> Dict[str, Any]:
    """Does a root_admin row exist? ``{has_admin, username}``."""
    admin = auth_db.get_root_admin()
    return {
        "has_admin": admin is not None,
        "username": admin["username"] if admin else None,
    }


@router.post("/login-account/update")
def login_account_update(
    body: AdminUpdateIn,
    user: Dict[str, Any] = Depends(require_role("root_admin")),
) -> Dict[str, Any]:
    """
    Update the root_admin row's username and/or password. Caller must
    be root_admin AND supply the current root_admin password.
    """
    admin = auth_db.get_root_admin()
    if admin is None:
        raise HTTPException(
            status_code=404,
            detail="No root_admin exists. Complete first-boot setup first.",
        )
    verified = auth_db.verify_password(admin["username"], body.current_password)
    if verified is None or verified.get("role") != "root_admin":
        raise HTTPException(
            status_code=401,
            detail="Current login password is incorrect.",
        )

    effective_username = admin["username"]
    if body.new_username and body.new_username.strip() != admin["username"]:
        try:
            auth_db.update_username(admin["username"], body.new_username.strip())
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        effective_username = body.new_username.strip()

    if body.new_password:
        try:
            auth_db.update_password(effective_username, body.new_password)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    return {"username": effective_username, "role": "root_admin"}
