"""Playlist Management routes.

Every ``/api/playlist-mgmt/*`` handler. Behaviour is preserved
verbatim from the prior in-app.py definitions.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException

from server import (
    auth_router as _auth_router_module,
)
from server.jobs import get_queue
from server.models import (
    PlaylistSpec as PlaylistSpecOut,
    PlaylistItem as PlaylistItemOut,
    PlaylistDetail,
    PlaylistCopyIn,
    PlaylistCopyResult,
    PlaylistCopyBatchIn,
    PlaylistCopyBatchCancelItemIn,
    SmartPlaylistMigrateIn,
    CacheStatus,
    PlaylistCacheRefreshIn,
    PlaylistCacheRefreshResult,
)


log = logging.getLogger("plexmigrate.server")

router = APIRouter(tags=["playlist-mgmt"])


# Pre-warm idempotency: a module-level lock plus a set of in-flight
# server_ids so two racing prewarm requests for the same server don't
# fan out duplicate background threads. The set is keyed by server_id
# (not the adapter object) so it survives adapter re-creation.
import threading as _threading_for_prewarm
_PREWARM_LOCK = _threading_for_prewarm.Lock()
_PREWARM_IN_FLIGHT: set[str] = set()


def _playlist_copy_job_to_dict(rec: Any) -> Dict[str, Any]:
    """Serialize a playlist_copy JobRecord into the
    /api/playlist-mgmt/copy-jobs response shape. Kept module-level so
    both the list route and any future per-job lookup can share it.

    Excludes the engine's internal fields (run_log_dir is empty for
    these jobs by design; the worker doesn't write per-run logs).
    ``summary`` is the end user-visible payload - ``playlist_copy``
    carries the single-copy per-deploy result; ``playlist_copy_batch``
    carries the batch tallies + per-item drill-down. ``params`` carries
    the original submission so the panel can render Clone-deploy
    without local memory.

    The mode field is surfaced as ``mode`` so the frontend can choose
    between the single-row and batch-collapsed-with-drilldown render
    shapes from one list endpoint."""
    summary = rec.summary or {}
    # Strip the items list out of the echoed params for batch rows so
    # the response payload stays light. The frontend reconstructs the
    # original submission shape from the results list when needed.
    echoed_params = summary.get("params") or rec.params or {}
    if rec.mode == "playlist_copy_batch":
        echoed_params = {
            k: v for k, v in echoed_params.items()
            if k != "items"
        }
    return {
        "job_id": rec.job_id,
        "mode": rec.mode,
        "state": rec.state,
        "queued_at": rec.queued_at,
        "started_at": rec.started_at,
        "finished_at": rec.finished_at,
        "error": rec.error,
        "result": summary.get("playlist_copy"),
        "batch": summary.get("playlist_copy_batch"),
        "params": echoed_params,
    }


def _playlist_mgmt_role_from_kind(kind: Optional[str], service_type: str) -> str:
    """Map a ``managed_users.kind`` value to the end user-facing role
    string returned by ``/api/playlist-mgmt/users``. Mirrors the
    three-tier convention from
    ``services.restore.adapter.restorer._normalise_dest_role``:

      * Plex owner kind  → "owner"  (single-admin server model)
      * J/E  owner kind  → "admin"  (multi-admin server model)
      * any "managed" kind → "managed"
    """
    k = (kind or "").lower()
    if k == "owner":
        return "owner" if (service_type or "").lower() == "plex" else "admin"
    return "managed"


# ── Playlist Management ──────────────────────────────────────────
#
# Seven endpoints under /api/playlist-mgmt/* back the Run Jobs
# > Playlist Management sub-tab. Read paths consult the cache when
# fresh; copy + refresh paths always hit live and write through.
# All server_id values carry the prefixed
# ``<service_type>_<uuid>`` UID format; Pydantic validators reject
# malformed values at the API boundary.


@router.get("/api/playlist-mgmt/users")
def get_playlist_mgmt_users(
    server_id: str,
    force_refresh: bool = False,
) -> Dict[str, Any]:
    """Return the user roster for one server. Used by both column
    pickers (source + destination) in the Playlist Management UI.

    Cache-first. Reads from the local ``managed_users`` table by
    default and applies the same share-state filter the Run Job
    picker uses at ``frontend/src/components/JobFormPanel.tsx:96``:
    owners always shown; managed users only when
    ``active_share != False``. Hits Plex live only when (a) the
    end user passed ``force_refresh=true`` or (b) the DB is empty
    (cold-start recovery).

    Why cache-first: a live-every-call path would make the
    Playlist Management panel hit Plex's user-list on every open +
    every server pick. The local DB already has everything the UI
    needs (username, role/kind, has_token, app_user_uuid,
    active_share) and is populated by the same
    ``sync_managed_users_from_live`` helper the Servers tab runs on
    Refresh / Add. So the panel can be near-instant on every load
    as long as the end user has refreshed the server at least once.
    """
    from server import server_registry, media_db
    server_row = server_registry.get_server_by_id(
        server_id, include_token=False,
    )
    if server_row is None:
        raise HTTPException(
            status_code=404,
            detail=f"Server {server_id!r} not registered.",
        )
    service_type = (server_row.get("service_type") or "plex").strip().lower()

    # Force-refresh: pull live + persist before the read. Errors are
    # caught + logged rather than raised, so the panel can still
    # render with whatever cached rows survive.
    if force_refresh:
        try:
            media_db.sync_managed_users_from_live(server_id, log)
        except Exception:
            log.exception(
                "playlist-mgmt force-refresh sync failed for %s",
                server_id,
            )

    # Route through services.user_management.activity_filter so the JobForm
    # picker honours the same tombstone + auth-health filter as
    # every other backend-touching path. The auth health gate
    # fires when the master switch is on; with it off the result
    # matches list_managed_users(include_hidden=False).
    from services.user_management.activity_filter import list_active_users
    rows = list_active_users(server_id)

    # Cold-start recovery (matches JobFormPanel.fetchPickerUsers): if
    # the DB has no rows yet (e.g. the end user just added this
    # server and hasn't hit Refresh), fire one sync + re-read.
    if not rows and not force_refresh:
        try:
            media_db.sync_managed_users_from_live(server_id, log)
            rows = list_active_users(server_id)
        except Exception:
            log.exception(
                "playlist-mgmt cold-start sync failed for %s",
                server_id,
            )

    # Share-state filter — owners are always retained (admin token
    # covers them regardless of the per-user share check); managed
    # users are dropped when active_share is explicitly False (the
    # end user un-shared them on plex.tv). active_share is None on
    # rows the share-state refresher hasn't touched yet; we keep
    # those visible rather than hide on a missing signal.
    transferable = [
        r for r in rows
        if (r.get("kind") or "").lower() == "owner"
        or r.get("active_share") is not False
    ]

    return {
        "server_id": server_id,
        "service_type": service_type,
        "users": [
            {
                "backend_user_id": r.get("backend_user_id") or "",
                "username": r.get("username") or "",
                "role": _playlist_mgmt_role_from_kind(
                    r.get("kind"), service_type,
                ),
                "has_token": bool(r.get("has_token")),
                "app_user_uuid": r.get("app_user_uuid"),
            }
            for r in transferable
        ],
    }


@router.get("/api/playlist-mgmt/playlists")
def get_playlist_mgmt_playlists(
    server_id: str,
    user_id: str,
    force_refresh: bool = False,
) -> Dict[str, Any]:
    """List one user's playlists. Cache-aware unless
    ``force_refresh=true``."""
    from services import playlist_copy
    try:
        result = playlist_copy.list_user_playlists(
            server_id=server_id,
            user_id=user_id,
            force_refresh=bool(force_refresh),
        )
    except playlist_copy.PlaylistCopyError as exc:
        raise HTTPException(
            status_code=exc.http_status,
            detail={"code": exc.code, "message": str(exc)},
        )
    return {
        "server_id": server_id,
        "user_id": user_id,
        "from_cache": result.from_cache,
        "fetched_at": result.fetched_at,
        "playlists": [
            PlaylistSpecOut(
                playlist_id=str(r["playlist_id"]),
                name=str(r["name"]),
                item_count=int(r.get("item_count") or 0),
                is_smart=bool(r.get("is_smart") or False),
                playlist_type=str(r.get("playlist_type") or ""),
                primary_library_id=r.get("primary_library_id"),
                primary_library_name=r.get("primary_library_name"),
            ).model_dump()
            for r in result.playlists
        ],
    }


@router.get(
    "/api/playlist-mgmt/playlist-detail",
    response_model=PlaylistDetail,
)
def get_playlist_mgmt_playlist_detail(
    server_id: str,
    user_id: str,
    playlist_id: str,
    force_refresh: bool = False,
) -> PlaylistDetail:
    """Return one playlist's full item list. Cache-aware unless
    ``force_refresh=true``."""
    from services import playlist_copy
    try:
        data = playlist_copy.get_playlist_detail(
            server_id=server_id,
            user_id=user_id,
            playlist_id=playlist_id,
            force_refresh=bool(force_refresh),
        )
    except playlist_copy.PlaylistCopyError as exc:
        raise HTTPException(
            status_code=exc.http_status,
            detail={"code": exc.code, "message": str(exc)},
        )
    return PlaylistDetail(
        playlist_id=str(data["playlist_id"]),
        name=str(data["name"]),
        is_smart=bool(data.get("is_smart") or False),
        items=[
            PlaylistItemOut(
                title=str(it.get("title") or ""),
                guids=list(it.get("guids") or []),
                type=str(it.get("type") or ""),
                duration_ms=it.get("duration_ms"),
            )
            for it in (data.get("items") or [])
        ],
        fetched_at=float(data.get("fetched_at") or 0.0),
        from_cache=bool(data.get("from_cache") or False),
    )


@router.post(
    "/api/playlist-mgmt/copy",
    response_model=PlaylistCopyResult,
)
def post_playlist_mgmt_copy(
    body: PlaylistCopyIn,
    _user: Dict[str, Any] = Depends(
        _auth_router_module.require_role("operator")
    ),
) -> PlaylistCopyResult:
    """Copy one playlist from source -> destination.

    NOTE: synchronous path. Kept for back-compat / scripted callers
    who want the result in the response body. The UI now uses the
    async ``/copy-job`` route below so deploys survive page reload
    and stack on the active-deploys panel."""
    from services import playlist_copy
    try:
        result = playlist_copy.copy_playlist(
            source_server_id=body.source_server_id,
            source_user_id=body.source_user_id,
            source_playlist_id=body.source_playlist_id,
            dest_server_id=body.dest_server_id,
            dest_user_id=body.dest_user_id,
            dest_playlist_name=body.dest_playlist_name,
        )
    except playlist_copy.PlaylistCopyError as exc:
        raise HTTPException(
            status_code=exc.http_status,
            detail={"code": exc.code, "message": str(exc)},
        )
    return PlaylistCopyResult(
        success=bool(result.get("success")),
        new_playlist_id=result.get("new_playlist_id"),
        items_written=int(result.get("items_written") or 0),
        items_skipped_no_match=int(result.get("items_skipped_no_match") or 0),
        items_failed=int(result.get("items_failed") or 0),
        errors=list(result.get("errors") or []),
        elapsed_seconds=float(result.get("elapsed_seconds") or 0.0),
    )


@router.post("/api/playlist-mgmt/copy-job")
def post_playlist_mgmt_copy_job(
    body: PlaylistCopyIn,
    _user: Dict[str, Any] = Depends(
        _auth_router_module.require_role("operator")
    ),
) -> Dict[str, Any]:
    """Async path: enqueue the playlist copy as a job and return
    the ``job_id`` immediately. Deploy on the Playlist Management
    surface:
      * Persists across page reload (job state lives on the server).
      * Supports multi-deploy via N parallel queue entries.
      * Surfaces progress / result via the existing job-status API.
      * Allows clone-deploy by submitting the same body again.

    Body shape matches the synchronous /copy route. Response is
    ``{job_id: str, state: str}``; the panel then polls
    /api/playlist-mgmt/copy-jobs (or subscribes via the existing WS)
    for progress + result."""
    rec = get_queue().submit_playlist_copy({
        "source_server_id": body.source_server_id,
        "source_user_id": body.source_user_id,
        "source_playlist_id": body.source_playlist_id,
        "dest_server_id": body.dest_server_id,
        "dest_user_id": body.dest_user_id,
        "dest_playlist_name": body.dest_playlist_name,
    })
    return {"job_id": rec.job_id, "state": rec.state}


@router.get("/api/playlist-mgmt/copy-jobs")
def get_playlist_mgmt_copy_jobs() -> Dict[str, Any]:
    """List every playlist_copy + playlist_copy_batch job currently
    known to the queue (active + queued + recent history). Drives
    the Playlist Mgmt active-deploys panel: on mount the frontend
    fetches this to repopulate the list across page reload.

    Each row carries a ``mode`` field so the frontend picks the
    single-row vs batch-collapsed render shape; batch rows expose
    the tallies under ``batch`` and the per-item drill-down inside
    ``batch.results``.

    Returns ``{jobs: [{job_id, mode, state, queued_at, started_at,
    finished_at, error, result, batch, params}]}``; params let the
    frontend render "Clone deploy" without keeping local memory of
    what was originally submitted."""
    q = get_queue()
    out: List[Dict[str, Any]] = []
    cur = q.current()
    _PLAYLIST_MODES = {"playlist_copy", "playlist_copy_batch"}
    if cur is not None and cur.mode in _PLAYLIST_MODES:
        out.append(_playlist_copy_job_to_dict(cur))
    for rec in q.pending():
        if rec.mode in _PLAYLIST_MODES and (cur is None or rec.job_id != cur.job_id):
            out.append(_playlist_copy_job_to_dict(rec))
    for rec in q.history():
        if rec.mode in _PLAYLIST_MODES:
            out.append(_playlist_copy_job_to_dict(rec))
    # Dedupe by job_id (current can appear in both current() and
    # history() during the transition window). Newer-first ordering
    # comes from the iteration order above (active → queued →
    # history) since history is appended chronologically.
    seen: set = set()
    deduped: List[Dict[str, Any]] = []
    for j in out:
        jid = j.get("job_id")
        if jid in seen:
            continue
        seen.add(jid)
        deduped.append(j)
    return {"jobs": deduped}


@router.post("/api/playlist-mgmt/prewarm/{server_id}")
def post_playlist_mgmt_prewarm(server_id: str) -> Dict[str, Any]:
    """Kick off a background build of the destination's path
    indexes so the next copy_playlist batch against this server
    skips the ~32-second walk. Returns immediately (202-style)
    with the scopes that will be warmed.

    Idempotent: if a prewarm is already in-flight for this
    server_id, returns ``status='in_progress'`` without
    starting a second thread.

    The build uses the persistent adapter cache so the warmed
    indexes survive subsequent batches; when the persistence
    tunable is enabled (default ON), the warmed indexes also
    land on disk and survive app restart.

    Scopes built: ``track`` (music sections), ``show`` (TV
    sections), ``movie`` (movie sections). Each scope is built
    separately because PlexAdapter keys its index cache by
    item-type."""
    import threading as _t
    from server import server_registry
    try:
        conn = server_registry.connect_registered_server(
            server_id, log,
        )
    except (ValueError, ConnectionError) as exc:
        raise HTTPException(
            status_code=404 if isinstance(exc, ValueError) else 502,
            detail=str(exc),
        )

    adapter = conn.adapter
    # Idempotency: the in-flight marker is keyed by server_id in a
    # module-level set, not stored on the adapter - a re-created
    # adapter for the same server can't slip a second prewarm past
    # the check.
    with _PREWARM_LOCK:
        if server_id in _PREWARM_IN_FLIGHT:
            return {
                "status": "in_progress",
                "server_id": server_id,
            }
        _PREWARM_IN_FLIGHT.add(server_id)

    def _build():
        try:
            _builder = getattr(adapter, "_build_path_indexes", None)
            if not callable(_builder):
                return
            # Build for every common item type. Skip when the
            # scope's _sections_for_item_type returns empty (no
            # matching section types on this server).
            for scope in ("track", "movie", "episode"):
                try:
                    sections = adapter._sections_for_item_type(scope)
                    if not sections:
                        continue
                    _builder(
                        item_type_hint=scope,
                        tail_components=3,
                    )
                except Exception:
                    log.exception(
                        "playlist_mgmt_prewarm: scope=%r failed", scope,
                    )
        finally:
            with _PREWARM_LOCK:
                _PREWARM_IN_FLIGHT.discard(server_id)

    _t.Thread(
        target=_build, name=f"prewarm-{server_id}",
        daemon=True,
    ).start()
    return {
        "status": "started",
        "server_id": server_id,
        "scopes": ["track", "movie", "episode"],
    }


@router.post("/api/playlist-mgmt/copy-batch-job")
def post_playlist_mgmt_copy_batch_job(
    body: PlaylistCopyBatchIn,
) -> Dict[str, Any]:
    """Enqueue a BATCH playlist copy as ONE job. The helper's
    internal ThreadPoolExecutor provides per-item parallelism
    while the queue's serial guarantee is preserved.

    Body: PlaylistCopyBatchIn (validated). Response: ``{job_id,
    state}``; the frontend then polls /api/playlist-mgmt/copy-jobs
    (or subscribes via the existing WS) for progress + result.

    Hard batch-size limit + parallelism clamping happen on the
    Pydantic validator using the live ``playlist_mgmt_batch_max_size``
    tunable so the same source of truth applies to UI + server."""
    items_serialised = [
        {
            "source_server_id": it.source_server_id,
            "source_user_id": it.source_user_id,
            "source_playlist_id": it.source_playlist_id,
            "dest_server_id": it.dest_server_id,
            "dest_user_id": it.dest_user_id,
            "dest_playlist_name": it.dest_playlist_name,
        }
        for it in body.items
    ]
    rec = get_queue().submit_playlist_copy_batch({
        "items": items_serialised,
        "parallelism": body.parallelism,
        "label": body.label,
    })
    return {"job_id": rec.job_id, "state": rec.state}


@router.post("/api/playlist-mgmt/smart-migrate-job")
def post_playlist_mgmt_smart_migrate_job(
    body: SmartPlaylistMigrateIn,
) -> Dict[str, Any]:
    """Enqueue a Smart Playlist Migration job. Each item's
    source Plex smart playlist has its filter read + translated
    (server-specific tag ids -> names) and re-created on the
    destination - a true smart playlist on Plex, a static
    materialisation on Jellyfin / Emby.

    Body: SmartPlaylistMigrateIn (validated). Response:
    ``{job_id, state}``; the panel then polls the job + reads the
    smart-playlist log for live feedback."""
    items_serialised = [
        {
            "source_server_id": it.source_server_id,
            "source_user_id": it.source_user_id,
            "source_playlist_id": it.source_playlist_id,
            "dest_server_id": it.dest_server_id,
            "dest_user_id": it.dest_user_id,
            "dest_playlist_name": it.dest_playlist_name,
            "hard_copy": it.hard_copy,
        }
        for it in body.items
    ]
    rec = get_queue().submit_smart_playlist_migrate({
        "items": items_serialised,
        "label": body.label,
    })
    return {"job_id": rec.job_id, "state": rec.state}


@router.get("/api/playlist-mgmt/smart-playlist-preview")
def get_playlist_mgmt_smart_preview(
    server_id: str,
    playlist_id: str,
) -> Dict[str, Any]:
    """Decode one source smart playlist's filter into the portable,
    server-agnostic form for the migration panel's inspector +
    preflight. The source must be a Plex server (smart playlists
    are a Plex-only concept)."""
    from server import server_registry
    from services.smart_playlist import filter_model as _sp
    try:
        conn = server_registry.connect_registered_server(server_id, log)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ConnectionError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    if getattr(conn.adapter, "backend", "") != "plex":
        raise HTTPException(
            status_code=400,
            detail="smart playlists exist only on Plex servers",
        )
    reader = getattr(conn.adapter, "read_smart_playlist", None)
    raw = reader(playlist_id) if callable(reader) else None
    if raw is None:
        raise HTTPException(
            status_code=404,
            detail="playlist is missing or is not a smart playlist",
        )
    portable = _sp.to_portable(raw)
    return {
        "playlist_name": raw.playlist_name,
        "filter": _sp.to_dict(portable),
    }


@router.get("/api/playlist-mgmt/smart-migrations")
def get_playlist_mgmt_smart_migrations(
    job_id: str = "",
    limit: int = 100,
) -> Dict[str, Any]:
    """Recent Smart Playlist Migration records, newest first - the
    panel's results view. Filtered to one ``job_id`` when given."""
    from server import smart_playlist_db
    return {
        "migrations": smart_playlist_db.list_migrations(
            job_id=(job_id or None), limit=limit,
        ),
    }


