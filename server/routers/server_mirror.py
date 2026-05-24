"""Server-mirror DB routes plus the collection-cache routes that
sit alongside it.

Every ``/api/server-mirror/*`` handler and every
``/api/collection-cache/*`` handler. Behaviour preserved verbatim.

Note on grouping: the collection-cache routes are kept here rather
than in ``misc.py`` because they share the same operational surface
(per-server cache management) as the mirror invalidate / warm endpoints
and are documented together in the original app.py file.
"""

from __future__ import annotations

import logging
import threading as _t
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException

from server import auth_router as _auth_router_module


log = logging.getLogger("plexmigrate.server")

router = APIRouter(tags=["server-mirror"])


# ── Mirror sync single-flight ───────────────────────────────────────────────
# Module-scope so it survives request-by-request. The in-flight set
# is keyed by server_id; a duplicate Sync-Now click while a thread is
# still walking the server returns "in_progress" without spawning a
# second walker. A lock around the set keeps add/remove atomic
# against the worker thread's finally block.

_MIRROR_SYNC_INFLIGHT: "set[str]" = set()
_MIRROR_SYNC_INFLIGHT_LOCK = _t.Lock()


def _launch_mirror_sync(
    *, server_id: str, adapter: Any, force_full: bool = False,
) -> str:
    """Spawn a background mirror-walk for ``server_id`` using the
    given adapter's ``iter_sections_for_mirror`` +
    ``iter_section_items_for_mirror``. Single-flight: returns
    ``"in_progress"`` when a walker is already running for this
    server; ``"started"`` otherwise. The walker updates
    ``mirror_server_state.last_full_sync_at`` per section as it
    completes them (via ``server_mirror.full_sync_section``)."""
    with _MIRROR_SYNC_INFLIGHT_LOCK:
        if server_id in _MIRROR_SYNC_INFLIGHT:
            return "in_progress"
        _MIRROR_SYNC_INFLIGHT.add(server_id)

    def _worker() -> None:
        from services import server_mirror
        try:
            sections = adapter.iter_sections_for_mirror()
            if not sections:
                log.info(
                    "mirror sync: server=%s no sections returned; "
                    "marking complete without items.", server_id,
                )
            else:
                summary = server_mirror.sync_for_job(
                    server_id=server_id,
                    sections=sections,
                    item_provider=adapter.iter_section_items_for_mirror,
                    force_full=force_full,
                )
                total_added = sum(int(s.added or 0) for s in summary.sections)
                total_updated = sum(int(s.updated or 0) for s in summary.sections)
                total_removed = sum(int(s.removed or 0) for s in summary.sections)
                log.info(
                    "mirror sync: server=%s done in %.1fs "
                    "(%d section(s); +%d added, %d updated, %d removed)",
                    server_id,
                    summary.finished_at - summary.started_at,
                    len(summary.sections),
                    total_added, total_updated, total_removed,
                )
        except Exception:
            log.exception(
                "mirror sync: server=%s walker raised", server_id,
            )
        finally:
            with _MIRROR_SYNC_INFLIGHT_LOCK:
                _MIRROR_SYNC_INFLIGHT.discard(server_id)

    th = _t.Thread(
        target=_worker, name=f"mirror-sync-{server_id}", daemon=True,
    )
    th.start()
    return "started"


# ── Server Mirror DB ─────────────────────────────────────────────
# 6 endpoints expose mirror state, sync triggers, drift events,
# invalidation, and the per-server mode override. require_role
# gates default to ``viewer`` for reads and ``db_admin`` for any
# write that invalidates or rebuilds. Operators can flip a
# server's mode without re-auth (mode_override is a UX choice,
# not a credential).


@router.get("/api/server-mirror/state")
def get_server_mirror_state(
    _user: Dict[str, Any] = Depends(
        _auth_router_module.require_role("viewer")
    ),
) -> Dict[str, Any]:
    """Return every mirror_server_state row + per-server item
    counts + db size for the operator's Servers panel badge."""
    from services import server_mirror
    from server import server_mirror_db
    states = server_mirror.list_server_states()
    # Augment with item count + last drift event timestamp.
    for s in states:
        s["item_count"] = server_mirror.count_items(
            server_id=s["server_id"]
        )
        recent = server_mirror.list_recent_drift_events(
            server_id=s["server_id"], limit=1,
        )
        s["last_drift_event_at"] = (
            recent[0]["detected_at"] if recent else None
        )
    return {
        "servers": states,
        "db_size_bytes": server_mirror_db.db_size_bytes(),
    }


