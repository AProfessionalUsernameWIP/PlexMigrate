"""Catch-all routes that don't fit a cleaner per-prefix group.

Health probes, server-time, run-timings, recent-runs, network
request listing, identity-map management, ad-hoc cross-backend
user copy, and the developer-tools test runner. Behaviour preserved
verbatim from the prior in-app.py definitions.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException

import services.state as state

from server import (
    SERVER_API_VERSION,
    auth_router as _auth_router_module,
)
from server.models import (
    DevTestRunIn,
    UserCopyIn,
    UserIdentityMapIn,
)
from server.routers._deps import _auto_sync_managed_users  # noqa: F401  (kept for parity)


log = logging.getLogger("plexmigrate.server")

router = APIRouter(tags=["misc"])


# ── Health ───────────────────────────────────────────────────────


@router.get("/api/health")
def health() -> Dict[str, Any]:
    """
    Liveness probe. Docker compose health check hits this.

    ``app_version`` (engine VERSION) is included so the frontend
    Troubleshooting page can stamp bug reports with the right
    build. ``api_version`` is the wire-protocol version (separate
    from the engine release; bumps when REST/WS shapes change).
    ``debug_mode`` mirrors the PLEXMIGRATE_DEBUG_MODE env var; the
    frontend uses it to gate the Developer tab.
    """
    from server import debug_mode
    return {
        "ok": True,
        "api_version": SERVER_API_VERSION,
        "app_version": state.VERSION,
        "debug_mode": debug_mode.is_enabled(),
    }


@router.get("/api/server-time")
def server_time() -> Dict[str, Any]:
    """
    Report the backend's wallclock + timezone so the UI can label
    the schedule editor with the zone the hour/minute fields are
    interpreted in. Without this the user has no signal that a
    misconfigured container TZ (defaults to UTC) is offsetting
    their schedules.
    """
    import datetime as _dt
    import time as _time
    now_local = _dt.datetime.now().astimezone()
    # IANA name from $TZ when set (Docker path); fall back to the
    # abbreviation if the host has no TZ env (rare for containers).
    iana = os.environ.get("TZ") or ""
    abbrev = _time.tzname[_time.localtime().tm_isdst] if _time.tzname else ""
    return {
        "now": _time.time(),
        "tz": iana or abbrev,
        "tz_abbrev": abbrev,
        "iso": now_local.isoformat(timespec="seconds"),
    }


# ── Run timings (Feature 1 phase 1.5) ────────────────────────────
#
# The dashboard's "Runtime breakdown" panel reads these two
# endpoints. The first lists recent runs (one row per run_id, with
# wall-clock totals); the second drills into one run's individual
# timing entries grouped by scope.


@router.get("/api/run-timings/runs")
def list_runtime_runs(limit: int = 25) -> Dict[str, Any]:
    """
    Return summary rows for the most recent ``limit`` runs (default
    25). Each row carries ``run_id``, ``started_at``, ``ended_at``,
    ``duration_seconds``, ``entry_count``, and
    ``total_items_processed``.

    Clamped at 1-200 to bound the response size; 0 / negative
    values default to the spec value of 25.
    """
    from server import run_timings_db
    try:
        clamped = max(1, min(int(limit) or 25, 200))
    except (TypeError, ValueError):
        clamped = 25
    return {"runs": run_timings_db.list_recent_runs(limit=clamped)}


@router.get("/api/run-timings/runs/{run_id}")
def get_runtime_run_detail(run_id: str) -> Dict[str, Any]:
    """
    Return every timing entry for one run, ordered by ``started_at``
    ascending. Each entry includes the full ``extra`` annotation
    dict so the frontend can render context (strategy chosen,
    bulk_used flag, http_calls, etc.) inline next to durations.
    """
    from server import run_timings_db
    entries = run_timings_db.get_run_entries(run_id)
    if not entries:
        # Empty list is a valid response: the run may genuinely
        # have no entries, or the run_id was unknown. 404 would
        # force the frontend to handle two empty cases (existing-
        # but-empty vs missing); returning {entries: []} keeps the
        # contract uniform.
        return {"run_id": run_id, "entries": []}
    return {"run_id": run_id, "entries": entries}


# ── Developer tool: unit test runner (Feature 3 phase 3.3) ───────
#
# Two endpoints, both gated behind the debug_mode flag. Production
# deployments leave PLEXMIGRATE_DEBUG_MODE unset; calls to these
# endpoints return 403 in that case. Developer / dev-container
# deployments set the env var and the Developer tab in the
# frontend becomes visible.
#
# Concurrency: only one test run at a time. A second POST while a
# previous run is in-flight returns 409. The lock is module-level
# so it survives across requests but does NOT survive a process
# restart (intentional: a crashed run shouldn't deadlock the next
# one forever).

import threading as _threading_for_dev
_dev_run_lock = _threading_for_dev.Lock()


@router.post("/api/dev/run-tests")
def run_dev_tests(
    body: DevTestRunIn,
    _admin: Dict[str, Any] = Depends(
        _auth_router_module.require_role("root_admin")
    ),
) -> Dict[str, Any]:
    """
    Fire a unit-test run in the chosen mode and return the parsed
    summary. The .log + .summary.json artefacts are written to
    ``server_data/logs/test_runs/`` for the frontend's run-history
    list to consume.

    Debug-mode-gated. Live mode requires ``confirm_live=True``;
    the frontend collects the end user's explicit confirmation
    before submitting. The pytest filter is sanitised at the
    boundary; an invalid filter returns 400.

    A run in flight blocks concurrent submissions with 409
    Conflict so two end users cannot stomp on each other's log
    artefacts in the same second.
    """
    from server import debug_mode
    debug_mode.require_enabled()

    if body.mode == "live" and not body.confirm_live:
        raise HTTPException(
            status_code=400,
            detail=(
                "Live mode requires explicit confirm_live=true. "
                "The frontend should surface a typed confirmation "
                "before submitting."
            ),
        )

    from server import dev_test_harness
    if not _dev_run_lock.acquire(blocking=False):
        raise HTTPException(
            status_code=409,
            detail="A test run is already in flight; wait for it to finish.",
        )
    try:
        try:
            result = dev_test_harness.run_tests(
                mode=body.mode,
                pytest_filter=body.pytest_filter,
                operator=None,  # auth identity wiring TBD
            )
        except ValueError as exc:
            # Bad mode / bad filter / missing test dir.
            raise HTTPException(status_code=400, detail=str(exc))
    finally:
        _dev_run_lock.release()

    return result.to_summary_dict()


# ── Recent run history ──────────────────────────────────────────
#
# Higher-level than /api/run-timings/runs: one row per RUN with
# job_type, server, libraries, users_affected, duration, plus
# boolean flags for has_settings_log / has_restoration_log so the
# frontend can deep-link to those files. Drives the Servers >
# Recent Runtimes sub-tab.


@router.get("/api/network/recent-requests")
def list_recent_network_requests_endpoint(
    limit: int = 200,
    job_id: Optional[str] = None,
    host: Optional[str] = None,
    _user: Dict[str, Any] = Depends(_auth_router_module.require_role("viewer")),
) -> Dict[str, Any]:
    """Full per-request HTTP timeline (URL + method + status +
    duration + job_id + host) populated by the response hook in
    :mod:`services.auth`. Optional filters:

    * ``job_id`` — only entries tagged with this job (set on the
      worker via :func:`services.dashboard.job_http_context`)
    * ``host`` — only entries to one host/server
    * ``limit`` — clamped to [1, 5000]

    viewer+ role gate so URLs aren't readable anonymously."""
    from server import network_collector
    try:
        clamped = max(1, min(int(limit) or 200, 5000))
    except (TypeError, ValueError):
        clamped = 200
    rows = network_collector.list_recent_requests(
        job_id=job_id or None,
        host=host or None,
        limit=clamped,
    )
    return {"requests": rows, "count": len(rows)}


