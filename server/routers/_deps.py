"""Shared route helpers used by multiple router modules.

Extracted from the original ``server.app`` god file as part of the
Phase-3a organizational decomposition. The functions here MUST stay
behaviour-identical to their previous in-app.py definitions — every
extraction is move-only and the original call sites import from this
module instead of the now-removed local definition.

Both ``_apply_preflight_ack`` and ``_auto_sync_managed_users`` are
called by multiple router groups (jobs, servers) so they live in this
shared module rather than being duplicated.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from server import server_registry


log = logging.getLogger("plexmigrate.server")


def _auto_sync_managed_users(
    server_id: str,
    *,
    force_capture: bool = False,
    capture_only_if_missing: bool = True,
) -> Dict[str, Any]:
    """
    Fire a best-effort managed-users metadata sync for one server,
    then a best-effort per-user token capture for the same server.
    Used by the server-connect hooks (create / update / test) so the
    DB stays warm without end user action. Swallows all exceptions:
    sync or capture failures must NOT block the surrounding
    server-registry call from succeeding.

    The token-capture half is rate-limited per server (see
    ``user_token_capture_throttle_per_hour`` in settings). When this
    function is called by an explicit end user action that should
    bypass the throttle (e.g. a Refresh-server button), pass
    ``force_capture=True``.

    The token-capture half is additive-only by default: users that
    already have a stored auth_token are skipped, so an existing
    valid token is never overwritten by a refresh sweep. Pass
    ``capture_only_if_missing=False`` only from a deliberate
    rotation path (per-user Rotate-token button or the future
    ``auto_rotate_tokens_on_refresh`` tunable).

    Returns the token-capture summary dict from
    ``user_capture.capture_managed_user_tokens`` (``captured``,
    ``skipped_existing``, ``throttled``, ``errors``). Sync errors are
    logged but not included in the return value because they're
    surfaced via the per-server status display elsewhere.
    """
    # Resolve the server's friendly name for the verbose section header
    # so the operator can scan the app log and tell at a glance which
    # server each block belongs to. Lookup is cheap (single registry
    # read) and best-effort - on failure we fall back to server_id.
    server_name_for_log = server_id
    try:
        srv_row = server_registry.get_server_by_id(server_id, include_token=False)
        if srv_row and (srv_row.get("name") or "").strip():
            server_name_for_log = srv_row["name"]
    except Exception:
        pass
    log.info(
        "=" * 8 + " Auto-sync start: server=%r (id=%s, force=%s, additive=%s) " + "=" * 8,
        server_name_for_log, server_id, force_capture, capture_only_if_missing,
    )

    try:
        from server import media_db
        result = media_db.sync_managed_users_from_live(server_id, log)
        if result.get("error"):
            log.warning(
                "Auto-sync managed users for %r returned: %s",
                server_name_for_log, result["error"],
            )
        elif result.get("synced"):
            log.info(
                "Auto-synced %d managed user(s) for %r",
                result["synced"], server_name_for_log,
            )
    except Exception:  # pragma: no cover (defensive)
        log.exception(
            "Auto-sync managed users failed unexpectedly for %r",
            server_name_for_log,
        )

    # Per-user token capture, throttled per server. PIN-protected
    # users without a stored PIN are silently skipped here; the
    # preflight check surfaces them to the end user before each job.
    cap_summary: Dict[str, Any] = {
        "captured": 0,
        "skipped_existing": 0,
        "throttled": False,
        "errors": [],
    }
    try:
        from server import user_capture
        cap_summary = user_capture.capture_managed_user_tokens(
            server_id,
            force=force_capture,
            only_if_missing=capture_only_if_missing,
            logger=log,
        )
        if cap_summary.get("throttled"):
            log.info(
                "Auto-sync: per-user token capture for %r THROTTLED "
                "(default 4/hour/server); no AuthenticateByName calls "
                "fired this round.",
                server_name_for_log,
            )
        elif cap_summary.get("captured"):
            log.info(
                "Auto-sync: captured %d per-user token(s) for %r "
                "(skipped %d already-stored)",
                cap_summary["captured"], server_name_for_log,
                cap_summary.get("skipped_existing", 0),
            )
        # Per-user errors (PINs not stored, AuthenticateByName 401s,
        # missing AccessTokens) are operator-actionable; surface them
        # at INFO so they land in app.log alongside the per-user lines
        # the capture flow already emits.
        for err in (cap_summary.get("errors") or []):
            log.info("  auto-sync error for %r: %s", server_name_for_log, err)
    except Exception as exc:  # pragma: no cover (defensive)
        log.exception(
            "Auto-capture user tokens failed unexpectedly for %r",
            server_name_for_log,
        )
        cap_summary["errors"].append(f"unexpected: {exc}")

    log.info(
        "=" * 8 + " Auto-sync done:  server=%r (captured=%d, skipped=%d, "
        "throttled=%s, errors=%d) " + "=" * 8,
        server_name_for_log,
        cap_summary.get("captured", 0),
        cap_summary.get("skipped_existing", 0),
        cap_summary.get("throttled", False),
        len(cap_summary.get("errors") or []),
    )
    return cap_summary


def _apply_preflight_ack(params: Dict[str, Any]) -> None:
    """
    Map the public ``pin_preflight_acknowledged`` / ``pin_preflight_at_risk``
    fields a job-submit body may carry onto the underscore-prefixed
    synthetic params the engine expects on a JobRecord. Called by every
    job endpoint after ``body.model_dump`` so the convention is uniform.

    No-op when the end user never saw the modal: the flags default to
    False/None on the model, get stripped by ``exclude_none=True``,
    and we drop the False ack defensively.
    """
    ack = bool(params.pop("pin_preflight_acknowledged", False))
    at_risk_raw = params.pop("pin_preflight_at_risk", None)
    if not ack:
        return
    params["_pin_preflight_acknowledged"] = True
    if isinstance(at_risk_raw, list):
        params["_pin_preflight_at_risk"] = [
            str(x) for x in at_risk_raw if isinstance(x, str)
        ]
    else:
        params["_pin_preflight_at_risk"] = []
