"""Server-registry + library-walk routes.

Every ``/api/servers/...`` handler plus the legacy ``/api/libraries``
alias. Extracted from ``server.app`` as part of the Phase-3a
organizational split; behaviour is preserved verbatim from the prior
in-app.py definitions.

Routes carry mixed prefixes (``/api/servers``, ``/api/libraries``) so
the router itself takes NO prefix and each handler keeps the full
path it had before the split.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException

from server import (
    auth_router as _auth_router_module,
    persistence,
    server_registry,
)
from server.models import (
    PinMigrationApplyIn,
    ServerIn,
    TestUnsavedIn,
    SettingsIn,
    UserDisplayNameIn,
    UserCopyIn,
    UserIdentityMapIn,
)
from server.routers._deps import _auto_sync_managed_users
from services.auth import connect_to_server, discover_libraries


log = logging.getLogger("plexmigrate.server")

router = APIRouter(tags=["servers"])


# ── Library walk + prune missing items (Rule 2) ──────────────────


@router.get("/api/servers/{server_id}/library-walk")
def get_library_walk_status(server_id: str) -> Dict[str, Any]:
    """
    Most-recent library walk summary for one server plus a flag
    indicating whether a walk is currently running. Powers the
    "Last walked" column on the Servers panel and the "in
    progress…" state on the walk-now button.
    """
    from server import media_db, library_walk
    return {
        "running": library_walk.is_walk_running(server_id),
        "last": media_db.get_last_walk_summary(server_id),
        "recent": media_db.list_library_walks(server_id, limit=10),
    }


@router.post("/api/servers/{server_id}/library-walk")
def trigger_library_walk(
    server_id: str,
    _admin: Dict[str, Any] = Depends(
        _auth_router_module.require_role("admin")
    ),
) -> Dict[str, Any]:
    """
    Fire a library walk for one server. The walk runs in a
    background thread - this returns ``{walk_id, started}``
    immediately and the caller polls the GET endpoint to track
    progress, instead of holding a uvicorn worker for the whole
    (minutes-long) walk. Idempotent: a concurrent call returns the
    running walk's id with ``started=0``.
    """
    from server import server_registry, library_walk
    try:
        srv = server_registry.get_server_by_id(
            server_id, include_token=True,
        )
    except Exception:
        log.exception("get_server_by_id failed for %r", server_id)
        raise HTTPException(
            status_code=500,
            detail="Could not load the server record - see the server log.",
        )
    if srv is None:
        raise HTTPException(status_code=404, detail=f"No server with id {server_id!r}.")
    url = srv.get("url") or ""
    token = srv.get("token") or ""
    if not (url and token):
        raise HTTPException(
            status_code=400,
            detail="Server is missing a URL or auth token.",
        )
    return library_walk.start_walk_background(
        server_id=server_id, url=url, token=token,
    )


@router.get("/api/servers/{server_id}/prune-preview")
def prune_preview(
    server_id: str,
    older_than_days: float = 7.0,
) -> Dict[str, Any]:
    """
    Dry-run preview of the Prune Missing Items action. Returns
    ``{count, sample_items[], last_walk}`` so the UI can show
    "X items not seen in the last N days" plus a sample list,
    before the end user commits to the destructive call.

    The ``last_walk`` field surfaces the latest walk summary so
    the UI can render a freshness warning when no recent walk
    exists (rule-of-thumb: if last_walk is older than
    older_than_days, the prune preview is unreliable).
    """
    from server import media_db
    seconds = max(0.0, float(older_than_days) * 86400.0)
    count = media_db.count_stale_items(
        server_id=server_id, older_than_seconds=seconds,
    )
    # Cap the sample at 100 so the preview payload stays light;
    # the actual prune call is unbounded.
    sample = media_db.list_stale_items(
        server_id=server_id, older_than_seconds=seconds, limit=100,
    )
    return {
        "count": count,
        "sample_items": sample,
        "sample_truncated_at": 100,
        "last_walk": media_db.get_last_walk_summary(server_id),
        "older_than_days": older_than_days,
    }


@router.post("/api/servers/{server_id}/prune-stale-items")
def prune_stale_items(
    server_id: str,
    body: Dict[str, Any],
    _admin: Dict[str, Any] = Depends(_auth_router_module.require_role("admin")),
) -> Dict[str, Any]:
    """
    Execute the Prune Missing Items action. Two-factor gated -
    destructive across watch_events, ratings, server_items,
    playlist / collection memberships, and orphan items rows.
    Snapshot .db files are never touched (Rule 3).

    Requires an admin/root_admin JWT (the ``require_role``
    dependency) AND the separate db_admin credential in the body -
    a stolen login session alone cannot trigger a destructive DB
    op.

    Body: ``{db_admin_username, db_admin_password, older_than_days,
    dry_run}``. ``dry_run=true`` returns counters without writing
    (cheap path for a second-confirm step in the UI).
    """
    from server import auth_db, media_db
    _auth_router_module.verify_db_admin_from_body(body)
    try:
        older_than_days = float(body.get("older_than_days") or 7.0)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="older_than_days must be a number.")
    if older_than_days < 0:
        raise HTTPException(status_code=400, detail="older_than_days must be >= 0.")
    dry_run = bool(body.get("dry_run") or False)
    seconds = older_than_days * 86400.0
    try:
        counters = media_db.prune_stale_items(
            server_id=server_id,
            older_than_seconds=seconds,
            dry_run=dry_run,
        )
    except ValueError as e:
        # H2 walk-gate: no completed library walk for this server.
        raise HTTPException(status_code=409, detail=str(e))
    counters["dry_run"] = dry_run
    counters["older_than_days"] = older_than_days
    return counters


# ── Settings ─────────────────────────────────────────────────────


@router.get("/api/settings")
def get_settings() -> Dict[str, Any]:
    """
    Return the saved settings document with the Plex token redacted.
    The token itself is replaced by a boolean ``has_token``.
    """
    return persistence.redact_settings(persistence.load_settings())


@router.post("/api/settings")
def post_settings(
    body: SettingsIn,
    _user: Dict[str, Any] = Depends(
        _auth_router_module.require_permission("settings.edit")
    ),
) -> Dict[str, Any]:
    """
    Partial update: fields the client omits keep their prior value.
    Returns the redacted updated document.

    Gated on the ``settings.edit`` permission. A write that touches
    the ``tunables`` / ``tunables_per_server`` sub-documents needs
    the stricter ``settings.tunables`` permission as well: those
    infrastructure knobs (JWT TTLs, HTTP pool sizes, SQLite busy
    timeouts) can lock every user out if set wrong, so the general
    ``settings.edit`` grant is intentionally not enough for them.
    The two-tier check matches the permission split documented on
    ``ALL_PERMISSIONS`` in ``server/auth_router.py``.
    """
    patch = {k: v for k, v in body.model_dump().items() if v is not None}
    # ``audit_log_enabled`` is read-only via this endpoint - it
    # only flips through the db_admin-gated audit-log-toggle below.
    # Silently strip it from the patch so a stray PATCH from the
    # Settings UI can never disable the audit trail by accident.
    patch.pop("audit_log_enabled", None)
    # Tunables sub-documents need the stricter settings.tunables
    # permission. The general Settings page never sends a
    # ``tunables`` key (only the root-gated System Tunables page
    # does), so this conditional gate never trips a plain
    # settings.edit holder editing operational settings.
    if "tunables" in patch or "tunables_per_server" in patch:
        tunable_perms = _auth_router_module.effective_permissions_for(
            _user["username"], _user["effective_role"],
        )
        if "settings.tunables" not in tunable_perms:
            raise HTTPException(
                status_code=403,
                detail=(
                    "Editing system tunables requires the "
                    "'settings.tunables' permission."
                ),
            )
    # Refuse Windows host paths early so the end user gets an
    # actionable error in the Settings tab instead of a silent
    # write into the container's ephemeral filesystem.
    try:
        if "output_dir" in patch:
            persistence.validate_container_path(patch["output_dir"], "Output directory")
        if "log_dir" in patch:
            persistence.validate_container_path(patch["log_dir"], "Log directory")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    merged = persistence.save_settings(patch)
    return persistence.redact_settings(merged)


@router.post("/api/settings/audit-log-toggle")
def toggle_audit_log(
    body: Dict[str, Any],
    _admin: Dict[str, Any] = Depends(_auth_router_module.require_role("admin")),
) -> Dict[str, Any]:
    """
    Enable / disable the db-access audit log.

    Two-factor gated (matches the destructive-endpoint pattern):
    admin/root_admin JWT plus the separate db_admin credential in
    the request body. Body: ``{db_admin_username, db_admin_password,
    enabled: bool}``.

    Self-documenting transition: when disabling, the final audit
    line records who switched it off; when re-enabling, the first
    new line records who switched it back on. The audit trail
    always shows that disablement was an explicit, attributable
    act, which is what makes it safe to let the audit log itself
    be end user-toggleable.
    """
    from server import auth_db
    from services import db_access_log
    _auth_router_module.verify_db_admin_from_body(body)
    new_enabled = bool(body.get("enabled"))
    currently_enabled = db_access_log.is_enabled()
    if currently_enabled and not new_enabled:
        # Going OFF: write the final entry BEFORE flipping the flag
        # so the line lands. Then persist + cache.
        db_access_log.log_audit_disabled(_admin["username"])
        db_access_log.set_enabled(False)
        persistence.save_settings({"audit_log_enabled": False})
    elif (not currently_enabled) and new_enabled:
        # Going ON: flip first so the entry actually writes, then
        # log the resumption.
        db_access_log.set_enabled(True)
        db_access_log.log_audit_enabled(_admin["username"])
        persistence.save_settings({"audit_log_enabled": True})
    # Else: no-op transition (already in requested state).
    return {"audit_log_enabled": db_access_log.is_enabled()}


# ── Servers (multi-server registry, v0.9.0) ─────────────────────


@router.get("/api/servers")
def list_servers(
    _user: Dict[str, Any] = Depends(_auth_router_module.require_role("viewer")),
) -> List[Dict[str, Any]]:
    """
    Return every registered server with status fields and the
    cached library list (if any). Tokens are stripped before send.
    """
    return server_registry.list_servers(include_tokens=False)


@router.post("/api/servers")
def create_server(
    body: ServerIn,
    background_tasks: BackgroundTasks,
    _admin: Dict[str, Any] = Depends(
        _auth_router_module.require_role("admin")
    ),
) -> Dict[str, Any]:
    """
    Register a new server.

    v0.10.0 - :func:`server_registry.add_server` now probes the
    URL+token internally and refuses to persist a row if either
    (a) the probe fails (server unreachable, token rejected), or
    (b) the probed ``machine_identifier`` is already registered
    under a different friendly name. The same Plex account token
    works on every server that account owns, so the identifier is
    the only reliable signal that the end user typed the right
    URL for the server they meant.

    Managed-users sync (DB-warming for the User Management panel
    and the JobFormPanel picker) runs as a FastAPI BackgroundTask
    AFTER the response is sent, so registration of a Plex Home
    with N managed users stays snappy. The frontend's User
    Management panel auto-refreshes on focus / WS event so a
    user landing on a fresh server sees the synced roster within
    a few seconds without an explicit reload.
    """
    try:
        row = server_registry.add_server(
            body.name,
            body.url,
            body.token,
            logger=log,
            use_fallback_from_server_id=body.use_fallback_from_server_id,
            service_type=body.service_type,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    background_tasks.add_task(_auto_sync_managed_users, row["id"])
    # ``add_server`` already populated the probe results onto the row,
    # so a second test_connection call would just round-trip Plex
    # again - return what we have.
    latest = server_registry.get_server_by_id(row["id"], include_token=False)
    return latest or row


@router.post("/api/servers/test-unsaved")
def test_unsaved_server(
    body: TestUnsavedIn,
    _admin: Dict[str, Any] = Depends(
        _auth_router_module.require_role("admin")
    ),
) -> Dict[str, Any]:
    """
    Probe a URL+token combination without registering it.

    Returns:
      * ``ok``                - true if Plex accepted the token and
        we successfully enumerated libraries.
      * ``status`` / ``detail`` - same vocabulary the registry rows
        use (``ok`` / ``unreachable`` / ``auth_error``).
      * ``friendly_name``     - the server's own friendly name.
      * ``machine_identifier`` - Plex's stable install ID.
      * ``owner_name``        - Plex.tv username the token belongs to.
      * ``libraries``         - current catalogue with item counts.
      * ``response_ms``       - round-trip + library enumeration time.
      * ``name_mismatch``     - true when the end user's typed
        friendly name doesn't match what the server reports for
        itself. Surfaced so the UI can show a warning before save.
      * ``duplicate_of``      - friendly name of an existing
        registered server that shares this server's
        ``machine_identifier``. Null when there's no collision.
    """
    probe = server_registry.probe_unsaved(
        body.url, body.token, log, service_type=body.service_type,
    )
    # Mismatch detection: only meaningful when the probe actually
    # connected and surfaced a friendly name. We compare
    # case-insensitively because Plex sometimes title-cases
    # friendly names on its own.
    probed_friendly = (probe.get("friendly_name") or "").strip()
    typed = (body.name or "").strip()
    name_mismatch = bool(
        probe.get("ok") and probed_friendly and typed
        and probed_friendly.lower() != typed.lower()
    )
    # Duplicate-identifier preview.
    duplicate_of: Optional[str] = None
    machine_id = (probe.get("machine_identifier") or "").strip()
    if probe.get("ok") and machine_id:
        for row in server_registry.list_servers():
            if (row.get("machine_identifier") or "") == machine_id:
                duplicate_of = row.get("name") or ""
                break
    return {
        **probe,
        "name_mismatch": name_mismatch,
        "duplicate_of": duplicate_of,
    }


@router.get("/api/servers/{server_id}")
def get_one_server(server_id: str) -> Dict[str, Any]:
    row = server_registry.get_server_by_id(server_id, include_token=False)
    if row is None:
        raise HTTPException(status_code=404, detail=f"No server with id {server_id!r}")
    return row


@router.patch("/api/servers/{server_id}/auto-tombstone")
def patch_auto_tombstone(
    server_id: str,
    body: Dict[str, Any],
    _user: Dict[str, Any] = Depends(
        _auth_router_module.require_role("operator")
    ),
) -> Dict[str, Any]:
    """Toggle a server's auto-tombstone opt-in. Operator-gated.
    The sweeper still probes users and writes signals regardless
    of this toggle; it only governs whether N consecutive
    failures actually convert into a tombstone.
    """
    enabled = bool(body.get("enabled"))
    row = server_registry.update_server_settings(
        server_id,
        {"auto_tombstone_inactive_users_enabled": enabled},
    )
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=f"No server with id {server_id!r}.",
        )
    return row


@router.put("/api/servers/{server_id}")
def update_one_server(
    server_id: str,
    body: ServerIn,
    background_tasks: BackgroundTasks,
    _admin: Dict[str, Any] = Depends(
        _auth_router_module.require_role("admin")
    ),
) -> Dict[str, Any]:
    """
    Rename, change URL, or re-credential an existing server. An
    empty ``token`` means "leave the saved token unchanged" - the
    same write-only-token pattern Settings uses.

    Re-credentialling a server can flip its identity (new token +
    new owner = different managed-user set), so we also re-sync
    managed users after the probe completes. The sync runs as a
    BackgroundTask so the response returns immediately even on
    a Plex Home with many managed users.
    """
    try:
        row = server_registry.update_server(
            server_id,
            name=body.name,
            url=body.url,
            token=body.token if body.token else None,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    # Always probe after update so the status reflects the new creds.
    server_registry.test_connection(server_id, log)
    background_tasks.add_task(_auto_sync_managed_users, server_id)
    return server_registry.get_server_by_id(server_id, include_token=False) or row


@router.get("/api/servers/{server_id}/cascade-preview")
def preview_server_cascade(server_id: str) -> Dict[str, Any]:
    """
    Count what a cascading delete of this server would remove
    without actually deleting anything. Used by the Servers tab
    to populate the confirmation dialog with concrete numbers.
    """
    preview = server_registry.cascade_preview(server_id)
    if preview is None:
        raise HTTPException(status_code=404, detail=f"No server with id {server_id!r}")
    return preview


@router.delete("/api/servers/{server_id}")
def delete_one_server(
    server_id: str,
    _admin: Dict[str, Any] = Depends(
        _auth_router_module.require_role("admin")
    ),
) -> Dict[str, Any]:
    """
    Remove a registered server *and cascade-delete* everything
    attributable to it:

      * Schedules whose ``source_server_name`` matches.
      * ``snapshots/*_<slug>_<ts>.plexbackup.json`` files.
      * ``plex_logs/run_<slug>_*`` directories.

    Returns a summary dict with per-category counts and a list of
    per-file errors that the cascade encountered. Best-effort -
    a failure on one file does not stop the rest of the sweep.
    """
    summary = server_registry.remove_server(server_id)
    if summary is None:
        raise HTTPException(status_code=404, detail=f"No server with id {server_id!r}")
    return summary


@router.post("/api/servers/{server_id}/test")
def test_one_server(
    server_id: str,
    _admin: Dict[str, Any] = Depends(
        _auth_router_module.require_role("admin")
    ),
) -> Dict[str, Any]:
    """
    Re-probe a server's connection. The cached status, libraries,
    and last-contacted timestamp on the registry row are all
    refreshed. Used by the Refresh / Test button in the Servers tab.

    Re-test is the "reconnected" trigger, so we also re-sync
    managed users best-effort. Failure is logged but doesn't
    break the test response - status info is the primary
    contract of this endpoint.

    The Refresh button is an explicit end user action, so we
    bypass the per-server throttle on the token-capture sweep
    (``force_capture=True``)
    and the sweep is additive-only by default
    (``capture_only_if_missing=True``) so existing stored tokens
    are never overwritten. The capture summary is surfaced on the
    response under ``token_capture`` so the frontend can show a
    toast like "Captured N new user tokens".
    """
    try:
        out = server_registry.test_connection(server_id, log)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    cap_summary = _auto_sync_managed_users(
        server_id,
        force_capture=True,
        capture_only_if_missing=True,
    )
    if isinstance(out, dict):
        out["token_capture"] = cap_summary
    return out


@router.post("/api/servers/{server_id}/retry-pending-token")
def retry_pending_token(
    server_id: str,
    background_tasks: BackgroundTasks,
    _admin: Dict[str, Any] = Depends(_auth_router_module.require_role("admin")),
) -> Dict[str, Any]:
    """
    Re-probe the server's stashed pending token (the end user's
    original typed token that failed with 401 at Add Server time).
    On success: swap pending into active token, clear pending
    fields, return the new row. On failure: bump
    pending_token_last_probed_at and return the failure detail
    so the UI can grey the chip and surface a "last tried" hint.

    Admin-gated because rewriting the stored token requires the
    same authority as create/update. The swap replaces the active
    token (the old value is not preserved); end users who want a
    rollback path should test the pending token in a separate
    Add-form attempt before retrying.
    """
    try:
        out = server_registry.retry_pending_token(server_id, logger=log)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    # On a successful swap, also nudge the managed-users sync so
    # the user picker stays warm (mirrors what /test does). Runs as
    # a BackgroundTask so this endpoint stays snappy.
    if out.get("swapped"):
        background_tasks.add_task(
            _auto_sync_managed_users,
            server_id,
            force_capture=True,
            capture_only_if_missing=True,
        )
    return out


@router.get("/api/servers/{server_id}/pin-migration-suggestions")
def get_pin_migration_suggestions(
    server_id: str,
    _admin: Dict[str, Any] = Depends(_auth_router_module.require_role("admin")),
) -> Dict[str, Any]:
    """
    Cross-server PIN migration suggestions.

    Lists managed users on this server that don't have a stored
    Plex Home PIN, and for whom a managed user with the same
    Plex user ID (or username, when the
    ``pin_migration_allow_username_fallback`` tunable is on) on a
    different registered server DOES have a stored PIN.

    Read-only. Admin role is sufficient to view the list because
    no credential material is leaked - only usernames + which
    server each side belongs to. Applying a migration requires
    elevation; see ``apply_pin_migrations`` below.
    """
    try:
        from server import pin_migration
        from server.persistence import load_settings
        fallback = bool((load_settings() or {}).get(
            "pin_migration_allow_username_fallback", False,
        ))
        return pin_migration.compute_pin_migration_suggestions(
            server_id, allow_username_fallback=fallback,
        )
    except Exception as exc:  # pragma: no cover (defensive)
        log.exception("pin-migration-suggestions failed for %r", server_id)
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/api/servers/{server_id}/managed-users/{username}/rotate-token")
def rotate_one_managed_user_token(
    server_id: str,
    username: str,
    user: Dict[str, Any] = Depends(_auth_router_module.require_elevation()),
) -> Dict[str, Any]:
    """
    Per-user "Rotate token" button. Forces a token re-capture for
    ONE managed user on ``server_id``, bypassing the per-server
    throttle AND the additive-only contract that protects the
    Refresh sweep.

    Use this when a managed user's Plex token has rotated on the
    Plex side (they re-signed in, were re-invited, etc.) and the
    stored token is now stale. The Refresh button does NOT do
    this by default; the end user opts into per-user rotation
    here so accidental clicks can't burn the auth caches of a
    whole Plex Home in one go.

    Requires sudo-style elevation. Returns the same shape as the
    bulk capture (``captured``, ``skipped_existing``, ``throttled``,
    ``errors``) so the frontend can render a consistent toast.
    """
    from server import user_capture
    result = user_capture.capture_managed_user_tokens(
        server_id, force=True, only_if_missing=False, logger=log,
    )
    log.info(
        "Manual token rotation: server=%r user=%r actor=%r captured=%d errors=%d",
        server_id, username, user["username"],
        result.get("captured", 0),
        len(result.get("errors") or []),
    )
    return result


@router.post("/api/servers/{server_id}/pin-migration/apply")
def apply_pin_migration(
    server_id: str,
    body: PinMigrationApplyIn,
    user: Dict[str, Any] = Depends(_auth_router_module.require_elevation()),
) -> Dict[str, Any]:
    """
    Item 3 apply path. Each entry in ``body.suggestions`` carries
    the (target_username, source_server_id, source_username)
    triple from the suggestions list. Requires sudo-style
    elevation: the caller's session must have a fresh password
    re-confirm because PIN material is auth-equivalent.

    Returns ``{applied, skipped, errors}`` (see
    ``pin_migration.apply_pin_migrations`` for the exact shape).
    """
    from server import pin_migration
    try:
        confirmed = [s.model_dump() for s in body.suggestions]
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"bad body: {exc}")
    result = pin_migration.apply_pin_migrations(
        server_id, confirmed, actor=user["username"], logger=log,
    )
    log.info(
        "PIN migration apply: server=%r actor=%r applied=%d skipped=%d errors=%d",
        server_id, user["username"],
        len(result.get("applied") or []),
        len(result.get("skipped") or []),
        len(result.get("errors") or []),
    )
    return result


@router.post("/api/servers/{server_id}/ping")
def ping_one_server(server_id: str) -> Dict[str, Any]:
    """
    Lightweight reachability probe (v0.9.1). Issues a single GET
    ``/identity`` against the registered URL with the saved token
    and returns ``{ok, response_ms, status, detail}`` - without
    enumerating libraries. The frontend polls this every 30s for
    the live status dot in the Servers tab and the per-option
    chip in the JobForm source/destination selectors.

    Always returns a body (never raises HTTPException) so the
    client's poll loop has uniform shape across reachable and
    unreachable rows. A non-existent server_id surfaces as
    ``status: "unknown"`` with a detail message.
    """
    return server_registry.ping_server(server_id)


@router.get("/api/servers/{server_id}/libraries")
def list_server_libraries(
    server_id: str, refresh: bool = False,
    _user: Dict[str, Any] = Depends(_auth_router_module.require_role("viewer")),
) -> List[Dict[str, Any]]:
    """
    Return the library catalogue for one server.

    Defaults to the cached ``last_libraries`` row (instant). Pass
    ``?refresh=1`` to force a live re-probe. The Run Job form's
    DataToMigratePanel uses the cached path so opening the form
    doesn't trigger a 3-15s server handshake every time the
    operator changes the source-server selection.
    """
    try:
        if refresh:
            libs = server_registry.refresh_libraries(server_id, log)
        else:
            libs = server_registry.get_cached_libraries(server_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return libs


@router.get("/api/servers/{server_id}/users")
def list_server_users(
    server_id: str,
    _user: Dict[str, Any] = Depends(_auth_router_module.require_role("viewer")),
) -> Dict[str, Any]:
    """
    Return the user list for one server.

    Owner + every managed user surfaces here, each carrying the
    end user's chosen display name from the server's
    ``user_display_names`` map (or empty if none). 404 if the
    server is unregistered; 502 if Plex is unreachable. A
    successful response with a non-null ``error`` field means
    ``systemAccounts()`` failed but the owner row is still
    present - the UI renders "No managed users found".
    """
    try:
        return server_registry.get_server_users(server_id, log)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ConnectionError as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.patch("/api/servers/{server_id}/user-display-name")
def patch_user_display_name(
    server_id: str, body: UserDisplayNameIn,
    _user: Dict[str, Any] = Depends(
        _auth_router_module.require_role("operator")
    ),
) -> Dict[str, Any]:
    """
    Set or clear one entry in a server's ``user_display_names``
    map (v0.9.6 Feature 3). An empty ``display_name`` clears the
    mapping. Returns the updated server row (with token redacted).
    """
    try:
        updated = server_registry.set_user_display_name(
            server_id, body.plex_id, body.display_name,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if updated is None:
        raise HTTPException(status_code=404, detail=f"No server with id {server_id!r}")
    return updated


# ── Libraries (legacy single-server alias, deprecated) ────────────
# Kept for v0.8.x clients that still hit this endpoint. Resolves
# against the first registered server when present, or against
# legacy plex_url/plex_token in settings.json otherwise.


@router.get("/api/libraries")
def list_libraries_legacy() -> List[Dict[str, Any]]:
    rows = server_registry.list_servers(include_tokens=True)
    if rows:
        return server_registry.refresh_libraries(rows[0]["id"], log)
    settings = persistence.load_settings()
    url = settings.get("plex_url")
    token = settings.get("plex_token")
    if not url or not token:
        raise HTTPException(
            status_code=400,
            detail=(
                "No servers registered. Add one under the Servers tab "
                "before requesting libraries."
            ),
        )
    # connect_to_server with raise_on_failure=False (the default)
    # never raises - it returns None on any failure - so the None
    # check is the only failure branch needed.
    srv = connect_to_server(url, token, log)
    if srv is None:
        raise HTTPException(status_code=502, detail=f"Cannot reach Plex at {url}.")
    return discover_libraries(srv, log)
