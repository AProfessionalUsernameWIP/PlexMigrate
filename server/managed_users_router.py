"""
PR-10 - Managed Users management API.

Surfaces under ``/api/managed-users/{server_id}/...`` and powers the
new User Management sub-tab. Reads expose metadata + boolean presence
flags only (no plaintext credentials); writes require BOTH a valid
JWT (operator+) AND a separate db_admin username/password pair in the
request body. The db_admin gate is server-side: ``auth_db.verify_password``
re-validates on every write so a stolen JWT alone cannot mutate stored
credentials.

Sync (``POST /sync``) refreshes per-server metadata from the live
Plex/Emby/Jellyfin API. Sync writes are metadata-only - no credential
columns are touched - so the db_admin gate does NOT apply there. PR-11
will reuse the same helper for its automatic sync on server connect.

Storage is :mod:`server.media_db` with Fernet encryption per credential
cell (see ``server.secrets``). Files: ``server_data/media.db`` + ``.keyfile``.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from server import auth_db, media_db, server_registry
from server.auth_router import require_role


log = logging.getLogger("plexmigrate.server.managed_users_router")


# ── Pydantic models ─────────────────────────────────────────────────────────

class _DbAdminGate(BaseModel):
    """Mixin-shaped fields required on every db_admin-gated write."""
    db_admin_username: str = Field(
        description="Username of the Database Admin Account configured under Settings."
    )
    db_admin_password: str = Field(
        description="Password for that account. Re-verified server-side on every call."
    )


class ManagedUserUpdateIn(_DbAdminGate):
    """
    Update body for one managed-user row. Every field is optional;
    only supplied fields change. Sentinel ``clear_*`` flags are
    required to distinguish "field omitted (leave alone)" from
    "field explicitly cleared".

    PR-11.1 - ``tombstoned`` is the per-server hide flag. ``True``
    hides the row from the JobFormPanel picker and the default User
    Management list; ``False`` unhides. Credentials are preserved
    across hide/unhide cycles.
    """
    display_name: Optional[str] = None
    clear_display_name: bool = False
    auth_token: Optional[str] = None
    clear_auth_token: bool = False
    plex_home_pin: Optional[str] = None
    clear_plex_home_pin: bool = False
    service_password: Optional[str] = None
    clear_service_password: bool = False
    tombstoned: Optional[bool] = None


class ManagedUserDeleteIn(_DbAdminGate):
    """
    DELETE body. PR-11.1 changed the semantics: this is now a
    'hide on this server' alias - sets ``tombstoned=1`` on the row
    instead of purging. Credentials are preserved. To restore, PATCH
    with ``tombstoned: false`` or use the unhide path. To hide a
    username across every server, use the global-tombstones endpoints.
    """


class GlobalTombstoneIn(_DbAdminGate):
    """
    Body for ``POST /api/managed-users/global-tombstones``. The
    ``username`` is what the sync helper will skip across every
    registered server; existing per-server rows for this username
    stay in the DB (creds preserved) but are reported as hidden.
    """
    username: str = Field(description="Username to hide on every registered server.")


class GlobalTombstoneDeleteIn(_DbAdminGate):
    """
    Body for ``DELETE /api/managed-users/global-tombstones/{username}``.
    db_admin gate only; the username comes from the path.
    """


class GlobalCredentialIn(_DbAdminGate):
    """
    PR-13 fix #2 - apply one credential to every managed-user row
    matching ``username`` across every registered server.

    ``kind`` selects which credential cell to write
    (``auth_token`` / ``plex_home_pin`` / ``service_password``);
    ``plaintext`` is the value (empty string clears). One write per
    matching row; rows globally tombstoned are still updated so an
    operator who un-tombstones later finds the credential intact.
    """
    kind: str = Field(
        description="Credential to apply: 'auth_token', 'plex_home_pin', or 'service_password'."
    )
    plaintext: str = Field(
        default="",
        description="Value to encrypt and store. Empty string clears the credential on every match.",
    )


# ── Router ──────────────────────────────────────────────────────────────────

router = APIRouter(prefix="/api/managed-users", tags=["managed-users"])


def _verify_db_admin(username: str, password: str) -> None:
    """
    Re-validate the supplied db_admin credentials. Raises 401 on any
    failure path - "no such user", "wrong password", or "username
    exists but is not a db_admin row" all collapse to the same
    response so an attacker can't probe for db_admin existence.
    """
    if not username or not password:
        raise HTTPException(status_code=401, detail="Database admin credentials are required.")
    verified = auth_db.verify_password(username, password)
    if verified is None or verified.get("role") != "db_admin":
        raise HTTPException(status_code=401, detail="Database admin credentials are invalid.")


def _ensure_server_exists(server_id: str) -> Dict[str, Any]:
    """Resolve a registered server row or raise 404."""
    row = server_registry.get_server_by_id(server_id, include_token=False)
    if row is None:
        raise HTTPException(status_code=404, detail=f"No server with id {server_id!r}.")
    return row


@router.get("/global-tombstones")
def list_global_tombstones(
    user: Dict[str, Any] = Depends(require_role("operator")),
) -> Dict[str, Any]:
    """
    PR-11.1 - return every globally-tombstoned username. Used by the
    User Management panel to surface usernames that are hidden across
    all servers (and therefore won't appear under any specific server
    in the per-server list, since the sync helper skips them).
    """
    return {"usernames": media_db.list_global_tombstones()}


@router.post("/global-tombstones")
def add_global_tombstone(
    body: GlobalTombstoneIn,
    user: Dict[str, Any] = Depends(require_role("operator")),
) -> Dict[str, Any]:
    """
    Add a username to the global tombstones table. db_admin gated -
    matches every credential-write surface in this router.

    Existing per-server rows for this username are kept (creds
    preserved); they'll just report ``hidden_scope='global'`` on
    subsequent reads. Future syncs skip the username entirely.
    """
    _verify_db_admin(body.db_admin_username, body.db_admin_password)
    try:
        media_db.add_global_tombstone(body.username)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"username": body.username, "tombstoned": True}


@router.delete("/global-tombstones/{username}")
def remove_global_tombstone(
    username: str,
    body: GlobalTombstoneDeleteIn,
    user: Dict[str, Any] = Depends(require_role("operator")),
) -> Dict[str, Any]:
    """
    Remove a username from the global tombstones table. db_admin
    gated. The username becomes syncable again - the next live-API
    sync on any server will resurface them.
    """
    _verify_db_admin(body.db_admin_username, body.db_admin_password)
    try:
        media_db.remove_global_tombstone(username)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"username": username, "tombstoned": False}


@router.post("/global-credential/{username}")
def set_global_credential(
    username: str,
    body: GlobalCredentialIn,
    user: Dict[str, Any] = Depends(require_role("operator")),
) -> Dict[str, Any]:
    """
    PR-13 fix #2 - write one credential (auth_token / plex_home_pin /
    service_password) to every ``managed_users`` row that shares this
    username across registered servers. Useful when one Plex Home
    user exists on multiple registered Plex servers with the same
    PIN / password / token: the operator types the value once and
    every server's row picks it up.

    db_admin gated. Plaintext is encrypted by
    :func:`media_db.set_managed_user_credential` before storage.

    Empty ``plaintext`` clears the credential on every match.

    Returns ``{username, kind, applied: int, missing: int}``:
      * ``applied`` - rows whose credential was successfully written.
      * ``missing`` - servers where the username has no row yet
        (silently skipped; operator can sync those servers first).
    """
    _verify_db_admin(body.db_admin_username, body.db_admin_password)
    if body.kind not in media_db.MANAGED_USER_CREDENTIAL_KINDS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unknown credential kind {body.kind!r}. Valid: "
                f"{', '.join(media_db.MANAGED_USER_CREDENTIAL_KINDS)}"
            ),
        )

    applied = 0
    missing = 0
    # Walk every registered server. ``media_db.get_managed_user``
    # returns None when the username has no row on that server.
    try:
        servers = server_registry.list_servers(include_tokens=False)
    except Exception:
        servers = []
    for srv in servers:
        sid = str(srv.get("id") or "")
        if not sid:
            continue
        row = media_db.get_managed_user(sid, username)
        if row is None:
            missing += 1
            continue
        try:
            media_db.set_managed_user_credential(
                server_id=sid,
                username=username,
                kind=body.kind,
                plaintext=body.plaintext or "",
            )
            applied += 1
        except ValueError:
            # Defensive: row was deleted between get and set, treat
            # as missing rather than fail the whole sweep.
            missing += 1
    return {
        "username": username,
        "kind": body.kind,
        "applied": applied,
        "missing": missing,
    }


@router.get("/{server_id}")
def list_users(
    server_id: str,
    include_hidden: bool = False,
    user: Dict[str, Any] = Depends(require_role("operator")),
) -> Dict[str, Any]:
    """
    Return every managed-user row known for one server. Credentials
    are never inlined - only ``has_token`` / ``has_pin`` / ``has_password``
    booleans tell the UI whether something is stored. Operators+ only;
    viewer doesn't see User Management at all.

    ``include_hidden=true`` (default false) also returns rows whose
    per-server tombstone is set or whose username is globally
    tombstoned. The User Management 'Show hidden' toggle and the
    ServersPanel diff effect both pass this so they can reason about
    hidden rows without losing data.
    """
    _ensure_server_exists(server_id)
    return {
        "server_id": server_id,
        "users": media_db.list_managed_users(server_id, include_hidden=include_hidden),
    }


@router.get("/{server_id}/{username}")
def get_one_user(
    server_id: str,
    username: str,
    user: Dict[str, Any] = Depends(require_role("operator")),
) -> Dict[str, Any]:
    """Single managed-user row, same presence-only shape as the list endpoint."""
    _ensure_server_exists(server_id)
    row = media_db.get_managed_user(server_id, username)
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=f"No managed user {username!r} on server {server_id!r}.",
        )
    return row


@router.patch("/{server_id}/{username}")
def update_user(
    server_id: str,
    username: str,
    body: ManagedUserUpdateIn,
    user: Dict[str, Any] = Depends(require_role("operator")),
) -> Dict[str, Any]:
    """
    Update credentials and/or display name for one managed user.

    Requires:
      * Operator+ JWT (frontend tab visibility gate).
      * Valid db_admin username + password in the body. Re-verified
        on every call - there is no persistent db_admin session.

    Each credential field has two write modes:
      * Supplying a non-empty string sets / replaces the stored value
        (Fernet-encrypted before write).
      * Setting ``clear_<field>: true`` wipes the stored value.
      * Omitting both leaves the existing value untouched.

    The same ``clear_display_name`` semantics apply for the friendly
    name. Returns the updated row in public shape.
    """
    _verify_db_admin(body.db_admin_username, body.db_admin_password)
    _ensure_server_exists(server_id)
    existing = media_db.get_managed_user(server_id, username)
    if existing is None:
        raise HTTPException(
            status_code=404,
            detail=f"No managed user {username!r} on server {server_id!r}. "
                   "Run a sync first to populate the table.",
        )

    try:
        # Display name (plaintext metadata).
        if body.clear_display_name:
            media_db.set_managed_user_display_name(
                server_id=server_id, username=username, display_name=None,
            )
        elif body.display_name is not None:
            media_db.set_managed_user_display_name(
                server_id=server_id, username=username,
                display_name=body.display_name,
            )

        # Three credential cells, each handled independently.
        cred_inputs = [
            ("auth_token", body.auth_token, body.clear_auth_token),
            ("plex_home_pin", body.plex_home_pin, body.clear_plex_home_pin),
            ("service_password", body.service_password, body.clear_service_password),
        ]
        for kind, value, clear in cred_inputs:
            if clear:
                media_db.set_managed_user_credential(
                    server_id=server_id, username=username,
                    kind=kind, plaintext="",
                )
            elif value is not None and value != "":
                media_db.set_managed_user_credential(
                    server_id=server_id, username=username,
                    kind=kind, plaintext=value,
                )
            # else: not provided - leave alone.

        # Per-server tombstone flag (PR-11.1). None = leave alone;
        # True/False = explicit hide/unhide.
        if body.tombstoned is not None:
            media_db.set_managed_user_tombstone(
                server_id=server_id, username=username,
                tombstoned=bool(body.tombstoned),
            )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    updated = media_db.get_managed_user(server_id, username)
    assert updated is not None
    return updated


@router.delete("/{server_id}/{username}")
def hide_user_on_server(
    server_id: str,
    username: str,
    body: ManagedUserDeleteIn,
    user: Dict[str, Any] = Depends(require_role("operator")),
) -> Dict[str, Any]:
    """
    PR-11.1 - hide this user on this server (per-server tombstone).
    Credentials are preserved across hide/unhide cycles. The row
    stays in the DB but is filtered from the default User Management
    list and the JobFormPanel picker. db_admin gated.

    Restoring is either ``PATCH ... {tombstoned: false}`` from the
    detail view's Unhide button, or letting a future sync run with
    no per-server tombstone in place (a fresh sync from the same
    server would re-upsert as ``tombstoned=0`` only if the operator
    has explicitly toggled it; the sync helper itself doesn't reset
    the flag).

    Pre-PR-11.1 callers that expected a row purge can use the
    'Globally hide username' surface instead, which still preserves
    the row but ensures the sync helper never resurfaces it on any
    server.
    """
    _verify_db_admin(body.db_admin_username, body.db_admin_password)
    _ensure_server_exists(server_id)
    try:
        media_db.set_managed_user_tombstone(
            server_id=server_id, username=username, tombstoned=True,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {"hidden": username, "server_id": server_id, "hidden_scope": "server"}


@router.post("/{server_id}/sync")
def sync_users(
    server_id: str,
    user: Dict[str, Any] = Depends(require_role("operator")),
) -> Dict[str, Any]:
    """
    Pull the live user list from the registered server and upsert it
    into the managed_users table. Metadata-only - no credential
    columns are touched, so existing stored tokens / PINs / passwords
    are preserved across syncs.

    Does NOT require db_admin credentials: this is a metadata refresh,
    not a credential write. PR-11's auto-sync hooks (in app.py's
    create_server / update_one_server / test_one_server) call the same
    underlying ``media_db.sync_managed_users_from_live`` helper.

    Additive-only: users present in the DB but absent from the live
    API are NOT auto-deleted. Manual delete is the only removal path.
    """
    _ensure_server_exists(server_id)
    result = media_db.sync_managed_users_from_live(server_id, log)
    # 502 if the live API was unreachable (sync helper swallows the
    # exception so the operator-facing message stays clean here).
    if result.get("error"):
        err_low = result["error"].lower()
        status = 404 if "no server" in err_low else 502
        raise HTTPException(status_code=status, detail=result["error"])
    return {
        "server_id": server_id,
        "synced": int(result.get("synced") or 0),
        "source_error": result.get("source_error"),
    }