@router.post(
    "/api/playlist-mgmt/copy-batch-job/{job_id}/cancel-item",
)
def post_playlist_mgmt_cancel_batch_item(
    job_id: str,
    body: PlaylistCopyBatchCancelItemIn,
) -> Dict[str, Any]:
    """Cancel ONE item inside a running batch. Plan section 8a Q5:
    end user can drop a mistakenly-selected playlist without nuking
    the whole batch. The worker plumbs a per-item cancel_event
    dict onto the JobRecord at start time; this endpoint finds the
    right event and sets it.

    Returns ``{status, job_id, item_index}`` where ``status`` is
    one of ``cancelled / not_running / not_batch /
    index_out_of_range / not_found`` (mapped to HTTP from the queue
    method's return value)."""
    status = get_queue().cancel_batch_item(job_id, body.item_index)
    if status == "cancelled":
        http = 200
    elif status == "not_found":
        http = 404
    elif status == "not_running":
        http = 409
    elif status == "not_batch":
        http = 409
    elif status == "index_out_of_range":
        http = 422
    else:
        http = 500
    if http != 200:
        raise HTTPException(
            status_code=http,
            detail={
                "status": status,
                "job_id": job_id,
                "item_index": body.item_index,
            },
        )
    return {"status": status, "job_id": job_id, "item_index": body.item_index}


