"""
Managed Users management API.

Surfaces under ``/api/managed-users/{server_id}/...`` and powers the
User Management sub-tab. Reads expose metadata + boolean presence
flags only (no plaintext credentials); writes require BOTH a valid
JWT (end user+) AND a separate db_admin username/password pair in the
request body. The db_admin gate is server-side: ``auth_db.verify_password``
re-validates on every write so a stolen JWT alone cannot mutate stored
credentials.

Sync (``POST /sync``) refreshes per-server metadata from the live
Plex/Emby/Jellyfin API. Sync writes are metadata-only - no credential
columns are touched - so the db_admin gate does NOT apply there. The
automatic sync on server connect reuses the same helper.

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

    ``tombstoned`` is the per-server hide flag. ``True`` hides the row
    from the JobFormPanel picker and the default User Management list;
    ``False`` unhides. Credentials are preserved across hide/unhide
    cycles.
    """
    display_name: Optional[str] = None
    clear_display_name: bool = False
    auth_token: Optional[str] = None
    clear_auth_token: bool = False
    plex_home_pin: Optional[str] = None
    clear_plex_home_pin: bool = False
    # Migration v16: per-backend PIN columns. The operator may store an
    # Emby EasyPassword or Jellyfin EasyPassword on the corresponding
    # row. PATCH never returns plaintext; reveal goes through the
    # dedicated reveal-credentials endpoint.
    emby_easy_pin: Optional[str] = None
    clear_emby_easy_pin: bool = False
    jellyfin_easy_pin: Optional[str] = None
    clear_jellyfin_easy_pin: bool = False
    service_password: Optional[str] = None
    clear_service_password: bool = False
    # When True, PIN writes in this PATCH also propagate to
    # CROSS-backend identity_map links (e.g., a Plex row linked to a
    # Jellyfin row receives the same encrypted value into its
    # backend-appropriate PIN column). Same-backend propagation always
    # fires for PIN kinds - this flag only governs cross-backend
    # fan-out, matching the User Management two-button save UI.
    cross_backend_pin: bool = False
    tombstoned: Optional[bool] = None


