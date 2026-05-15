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
  (:mod:`server.preflight`) surfaces those users to the operator
  before each job runs, so the operator can save the PIN under
  User Management and the *next* sync completes the capture.

* The Plex.tv calls behind ``account.users()`` and
  ``user.get_token()`` are rate-limited per server (default 4
  attempts per hour). The throttle is the gate that protects against
  a chatty operator clicking Test Connection repeatedly and hammering
  Plex.tv. ``force=True`` bypasses it for an explicit operator action
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
    server. Floor of 1/hour so an operator can't disable the limiter
    by writing 0; ceiling is whatever Plex.tv will tolerate (we leave
    that to operator judgement).
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
    operator-triggered paths (e.g. a "Refresh users" button).
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
    logger: Optional[logging.Logger] = None,
) -> Dict[str, Any]:
    """
    Best-effort: capture per-user auth tokens for every managed user
    on ``server_id`` and store them encrypted in media.db.

    Returns ``{captured, throttled, errors}``:

      * ``captured`` (int)    - tokens successfully stored.
      * ``throttled`` (bool)  - True if the per-server rate limit
                                blocked this attempt; nothing was tried.
      * ``errors`` (list[str]) - human-readable per-user failure
                                strings. PIN-protected users without a
                                stored PIN do NOT count as errors here;
                                they simply aren't returned by
                                ``get_home_users`` and the preflight
                                check surfaces them later.

    Pass ``force=True`` to bypass the throttle (operator-triggered
    refresh).
    """
    logger = logger or log
    out: Dict[str, Any] = {"captured": 0, "throttled": False, "errors": []}

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

    for username, token, _user_server in home_users:
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
            "Captured per-user tokens for %d managed user(s) on server %r",
            out["captured"], server_id,
        )
    return out