@router.post(
    "/api/playlist-mgmt/cache/refresh",
    response_model=PlaylistCacheRefreshResult,
)
def post_playlist_mgmt_cache_refresh(
    body: PlaylistCacheRefreshIn,
) -> PlaylistCacheRefreshResult:
    """Force-refresh one user's playlist cache (per-user path).
    ``user_id`` must be set; the bulk path uses /refresh-server."""
    from services import playlist_copy
    if not (body.user_id or "").strip():
        raise HTTPException(
            status_code=422,
            detail=(
                "user_id is required for /cache/refresh; "
                "use /cache/refresh-server for the per-server bulk path."
            ),
        )
    row = playlist_copy.refresh_user_cache(
        server_id=body.server_id, user_id=body.user_id,
    )
    return PlaylistCacheRefreshResult(
        server_id=str(row["server_id"]),
        user_id=row.get("user_id"),
        refreshed_at=float(row.get("refreshed_at") or 0.0),
        playlists_count=int(row.get("playlists_count") or 0),
        items_count=int(row.get("items_count") or 0),
        duration_ms=int(row.get("duration_ms") or 0),
        error=row.get("error"),
    )


@router.post("/api/playlist-mgmt/cache/refresh-server")
def post_playlist_mgmt_cache_refresh_server(
    body: PlaylistCacheRefreshIn,
    source: str = "api",
) -> Dict[str, Any]:
    """Per-server bulk refresh: re-fetch every user's playlists on
    the given server. Returns a list of per-user rows plus a final
    aggregate row with ``user_id=None``.

    ``source`` is an optional free-form tag (query param) the caller
    can set to attribute the trigger surface in the audit log.
    Defaults to ``"api"`` for raw-call attribution; the UI passes
    ``"servers-refresh"`` or ``"playlist-mgmt-panel"`` so the
    end user can grep ``playlist_cache.log`` by trigger."""
    from services import playlist_copy
    rows = playlist_copy.refresh_server_cache(
        server_id=body.server_id, source=source,
    )
    return {
        "server_id": body.server_id,
        "results": [
            PlaylistCacheRefreshResult(
                server_id=str(r["server_id"]),
                user_id=r.get("user_id"),
                refreshed_at=float(r.get("refreshed_at") or 0.0),
                playlists_count=int(r.get("playlists_count") or 0),
                items_count=int(r.get("items_count") or 0),
                duration_ms=int(r.get("duration_ms") or 0),
                error=r.get("error"),
            ).model_dump()
            for r in rows
        ],
    }