class ManagedUserDeleteIn(_DbAdminGate):
    """
    DELETE body. This is a 'hide on this server' alias - it sets
    ``tombstoned=1`` on the row instead of purging. Credentials are
    preserved. To restore, PATCH with ``tombstoned: false`` or use
    the unhide path. To hide a username across every server, use the
    global-tombstones endpoints.
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


class RevealCredentialsIn(_DbAdminGate):
    """Body for the credential-reveal endpoint
    ``POST /api/managed-users/{server_id}/{username}/reveal-credentials``.

    Returns the Fernet-decrypted plaintext for the stored auth_token
    + plex_home_pin cells on the matching ``managed_users`` row.
    Requires BOTH gates to pass before the decrypt fires:

      * The standard ``db_admin_username`` + ``db_admin_password`` pair
        that every other credential-write endpoint requires (inherited
        from :class:`_DbAdminGate`).
      * The signed-in operator's OWN ``root_admin`` password, supplied
        in ``root_admin_password`` here. The username comes from the
        JWT, not the request body — only an active root_admin session
        can hit this endpoint at all (per the ``require_role`` in the
        handler), and the password re-check binds the action to a
        live human acknowledgement instead of just a stolen JWT.

    No response caching, no body fields persisted in the request
    log, no token written into the response anywhere except the
    decrypted plaintext. The audit trail records who revealed which
    credential when via :mod:`services.run_logs.db_access`.
    """
    root_admin_password: str = Field(
        description=(
            "Password for the signed-in root_admin's own account. "
            "Required in addition to the db_admin gate; both must "
            "pass before the plaintext credentials are returned."
        ),
    )


class RevealCredentialsResponse(BaseModel):
    """Response shape for the reveal-credentials endpoint. ``None``
    on any field means the underlying cell is empty (operator never
    saved it), NOT "you weren't allowed to see it" — the request
    itself fails with 401 in that case.

    The PIN fields are returned per backend (Plex Home PIN, Emby
    EasyPassword, Jellyfin EasyPassword). A given row only ever
    populates the one matching its own ``service_type``; the others
    will always be ``None`` for that row."""
    auth_token: Optional[str] = None
    plex_home_pin: Optional[str] = None
    emby_easy_pin: Optional[str] = None
    jellyfin_easy_pin: Optional[str] = None


class PlexHomeTokenIn(_DbAdminGate):
    """Body for the Playlist Management convenience endpoint
    ``POST /api/managed-users/{server_id}/{username}/plex-home-token``.

    Lets the end user paste a Plex Home user's token (obtained from
    plex.tv > Account > Authorized Devices > <device> > X-Plex-Token)
    so the Playlist Management copy path can act AS that user when the
    `playlist_mgmt_plex_home_auth_mode` tunable is set to
    `per_user_token` (instead of the owner_token default).

    Different from the existing PATCH endpoint: this one auto-upserts
    the managed_users row when missing (saves the end user the
    additional "run a sync first" step the PATCH requires). The token
    field uses two semantics shared with the PATCH endpoint:
      * Supplying a non-empty `auth_token` stores it.
      * `clear_auth_token=true` wipes the stored token.
    """
    auth_token: Optional[str] = None
    clear_auth_token: bool = False
    # Plex Home users have rotating tokens. The end user may use this
    # endpoint to refresh after a token rotates without re-typing
    # every per-user-token; the optional friendly display_name lets
    # them annotate the row on first write so the UI shows a clearer
    # label than the raw username.
    display_name: Optional[str] = None


class GlobalCredentialIn(_DbAdminGate):
    """
    Apply one credential to every managed-user row matching
    ``username`` across every registered server.

    ``kind`` selects which credential cell to write - any value in
    :data:`media_db.MANAGED_USER_CREDENTIAL_KINDS`. ``plaintext`` is
    the value (empty string clears). One write per matching row; rows
    globally tombstoned are still updated so an end user who
    un-tombstones later finds the credential intact.

    Migration v16: PIN kinds (``plex_home_pin``, ``emby_easy_pin``,
    ``jellyfin_easy_pin``) additionally trigger the identity-link PIN
    propagation downstream in ``set_managed_user_credential``. When
    ``cross_backend_pin`` is True, PIN propagation crosses backend
    boundaries (e.g., a Plex<->Jellyfin manual link receives the same
    value into its Jellyfin EasyPassword column).

    The sweep is scoped by backend by default: passing
    ``service_type_filter`` restricts the registry walk to servers
    whose ``service_type`` matches. The UI's default "Apply on every
    server" path passes the origin row's service_type so a Plex Home
    PIN is not accidentally pushed to Emby/Jellyfin rows that happen
    to share the username. Pass ``None`` (the field default) for the
    all-backend behaviour - the UI's "every backend" override.
    """
    kind: str = Field(
        description=(
            "Credential to apply: 'auth_token', 'plex_home_pin', "
            "'emby_easy_pin', 'jellyfin_easy_pin', or 'service_password'."
        ),
    )
    plaintext: str = Field(
        default="",
        description="Value to encrypt and store. Empty string clears the credential on every match.",
    )
    cross_backend_pin: bool = Field(
        default=False,
        description=(
            "When True AND ``kind`` is a PIN kind, identity-link "
            "propagation crosses backend boundaries downstream. "
            "Ignored for non-PIN kinds."
        ),
    )
    service_type_filter: Optional[str] = Field(
        default=None,
        description=(
            "Optional backend filter. When set (e.g. 'plex', 'emby', "
            "'jellyfin'), the sweep visits only servers whose "
            "service_type matches. Default None keeps the legacy "
            "all-backend behaviour."
        ),
    )


# ── Router ──────────────────────────────────────────────────────────────────

router = APIRouter(prefix="/api/managed-users", tags=["managed-users"])


def _verify_db_admin(username: str, password: str) -> None:
    """
    Re-validate the supplied db_admin credentials. Raises 401 on any
    failure path - "no such user", "wrong password", or "username
    exists but is not a db_admin row" all collapse to the same
    response so an attacker can't probe for db_admin existence.

    AUTH-05: delegates to the single shared gate in auth_router so
    db_admin verification has exactly one implementation.
    """
    from server.auth_router import verify_db_admin
    verify_db_admin(username, password)


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
    Return every globally-tombstoned username. Used by the
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
    Write one credential (auth_token / plex_home_pin /
    service_password) to every ``managed_users`` row that shares this
    username across registered servers. Useful when one Plex Home
    user exists on multiple registered Plex servers with the same
    PIN / password / token: the end user types the value once and
    every server's row picks it up.

    db_admin gated. Plaintext is encrypted by
    :func:`media_db.set_managed_user_credential` before storage.

    Empty ``plaintext`` clears the credential on every match.

    Returns ``{username, kind, applied: int, missing: int}``:
      * ``applied`` - rows whose credential was successfully written.
      * ``missing`` - servers where the username has no row yet
        (silently skipped; end user can sync those servers first).
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
    # Optional backend filter so a Plex Home PIN sweep doesn't
    # accidentally write to Emby/Jellyfin rows that happen to share
    # the username. The UI's default "every server" path passes the
    # origin row's service_type here; the "every backend" override
    # omits the filter (None) and sweeps every backend.
    if body.service_type_filter:
        wanted = body.service_type_filter.strip().lower()
        servers = [
            s for s in servers
            if str(s.get("service_type") or s.get("service") or "").strip().lower() == wanted
        ]
    for srv in servers:
        sid = str(srv.get("id") or "")
        if not sid:
            continue
        row = media_db.get_managed_user(sid, username)
        if row is None:
            missing += 1
            continue
        # When sweeping every-backend with a PIN kind
        # (service_type_filter is None), remap the kind per target
        # row's service_type so the value lands in the row's natural
        # PIN column. Without this, a "Save on every server (all
        # backends)" PIN sweep from a Plex row would write the value
        # into the meaningless ``plex_home_pin_enc`` column on the
        # Emby/Jellyfin rows instead of those backends' EasyPassword
        # columns, and the PIN would never show as populated because
        # the ``has_emby_pin`` / ``has_jellyfin_pin`` booleans key off
        # the backend-natural columns.
        #
        # Only fires for PIN kinds when there's no backend filter; with
        # the filter set the sweep is already constrained to one
        # backend so the kind matches every visited row by
        # construction.
        effective_kind = body.kind
        if (
            body.service_type_filter is None
            and body.kind in media_db.PIN_KINDS
        ):
            row_svc = str(row.get("service_type") or "").lower()
            natural_kind = media_db.PIN_KIND_FOR_SERVICE.get(row_svc)
            if natural_kind is None:
                # Backend has no PIN-equivalent column; skip rather
                # than write to the wrong place.
                continue
            effective_kind = natural_kind
        try:
            media_db.set_managed_user_credential(
                server_id=sid,
                username=username,
                kind=effective_kind,
                plaintext=body.plaintext or "",
                cross_backend=bool(body.cross_backend_pin),
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
    booleans tell the UI whether something is stored. End users+ only;
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
      * End user+ JWT (frontend tab visibility gate).
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

        # Five credential cells, each handled independently. PIN kinds
        # (plex_home_pin / emby_easy_pin / jellyfin_easy_pin) honour
        # the body's ``cross_backend_pin`` flag so a single PATCH can
        # propagate the value across identity-linked rows on other
        # backends when the operator clicks "Save & apply across
        # backends" in User Management.
        cred_inputs = [
            ("auth_token", body.auth_token, body.clear_auth_token),
            ("plex_home_pin", body.plex_home_pin, body.clear_plex_home_pin),
            ("emby_easy_pin", body.emby_easy_pin, body.clear_emby_easy_pin),
            ("jellyfin_easy_pin", body.jellyfin_easy_pin, body.clear_jellyfin_easy_pin),
            ("service_password", body.service_password, body.clear_service_password),
        ]
        for kind, value, clear in cred_inputs:
            kwargs: Dict[str, Any] = {
                "server_id": server_id, "username": username, "kind": kind,
            }
            if kind in {"plex_home_pin", "emby_easy_pin", "jellyfin_easy_pin"}:
                kwargs["cross_backend"] = bool(body.cross_backend_pin)
            if clear:
                media_db.set_managed_user_credential(plaintext="", **kwargs)
            elif value is not None and value != "":
                media_db.set_managed_user_credential(plaintext=value, **kwargs)
            # else: not provided - leave alone.

        # Per-server tombstone flag. None = leave alone;
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


@router.post("/{server_id}/{username}/plex-home-token")
def set_plex_home_token(
    server_id: str,
    username: str,
    body: PlexHomeTokenIn,
    user: Dict[str, Any] = Depends(require_role("operator")),
) -> Dict[str, Any]:
    """Save (or clear) a Plex Home user's X-Plex-Token so the
    Playlist Management copy path can act AS that user when
    ``playlist_mgmt_plex_home_auth_mode`` is ``per_user_token``.

    Behaviour:
      * Auto-upserts the managed_users row (kind='managed',
        service_type='plex') when it doesn't yet exist. Saves the
        end user the "run a sync first" step that the PATCH
        endpoint enforces.
      * Non-empty ``auth_token`` stores Fernet-encrypted via
        :mod:`server.secrets`.
      * ``clear_auth_token=true`` wipes the stored token.
      * ``display_name`` (optional) updates the end user-visible
        label on first write.

    Plex-destination convenience only. The destination must be a
    Plex server (other backends ignore the per-user-token mode
    because admin-token-with-UserId writes work universally).
    db_admin re-auth on every call, same as the broader PATCH.
    """
    _verify_db_admin(body.db_admin_username, body.db_admin_password)
    server_row = _ensure_server_exists(server_id)
    if (server_row.get("service_type") or "plex").lower() != "plex":
        raise HTTPException(
            status_code=400,
            detail=(
                "Per-user-token storage is Plex-only. Destination "
                f"server {server_id!r} is "
                f"{server_row.get('service_type') or 'plex'!r}; "
                "Jellyfin / Emby admin tokens write per-user via "
                "the UserId in the URL so this endpoint is not needed."
            ),
        )
    # Auto-upsert. Metadata only; the credential set below is the
    # actual write the end user cares about.
    try:
        media_db.upsert_managed_user(
            server_id=server_id, username=username,
            display_name=body.display_name,
            service_type="plex", kind="managed",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # Resolve the credential mutation.
    try:
        if body.clear_auth_token:
            media_db.set_managed_user_credential(
                server_id=server_id, username=username,
                kind="auth_token", plaintext="",
            )
        elif body.auth_token is not None and body.auth_token != "":
            media_db.set_managed_user_credential(
                server_id=server_id, username=username,
                kind="auth_token", plaintext=body.auth_token,
            )
        else:
            # Neither set nor clear: nothing to do; surface the
            # current row so the UI can re-render its "has_token"
            # badge without a follow-up GET.
            current = media_db.get_managed_user(server_id, username)
            assert current is not None
            return current
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    updated = media_db.get_managed_user(server_id, username)
    assert updated is not None
    return updated


@router.post(
    "/{server_id}/{username}/reveal-credentials",
    response_model=RevealCredentialsResponse,
)
def reveal_managed_user_credentials(
    server_id: str,
    username: str,
    body: RevealCredentialsIn,
    user: Dict[str, Any] = Depends(require_role("root_admin")),
) -> RevealCredentialsResponse:
    """Reveal the decrypted ``auth_token`` + ``plex_home_pin`` for a
    managed-user row, gated behind a dual-credential check.

    Three layers of gate before the plaintext is returned:

    1. **Root-admin JWT.** Only an active ``root_admin`` session can
       reach this endpoint (``require_role("root_admin")``). Lower
       roles get the same 403 they'd get for any root-only route.
    2. **Root-admin password re-check.** The end user's own password
       is re-verified server-side via :func:`auth_db.verify_password`
       against the signed-in user's username (from the JWT, not the
       body — preventing a stolen JWT alone from making this work
       without the live human's password too).
    3. **db_admin password re-check.** The standard db_admin gate
       every other credential-write surface enforces (so a single
       compromised credential set can't unlock this either; the
       attacker needs BOTH).

    On any gate failure the endpoint returns 401 with the same
    detail string regardless of which gate rejected (the attacker
    can't probe which credential was the wrong one).

    The decrypt + return is audit-logged via
    :mod:`services.run_logs.db_access` so the operator's forensic trail
    has a record of every reveal: who, when, which server, which
    user, which credential cell.

    No-credential cases (the operator never saved a token / PIN)
    return ``null`` for that field. Missing managed_users row
    returns 404. Migration v16: all three PIN-equivalent cells are
    surfaced (``plex_home_pin``, ``emby_easy_pin``,
    ``jellyfin_easy_pin``); the operator decides which is relevant
    based on the row's ``service_type``. A given row only ever
    populates the one matching its own backend.
    """
    # Gate 1: root_admin JWT already enforced by require_role.
    # Gate 2: re-verify root_admin password. ``require_role`` returns
    # the user dict keyed on ``username`` (the JWT ``sub`` claim is not
    # propagated into that dict).
    root_username = (user.get("username") or "").strip()
    if not root_username:
        raise HTTPException(
            status_code=401,
            detail="Root admin re-authentication failed.",
        )
    verified = auth_db.verify_password(root_username, body.root_admin_password)
    if verified is None or verified.get("role") != "root_admin":
        raise HTTPException(
            status_code=401,
            detail="Root admin re-authentication failed.",
        )
    # Gate 3: re-verify db_admin password (same shape as every other
    # credential-write endpoint).
    _verify_db_admin(body.db_admin_username, body.db_admin_password)
    # All three gates passed. Confirm the target row exists; 404 if not.
    _ensure_server_exists(server_id)
    existing = media_db.get_managed_user(server_id, username)
    if existing is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"No managed user {username!r} on server {server_id!r}."
            ),
        )
    # Fetch the decrypted plaintexts. Each call internally logs the
    # read via db_access_log; we also write a single dedicated
    # "reveal" event so the audit trail makes the reveal action
    # explicit + carries the operator's identity.
    auth_token: Optional[str] = None
    pins: Dict[str, Optional[str]] = {
        "plex_home_pin": None,
        "emby_easy_pin": None,
        "jellyfin_easy_pin": None,
    }
    try:
        auth_token = media_db.get_managed_user_credential(
            server_id, username, kind="auth_token",
        )
    except Exception:
        log.exception(
            "reveal_managed_user_credentials: auth_token decrypt "
            "failed for (%r, %r); returning null.",
            server_id, username,
        )
        auth_token = None
    for pin_kind in tuple(pins.keys()):
        try:
            pins[pin_kind] = media_db.get_managed_user_credential(
                server_id, username, kind=pin_kind,
            )
        except Exception:
            log.exception(
                "reveal_managed_user_credentials: %s decrypt failed for "
                "(%r, %r); returning null.",
                pin_kind, server_id, username,
            )
            pins[pin_kind] = None
    # Dedicated audit event — easier to grep than per-cell read logs.
    try:
        from services.run_logs import db_access as db_access_log
        db_access_log.log_event(
            "Credential reveal: root_admin=%r revealed managed_users "
            "credentials for (server=%r, username=%r) after "
            "dual-credential gate (root_admin + db_admin).",
            root_username, server_id, username,
        )
    except Exception:
        pass
    return RevealCredentialsResponse(
        auth_token=auth_token,
        plex_home_pin=pins["plex_home_pin"],
        emby_easy_pin=pins["emby_easy_pin"],
        jellyfin_easy_pin=pins["jellyfin_easy_pin"],
    )


@router.delete("/{server_id}/{username}")
def hide_user_on_server(
    server_id: str,
    username: str,
    body: ManagedUserDeleteIn,
    user: Dict[str, Any] = Depends(require_role("operator")),
) -> Dict[str, Any]:
    """
    Hide this user on this server (per-server tombstone).
    Credentials are preserved across hide/unhide cycles. The row
    stays in the DB but is filtered from the default User Management
    list and the JobFormPanel picker. db_admin gated.

    Restoring is either ``PATCH ... {tombstoned: false}`` from the
    detail view's Unhide button, or letting a future sync run with
    no per-server tombstone in place (a fresh sync from the same
    server would re-upsert as ``tombstoned=0`` only if the end user
    has explicitly toggled it; the sync helper itself doesn't reset
    the flag).

    To hide a username everywhere rather than purge a row, use the
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


@router.post("/{server_id}/{username}/reset-auth-counter")
def reset_auth_counter(
    server_id: str,
    username: str,
    user: Dict[str, Any] = Depends(require_role("operator")),
) -> Dict[str, Any]:
    """Operator action to clear a user's auth-failure counter without
    un-tombstoning anyone.

    Used by the per-user "Reset counter" affordance in the User
    Mapping panel. Sets ``consecutive_auth_failures = 0`` and
    ``last_auth_status = 'unknown'`` so the sweeper / activity
    filter treats the user as a clean slate going forward. Does NOT
    clear an existing tombstone — operators use the unhide path for
    that, and only db_admin can.

    Operator-gated. Recording goes through db_access_log via the
    underlying media_db helper.
    """
    _ensure_server_exists(server_id)
    try:
        row = media_db.reset_managed_user_auth_signal(
            server_id=server_id, username=username,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=f"No managed user {username!r} on server {server_id!r}.",
        )
    return {
        "ok": True,
        "server_id": server_id,
        "username": username,
        "last_auth_status": row.get("last_auth_status"),
        "consecutive_auth_failures": row.get("consecutive_auth_failures"),
    }


@router.post("/{server_id}/sync")
def sync_users(
    server_id: str,
    user: Dict[str, Any] = Depends(require_role("operator")),
) -> Dict[str, Any]:
    """
    Pull the live user list from the registered server, upsert it
    into managed_users, then run a per-user token capture sweep so
    any users with a stored PIN / password also end up with a
    captured per-user auth_token.

    This endpoint and /api/servers/{id}/test (the Servers tab's
    Refresh button) converge on the same behaviour: every user whose
    PIN / password is stored ends up with a per-user auth_token after
    the sweep, so the operator can use either UI surface.

    Operator-gated (not db_admin) because:
      * Metadata writes never touch credential columns.
      * Token capture writes auth_token_enc, but only based on the
        PIN / password material already saved under User Management
        (which IS db_admin-gated). Capture only translates stored
        credentials into a session token; it never introduces new
        secrets.

    Additive-only on both halves: users present in the DB but
    absent from the live API are NOT auto-deleted; existing stored
    tokens are NOT overwritten by this sync.
    """
    _ensure_server_exists(server_id)
    result = media_db.sync_managed_users_from_live(server_id, log)
    # 502 if the live API was unreachable (sync helper swallows the
    # exception so the end user-facing message stays clean here).
    if result.get("error"):
        err_low = result["error"].lower()
        status = 404 if "no server" in err_low else 502
        raise HTTPException(status_code=status, detail=result["error"])

    # Parity with the Servers tab's Refresh button. ``force=True``
    # bypasses the per-server throttle because this is an explicit
    # operator-initiated action. ``only_if_missing=True`` preserves
    # the additive contract: already-captured tokens are never
    # overwritten.
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
            force=True,
            only_if_missing=True,
            logger=log,
        )
        if cap_summary.get("captured"):
            log.info(
                "Sync-users button: captured %d per-user token(s) "
                "for server %r (skipped %d already-stored)",
                cap_summary["captured"], server_id,
                cap_summary.get("skipped_existing", 0),
            )
        for err in (cap_summary.get("errors") or []):
            log.info("  sync-users capture error: %s", err)
    except Exception as exc:  # pragma: no cover (defensive)
        log.exception(
            "Sync-users button: token capture failed for server %r "
            "(metadata sync above completed; tokens unchanged).",
            server_id,
        )
        cap_summary["errors"].append(f"unexpected: {exc}")

    return {
        "server_id": server_id,
        "synced": int(result.get("synced") or 0),
        "source_error": result.get("source_error"),
        "token_capture": cap_summary,
    }


