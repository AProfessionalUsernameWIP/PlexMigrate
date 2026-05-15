"""
PR-12 preflight checks. Today the only check is the PIN preflight:
for each managed user in scope on the relevant server(s), confirm
that the operator has stored either an auth token or a Plex Home
PIN in :mod:`server.media_db`.

A managed user with neither credential on file is at risk: the engine
falls back to admin-token impersonation, which can return incomplete
data for PIN-scoped content. The pre-flight check surfaces those
users to the operator BEFORE the job commits so a PIN can be saved
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
    # authenticated.
    server_names_to_check: List[str] = []
    if source_server_name:
        server_names_to_check.append(source_server_name)
    if mode == "direct" and dest_server_names:
        for dn in dest_server_names:
            if dn and dn not in server_names_to_check:
                server_names_to_check.append(dn)

    if not server_names_to_check:
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

    for sname in server_names_to_check:
        row = server_registry.get_server_by_name(sname, include_token=False)
        if row is None:
            # Unknown server name. Skip silently; the job submission
            # itself will reject the request with a clearer error.
            continue
        server_id = row.get("id") or ""
        if not server_id:
            continue
        try:
            users = media_db.list_managed_users(server_id, include_hidden=False)
        except Exception:
            # media.db hiccup. Treat as "no managed users to worry
            # about" rather than failing the preflight - the job
            # submission still goes through and the engine handles
            # missing-auth at the per-user level as before.
            log.exception("list_managed_users failed for server %r", server_id)
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