@router.get("/api/runs/recent")
def list_recent_run_history_endpoint(
    limit: int = 200,
    server_id: Optional[str] = None,
    library: Optional[str] = None,
    _user: Dict[str, Any] = Depends(_auth_router_module.require_role("viewer")),
) -> Dict[str, Any]:
    """
    Return the most recent ``limit`` run-history rows.

    Optional filters:
    * ``server_id`` - narrow to one registered server's runs.
    * ``library`` - narrow to runs that touched a specific library.

    Limit is clamped to 1-500 to bound the response size.
    viewer+ role gate because the rows surface server names and
    library lists; an anonymous caller has no business reading them.
    """
    from server import run_timings_db
    try:
        clamped = max(1, min(int(limit) or 200, 500))
    except (TypeError, ValueError):
        clamped = 200
    rows = run_timings_db.list_recent_run_history(
        limit=clamped,
        server_id=server_id or None,
        library=library or None,
    )
    return {"runs": rows}


@router.get("/api/runs/recent/{run_id}")
def get_recent_run_detail(
    run_id: str,
    _user: Dict[str, Any] = Depends(_auth_router_module.require_role("viewer")),
) -> Dict[str, Any]:
    """Return one run-history row by ``run_id``. 404 when absent."""
    from server import run_timings_db
    row = run_timings_db.get_run_history(run_id)
    if row is None:
        raise HTTPException(status_code=404, detail="run_id not found")
    return row