@router.get("/api/server-mirror/drift-events")
def get_server_mirror_drift_events(
    server_id: Optional[str] = None,
    since: Optional[float] = None,
    limit: int = 100,
    _user: Dict[str, Any] = Depends(
        _auth_router_module.require_role("viewer")
    ),
) -> Dict[str, Any]:
    """Return recent drift_events rows, newest first. Optional
    filters: ``server_id`` narrows to one server; ``since``
    (epoch seconds) filters to events at-or-after that time;
    ``limit`` clamps to [1, 10000]."""
    from services import server_mirror
    try:
        clamped = max(1, min(int(limit) or 100, 10000))
    except (TypeError, ValueError):
        clamped = 100
    rows = server_mirror.list_recent_drift_events(
        server_id=server_id or None,
        since_ts=since,
        limit=clamped,
    )
    return {"events": rows, "count": len(rows)}


@router.post("/api/server-mirror/sync/{server_id}")
def post_server_mirror_sync(
    server_id: str,
    _user: Dict[str, Any] = Depends(
        _auth_router_module.require_role("operator")
    ),
) -> Dict[str, Any]:
    """Operator-triggered immediate mirror sync for one server.
    Returns 'started' once the background thread is launched.
    Idempotent: a sync already in flight for this server returns
    'in_progress' without spawning a second walker.

    The sync is adapter-driven: the thread enumerates sections
    via the adapter's ``iter_sections_for_mirror``, runs
    ``sync_for_job`` against the adapter's
    ``iter_section_items_for_mirror`` provider, and
    ``server_mirror`` updates ``last_full_sync_at`` per section
    on completion (mirror_server_state row).
    """
    from server import server_registry
    from services import server_mirror

    try:
        conn = server_registry.connect_registered_server(
            server_id, log,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ConnectionError as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    # Ensure the server-state row exists before launching the
    # walker so the FE badge has something to render against
    # while the sync is in flight.
    backend = getattr(conn.adapter, "backend", "unknown")
    server_mirror.upsert_server_state(
        server_id=server_id, backend=backend,
    )
    status = _launch_mirror_sync(
        server_id=server_id,
        adapter=conn.adapter,
        force_full=True,
    )
    return {"status": status, "server_id": server_id}


@router.post("/api/server-mirror/sync-all")
def post_server_mirror_sync_all(
    _user: Dict[str, Any] = Depends(
        _auth_router_module.require_role("operator")
    ),
) -> Dict[str, Any]:
    """Force-sync every registered server. Drives the first-run
    "Yes" dialog response AND the global "Sync mirrors" button
    on Servers > Overview. Per-server walkers run as independent
    background threads (no shared lock between them) so a slow
    server doesn't hold up the others. Returns immediately with
    a per-server status: 'started' for newly launched walkers
    and 'in_progress' for syncs already running."""
    from server import server_registry
    from services import server_mirror

    registered = server_registry.list_servers() or []
    per_server: List[Dict[str, Any]] = []
    for s in registered:
        sid = s.get("id") or s.get("server_id")
        if not sid:
            continue
        backend = s.get("service_type", "unknown")
        server_mirror.upsert_server_state(
            server_id=sid, backend=backend,
        )
        try:
            conn = server_registry.connect_registered_server(sid, log)
        except (ValueError, ConnectionError) as exc:
            per_server.append({
                "server_id": sid,
                "status": "error",
                "error": str(exc),
            })
            continue
        status = _launch_mirror_sync(
            server_id=sid,
            adapter=conn.adapter,
            force_full=True,
        )
        per_server.append({"server_id": sid, "status": status})
    return {"status": "started", "results": per_server}


@router.post("/api/server-mirror/invalidate/{server_id}")
def post_server_mirror_invalidate(
    server_id: str,
    _user: Dict[str, Any] = Depends(
        _auth_router_module.require_role("admin")
    ),
) -> Dict[str, Any]:
    """Drop the mirror for one server (db_admin gate; this is a
    cache wipe, not a destructive data op, but operators should
    be re-auth'd anyway since the next job will pay the cold-
    start cost). Pass ``server_id='_all_'`` to wipe every server.
    """
    from services import server_mirror
    if server_id == "_all_":
        rows = server_mirror.invalidate_mirror()
        return {"deleted": rows, "scope": "all"}
    rows = server_mirror.invalidate_mirror(server_id=server_id)
    return {"deleted": rows, "scope": "server", "server_id": server_id}


@router.patch("/api/server-mirror/mode/{server_id}")
def patch_server_mirror_mode(
    server_id: str,
    body: Dict[str, Any],
    _user: Dict[str, Any] = Depends(
        _auth_router_module.require_role("operator")
    ),
) -> Dict[str, Any]:
    """Set or clear a per-server mode override (D2 + D7).

    Body: ``{"mode": "auto" | "always-live" | null}``. Passing
    null clears the override; the server then follows the
    global ``engine_mirror_mode`` tunable.
    """
    from services import server_mirror
    raw_mode = body.get("mode") if isinstance(body, dict) else None
    if raw_mode is not None and not isinstance(raw_mode, str):
        raise HTTPException(
            status_code=400,
            detail="mode must be 'auto', 'always-live', or null",
        )
    try:
        server_mirror.set_server_mode_override(
            server_id=server_id, mode=raw_mode,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {
        "server_id": server_id,
        "mode_override": raw_mode,
        "effective_mode": server_mirror.effective_mode_for(server_id),
    }


# ── Collection-children cache (Movies N+1 fix) ───────────────────


@router.post("/api/collection-cache/invalidate/{server_id}")
def post_collection_cache_invalidate(
    server_id: str,
    _user: Dict[str, Any] = Depends(
        _auth_router_module.require_role("admin")
    ),
) -> Dict[str, Any]:
    """Drop cached collection-children for one server, OR for all
    servers when ``server_id='_all_'``. The next snapshot of that
    server's Movies (and any other library with collections) will
    re-fetch every collection's children from Plex and re-warm
    the cache."""
    from server import collection_cache_db
    from services import collection_cache_log
    if server_id == "_all_":
        deleted = collection_cache_db.invalidate_collection_cache()
        collection_cache_log.log_clear(
            server_id="_all_", deleted=deleted,
        )
        return {"deleted": deleted, "scope": "all"}
    deleted = collection_cache_db.invalidate_collection_cache(
        server_id=server_id,
    )
    collection_cache_log.log_clear(
        server_id=server_id, deleted=deleted,
    )
    return {
        "deleted": deleted,
        "scope": "server",
        "server_id": server_id,
    }


@router.get("/api/collection-cache/status")
def get_collection_cache_status(
    _user: Dict[str, Any] = Depends(
        _auth_router_module.require_role("viewer")
    ),
) -> Dict[str, Any]:
    """Per-server collection cache stats. Each row reports the
    number of cached collections, total cached items across those
    collections, and the freshness timestamps. Use this to verify
    warm-cache behavior between snapshot runs.

    ``servers_per_owner`` adds a per-(server, owner_user_id)
    breakdown so the operator can see how many collections each
    owning user has cached on each server. ``owner_user_id ==
    "_owner"`` is the library-wide / auto-generated bucket;
    anything else is a managed user's librarySectionUserID."""
    from server import collection_cache_db
    rows = collection_cache_db.stats_per_server()
    per_owner = collection_cache_db.stats_per_server_per_owner()
    total = collection_cache_db.cache_stats()
    return {
        "servers": rows,
        "servers_per_owner": per_owner,
        "totals": total,
    }


@router.post("/api/collection-cache/warm/{server_id}")
def post_collection_cache_warm(
    server_id: str,
    source: str = "api",
    _user: Dict[str, Any] = Depends(
        _auth_router_module.require_role("operator")
    ),
) -> Dict[str, Any]:
    """Populate the collection cache for one server without
    running a full snapshot. Walks every library, fetches every
    collection's children, writes them to the cache. Returns
    a per-library summary + aggregate.

    Wall-clock equals the bulk of one snapshot's collection
    phase (Plex serializes the /children endpoint server-side).
    Cached collections are skipped if their ``updatedAt`` hasn't
    advanced, so re-running the warm is fast on subsequent calls.

    ``source`` is a free-form audit tag (``"servers-overview-row"``
    for per-row clicks, ``"servers-bulk"`` for the bulk button,
    ``"api"`` for raw external calls) so the operator can grep
    ``collection_cache.log`` by trigger surface.
    """
    from services import collection_cache_warmer
    from fastapi import HTTPException
    result = collection_cache_warmer.warm_collection_cache_for_server(
        server_id, logger=log, source=source,
    )
    if result.get("error"):
        err = result["error"]
        # Map the warmer's structured errors to HTTP codes.
        if "not registered" in err:
            raise HTTPException(status_code=404, detail=err)
        if "unreachable" in err:
            raise HTTPException(status_code=502, detail=err)
        raise HTTPException(status_code=500, detail=err)
    return result


@router.post("/api/collection-cache/warm-all")
def post_collection_cache_warm_all(
    source: str = "servers-bulk",
    _user: Dict[str, Any] = Depends(
        _auth_router_module.require_role("operator")
    ),
) -> Dict[str, Any]:
    """Warm the collection cache for every registered server.
    Per-server failures are recorded in the response (with reason)
    AND in ``collection_cache.log`` so the bulk-cache UI's failure
    list and the audit log stay in sync. One server's failure does
    NOT abort the rest of the warm pass."""
    from services import collection_cache_warmer
    return collection_cache_warmer.warm_collection_cache_for_all_servers(
        logger=log, source=source,
    )
