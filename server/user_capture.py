"""
Per-user auth-token capture at server-add / sync time.

For each managed user on a registered server, this module tries to
obtain a per-user auth token using the PIN / password material the
end user has already saved under User Management, and stores it
encrypted in ``media_db.managed_users.auth_token_enc`` via
:func:`server.media_db.set_managed_user_credential`.

The module dispatches on the registry row's ``service_type``:

* **Plex** uses the same PIN-aware flow
  :func:`services.auth.get_home_users` uses on a snapshot run -
  ``user.get_token()`` for unprotected users, ``signInHomeUser`` with
  the stored Plex Home PIN for PIN-protected users. The owner row's
  token is the operator's Plex.tv account token (the admin authToken
  on the registry row).

* **Emby / Jellyfin** authenticate per-user via
  ``POST /Users/AuthenticateByName`` (see
  :func:`services.adapters.jellyfin.authenticate_by_name`). The
  "password" parameter accepts either the Emby/Jellyfin password OR
  an Emby Easy PIN through the same endpoint, so this module pulls
  whichever credential the operator has stored:

    1. ``emby_easy_pin_enc`` (Emby) / ``jellyfin_easy_pin_enc``
       (Jellyfin) - the per-backend PIN columns added in v16.
    2. ``service_password_enc`` - the generic per-user password slot
       for Jellyfin users that authenticate with a regular password.
    3. Empty string (last resort) for Emby/Jellyfin users that have
       no password set at all.

  The owner row's token on Emby/Jellyfin is the admin access token
  on the registry row, same equivalence as Plex.

Two layers of leniency cover the realistic failure modes:

* Users without a usable PIN / password cannot have their tokens
  captured here. The companion preflight check
  (:mod:`server.preflight`) surfaces those users to the end user
  before each job runs, so the end user can save the credential
  under User Management and the *next* sync completes the capture.

* The Plex.tv calls behind ``account.users()`` and
  ``user.get_token()`` are rate-limited per server (default 4
  attempts per hour). The same throttle covers the Emby / Jellyfin
  capture so a chatty Refresh-users click can't hammer either
  backend. ``force=True`` bypasses it for an explicit end user
  action (a "Refresh users" click).

Stored values are encrypted at rest by the existing Fernet machinery
in :mod:`server.secrets`. No plaintext token reaches the database.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, Optional


log = logging.getLogger("plexmigrate.server.user_capture")


# ── Per-server rate-limit gate ───────────────────────────────────────────────

_last_attempt_lock = threading.Lock()
_last_attempt: Dict[str, float] = {}

# Fallback default when the persisted setting is missing or unreadable.
_DEFAULT_THROTTLE_PER_HOUR = 4


def _throttle_min_interval_seconds() -> float:
    """
    Read the ``user_token_capture_throttle_per_hour`` setting and
    return the minimum interval between attempts in seconds, per
    server. Floor of 1/hour so an end user can't disable the limiter
    by writing 0; ceiling is whatever Plex.tv will tolerate (we leave
    that to end user judgement).
    """
    try:
        from server.persistence import load_settings
        per_hour = (load_settings() or {}).get(
            "user_token_capture_throttle_per_hour"
        )
        if per_hour is None:
            per_hour = _DEFAULT_THROTTLE_PER_HOUR
        per_hour = max(1, int(per_hour))
    except Exception:
        per_hour = _DEFAULT_THROTTLE_PER_HOUR
    return 3600.0 / per_hour


def _throttle_allows(server_id: str, *, force: bool = False) -> bool:
    """
    Return True if the per-server throttle permits a capture attempt
    right now. On True, the gate stamps the "last attempt" timestamp so
    subsequent calls within the interval get rejected.

    ``force=True`` always allows AND stamps. Use it only from explicit
    end user-triggered paths (e.g. a "Refresh users" button).
    """
    now = time.time()
    interval = _throttle_min_interval_seconds()
    with _last_attempt_lock:
        last = _last_attempt.get(server_id, 0.0)
        if not force and (now - last) < interval:
            return False
        _last_attempt[server_id] = now
        return True


def _next_attempt_seconds(server_id: str) -> float:
    """
    Seconds until the throttle next permits an attempt for
    ``server_id``. Negative if an attempt would be allowed right now.
    """
    interval = _throttle_min_interval_seconds()
    with _last_attempt_lock:
        last = _last_attempt.get(server_id, 0.0)
    return last + interval - time.time()


def _reset_throttle_for_tests() -> None:
    """Wipe the rate-limit state. Not called from production code."""
    with _last_attempt_lock:
        _last_attempt.clear()


# ── Public API ───────────────────────────────────────────────────────────────

def capture_managed_user_tokens(
    server_id: str,
    *,
    force: bool = False,
    only_if_missing: bool = True,
    logger: Optional[logging.Logger] = None,
    username_filter: Optional[Any] = None,
) -> Dict[str, Any]:
    """
    Best-effort: capture per-user auth tokens for every managed user
    on ``server_id`` and store them encrypted in media.db.

    Returns ``{captured, skipped_existing, throttled, errors}``:

      * ``captured`` (int)         - tokens successfully stored.
      * ``skipped_existing`` (int) - users that already had a stored
                                     token and were skipped because
                                     ``only_if_missing=True``.
      * ``throttled`` (bool)       - True if the per-server rate limit
                                     blocked this attempt; nothing
                                     was tried.
      * ``errors`` (list[str])     - human-readable per-user failure
                                     strings. PIN-protected users
                                     without a stored PIN do NOT count
                                     as errors here; they simply aren't
                                     returned by ``get_home_users`` and
                                     the preflight check surfaces them
                                     later.

    Parameters:

      * ``force=True`` bypasses the per-server throttle. Use it only
        from explicit end user-triggered paths (e.g. a Refresh-server
        click).
      * ``only_if_missing=True`` (default) skips users that already
        have a stored auth_token in media.db. This is the additive-
        only contract the Refresh-server flow requires: never
        overwrite an existing token. Set to False on an explicit
        per-user "Rotate token" action.
      * ``username_filter`` (iterable of str, optional, default None)
        scopes the per-user loop to just those usernames. Users
        outside the filter are silently skipped from the capture
        attempt. The owner-mirror / fallback paths still inspect all
        owner rows on the server so per-row tally fields are
        consistent. Used by the per-user "Sync this user" button on
        the User Management panel.
    """
    logger = logger or log
    out: Dict[str, Any] = {
        "captured": 0,
        "skipped_existing": 0,
        "throttled": False,
        "errors": [],
    }

    if not _throttle_allows(server_id, force=force):
        wait = max(0.0, _next_attempt_seconds(server_id))
        logger.debug(
            "User-token capture throttled for server %r (next attempt in %.0fs)",
            server_id, wait,
        )
        out["throttled"] = True
        return out

    # Late imports keep this module light at import time and shield us
    # from any startup-time cycle that future refactors might create.
    from server import media_db, server_registry
    from server.server_registry import ServerCredentialError

    row = server_registry.get_server_by_id(server_id, include_token=True)
    if row is None:
        out["errors"].append(f"unknown server_id: {server_id!r}")
        return out

    try:
        admin_token = server_registry.decrypt_server_token(row)
    except ServerCredentialError as exc:
        out["errors"].append(f"could not decrypt server token: {exc}")
        return out

    url = (row.get("url") or "").rstrip("/")
    if not (url and admin_token):
        out["errors"].append("server has no URL or token")
        return out

    # Service-type dispatch: Plex uses plexapi + Plex.tv home-user flow;
    # Emby / Jellyfin use AuthenticateByName + the per-backend PIN /
    # password material under User Management.
    service_type = (row.get("service_type") or "plex").lower()
    # Normalise username_filter to a set so contains-checks are O(1)
    # and we tolerate either a list, tuple, set, or single string.
    if username_filter is not None:
        if isinstance(username_filter, str):
            username_filter_set: Optional[set] = {username_filter}
        else:
            username_filter_set = {u for u in username_filter if u}
        if not username_filter_set:
            out["errors"].append("username_filter resolved to empty set")
            return out
    else:
        username_filter_set = None
    if service_type in ("emby", "jellyfin"):
        return _capture_emby_jellyfin_managed_users(
            server_id=server_id,
            service_type=service_type,
            admin_token=admin_token,
            base_url=url,
            only_if_missing=only_if_missing,
            logger=logger,
            out=out,
            username_filter=username_filter_set,
        )

    from services.auth import connect_to_server, get_home_users
    try:
        server = connect_to_server(url, admin_token, logger)
    except Exception as exc:
        out["errors"].append(f"connect failed: {exc}")
        return out
    if server is None:
        out["errors"].append("connect returned None")
        return out

    # Reuse the existing snapshot-time authentication flow. For each
    # user, ``get_home_users`` first tries ``user.get_token()`` (works
    # for unprotected users) and, on failure, falls back to a
    # PIN-based ``signInHomeUser`` using the stored PIN from
    # ``managed_users.plex_home_pin_enc``. PIN-protected users without
    # a stored PIN are dropped from the returned list; the preflight
    # check picks them up.
    try:
        home_users = get_home_users(server, url, logger)
    except Exception as exc:
        out["errors"].append(f"get_home_users failed: {exc}")
        return out

    # Build the "already has a stored token" set when additive-only.
    # We include hidden rows so a tombstoned user with a token doesn't
    # get its token quietly overwritten by an additive sweep.
    existing_token_users: set[str] = set()
    if only_if_missing:
        try:
            for u in media_db.list_managed_users(server_id, include_hidden=True):
                if u.get("has_token"):
                    existing_token_users.add(u["username"])
        except Exception as exc:
            # If the existence check fails we cannot guarantee the
            # additive contract, so fail closed: log and abort. Better
            # to do nothing than to silently overwrite tokens.
            logger.warning(
                "user_capture: could not read existing tokens for server %r"
                " (additive contract requires this); aborting sweep: %s",
                server_id, exc,
            )
            out["errors"].append(f"existing-token check failed: {exc}")
            return out

    for username, token, _user_server in home_users:
        if username_filter_set is not None and username not in username_filter_set:
            # Per-user "Sync this user" button: skip everyone outside
            # the filter so a one-off recapture doesn't fan out to the
            # whole Plex Home.
            continue
        if only_if_missing and username in existing_token_users:
            out["skipped_existing"] += 1
            continue
        try:
            media_db.set_managed_user_credential(
                server_id=server_id,
                username=username,
                kind="auth_token",
                plaintext=token,
            )
            out["captured"] += 1
        except Exception as exc:
            out["errors"].append(f"{username}: {exc}")

    # Capture the owner's personal token + Plex.tv userID on the
    # owner row. ``get_home_users`` returns only the managed users
    # by design (it iterates ``account.users()`` which excludes the
    # owner), so without this block the owner's ``auth_token_enc``
    # column is always NULL and their ``backend_user_id`` only gets
    # populated when ``_refresh_share_state``'s myPlexAccount() call
    # succeeds. Closes the cross-server auto-link gap when the same
    # human is admin on one server + managed user on another.
    #
    # Source of the owner's personal token: the admin_token we used
    # to connect. For Plex servers the admin authToken IS the
    # operator's Plex.tv account token (the same value Plex.tv
    # issues for accessing every Plex server they're authorized on);
    # storing it on the owner row enables the per-user-token write
    # paths to attribute writes to the operator's account
    # consistently. Pre-flight is intentionally NOT done here
    # (no separate Plex.tv lookup); we just plumb the value we
    # already have.
    try:
        owner_captured = _capture_owner_token_and_userid(
            server_id=server_id,
            admin_token=admin_token,
            server=server,
            only_if_missing=only_if_missing,
            logger=logger,
        )
        if owner_captured.get("token_captured"):
            out["captured"] += 1
        out["owner_captured"] = owner_captured
    except Exception as exc:
        logger.exception(
            "Owner-token capture for server %r failed; managed-user "
            "tokens above are unaffected.", server_id,
        )
        out["errors"].append(f"owner token capture: {exc}")

    if out["captured"]:
        logger.info(
            "Captured per-user tokens for %d user(s) on server %r"
            " (skipped %d already-stored)",
            out["captured"], server_id, out["skipped_existing"],
        )
    elif out["skipped_existing"]:
        logger.debug(
            "User-token sweep for server %r: no new tokens (skipped %d already-stored)",
            server_id, out["skipped_existing"],
        )
    return out


def _capture_owner_token_and_userid(
    *,
    server_id: str,
    admin_token: str,
    server: Any,
    only_if_missing: bool,
    logger: logging.Logger,
) -> Dict[str, Any]:
    """Belt-and-braces capture for the owner row's identity.

    Two writes (both additive when ``only_if_missing=True``):

      1. Store ``admin_token`` in the owner's ``auth_token_enc``
         column so per-user-token write paths can attribute writes
         to the operator's own Plex.tv account on this server.
      2. Stamp the owner's Plex.tv numeric userID on the owner row's
         ``backend_user_id`` column. Read from ``myPlexAccount()``;
         falls back to no-op when the live lookup fails (the
         existing ``_refresh_share_state`` path will retry on the
         next refresh).

    Returns ``{owner_username, token_captured, token_skipped_existing,
    userid_captured, userid_skipped_existing, errors}``. Caller folds
    ``token_captured`` into the outer ``captured`` counter when the
    summary lands on the wire.
    """
    out: Dict[str, Any] = {
        "owner_username":         None,
        "token_captured":         False,
        "token_skipped_existing": False,
        "userid_captured":        False,
        "userid_skipped_existing": False,
        "errors":                 [],
    }
    from server import media_db

    # Find the owner row (kind='owner'). The sync helper writes one
    # per server; on a brand-new install where the sync hasn't run
    # yet, there's no owner row to update and we silently no-op.
    rows = media_db.list_managed_users(server_id, include_hidden=True)
    owner_row = next(
        (r for r in rows if (r.get("kind") or "").lower() == "owner"),
        None,
    )
    if owner_row is None:
        logger.debug(
            "Owner-token capture for server %r: no owner row in "
            "managed_users yet (sync may not have completed). Skipping.",
            server_id,
        )
        return out
    owner_username = owner_row.get("username") or ""
    out["owner_username"] = owner_username
    if not owner_username:
        # Defensive: an empty owner username can't be addressed by
        # the per-row write helpers.
        return out

    # Token: additive-only by default; force-mode (only_if_missing=
    # False) overwrites.
    has_token = bool(owner_row.get("has_token"))
    if has_token and only_if_missing:
        out["token_skipped_existing"] = True
    else:
        try:
            media_db.set_managed_user_credential(
                server_id=server_id,
                username=owner_username,
                kind="auth_token",
                plaintext=admin_token,
            )
            out["token_captured"] = True
        except Exception as exc:
            out["errors"].append(f"owner token write: {exc}")

    # Backend user id: pulled from the live account. Best-effort; a
    # failure here just leaves the column NULL and the existing
    # _refresh_share_state path picks it up on the next refresh.
    existing_uid = (owner_row.get("backend_user_id") or "").strip()
    if existing_uid and only_if_missing:
        out["userid_skipped_existing"] = True
    else:
        owner_userid = ""
        try:
            account = server.myPlexAccount()
            for attr in ("id", "userID", "userid"):
                val = getattr(account, attr, "") or ""
                if val:
                    owner_userid = str(val).strip()
                    break
        except Exception as exc:
            # Network blip / token expired / no Plex.tv linkage. The
            # share-state refresh path retries; this is a fallback,
            # not a primary path.
            logger.debug(
                "Owner-userid capture for server %r: myPlexAccount() "
                "failed (%s); leaving backend_user_id alone. "
                "_refresh_share_state will retry on next sync.",
                server_id, exc,
            )
        if owner_userid:
            try:
                media_db.set_managed_user_share_state(
                    server_id=server_id,
                    username=owner_username,
                    backend_user_id=owner_userid,
                )
                out["userid_captured"] = True
            except Exception as exc:
                out["errors"].append(f"owner userid write: {exc}")
    return out


# ── Emby / Jellyfin per-user token capture ──────────────────────────────────
#
# Mirrors the shape of the Plex flow above:
#
#   * Throttle gate already applied by the public entry point.
#   * Same additive-only contract (``only_if_missing`` skips users with
#     a stored ``auth_token_enc``).
#   * Same return dict (``captured``, ``skipped_existing``, ``throttled``,
#     ``errors``, ``owner_captured``).
#
# Mechanism per user:
#
#   POST {base_url}/Users/AuthenticateByName {Username, Pw} -> AccessToken
#
# The ``Pw`` field accepts either an Emby Easy PIN or a Jellyfin
# password depending on what's stored under User Management. The
# helper authenticates with one of those (priority order: per-backend
# PIN > generic service password > empty string), and writes the
# returned ``AccessToken`` into ``managed_users.auth_token_enc``.
#
# Owner token: the operator's admin access token IS their user token
# on Emby / Jellyfin (the access token issued at admin login is the
# same one a per-user session would issue), so we just mirror the
# admin token into the owner row's ``auth_token_enc`` - matching the
# Plex assumption in ``_capture_owner_token_and_userid``.

_PIN_KIND_BY_SERVICE = {
    "emby":     "emby_easy_pin",
    "jellyfin": "jellyfin_easy_pin",
}


def _capture_emby_jellyfin_managed_users(
    *,
    server_id: str,
    service_type: str,
    admin_token: str,
    base_url: str,
    only_if_missing: bool,
    logger: logging.Logger,
    out: Dict[str, Any],
    username_filter: Optional[set] = None,
) -> Dict[str, Any]:
    """Emby / Jellyfin equivalent of the Plex managed-user sweep.

    Returns the same ``out`` dict the public entry point seeded;
    callers do not introspect intermediate state."""
    from server import media_db
    from services.adapters.jellyfin import (
        JellyfinAdapter,
        authenticate_by_name,
    )
    from services.adapters.emby import EmbyAdapter

    adapter_cls = EmbyAdapter if service_type == "emby" else JellyfinAdapter
    try:
        adapter = adapter_cls(base_url, admin_token)
    except Exception as exc:
        out["errors"].append(f"adapter construction failed: {exc}")
        return out

    try:
        users = adapter.list_users()
    except Exception as exc:
        out["errors"].append(f"list_users failed: {exc}")
        return out

    # Build the "already has a stored token" set for the additive
    # contract. Includes hidden rows so a tombstoned user's existing
    # token doesn't get quietly overwritten.
    existing_token_users: set[str] = set()
    if only_if_missing:
        try:
            for u in media_db.list_managed_users(server_id, include_hidden=True):
                if u.get("has_token"):
                    existing_token_users.add(u["username"])
        except Exception as exc:
            logger.warning(
                "user_capture: could not read existing tokens for server %r"
                " (additive contract requires this); aborting sweep: %s",
                server_id, exc,
            )
            out["errors"].append(f"existing-token check failed: {exc}")
            return out

    pin_kind = _PIN_KIND_BY_SERVICE.get(service_type)
    captured_this_run: set[str] = set()
    logger.info(
        "user_capture %s server %r: sweep starting (only_if_missing=%s, "
        "users_from_adapter=%d, users_with_existing_token=%d, "
        "username_filter=%s)",
        service_type, server_id, only_if_missing,
        len(users or []), len(existing_token_users),
        ("all" if username_filter is None
         else ", ".join(repr(u) for u in sorted(username_filter))),
    )
    for user in users or []:
        # Capture admins via the same AuthenticateByName flow as
        # everyone else. Earlier shape blanket-skipped is_admin=True
        # users on the theory that the owner-mirror below would handle
        # them; that left the API-key-owning operator with NULL on
        # their own row when the owner-mirror's "first kind=owner"
        # picker landed on a different admin row instead (Emby/
        # Jellyfin can have multiple admins, and the per-user role
        # mapping sets every admin's role="owner"). Per-user
        # AuthenticateByName returns the admin's real session token,
        # which is what UserData attribution actually needs - the
        # long-lived admin API key is server-scoped, not user-scoped.
        # The owner-mirror remains as a fallback for the row marked
        # kind='owner' when no usable PIN / password was stored;
        # ``captured_this_run`` tracks who got a real per-user token
        # so the mirror never clobbers it (even in rotate mode).
        username = user.username or ""
        if not username:
            continue
        if username_filter is not None and username not in username_filter:
            # Per-user "Sync this user" path - only process the
            # operator-selected username, silently skip everyone else.
            continue
        if only_if_missing and username in existing_token_users:
            out["skipped_existing"] += 1
            logger.info(
                "user_capture %s %r: skipped (additive contract: "
                "row already has a stored auth_token)",
                service_type, username,
            )
            continue

        # Pull whichever credential the operator has stored. PIN first
        # (the common case on Emby Easy PIN users), then the generic
        # password slot (for password-protected Jellyfin users), then
        # empty string as a last resort (works for fully-unprotected
        # users on either backend).
        password: Optional[str] = None
        tried: list[str] = []
        if pin_kind is not None:
            try:
                _pin = media_db.get_managed_user_credential(
                    server_id, username, kind=pin_kind,
                )
                if _pin:
                    password = _pin
                    tried.append(pin_kind)
            except Exception as exc:
                logger.debug(
                    "user_capture %r/%s: PIN lookup failed: %s",
                    server_id, username, exc,
                )
        if password is None:
            try:
                _pw = media_db.get_managed_user_credential(
                    server_id, username, kind="service_password",
                )
                if _pw:
                    password = _pw
                    tried.append("service_password")
            except Exception as exc:
                logger.debug(
                    "user_capture %r/%s: password lookup failed: %s",
                    server_id, username, exc,
                )
        if password is None:
            # Empty password: works for genuinely unprotected users.
            # Server will 401 if a password / PIN is actually required;
            # we surface that as an error so the preflight check can
            # remind the operator to save the credential.
            password = ""
            tried.append("empty")

        logger.info(
            "user_capture %s %r: attempting AuthenticateByName "
            "(credential source=%s, is_admin=%s)",
            service_type, username, "+".join(tried), user.is_admin,
        )
        try:
            result = authenticate_by_name(
                base_url, username=username, password=password,
                backend=service_type,
            )
        except Exception as exc:
            logger.info(
                "user_capture %s %r: AuthenticateByName FAILED "
                "(credential source=%s): %s",
                service_type, username, "+".join(tried), exc,
            )
            # Passive capture: every AuthenticateByName failure is an
            # auth_error signal we want recorded so the sweeper /
            # the activity filter can see it. Best-effort; never
            # let the auth-signal write block the capture flow.
            try:
                from services.user_activity_filter import (
                    record_auth_result, RESULT_AUTH_ERROR,
                )
                record_auth_result(
                    server_id=server_id, username=username,
                    result=RESULT_AUTH_ERROR,
                    detail=f"AuthenticateByName: {exc}",
                )
            except Exception:
                logger.debug("auth-signal record failed", exc_info=True)
            out["errors"].append(
                f"{username}: AuthenticateByName failed "
                f"(tried={'+'.join(tried)}): {exc}"
            )
            continue

        access_token = (result or {}).get("AccessToken") or ""
        if not access_token:
            out["errors"].append(
                f"{username}: AuthenticateByName succeeded but no "
                f"AccessToken in response"
            )
            continue

        try:
            media_db.set_managed_user_credential(
                server_id=server_id,
                username=username,
                kind="auth_token",
                plaintext=access_token,
            )
            out["captured"] += 1
            captured_this_run.add(username)
            logger.info(
                "user_capture %s %r: AuthenticateByName OK; "
                "stored per-user token (len=%d)",
                service_type, username, len(access_token),
            )
            # Passive capture: a successful capture resets the
            # failure counter to 0 so the filter / sweeper sees a
            # clean slate going forward.
            try:
                from services.user_activity_filter import (
                    record_auth_result, RESULT_OK,
                )
                record_auth_result(
                    server_id=server_id, username=username,
                    result=RESULT_OK,
                    detail="AuthenticateByName ok",
                )
            except Exception:
                logger.debug("auth-signal record failed", exc_info=True)
        except Exception as exc:
            logger.info(
                "user_capture %s %r: token WRITE failed: %s",
                service_type, username, exc,
            )
            out["errors"].append(f"{username}: token write: {exc}")

    # The Emby/Jellyfin admin token is a SERVER-scoped API key, not a
    # per-user session token. It must NEVER be written into any
    # managed_users row - not even as a fallback for the admin/owner
    # row, because writes against /Users/{uid}/UserData with that
    # token attribute to "the API key", not to the addressed user.
    # Every user (including admins) has their own distinct per-user
    # access token, and we must capture THAT via AuthenticateByName -
    # or leave the row NULL and surface the missing credential via
    # the preflight check. Users without a stored PIN / password get
    # a clear "no credential to authenticate with" error so the
    # operator knows to save one under User Management.
    #
    # Plex is the only backend where the admin authToken IS the
    # operator's per-user identity token (Plex.tv account token);
    # that path remains in _capture_owner_token_and_userid above.
    owner_rows_missing_token: list[str] = []
    try:
        from server import media_db as _media_db_check
        for _row in _media_db_check.list_managed_users(server_id, include_hidden=True):
            if (_row.get("kind") or "").lower() != "owner":
                continue
            uname = _row.get("username") or ""
            if not uname or uname in captured_this_run:
                continue
            if _row.get("has_token"):
                continue
            owner_rows_missing_token.append(uname)
    except Exception as exc:
        logger.debug(
            "user_capture %s server %r: post-sweep owner inventory failed: %s",
            service_type, server_id, exc,
        )

    if owner_rows_missing_token:
        logger.info(
            "user_capture %s server %r: %d admin row(s) still without a "
            "per-user token after the sweep (%s). The preflight check "
            "will prompt the operator to save a PIN / password for each "
            "under User Management; we never mirror the server-scoped "
            "admin API key onto a per-user row on this backend.",
            service_type, server_id, len(owner_rows_missing_token),
            ", ".join(repr(u) for u in owner_rows_missing_token),
        )
        for uname in owner_rows_missing_token:
            out["errors"].append(
                f"{uname}: no per-user token captured (no PIN / password "
                f"stored; save one under User Management and re-Refresh)"
            )

    # Compatibility shape on the return dict: existing callers
    # (frontend toast, server-add hook) read out["owner_captured"]
    # opportunistically. Keep the key so they don't break, but the
    # shape now reflects "no mirror happens on this backend."
    out["owner_captured"] = {
        "owner_username":         (owner_rows_missing_token[0] if owner_rows_missing_token else None),
        "token_captured":         False,
        "token_skipped_existing": False,
        "userid_captured":        False,
        "userid_skipped_existing": True,
        "errors":                 [],
    }

    if out["captured"]:
        logger.info(
            "Captured per-user tokens for %d user(s) on %s server %r"
            " (skipped %d already-stored)",
            out["captured"], service_type, server_id,
            out["skipped_existing"],
        )
    elif out["skipped_existing"]:
        logger.debug(
            "User-token sweep for %s server %r: no new tokens "
            "(skipped %d already-stored)",
            service_type, server_id, out["skipped_existing"],
        )
    return out
