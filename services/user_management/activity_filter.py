"""
User-activity filter providing a consistent layered gate for auth health and tombstones.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

log = logging.getLogger("plexmigrate.services.user_management.activity_filter")


RESULT_OK = "ok"
RESULT_AUTH_ERROR = "auth_error"
RESULT_UNREACHABLE = "unreachable"
RESULT_UNKNOWN = "unknown"

_VALID_RESULTS = {RESULT_OK, RESULT_AUTH_ERROR, RESULT_UNREACHABLE, RESULT_UNKNOWN}


def list_active_users(
    server_id: str,
    *,
    include_owner: bool = True,
    bypass_health_filter: bool = False,
) -> List[Dict[str, Any]]:
    """Return managed_users rows for ``server_id`` with both the
    tombstone gate AND the auth-health gate applied.

    Tombstone gate (always on):
      Drops rows where hidden_scope is anything other than "none"
      (per-server tombstone, global tombstone). Today's
      list_managed_users(include_hidden=False) already does this;
      we route through it.

    Auth-health gate (Phase B):
      Drops rows where consecutive_auth_failures > 0 when the
      engine-wide ``user_activity_filter_enabled`` tunable is on.
      Phase A ships the gate but the data column doesn't exist yet
      so the gate is a no-op; Phase B's v17 migration adds the
      column and the gate starts firing.

    ``bypass_health_filter=True`` is for admin / UI surfaces that
    explicitly want to see auth-failing users (User Management
    panel, Server Editor, the sweep log itself). They still get
    tombstones filtered unless they ALSO pass through a different
    code path (e.g. managed_users_router with include_hidden=True
    URL param).

    ``include_owner=False`` excludes the owner row from the result.
    Some callers (sync worker, snapshot per-user fan-out) treat
    owner separately and don't want it folded in here.
    """
    try:
        from server import media_db
    except ImportError:
        log.warning(
            "user_activity_filter: media_db import failed; returning empty list"
        )
        return []

    try:
        rows = media_db.list_managed_users(server_id, include_hidden=False) or []
    except Exception as exc:
        log.warning(
            "user_activity_filter: list_managed_users(%s) failed: %s",
            server_id, exc,
        )
        return []

    if not include_owner:
        rows = [r for r in rows if (r.get("kind") or "").lower() != "owner"]

    if bypass_health_filter:
        return list(rows)

    # Phase A: the engine-wide filter switch reads as False unless
    # the operator opted in. Phase B starts honouring it once the
    # signal column exists. This branch is deliberately written so
    # it's a no-op in Phase A: even when the switch is on, if the
    # column doesn't exist yet on a row, we keep the user.
    try:
        from services.tunables import user_activity_filter_enabled
        filter_on = bool(user_activity_filter_enabled())
    except Exception:
        filter_on = False

    if not filter_on:
        return list(rows)

    kept: List[Dict[str, Any]] = []
    for r in rows:
        failures = r.get("consecutive_auth_failures")
        # Phase A: column doesn't exist yet; None means "we have no
        # signal" which counts as healthy. Phase B's migration
        # backfills the column to 0 on every row so this path stays
        # well-defined.
        if failures is None or int(failures) <= 0:
            kept.append(r)
            continue
        log.debug(
            "user_activity_filter: dropping %s on server %s "
            "(%d consecutive failures, status=%s)",
            r.get("username"), server_id, int(failures),
            r.get("last_auth_status") or "unknown",
        )
    return kept


def record_auth_result(
    *,
    server_id: str,
    username: str,
    result: str,
    detail: Optional[str] = None,
) -> None:
    """Persist the outcome of one auth probe / passive 401 capture.

    ``result`` must be one of the four enum values: ok, auth_error,
    unreachable, unknown. Anything else is silently coerced to
    'unknown' so an upstream typo never crashes the caller.

    Phase A: this function is a no-op (the schema columns don't
    exist yet). Phase B's media_db v17 migration adds the columns
    and the body becomes:
      - ok: reset consecutive_auth_failures to 0, stamp
        last_auth_status='ok', last_auth_checked_at=now
      - auth_error / unreachable: increment counter, stamp status,
        stamp checked-at
      - unknown: stamp checked-at only; don't touch the counter

    Idempotent; safe to call on every probe + every passive capture.
    """
    if result not in _VALID_RESULTS:
        log.warning(
            "user_activity_filter.record_auth_result: invalid result "
            "%r for %s on %s; coercing to %r",
            result, username, server_id, RESULT_UNKNOWN,
        )
        result = RESULT_UNKNOWN

    log.debug(
        "user_activity_filter.record_auth_result(server=%s, user=%s, "
        "result=%s, detail=%s)",
        server_id, username, result, detail or "",
    )

    # Phase B: persist to managed_users via the new schema columns.
    # Idempotent; safe to call on every probe + every passive capture.
    # We swallow exceptions so a transient DB hiccup never crashes a
    # caller — the worst case is "this one signal didn't land", which
    # is preferable to a probe / job failing.
    try:
        from server import media_db
        media_db.update_managed_user_auth_signal(
            server_id=server_id, username=username,
            result=result, detail=detail,
        )
    except Exception as exc:
        log.warning(
            "user_activity_filter.record_auth_result: persist failed "
            "for server=%s user=%s: %s",
            server_id, username, exc,
        )


def should_auto_tombstone(
    *,
    server_id: str,
    username: str,
) -> bool:
    """Return True only when EVERY safety layer aligns:

      1. Global tunable user_activity_sweeper_enabled is on
      2. Per-server auto_tombstone_inactive_users_enabled is on
      3. The user's consecutive_auth_failures has reached the
         threshold (user_activity_consecutive_failure_threshold)
      4. The latest failure type has its per-trigger toggle on
         (auto_tombstone_on_auth_error /
          auto_tombstone_on_unreachable)

    Phase C body. Read-only: never mutates anything. The sweeper
    calls this before set_managed_user_tombstone so all five layers
    enforce in one place.
    """
    try:
        from services.tunables import (
            user_activity_sweeper_enabled,
            user_activity_consecutive_failure_threshold,
            auto_tombstone_on_auth_error,
            auto_tombstone_on_unreachable,
        )
    except Exception:
        return False
    # Layer 1: global sweeper master switch.
    if not user_activity_sweeper_enabled():
        return False
    # Layer 5: per-server opt-in.
    try:
        from server import server_registry
        rows = server_registry.list_servers() or []
        server_row = next(
            (r for r in rows if r.get("id") == server_id), None,
        )
    except Exception:
        return False
    if server_row is None:
        return False
    if not bool(server_row.get("auto_tombstone_inactive_users_enabled")):
        return False
    # Layer 3: counter threshold.
    try:
        from server import media_db
        user = media_db.get_managed_user(server_id, username)
    except Exception:
        return False
    if user is None:
        return False
    failures = int(user.get("consecutive_auth_failures") or 0)
    if failures < user_activity_consecutive_failure_threshold():
        return False
    # Layer 4: per-trigger toggle for the latest failure type. Each
    # signal type was recorded regardless of the toggle, but the
    # tombstone only fires when the matching toggle is on. This lets
    # an operator enable just auth_error counting without unreachable
    # ever triggering (and vice versa).
    last_status = (user.get("last_auth_status") or "").lower()
    if last_status == "auth_error" and auto_tombstone_on_auth_error():
        return True
    if last_status == "unreachable" and auto_tombstone_on_unreachable():
        return True
    return False