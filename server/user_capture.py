"""
Per-user Plex auth-token capture at server-add / sync time.

For each managed user on a registered server, this module tries to
obtain a per-user auth token via the same PIN-aware flow
:func:`services.auth.get_home_users` uses on a snapshot run, and
stores it encrypted in ``media_db.managed_users.auth_token_enc`` via
:func:`server.media_db.set_managed_user_credential`.

Two layers of leniency cover the realistic failure modes:

* Users that are PIN-protected with no stored PIN cannot have their
  tokens captured here. The companion PR-12 preflight check
  (:mod:`server.preflight`) surfaces those users to the end user
  before each job runs, so the end user can save the PIN under
  User Management and the *next* sync completes the capture.

* The Plex.tv calls behind ``account.users()`` and
  ``user.get_token()`` are rate-limited per server (default 4
  attempts per hour). The throttle is the gate that protects against
  a chatty end user clicking Test Connection repeatedly and hammering
  Plex.tv. ``force=True`` bypasses it for an explicit end user action
  (a "Refresh users" click).

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
    from services.auth import connect_to_server, get_home_users

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

    if out["captured"]:
        logger.info(
            "Captured per-user tokens for %d managed user(s) on server %r"
            " (skipped %d already-stored)",
            out["captured"], server_id, out["skipped_existing"],
        )
    elif out["skipped_existing"]:
        logger.debug(
            "User-token sweep for server %r: no new tokens (skipped %d already-stored)",
            server_id, out["skipped_existing"],
        )
    return out