@router.post("/api/playlist-mgmt/cache/refresh-all")
def post_playlist_mgmt_cache_refresh_all(
    source: str = "api",
    _user: Dict[str, Any] = Depends(
        _auth_router_module.require_role("operator")
    ),
) -> Dict[str, Any]:
    """Bulk per-server refresh: warms the playlist cache for every
    registered server in turn. One server's failure does NOT abort
    the others. Returns per-server summary rows + aggregate counts
    so the UI can surface a per-server failure list. Symmetric with
    ``/api/collection-cache/warm-all`` so the bulk-cache UI control
    can drive either or both cache types in one click."""
    from services import playlist_copy
    from server import server_registry
    import concurrent.futures
    started = time.monotonic()
    try:
        servers = server_registry.list_servers() or []
    except Exception as exc:
        log.warning(
            "playlist refresh-all: server registry lookup "
            "failed: %s", exc,
        )
        servers = []
    results: List[Dict[str, Any]] = []
    servers_warmed = 0
    servers_failed = 0
    total_playlists = 0
    total_items = 0
    # Per-server walkers run in parallel via a small ThreadPool
    # so a multi-server install doesn't serialise the warm across
    # servers. Bound at 3 workers so we don't open one socket per
    # server at once against the registry.
    server_ids = [
        row.get("id") or row.get("server_id")
        for row in servers
        if row.get("id") or row.get("server_id")
    ]
    if not server_ids:
        return {
            "servers_warmed": 0, "servers_failed": 0,
            "total_playlists": 0, "total_items": 0,
            "elapsed_seconds": time.monotonic() - started,
            "results": [],
        }
    n_workers = max(1, min(3, len(server_ids)))

    def _refresh_one(sid: str) -> Dict[str, Any]:
        return {
            "server_id": sid,
            "rows": playlist_copy.refresh_server_cache(
                server_id=sid, source=source,
            ),
        }
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=n_workers, thread_name_prefix="warm-pl",
    ) as pool:
        futures = {pool.submit(_refresh_one, sid): sid for sid in server_ids}
        done_rows: Dict[str, Any] = {}
        for fut in concurrent.futures.as_completed(futures):
            sid = futures[fut]
            try:
                done_rows[sid] = fut.result()["rows"]
            except Exception as exc:
                done_rows[sid] = exc
    # Preserve registry order in the response so the UI's
    # per-server list matches the order the operator sees in
    # the registered-servers table.
    for sid in server_ids:
        rows_or_exc = done_rows.get(sid)
        if isinstance(rows_or_exc, Exception):
            exc = rows_or_exc
            # Defensive: refresh_server_cache catches its own
            # failures and returns them in-band, but a misbehaving
            # adapter could still raise. Surface the reason so the
            # UI failure list explains WHY this server failed.
            log.exception(
                "playlist refresh-all: server %s raised: %s",
                sid, exc,
            )
            servers_failed += 1
            results.append({
                "server_id": sid,
                "error": f"{type(exc).__name__}: {exc}",
                "playlists_count": 0,
                "items_count": 0,
            })
            continue
        rows = rows_or_exc or []
        agg = next((r for r in rows if r.get("user_id") is None), None)
        if agg and agg.get("error"):
            servers_failed += 1
            results.append({
                "server_id": sid,
                "error": agg.get("error"),
                "playlists_count": int(agg.get("playlists_count") or 0),
                "items_count": int(agg.get("items_count") or 0),
            })
            continue
        # Per-user errors are tolerable; count this server as
        # warmed but include the per-user error tally.
        per_user_errors = sum(
            1 for r in rows
            if r.get("user_id") is not None and r.get("error")
        )
        servers_warmed += 1
        total_playlists += int((agg or {}).get("playlists_count") or 0)
        total_items += int((agg or {}).get("items_count") or 0)
        results.append({
            "server_id": sid,
            "error": None,
            "playlists_count": int((agg or {}).get("playlists_count") or 0),
            "items_count": int((agg or {}).get("items_count") or 0),
            "per_user_errors": per_user_errors,
        })
    return {
        "servers_warmed": servers_warmed,
        "servers_failed": servers_failed,
        "total_playlists": total_playlists,
        "total_items": total_items,
        "elapsed_seconds": time.monotonic() - started,
        "results": results,
    }


