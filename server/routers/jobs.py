"""Job-control, schedules, and cross-platform-preflight routes.

Houses every ``/api/job/*``, ``/api/jobs/*`` and ``/api/schedules/*``
handler plus the closely-related preflight helpers. Behaviour is
preserved verbatim from the prior in-app.py definitions.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException

from server import (
    auth_router as _auth_router_module,
    persistence,
)
from server.jobs import get_queue
from server.models import (
    DirectTransferIn,
    JobStatusOut,
    PinPreflightIn,
    SnapshotJobIn,
    RestoreJobIn,
    RestoreFromSnapshotIn,
    ScheduleIn,
    ScheduleResolutionsPatchIn,
    CrossPlatformPreflightJobIn,
    PreflightResponse,
    CrossPlatformPreflightReport,
    UserResolutionOut,
    UserRowCountsOut,
    DestUserOptionOut,
    LibraryTypeNoteOut,
    TombstoneNoteOut,
    ZeroRowSkipOut,
    InlineCreateUserIn,
    InlineCreateUserResponse,
)
from server.routers._deps import _apply_preflight_ack
from server.schedules import ensure_next_run_at, list_schedules
from server.ws import build_dashboard_frame


log = logging.getLogger("plexmigrate.server")

router = APIRouter(tags=["jobs"])


# ── Job control ──────────────────────────────────────────────────


@router.get("/api/job", response_model=JobStatusOut)
def get_job() -> JobStatusOut:
    """
    One-shot snapshot of the current (or most recent) job. The
    WebSocket carries the same data live; this endpoint exists so
    a fresh page load can paint immediately before its socket
    opens.
    """
    snap = build_dashboard_frame()
    job = snap.get("job")
    if job is None:
        return JobStatusOut(state="idle")
    return JobStatusOut(
        state=job["state"],
        job_id=job.get("job_id"),
        mode=job.get("mode"),
        started_at=job.get("started_at"),
        finished_at=job.get("finished_at"),
        error=job.get("error"),
        dashboard=snap.get("dashboard"),
    )


@router.get("/api/job/history")
def get_job_history(
    _user: Dict[str, Any] = Depends(_auth_router_module.require_role("viewer")),
) -> List[Dict[str, Any]]:
    """
    Return the in-memory job history (newest first). Cleared on
    server restart by design - this is a live operations view,
    not an audit log. Per-run on-disk artefacts under
    ``plex_logs/`` are the durable record.
    """
    recs = list(reversed(get_queue().history()))
    out: List[Dict[str, Any]] = []
    for r in recs:
        out.append({
            "job_id": r.job_id,
            "mode": r.mode,
            "state": r.state,
            "queued_at": r.queued_at,
            "started_at": r.started_at,
            "finished_at": r.finished_at,
            "error": r.error,
            "run_log_dir": r.run_log_dir,
            "params": {k: v for k, v in r.params.items() if k != "plex_token"},
        })
    return out


@router.post("/api/job/preflight-pin-check")
def post_job_preflight_pin_check(body: PinPreflightIn) -> Dict[str, Any]:
    """
    Preflight check. Returns the list of managed users in the
    about-to-submit job's scope who have neither a stored auth
    token nor a stored Plex Home PIN in media.db. The frontend
    renders a warning modal when the response contains at-risk
    users and submits with ``pin_preflight_acknowledged=true`` if
    the end user clicks Continue anyway.

    Read-only: no side effects, no mutation of any store. Any
    logged-in end user can call it.
    """
    from server import preflight
    return preflight.compute_pin_preflight(
        mode=body.mode,
        source_server_name=body.source_server_name,
        dest_server_names=body.dest_server_names,
        user_filter=body.user_filter,
    )


@router.post("/api/job/snapshot")
def post_job_export(
    body: SnapshotJobIn,
    _user: Dict[str, Any] = Depends(
        _auth_router_module.require_role("operator")
    ),
) -> Dict[str, Any]:
    """
    Enqueue an snapshot job. Returns the JobRecord immediately;
    progress is reported live via the WebSocket.
    """
    params = body.model_dump(exclude_none=True)
    # Reject Windows host paths early so a misconfigured ad-hoc
    # snapshot per-run output_dir doesn't silently land inside
    # the container's ephemeral filesystem.
    try:
        if "output_dir" in params:
            persistence.validate_container_path(params["output_dir"], "Output directory")
        if "log_dir" in params:
            persistence.validate_container_path(params["log_dir"], "Log directory")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    # Tag the run as user-initiated so the snapshot JSON (and the
    # Snapshots tab in the GUI) can distinguish it from scheduler
    # fires.
    params["_trigger"] = "manual"
    _apply_preflight_ack(params)
    rec = get_queue().submit_snapshot(params)
    return {"job_id": rec.job_id, "state": rec.state, "mode": rec.mode}


@router.post("/api/job/restore")
def post_job_restore(
    body: RestoreJobIn,
    _user: Dict[str, Any] = Depends(
        _auth_router_module.require_role("operator")
    ),
) -> Dict[str, Any]:
    """
    Enqueue a restore job. Mirrors :func:`post_job_snapshot`.
    """
    params = body.model_dump(exclude_none=True)
    # Containment-check every user-supplied path - each import
    # file and the per-run log dir - so a restore job can't be
    # aimed at server_data/ or escape via '..'.
    try:
        for _f in params.get("input_files") or []:
            persistence.validate_container_path(_f, "Import file")
        if "log_dir" in params:
            persistence.validate_container_path(params["log_dir"], "Log directory")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    _apply_preflight_ack(params)
    rec = get_queue().submit_restore(params)
    return {"job_id": rec.job_id, "state": rec.state, "mode": rec.mode}


@router.post("/api/job/restore-from-snapshot")
def post_job_restore_from_snapshot(
    body: RestoreFromSnapshotIn,
    _user: Dict[str, Any] = Depends(
        _auth_router_module.require_role("operator")
    ),
) -> Dict[str, Any]:
    """
    Enqueue a restore that pulls from a registered snapshot in
    ``snapshots.db`` rather than from an on-disk ``.plexexport.json``.

    Implementation: materialise (or reuse cached) JSON sidecar for
    the snapshot, then forward into the standard restore queue with
    the sidecar path filled into ``input_files``. The engine still
    consumes JSON; we just resolve the path here so the end user
    never has to type one in.
    """
    from server import snapshot_registry
    row = snapshot_registry.get(body.snapshot_id)
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=f"No snapshot with id {body.snapshot_id!r}.",
        )

    # Feature 2: pre-restore snapshot validation. Gated by
    # settings.validate_snapshot_before_restore (default OFF per
    # D4/D5). Runs the validator against a temp copy of the
    # snapshot.db; errors abort the restore submission with
    # 422 Unprocessable Entity so the end user sees the specific
    # invariant violation rather than a generic engine failure
    # mid-run.
    from services import snapshot_validator
    if snapshot_validator.is_before_restore_enabled():
        db_file = row.get("file_path")
        if db_file:
            report = snapshot_validator.validate_snapshot(Path(db_file))
            if not report.ok:
                err_lines = [
                    f"[{i.code}] {i.message}" for i in report.errors
                ]
                raise HTTPException(
                    status_code=422,
                    detail=(
                        "Pre-restore snapshot validation failed. "
                        "Disable validate_snapshot_before_restore in "
                        "Settings to bypass, or re-capture the "
                        "snapshot. Errors: "
                        + "; ".join(err_lines)
                    ),
                )
            # Warnings don't abort; surface in the log so the
            # end user can investigate after the restore lands.
            for issue in report.warnings:
                logging.getLogger("plexmigrate.server.app").warning(
                    "pre-restore validation warning [%s]: %s",
                    issue.code, issue.message,
                )

    sidecar = snapshot_registry.materialise_sidecar(body.snapshot_id)
    if not sidecar:
        raise HTTPException(
            status_code=500,
            detail=(
                "Could not render snapshot to JSON - the underlying "
                ".db is missing or the render failed. Try downloading "
                "the snapshot from the Backups tab to confirm."
            ),
        )
    params = body.model_dump(exclude_none=True)
    params.pop("snapshot_id", None)
    params["input_files"] = [sidecar]
    params["_imported_from_snapshot_id"] = body.snapshot_id
    _apply_preflight_ack(params)
    rec = get_queue().submit_restore(params)
    return {"job_id": rec.job_id, "state": rec.state, "mode": rec.mode}


@router.post("/api/job/direct")
def post_job_direct(
    body: DirectTransferIn,
    _user: Dict[str, Any] = Depends(
        _auth_router_module.require_role("operator")
    ),
) -> Dict[str, Any]:
    """
    Enqueue a direct server-to-server transfer job. Both
    ``source_server_name`` and ``dest_server_name`` are required
    and must resolve to different registered servers.
    """
    params = body.model_dump(exclude_none=True)
    _apply_preflight_ack(params)
    rec = get_queue().submit_direct(params)
    return {"job_id": rec.job_id, "state": rec.state, "mode": rec.mode}


# ── Cross-platform preflight ─────────────────────────────────────
#
# Four routes wire the preflight UX into the engine's dry-run
# resolver. The modal calls preflight before submit; the user
# creates missing destination users inline; schedules persist the
# end user's decisions so fires reuse them deterministically.


def _run_preflight_for_dest(
    payload: Dict[str, Any], dest_name: str,
    body: CrossPlatformPreflightJobIn,
) -> CrossPlatformPreflightReport:
    """Build the destination adapter, call dry_run_resolve_users,
    marshal the dataclass into the Pydantic wire shape."""
    from server import server_registry
    from services.restorer_adapter import dry_run_resolve_users
    try:
        connection = server_registry.connect_registered_server(
            dest_name, log,
        )
    except ValueError:
        raise HTTPException(
            status_code=404,
            detail=f"Destination server {dest_name!r} is not registered.",
        )
    except ConnectionError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Could not connect to destination {dest_name!r}: {exc}",
        )
    report_dc = dry_run_resolve_users(
        payload,
        connection.adapter,
        dest_server_id=str(connection.row.get("id") or ""),
        include_watch_history=body.include_watch_history,
        include_ratings=body.include_ratings,
        include_playlists=body.include_playlists,
        include_collections=body.include_collections,
        include_managed_users=body.include_managed_users,
        user_filter=body.user_filter,
        logger=log,
    )
    return CrossPlatformPreflightReport(
        source_kind=report_dc.source_kind,
        dest_kind=report_dc.dest_kind,
        source_server_id=report_dc.source_server_id,
        dest_server_id=report_dc.dest_server_id,
        is_cross_platform=report_dc.is_cross_platform,
        source_admin_count=report_dc.source_admin_count,
        dest_admin_count=report_dc.dest_admin_count,
        resolutions=[
            UserResolutionOut(
                source_username=r.source_username,
                source_role=r.source_role if r.source_role in ("owner", "admin", "managed") else "managed",
                source_row_counts=UserRowCountsOut(**{
                    "watch_history": r.source_row_counts.watch_history,
                    "ratings": r.source_row_counts.ratings,
                    "playlists": r.source_row_counts.playlists,
                    "collections": r.source_row_counts.collections,
                }),
                proposed_resolution=r.proposed_resolution,
                proposed_dest_user_id=r.proposed_dest_user_id,
                proposed_dest_username=r.proposed_dest_username,
                proposed_dest_role=r.proposed_dest_role,
                needs_ack=r.needs_ack,
                blocks_submit=r.blocks_submit,
                warnings=list(r.warnings),
                available_dest_users=[
                    DestUserOptionOut(
                        backend_user_id=opt.backend_user_id,
                        username=opt.username,
                        role=opt.role,
                        is_tombstoned=opt.is_tombstoned,
                    )
                    for opt in r.available_dest_users
                ],
            )
            for r in report_dc.resolutions
        ],
        smart_playlists_skipped=report_dc.smart_playlists_skipped,
        smart_playlist_names=list(report_dc.smart_playlist_names),
        library_type_notes=[
            LibraryTypeNoteOut(
                source_library=n.source_library,
                source_type=n.source_type,
                dest_type_used=n.dest_type_used,
                message=n.message,
            )
            for n in report_dc.library_type_notes
        ],
        tombstoned_users_excluded=[
            TombstoneNoteOut(
                dest_username=t.dest_username,
                reason=t.reason,
            )
            for t in report_dc.tombstoned_users_excluded
        ],
        zero_row_skipped=[
            ZeroRowSkipOut(
                source_username=z.source_username,
                empty_signals=list(z.empty_signals),
                filter_flags_in_effect=list(z.filter_flags_in_effect),
                message=z.message,
            )
            for z in report_dc.zero_row_skipped
        ],
        overall_verdict=report_dc.overall_verdict,
        blocking_reasons=list(report_dc.blocking_reasons),
    )


def _load_preflight_payload(
    body: CrossPlatformPreflightJobIn,
) -> Dict[str, Any]:
    """Resolve the preflight body to a single snapshot payload dict.

    snapshot_id: read from the snapshot registry + materialise via
    the existing sidecar mechanism. input_files: load the first
    file (multi-file restores all share the same per-server shape
    for preflight purposes - users are aggregated across libraries
    in a single file anyway). Either path raises HTTPException."""
    from server.jobs import _load_snapshot_payload as _load
    if body.snapshot_id:
        from server import snapshot_registry
        row = snapshot_registry.get(body.snapshot_id)
        if row is None:
            raise HTTPException(
                status_code=404,
                detail=f"No snapshot with id {body.snapshot_id!r}.",
            )
        db_file = row.get("file_path")
        if not db_file:
            raise HTTPException(
                status_code=422,
                detail=f"Snapshot {body.snapshot_id!r} has no file_path.",
            )
        # Forward-compat wrap: post-strip _load_snapshot_payload's
        # .json branch raises ValueError instead of returning a
        # wrapped dict. Surface as a clean 415 to the end user.
        try:
            payload = _load(str(db_file), log)
        except ValueError as _exc:
            raise HTTPException(
                status_code=415,
                detail=(
                    f"Snapshot {body.snapshot_id!r} at {db_file!r} "
                    f"uses an unsupported file shape: {_exc}"
                ),
            )
        if payload is None:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Could not load payload from snapshot "
                    f"{body.snapshot_id!r} at {db_file!r}."
                ),
            )
        return payload
    # File-based path: load the first input_file. For multi-file
    # restores the preflight focuses on the first file's user
    # roster; the engine still iterates all files at submit time.
    first = (body.input_files or [None])[0]
    if not first:
        raise HTTPException(
            status_code=400,
            detail="input_files is empty.",
        )
    # Resolve via the same three-attempt search the engine does.
    from pathlib import Path as _Path
    output_dir = "./snapshots"
    candidates = [
        _Path(first),
        _Path(output_dir) / first,
        _Path(output_dir) / "legacy" / first,
    ]
    for cand in candidates:
        if cand.is_file():
            # Same forward-compat wrap as the snapshot_id path.
            try:
                payload = _load(str(cand), log)
            except ValueError as _exc:
                raise HTTPException(
                    status_code=415,
                    detail=(
                        f"Input file {first!r} uses an unsupported "
                        f"file shape: {_exc}"
                    ),
                )
            if payload is not None:
                return payload
    raise HTTPException(
        status_code=404,
        detail=(
            f"Input file {first!r} not found (tried {candidates})."
        ),
    )


@router.post(
    "/api/jobs/cross-platform-preflight",
    response_model=PreflightResponse,
)
def post_jobs_cross_platform_preflight(
    body: CrossPlatformPreflightJobIn,
) -> PreflightResponse:
    """Compute per-destination cross-platform preflight reports
    for the submit body. Returns a PreflightResponse wrapping one
    report per destination_server_id. Read-only: no side effects,
    no engine writes."""
    payload = _load_preflight_payload(body)
    dest_names = body.resolved_destinations()
    reports: Dict[str, CrossPlatformPreflightReport] = {}
    aggregate = "ok"
    rank = {"ok": 0, "ack_required": 1, "blocked": 2}
    for dest_name in dest_names:
        report = _run_preflight_for_dest(payload, dest_name, body)
        reports[report.dest_server_id or dest_name] = report
        if rank[report.overall_verdict] > rank[aggregate]:
            aggregate = report.overall_verdict
    return PreflightResponse(
        reports=reports,
        aggregate_verdict=aggregate,
    )


@router.post(
    "/api/schedules/cross-platform-preflight",
    response_model=PreflightResponse,
)
def post_schedules_cross_platform_preflight(
    body: ScheduleIn,
) -> PreflightResponse:
    """Schedule-time preflight. Same shape as the jobs endpoint
    but takes a ScheduleIn body. Resolves the schedule's
    snapshot / input files + destinations and returns one report
    per destination_server_id."""
    # Map the schedule shape onto the preflight job body so the
    # implementation stays the same. Field names mostly overlap.
    body_dict = body.model_dump(exclude_none=True)
    preflight_body = CrossPlatformPreflightJobIn(
        snapshot_id=body_dict.get("snapshot_id"),
        input_files=body_dict.get("input_files") or [],
        dest_server_name=body_dict.get("dest_server_name"),
        dest_server_names=body_dict.get("dest_server_names"),
        include_watch_history=bool(body_dict.get("include_watch_history", True)),
        include_ratings=bool(body_dict.get("include_ratings", True)),
        include_playlists=bool(body_dict.get("include_playlists", True)),
        include_collections=bool(body_dict.get("include_collections", True)),
        include_managed_users=bool(body_dict.get("include_managed_users", True)),
        user_filter=body_dict.get("user_filter"),
    )
    return post_jobs_cross_platform_preflight(preflight_body)


@router.post(
    "/api/jobs/inline-create-user",
    response_model=InlineCreateUserResponse,
)
def post_jobs_inline_create_user(
    body: InlineCreateUserIn,
) -> InlineCreateUserResponse:
    """Create a destination user inline from the preflight modal.

    Idempotent on (destination_server_id, username) collision:
    returns the existing user with ``was_newly_created=false``.
    Admin creation requires ``acknowledgement=true`` (validated
    on the input model).

    Plex destinations are blocked at the picker but enforced here
    too: returns 400 with a pointer to the Plex Home invite flow."""
    from server import server_registry
    from services.adapters import UserPolicy
    from services.restorer_adapter import _normalise_dest_role
    try:
        connection = server_registry.connect_registered_server(
            body.destination_server_id, log,
        )
    except ValueError:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Destination server {body.destination_server_id!r} "
                f"is not registered."
            ),
        )
    except ConnectionError as exc:
        raise HTTPException(
            status_code=502,
            detail=(
                f"Could not connect to destination "
                f"{body.destination_server_id!r}: {exc}"
            ),
        )
    if (connection.service_type or "").lower() == "plex":
        raise HTTPException(
            status_code=400,
            detail=(
                "Plex destinations require the Plex Home invite "
                "flow for user creation. Open plex.tv > Manage "
                "Library Access and invite the user, then re-run "
                "the preflight."
            ),
        )
    # Idempotency check: look up by username in the destination's
    # current roster.
    try:
        existing = connection.adapter.list_users() or []
    except Exception:
        existing = []
    normalised = body.username.strip().lower()
    for u in existing:
        if (u.username or "").strip().lower() == normalised:
            return InlineCreateUserResponse(
                user=DestUserOptionOut(
                    backend_user_id=u.backend_user_id or "",
                    username=u.username,
                    role=_normalise_dest_role(u, connection.service_type),
                    is_tombstoned=False,
                ),
                was_newly_created=False,
            )
    # Create via the adapter. UserPolicy maps the end user's
    # role pick + acknowledgement into the backend's user-policy
    # shape; adapters that don't expose create_user surface as 501.
    policy = UserPolicy(is_administrator=(body.role == "admin"))
    try:
        spec = connection.adapter.create_user(
            username=body.username.strip(),
            password=body.initial_password or "",
            is_admin=(body.role == "admin"),
            policy=policy,
        )
    except NotImplementedError:
        raise HTTPException(
            status_code=501,
            detail=(
                f"Destination backend {connection.service_type!r} "
                f"does not support inline user creation."
            ),
        )
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=(
                f"Destination user creation failed: {exc}. Check "
                f"the destination's user-creation rules (allowed "
                f"characters, password policy) and try again."
            ),
        )
    if spec is None:
        raise HTTPException(
            status_code=502,
            detail="Destination user creation returned no UserSpec.",
        )
    return InlineCreateUserResponse(
        user=DestUserOptionOut(
            backend_user_id=spec.backend_user_id or "",
            username=spec.username,
            role=_normalise_dest_role(spec, connection.service_type),
            is_tombstoned=False,
        ),
        was_newly_created=True,
    )


@router.patch("/api/schedules/{schedule_id}/resolutions")
def patch_schedule_resolutions(
    schedule_id: str, body: ScheduleResolutionsPatchIn,
) -> Dict[str, Any]:
    """Update one schedule's stored cross-platform resolutions
    without touching any other schedule field. End user uses this
    from the schedule-row Resolution Editor surface.

    Returns the updated schedule row. 404 if the schedule
    doesn't exist."""
    existing = next(
        (s for s in persistence.load_schedules() if s.get("id") == schedule_id),
        None,
    )
    if existing is None:
        raise HTTPException(
            status_code=404,
            detail=f"No schedule with id {schedule_id!r}.",
        )
    existing["cross_platform_resolutions"] = {
        dest_id: ack.model_dump()
        for dest_id, ack in body.resolutions.items()
    }
    return persistence.upsert_schedule(existing)


