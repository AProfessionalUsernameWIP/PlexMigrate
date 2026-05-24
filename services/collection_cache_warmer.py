"""
Collection-children cache warmer.

The cache normally fills as a side-effect of ``snapshot_collections``
running during a snapshot. That works fine for warm-cache snapshots
in steady state, but two operator workflows need a way to populate
the cache OUTSIDE a snapshot:

  1. Testing speed / fidelity of the cache itself. After flipping the
     "Clear" button the operator wants to repopulate without paying
     the full snapshot cost (which also bulk-fetches watch history,
     ratings, playlists, etc.).
  2. Initial-onboarding / pre-warm before a big migration where the
     first snapshot's collection phase would otherwise be slow.

This module is the warm path: connect to the server, walk every
library section, walk every collection in each section, fetch
``collection.items()`` once per collection, write the result to the
cache. Wall-clock cost equals the bulk of one snapshot's collection
phase (Plex serializes the ``/library/metadata/X/children`` endpoint
server-side, so threading does not help here). Future runs hit the
cache instead.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from server import collection_cache_db
from services.resolver import serialize_collection


log = logging.getLogger("plexmigrate.services.collection_cache_warmer")


def warm_collection_cache_for_server(
    server_id: str,
    *,
    logger: Optional[logging.Logger] = None,
    source: Optional[str] = None,
) -> Dict[str, Any]:
    """Walk every library on ``server_id``, fetch every collection's
    children, write the result to ``collection_cache.db``. Returns a
    summary dict the REST endpoint forwards to the UI:

        {
          "server_id": str,
          "total_collections": int,
          "total_items": int,
          "elapsed_seconds": float,
          "libraries": [
            {"name": str, "collections": int, "items": int,
             "skipped": int, "errors": int},
            ...
          ],
          "error": Optional[str],   # set only on connection failure
        }

    Best-effort per collection: a collection whose ``items()`` call
    raises is counted under ``errors`` but does not abort the warm
    pass for that server.
    """
    log_ = logger or log
    from services import collection_cache_log
    collection_cache_log.log_warm_start(
        server_id=server_id, source=source,
    )
    started = time.monotonic()
    out: Dict[str, Any] = {
        "server_id": server_id,
        "total_collections": 0,
        "total_items": 0,
        "elapsed_seconds": 0.0,
        "libraries": [],
        "error": None,
    }

    # Connect to the server via the canonical registry path. Failures
    # bubble up as ValueError / ConnectionError; the REST endpoint
    # maps those to 404 / 502 respectively. Each path also writes a
    # ``warm-error`` line to collection_cache.log so the audit trail
    # explains WHY this server didn't warm. The bulk-cache UI control
    # reads the ``error`` string directly off the response; the log
    # file is the durable counterpart for after-the-fact review.
    from server import server_registry
    try:
        conn = server_registry.connect_registered_server(
            server_id, log_,
        )
    except ValueError as exc:
        out["error"] = f"server not registered: {exc}"
        out["elapsed_seconds"] = time.monotonic() - started
        collection_cache_log.log_warm_error(
            server_id=server_id, error=out["error"], source=source,
        )
        return out
    except ConnectionError as exc:
        out["error"] = f"server unreachable: {exc}"
        out["elapsed_seconds"] = time.monotonic() - started
        collection_cache_log.log_warm_error(
            server_id=server_id, error=out["error"], source=source,
        )
        return out

    server = conn.server
    try:
        sections = list(server.library.sections())
    except Exception as exc:
        out["error"] = f"library.sections() failed: {exc}"
        out["elapsed_seconds"] = time.monotonic() - started
        collection_cache_log.log_warm_error(
            server_id=server_id, error=out["error"], source=source,
        )
        return out

    for section in sections:
        section_summary: Dict[str, Any] = {
            "name": getattr(section, "title", ""),
            "collections": 0,
            "items": 0,
            "skipped": 0,
            "errors": 0,
        }
        try:
            collections = list(section.collections())
        except Exception as exc:
            log_.warning(
                "collection cache warm: section.collections() failed "
                "for %r: %s", section_summary["name"], exc,
            )
            section_summary["errors"] += 1
            out["libraries"].append(section_summary)
            continue

        for coll in collections:
            # Lightweight info we always have for free.
            try:
                rk = str(coll.ratingKey)
            except Exception:
                section_summary["errors"] += 1
                continue
            try:
                live_updated_at = float(getattr(coll, "updatedAt", 0) or 0)
                if hasattr(coll.updatedAt, "timestamp"):
                    live_updated_at = float(coll.updatedAt.timestamp())
            except (AttributeError, TypeError, ValueError):
                live_updated_at = 0.0

            # Check cache first to short-circuit unchanged collections.
            cached = collection_cache_db.lookup_cached_collection(
                server_id, rk, live_updated_at=live_updated_at,
            )
            if cached is not None:
                section_summary["skipped"] += 1
                section_summary["collections"] += 1
                section_summary["items"] += len(cached.get("items") or [])
                out["total_collections"] += 1
                out["total_items"] += len(cached.get("items") or [])
                continue

            # Cache miss: do the slow per-collection fetch + serialize
            # + write-through. Pre-call items() so a per-collection
            # Plex failure surfaces here (serialize_collection swallows
            # the exception internally and would otherwise cache an
            # empty-items dict — which is data fidelity-wrong; we'd
            # rather skip + count the error).
            try:
                _probe_members = coll.items()
                # plexapi caches the MediaContainer on the instance so
                # serialize_collection's subsequent .items() call is
                # free (no extra HTTP round-trip).
                del _probe_members
            except Exception as exc:
                log_.warning(
                    "collection cache warm: items() failed for "
                    "collection %r: %s",
                    getattr(coll, "title", "?"), exc,
                )
                collection_cache_log.log_collection_warm_error(
                    server_id=server_id,
                    library=str(section_summary["name"]),
                    collection_title=str(getattr(coll, "title", "?")),
                    error=f"items() failed: {exc}",
                )
                section_summary["errors"] += 1
                continue
            try:
                serialized = serialize_collection(coll)
            except Exception as exc:
                log_.warning(
                    "collection cache warm: serialize_collection "
                    "failed for %r: %s",
                    getattr(coll, "title", "?"), exc,
                )
                collection_cache_log.log_collection_warm_error(
                    server_id=server_id,
                    library=str(section_summary["name"]),
                    collection_title=str(getattr(coll, "title", "?")),
                    error=f"serialize_collection failed: {exc}",
                )
                section_summary["errors"] += 1
                continue

            # Classify ownership via Plex's librarySectionUserID
            # (the same attribute snapshot_collections' Layer 3 reads).
            # Falsy / absent / 0 means library-wide / admin-owned;
            # we record it as the "_owner" sentinel. A non-zero
            # value means a personal collection owned by the
            # corresponding managed user; record it as-is so the
            # operator can see WHICH user the entry belongs to in
            # the cache-status breakdown.
            try:
                _lsuid = getattr(coll, "librarySectionUserID", None)
            except Exception:
                _lsuid = None
            owner_user_id = (
                "_owner" if _lsuid is None or not _lsuid
                else str(_lsuid)
            )
            ok = collection_cache_db.write_collection_cache(
                server_id, rk,
                serialized=serialized,
                live_updated_at=live_updated_at,
                section_id=str(getattr(section, "key", "")) or None,
                owner_user_id=owner_user_id,
            )
            if not ok:
                section_summary["errors"] += 1
                continue

            n_items = len(serialized.get("items") or [])
            section_summary["collections"] += 1
            section_summary["items"] += n_items
            out["total_collections"] += 1
            out["total_items"] += n_items

        out["libraries"].append(section_summary)

    out["elapsed_seconds"] = time.monotonic() - started
    log_.info(
        "collection cache warm: server=%s done in %.1fs "
        "(%d collection(s), %d item(s) across %d library/libraries)",
        server_id, out["elapsed_seconds"],
        out["total_collections"], out["total_items"],
        len(out["libraries"]),
    )
    # Aggregate per-library errors / skipped counts for the audit log
    # so the operator can grep collection_cache.log without re-running
    # the warm to count them.
    err_total = sum(int(l.get("errors") or 0) for l in out["libraries"])
    skipped_total = sum(int(l.get("skipped") or 0) for l in out["libraries"])
    collection_cache_log.log_warm_end(
        server_id=server_id,
        ok_collections=int(out["total_collections"]),
        ok_items=int(out["total_items"]),
        skipped=skipped_total,
        errors=err_total,
        elapsed_s=float(out["elapsed_seconds"]),
        source=source,
    )
    return out


def warm_collection_cache_for_all_servers(
    *,
    logger: Optional[logging.Logger] = None,
    source: Optional[str] = None,
    max_workers: int = 3,
) -> Dict[str, Any]:
    """Warm the collection cache for every registered server.
    Returns aggregate stats + per-server results. One server's
    failure does NOT abort the others.

    Per-server walkers run in parallel via a small
    ThreadPool (default 3 workers). Each server's loop is still
    serial internally because Plex serializes the
    ``/library/metadata/X/children`` endpoint server-side per
    library section; the win here is from independent SERVERS
    running concurrently. Bound by ``max_workers`` so a multi-
    server install doesn't open one socket per server at once
    against Plex.tv discovery."""
    import concurrent.futures
    log_ = logger or log
    started = time.monotonic()
    from server import server_registry
    try:
        servers = server_registry.list_servers() or []
    except Exception as exc:
        log_.warning(
            "collection cache warm-all: server registry lookup "
            "failed: %s", exc,
        )
        servers = []

    per_server: List[Dict[str, Any]] = []
    aggregate_collections = 0
    aggregate_items = 0
    errors = 0
    server_ids = [
        row.get("id") or row.get("server_id")
        for row in servers
        if row.get("id") or row.get("server_id")
    ]
    n_workers = max(1, min(max_workers, len(server_ids))) if server_ids else 1
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=n_workers, thread_name_prefix="warm-coll",
    ) as pool:
        futures = {
            pool.submit(
                warm_collection_cache_for_server,
                sid, logger=log_, source=source,
            ): sid
            for sid in server_ids
        }
        for fut in concurrent.futures.as_completed(futures):
            sid = futures[fut]
            try:
                result = fut.result()
            except Exception as exc:
                log_.exception(
                    "collection cache warm-all: server %s raised: %s",
                    sid, exc,
                )
                errors += 1
                per_server.append({
                    "server_id": sid,
                    "error": f"{type(exc).__name__}: {exc}",
                    "total_collections": 0,
                    "total_items": 0,
                })
                continue
            per_server.append(result)
            if result.get("error"):
                errors += 1
            else:
                aggregate_collections += result.get("total_collections", 0)
                aggregate_items += result.get("total_items", 0)

    return {
        "servers_warmed": len([r for r in per_server if not r.get("error")]),
        "servers_failed": errors,
        "total_collections": aggregate_collections,
        "total_items": aggregate_items,
        "elapsed_seconds": time.monotonic() - started,
        "results": per_server,
    }