@router.get("/api/playlist-mgmt/cache/status")
def get_playlist_mgmt_cache_status(server_id: str) -> Dict[str, Any]:
    """Return per-user freshness rows for every user on a server.
    Drives the "fresh / stale / never refreshed" badges in the
    Playlist Management user picker."""
    from server import playlist_cache_db, server_registry
    from services.tunables import (
        playlist_cache_snapshot_threshold_seconds,
    )
    try:
        conn = server_registry.connect_registered_server(server_id, log)
    except ValueError:
        raise HTTPException(
            status_code=404, detail=f"Server {server_id!r} not registered.",
        )
    except ConnectionError as exc:
        raise HTTPException(
            status_code=502, detail=f"Server {server_id!r} unreachable: {exc}",
        )
    try:
        users = conn.adapter.list_users() or []
    except Exception as exc:
        raise HTTPException(
            status_code=502, detail=f"list_users failed: {exc}",
        )
    threshold = float(playlist_cache_snapshot_threshold_seconds())
    now = time.time()
    rows: List[Dict[str, Any]] = []
    for u in users:
        uid = u.backend_user_id or u.username
        if not uid:
            continue
        marker = playlist_cache_db.get_refresh_marker(server_id, uid)
        cached_count = len(
            playlist_cache_db.list_cached_playlists(server_id, uid)
        )
        if marker is None:
            rows.append(CacheStatus(
                server_id=server_id,
                user_id=uid,
                last_refreshed_at=0.0,
                playlists_count=cached_count,
                age_seconds=0.0,
                is_stale=True,
            ).model_dump())
            continue
        age = now - float(marker["last_refreshed_at"])
        rows.append(CacheStatus(
            server_id=server_id,
            user_id=uid,
            last_refreshed_at=float(marker["last_refreshed_at"]),
            playlists_count=cached_count,
            age_seconds=float(age),
            is_stale=bool(age > threshold or marker.get("error")),
        ).model_dump())
    return {"server_id": server_id, "rows": rows}