# ── Ad-hoc cross-backend user copy ───────────────────────────────


@router.post("/api/users/copy_to_destination")
def copy_user_to_destination(
    body: UserCopyIn,
    _user: Dict[str, Any] = Depends(_auth_router_module.require_role("admin")),
) -> Dict[str, Any]:
    """
    Create a single user on a Jellyfin / Emby destination via
    the same ``services/user_creation.create_users_for_job``
    path the Run Job D-OWNER modal uses, but as a synchronous
    one-off (no queued job, no run log entry).

    Used by the User Management panel's per-user "Copy user to
    destination" inline expansion. The follow-up data transfer
    is the end user's choice and lands as a separate
    ``/api/job/direct`` request orchestrated by the frontend.

    Failure modes:
      * target_server_id resolves to a Plex backend -> 400
        (Plex cannot create users via API).
      * source or target server id unknown -> 404.
      * adapter.create_user raises / returns None -> 500 with a
        ``UserCreationError`` detail; same two-phase rollback as
        the batch path (single-spec rollback is a no-op but the
        error shape is consistent).

    Returns ``{backend_user_id, target_username,
    source_user_handle}`` on success.
    """
    from server.server_registry import (
        connect_registered_server, get_server_by_id,
    )
    from server.models import UserCreateSpec
    from services.user_management.creation import (
        UserCreationError, create_users_for_job,
    )

    src_row = get_server_by_id(body.source_server_id, include_token=False)
    if src_row is None:
        raise HTTPException(
            status_code=404,
            detail=f"source server {body.source_server_id!r} not found",
        )
    dst_row = get_server_by_id(body.target_server_id, include_token=False)
    if dst_row is None:
        raise HTTPException(
            status_code=404,
            detail=f"target server {body.target_server_id!r} not found",
        )

    dst_service = (dst_row.get("service_type") or "plex").lower()
    if dst_service == "plex":
        raise HTTPException(
            status_code=400,
            detail=(
                "Plex destinations cannot create users via API; pick "
                "a Jellyfin or Emby server as the target."
            ),
        )

    # Connect to the target so we have a live adapter.
    try:
        dst_connection = connect_registered_server(
            str(body.target_server_id), log,
        )
    except Exception as exc:
        log.exception("copy_user_to_destination: connect failed: %s", exc)
        raise HTTPException(
            status_code=502,
            detail=f"could not connect to target server: {exc}",
        )

    spec = UserCreateSpec(
        source_user_handle=body.source_user_handle,
        target_username=body.target_username,
        temp_password=body.temp_password,
        target_user_policy=body.target_user_policy,
    )

    try:
        results = create_users_for_job(
            adapter=dst_connection.adapter,
            specs=[spec],
            dest_server_id=str(body.target_server_id),
            logger=log,
        )
    except UserCreationError as exc:
        log.error(
            "copy_user_to_destination: user_creation failed: %s",
            exc,
        )
        raise HTTPException(
            status_code=500,
            detail=f"user creation failed: {exc}",
        )

    if not results:
        raise HTTPException(
            status_code=500,
            detail="user creation returned no results",
        )
    r = results[0]
    if r.status != "created":
        raise HTTPException(
            status_code=500,
            detail=f"user creation status was {r.status!r}: {r.error}",
        )

    # Auto-write the identity mapping. Best-effort: failure here
    # does not undo the user creation (the user does exist on
    # destination; the end user can manually add the mapping
    # later via the standalone mapping panel).
    try:
        from server.media_db import add_identity_map
        add_identity_map(
            server_a_id=body.source_server_id,
            user_a_handle=body.source_user_handle,
            server_b_id=body.target_server_id,
            user_b_handle=r.target_username,
            source="auto_copy",
        )
    except Exception:
        log.exception(
            "copy_user_to_destination: auto-mapping write failed "
            "(user created successfully; mapping skipped)"
        )

    return {
        "backend_user_id": r.backend_user_id,
        "target_username": r.target_username,
        "source_user_handle": r.source_user_handle,
    }


