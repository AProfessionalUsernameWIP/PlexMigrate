"""
Preflight checks. The only check is the PIN preflight: for each
managed user in scope on the relevant server(s), confirm that the
end user has stored either an auth token or a Plex Home PIN in
:mod:`server.media_db`.

A managed user with neither credential on file is at risk: the engine
falls back to admin-token impersonation, which can return incomplete
data for PIN-scoped content. The pre-flight check surfaces those
users to the end user BEFORE the job commits so a PIN can be saved
under User Management and the job re-run cleanly.

Mode awareness
--------------
The check is gated on whether the run actually needs per-user
authentication:

* ``snapshot``  - checks the source server's managed users.
* ``direct``    - checks the source AND each destination server.
* ``restore``   - file-mediated, admin-token only; the check returns
  ``checked: False`` with an empty at-risk list (no friction).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Set


log = logging.getLogger("plexmigrate.server.preflight")


def compute_pin_preflight(
    mode: str,
    source_server_name: Optional[str],
    dest_server_names: Optional[List[str]],
    user_filter: Optional[List[str]],
    *,
    source_service_type: str = "plex",
    dest_service_types: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    Return ``{mode, checked, at_risk_users, servers_checked}``:

      * ``mode``           - echoes the input mode.
      * ``checked``        - True if the preflight applied to this run.
                             ``restore`` and runs with no resolvable
                             server return False.
      * ``at_risk_users``  - sorted, deduplicated list of usernames
                             that have no stored token AND no stored
                             PIN on any in-scope server. Owners are
                             excluded (admin token covers them).
      * ``servers_checked`` - list of registered server friendly names
                             the preflight actually inspected.

    ``user_filter`` narrows the scope to specific usernames when
    supplied (mirrors the per-user transfer scope used by
    :class:`DirectTransferIn`).

    ``source_service_type`` / ``dest_service_types`` disambiguate
    same-named-different-backend servers. Legacy callers omit them and
    we default to "plex" - matches the behaviour for installs without
    duplicate names.
    """
    # Restore is the only mode that bypasses per-user auth (the file
    # is the source of truth; the admin token writes everyone's data
    # on the destination). Return the no-friction shape so the
    # frontend can skip the modal entirely.
    if mode == "restore":
        return {
            "mode": mode,
            "checked": False,
            "at_risk_users": [],
            "servers_checked": [],
        }

    # Build the list of servers the run will need per-user auth from.
    # Snapshot reads from one server (the source); direct also writes
    # to each destination, so destinations also need their own users
    # authenticated. Each entry is a (name, service_type) tuple so
    # we can disambiguate same-named servers across backends.
    src_type = (source_service_type or "plex").lower()
    servers_to_check: List[tuple] = []
    seen: Set[tuple] = set()
    if source_server_name:
        key = (source_server_name, src_type)
        servers_to_check.append(key)
        seen.add(key)
    if mode == "direct" and dest_server_names:
        dst_types = dest_service_types or []
        for i, dn in enumerate(dest_server_names):
            if not dn:
                continue
            ds = (dst_types[i] if i < len(dst_types) else "plex") or "plex"
            ds = ds.lower()
            key = (dn, ds)
            if key not in seen:
                servers_to_check.append(key)
                seen.add(key)

    if not servers_to_check:
        return {
            "mode": mode,
            "checked": False,
            "at_risk_users": [],
            "servers_checked": [],
        }

    # Late imports keep this module light at import time.
    from server import media_db, server_registry

    user_filter_set: Optional[Set[str]] = (
        {u for u in user_filter if isinstance(u, str)}
        if user_filter is not None else None
    )
    at_risk: Set[str] = set()
    servers_checked: List[str] = []

    for sname, sservice in servers_to_check:
        row = server_registry.get_server_by_name(
            sname, include_token=False, service_type=sservice,
        )
        if row is None:
            # Unknown server name. Skip silently; the job submission
            # itself will reject the request with a clearer error.
            continue
        server_id = row.get("id") or ""
        if not server_id:
            continue
        try:
            # Route the preflight permission check through the shared
            # activity filter so a tombstoned user never trips a
            # PIN-missing warning (the engine won't touch them on the
            # actual run either).
            from services.user_management.activity_filter import list_active_users
            users = list_active_users(server_id)
        except Exception:
            # media.db hiccup. Treat as "no managed users to worry
            # about" rather than failing the preflight - the job
            # submission still goes through and the engine handles
            # missing-auth at the per-user level as before.
            log.exception("list_active_users failed for server %r", server_id)
            continue
        servers_checked.append(sname)
        for u in users:
            # Owners are covered by the admin token. Per-user PIN/token
            # only matters for managed-kind users.
            if (u.get("kind") or "managed") == "owner":
                continue
            uname = u.get("username") or ""
            if not uname:
                continue
            if user_filter_set is not None and uname not in user_filter_set:
                continue
            if u.get("has_token") or u.get("has_pin"):
                continue
            # Share-state gate. Flagging every credential-less row as
            # "PIN-protected" would include stale users the end user
            # had un-shared on Plex.tv. Two flags from
            # /api/servers/{mid}/shared_servers + /api/home/users tell
            # us which is which:
            #   * active_share=False -> user is gone from this server.
            #     The engine's share-state gate already drops them; no
            #     point asking the end user to save a PIN for a row
            #     they don't actually have access to.
            #   * is_pin_protected=False -> user is shared but does not
            #     have a PIN set on Plex.tv. They authenticate without
            #     one; a missing stored PIN isn't a problem.
            # Only flag the row when Plex.tv confirms an active share
            # AND a PIN requirement AND we don't have one stored.
            if not u.get("active_share", True):
                continue
            if not u.get("is_pin_protected", False):
                continue
            at_risk.add(uname)

    # ``checked`` means "the preflight actually inspected at least one
    # registered server." If every name in the input resolved to None
    # (unknown server) we return False so the frontend can skip the
    # modal entirely; the upcoming job submission will surface the
    # bad-server error from its own validation path.
    return {
        "mode": mode,
        "checked": bool(servers_checked),
        "at_risk_users": sorted(at_risk),
        "servers_checked": servers_checked,
    }
