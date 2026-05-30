"""Sync subscriptions routes.

Every ``/api/sync/*`` handler. Behaviour preserved verbatim.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException

from server import auth_router as _auth_router_module


log = logging.getLogger("plexmigrate.server")

router = APIRouter(tags=["sync"])


# ── Sync subscriptions ───────────────────────────────────────────
#
# Endpoints back the Sync Subscriptions UI. The library-pair sync
# engine consumes these rows; the worker polls each enabled
# subscription per its poll_interval_seconds and reconciles state
# under the configured conflict_policy.
#
# Granularity:
#   * library-pair: source_library_id + dest_library_id BOTH set
#   * server-pair: BOTH library ids null/empty — engine expands to
#     every currently-mapped library pair at poll time


@router.get("/api/sync/subscriptions")
def get_sync_subscriptions(
    source_server_id: Optional[str] = None,
    dest_server_id: Optional[str] = None,
    enabled_only: bool = False,
    _user: Dict[str, Any] = Depends(
        _auth_router_module.require_role("viewer")
    ),
) -> Dict[str, Any]:
    from server import sync_db
    rows = sync_db.list_subscriptions(
        source_server_id=source_server_id or None,
        dest_server_id=dest_server_id or None,
        enabled_only=bool(enabled_only),
    )
    return {"subscriptions": rows}


@router.put("/api/sync/subscriptions")
def put_sync_subscription(
    body: Dict[str, Any],
    _user: Dict[str, Any] = Depends(
        _auth_router_module.require_role("operator")
    ),
) -> Dict[str, Any]:
    """Create or update a subscription. Body fields mirror
    ``sync_db.upsert_subscription`` kwargs. Granularity must be
    symmetric (both library ids set OR both empty)."""
    from server import sync_db
    for key in ("source_server_id", "dest_server_id", "sync_type"):
        if not str(body.get(key) or "").strip():
            raise HTTPException(
                status_code=400, detail=f"{key} is required",
            )
    try:
        sub_id = sync_db.upsert_subscription(
            source_server_id=body["source_server_id"],
            dest_server_id=body["dest_server_id"],
            sync_type=body["sync_type"],
            source_library_id=body.get("source_library_id") or None,
            source_library_name=str(body.get("source_library_name") or ""),
            dest_library_id=body.get("dest_library_id") or None,
            dest_library_name=str(body.get("dest_library_name") or ""),
            conflict_policy=str(body.get("conflict_policy") or "max"),
            enabled=bool(body.get("enabled") or False),
            dry_run=bool(body.get("dry_run", True)),
            bidirectional=bool(body.get("bidirectional") or False),
            user_scope=str(body.get("user_scope") or "owner"),
            user_filter=body.get("user_filter") or None,
            poll_interval_seconds=int(
                body.get("poll_interval_seconds") or 300
            ),
            auto_sync_new_playlists=bool(
                body.get("auto_sync_new_playlists") or False
            ),
            created_by=str(body.get("_actor_username") or "")
            if body.get("_actor_username") else None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"id": sub_id}


@router.delete("/api/sync/subscriptions/{sub_id}")
def delete_sync_subscription(
    sub_id: int,
    _user: Dict[str, Any] = Depends(
        _auth_router_module.require_role("operator")
    ),
) -> Dict[str, Any]:
    from server import sync_db
    n = sync_db.delete_subscription(sub_id)
    return {"deleted": n}


@router.get("/api/sync/subscriptions/{sub_id}")
def get_sync_subscription_one(
    sub_id: int,
    _user: Dict[str, Any] = Depends(
        _auth_router_module.require_role("viewer")
    ),
) -> Dict[str, Any]:
    from server import sync_db
    r = sync_db.get_subscription(sub_id)
    if not r:
        raise HTTPException(status_code=404, detail="not found")
    return r


@router.get("/api/sync/subscriptions/{sub_id}/writes")
def get_sync_subscription_writes(
    sub_id: int,
    limit: int = 100,
    _user: Dict[str, Any] = Depends(
        _auth_router_module.require_role("viewer")
    ),
) -> Dict[str, Any]:
    """Recent write log for a subscription. Drives the per-row
    'View log' affordance in the UI so the operator can audit
    what the worker decided + actually wrote."""
    from server import sync_db
    return {
        "subscription_id": sub_id,
        "writes": sync_db.list_recent_writes(
            subscription_id=sub_id, limit=limit,
        ),
    }


# ── Playlist sync selections ─────────────────────────────────────
@router.get("/api/sync/subscriptions/{sub_id}/playlists")
def get_sync_playlist_selections(
    sub_id: int,
    enabled_only: bool = False,
    _user: Dict[str, Any] = Depends(
        _auth_router_module.require_role("viewer")
    ),
) -> Dict[str, Any]:
    from server import sync_db
    return {
        "subscription_id": sub_id,
        "selections": sync_db.list_playlist_selections(
            subscription_id=sub_id, enabled_only=bool(enabled_only),
        ),
    }


@router.put("/api/sync/subscriptions/{sub_id}/playlists")
def put_sync_playlist_selection(
    sub_id: int,
    body: Dict[str, Any],
    _user: Dict[str, Any] = Depends(
        _auth_router_module.require_role("operator")
    ),
) -> Dict[str, Any]:
    from server import sync_db
    pid = str(body.get("source_playlist_id") or "").strip()
    if not pid:
        raise HTTPException(
            status_code=400, detail="source_playlist_id is required",
        )
    ok = sync_db.add_playlist_selection(
        subscription_id=sub_id,
        source_playlist_id=pid,
        source_playlist_name=str(body.get("source_playlist_name") or ""),
        added_by=str(body.get("added_by") or "operator"),
        enabled=bool(body.get("enabled", True)),
    )
    return {"ok": bool(ok)}


@router.delete("/api/sync/subscriptions/{sub_id}/playlists/{source_playlist_id}")
def delete_sync_playlist_selection(
    sub_id: int,
    source_playlist_id: str,
    _user: Dict[str, Any] = Depends(
        _auth_router_module.require_role("operator")
    ),
) -> Dict[str, Any]:
    from server import sync_db
    n = sync_db.remove_playlist_selection(
        subscription_id=sub_id,
        source_playlist_id=source_playlist_id,
    )
    return {"deleted": n}


@router.get("/api/sync/stats")
def get_sync_stats(
    _user: Dict[str, Any] = Depends(
        _auth_router_module.require_role("viewer")
    ),
) -> Dict[str, int]:
    from server import sync_db
    return sync_db.stats()


@router.post("/api/sync/subscriptions/{sub_id}/run")
def post_sync_run_now(
    sub_id: int,
    background_tasks: BackgroundTasks,
    _user: Dict[str, Any] = Depends(
        _auth_router_module.require_role("operator")
    ),
) -> Dict[str, Any]:
    """Force an immediate reconcile of one subscription.

    The next-scheduled-tick wait is bypassed: the subscription's
    reconcile runs in a background thread and the endpoint
    returns 202 immediately so the UI doesn't block. The result
    of the run lands in sync_writes / sync_observations
    normally, viewable under Sync Activity once the run
    completes.

    Operator-gated. The reconcile honours the subscription's
    existing dry_run flag — a "Run now" against a dry-run
    subscription still only logs intents, never writes."""
    from server import sync_db
    sub = sync_db.get_subscription(sub_id)
    if sub is None:
        raise HTTPException(
            status_code=404,
            detail=f"No subscription with id {sub_id}.",
        )

    def _run() -> None:
        try:
            from services.mirror_sync import sync_worker as sync_worker
            sync_worker._reconcile_subscription(sub)
        except Exception:
            log.exception(
                "sync run-now for subscription %s failed", sub_id,
            )

    background_tasks.add_task(_run)
    return {
        "subscription_id": sub_id,
        "queued": True,
        "note": (
            "Reconcile scheduled. Watch Sync Activity for the "
            "result; the row's Last poll timestamp updates when "
            "the run completes."
        ),
    }