# ── User identity mapping (cross-server) ─────────────────────────


@router.post("/api/users/identity_map")
def add_user_identity_map(
    body: UserIdentityMapIn,
    _user: Dict[str, Any] = Depends(_auth_router_module.require_role("admin")),
) -> Dict[str, Any]:
    """Add one (server_a, handle_a) <-> (server_b, handle_b)
    link. Duplicate pairs return ``{id: null, duplicate: true}``;
    new pairs return the inserted row id."""
    from server.media_db import add_identity_map
    try:
        new_id = add_identity_map(
            server_a_id=body.server_a_id,
            user_a_handle=body.user_a_handle,
            server_b_id=body.server_b_id,
            user_b_handle=body.user_b_handle,
            source=body.source,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"id": new_id, "duplicate": new_id is None}


@router.get("/api/users/identity_map")
def list_user_identity_maps(
    _user: Dict[str, Any] = Depends(_auth_router_module.require_role("viewer")),
) -> Dict[str, Any]:
    """Return every identity-map row in insertion order. Viewer+
    gated because the mapping is operationally interesting but
    carries no credentials."""
    from server.media_db import list_identity_maps
    return {"maps": list_identity_maps()}


@router.delete("/api/users/identity_map/{map_id}")
def remove_user_identity_map(
    map_id: int,
    _user: Dict[str, Any] = Depends(_auth_router_module.require_role("admin")),
) -> Dict[str, Any]:
    """Delete one identity-map row by id. Returns 404 when the
    row did not exist."""
    from server.media_db import delete_identity_map
    ok = delete_identity_map(map_id)
    if not ok:
        raise HTTPException(status_code=404, detail="identity-map row not found")
    return {"removed": True, "id": map_id}


@router.post("/api/users/identity_map/rerun-auto-link")
def rerun_auto_link_identity_map(
    _user: Dict[str, Any] = Depends(_auth_router_module.require_role("admin")),
) -> Dict[str, Any]:
    """USER-MGMT-IDENTITY-AUDIT follow-on: explicit end user
    trigger for the backend_user_id auto-link helper.

    The helper runs automatically after every managed-users sync
    (services/media_db.sync_managed_users_from_live wires it in),
    so the normal operational path already covers it. This
    endpoint lets an end user force a rerun without waiting for
    the next sync - useful right after manually adding a new
    identity_map row, or to confirm cross-server detection picked
    up a just-registered server.

    Returns the helper's summary dict
    ``{pairs_written, pairs_skipped_duplicate, groups_seen}``.
    Best-effort: helper failures bubble up as 500 with the
    exception text so the end user can see what went wrong."""
    from server.media_db import auto_link_identity_map_by_backend_user_id
    try:
        summary = auto_link_identity_map_by_backend_user_id()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return summary


@router.get("/api/dev/test-runs")
def list_dev_test_runs(
    limit: int = 25,
    _admin: Dict[str, Any] = Depends(
        _auth_router_module.require_role("root_admin")
    ),
) -> Dict[str, Any]:
    """
    Return parsed summary JSON for the most recent ``limit`` dev
    tool runs, newest first by file mtime. The frontend's run-
    history list reads this and renders the per-run status chip +
    a deep link into the raw .log.
    """
    from server import debug_mode
    debug_mode.require_enabled()
    from server import dev_test_harness
    try:
        clamped = max(1, min(int(limit) or 25, 200))
    except (TypeError, ValueError):
        clamped = 25
    return {"runs": dev_test_harness.list_recent_runs(limit=clamped)}