@router.post("/api/job/stop")
def post_job_stop(
    hard: bool = False,
    _user: Dict[str, Any] = Depends(
        _auth_router_module.require_role("operator")
    ),
) -> Dict[str, Any]:
    """
    Ask the running job to wind down. Returns 409 if no job is
    currently running.

    Query params:
      * ``hard=true`` (v0.12.1) - force the engine off by tearing
        down the shared HTTP session in addition to setting the
        stop flag. Use when the soft Stop has been pending too
        long and the end user just wants the worker free.
    """
    ok = get_queue().request_stop(hard=hard)
    if not ok:
        raise HTTPException(status_code=409, detail="No job is currently running.")
    return {"stop_requested": True, "hard": bool(hard)}


# ── Schedules ────────────────────────────────────────────────────


@router.get("/api/schedules")
def get_schedules() -> List[Dict[str, Any]]:
    return list_schedules()


@router.post("/api/schedules")
def post_schedule(
    body: ScheduleIn,
    _user: Dict[str, Any] = Depends(
        _auth_router_module.require_role("operator")
    ),
) -> Dict[str, Any]:
    """
    Create a new schedule. ``id`` will be assigned by the server
    - clients should omit it on create.
    """
    doc = body.model_dump()
    doc.pop("id", None)
    # A per-schedule output_dir overrides settings; reject a
    # Windows host path here too so a misconfigured schedule
    # doesn't fire silently into the container's ephemeral fs.
    try:
        if doc.get("output_dir"):
            persistence.validate_container_path(doc["output_dir"], "Schedule output directory")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    ensure_next_run_at(doc)
    return persistence.upsert_schedule(doc)


@router.put("/api/schedules/{schedule_id}")
def put_schedule(
    schedule_id: str,
    body: ScheduleIn,
    _user: Dict[str, Any] = Depends(
        _auth_router_module.require_role("operator")
    ),
) -> Dict[str, Any]:
    """
    Replace an existing schedule. The path parameter is the
    authoritative id - body ``id`` is overwritten if it disagrees.
    """
    doc = body.model_dump()
    doc["id"] = schedule_id
    try:
        if doc.get("output_dir"):
            persistence.validate_container_path(doc["output_dir"], "Schedule output directory")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    ensure_next_run_at(doc)
    return persistence.upsert_schedule(doc)


@router.delete("/api/schedules/{schedule_id}")
def delete_schedule_route(
    schedule_id: str,
    _user: Dict[str, Any] = Depends(
        _auth_router_module.require_role("operator")
    ),
) -> Dict[str, Any]:
    ok = persistence.delete_schedule(schedule_id)
    if not ok:
        raise HTTPException(status_code=404, detail=f"No schedule with id {schedule_id!r}")
    return {"deleted": schedule_id}