@router.post("/{server_id}/{username}/capture-token")
def capture_one_user_token(
    server_id: str,
    username: str,
    user: Dict[str, Any] = Depends(require_role("operator")),
) -> Dict[str, Any]:
    """Capture a per-user auth_token for a single user. Wired to the
    "Sync this user" button on the User Management user-detail panel.

    Scoped to ONE username via the ``capture_managed_user_tokens``
    helper's ``username_filter`` parameter. Other users on the same
    server are NOT touched.

    Operator-gated. ``force=True`` (the throttle is per-server, and
    this is an explicit operator action). ``only_if_missing=False``
    so the operator can rotate the user's stored token without
    needing to clear it first (matches the Servers tab's per-user
    Rotate-token button).

    A single click rotates exactly one row, in contrast to the bulk
    Sync which sweeps every user on the server.
    """
    _ensure_server_exists(server_id)
    if not username:
        raise HTTPException(status_code=400, detail="username is required")

    from server import user_capture
    cap_summary: Dict[str, Any] = {
        "captured": 0,
        "skipped_existing": 0,
        "throttled": False,
        "errors": [],
    }
    try:
        cap_summary = user_capture.capture_managed_user_tokens(
            server_id,
            force=True,
            only_if_missing=False,
            logger=log,
            username_filter={username},
        )
        if cap_summary.get("captured"):
            log.info(
                "Per-user sync: captured token for %r on server %r",
                username, server_id,
            )
        for err in (cap_summary.get("errors") or []):
            log.info("  per-user sync error: %s", err)
    except Exception as exc:  # pragma: no cover (defensive)
        log.exception(
            "Per-user sync: capture failed for (%r, %r).",
            server_id, username,
        )
        cap_summary["errors"].append(f"unexpected: {exc}")

    return {
        "server_id": server_id,
        "username": username,
        "token_capture": cap_summary,
    }
