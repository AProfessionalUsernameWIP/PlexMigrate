"""Library auto-matcher + mapping persistence routes.

Every ``/api/library-mapping/*`` handler. Behaviour preserved
verbatim from the prior in-app.py definitions.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException

from server import auth_router as _auth_router_module


log = logging.getLogger("plexmigrate.server")

router = APIRouter(tags=["library-mapping"])


# ── Library auto-matcher + mapping persistence ───────────────────
#
# Four endpoints back the Servers > Library Mapping sub-tab:
#   POST /api/library-mapping/automap         - compute suggestions
#   GET  /api/library-mapping/list            - read saved mappings
#   PUT  /api/library-mapping/save            - operator confirm one
#   DELETE /api/library-mapping/{src...}      - operator unset one
# All gated on the ``operator`` role: library mappings affect
# restore + transfer behaviour. The matcher itself is read-only and
# has no destructive side effects, but its results drive saves
# downstream so the same gate applies to the compute step.


@router.post("/api/library-mapping/automap")
def post_library_mapping_automap(
    body: Dict[str, Any],
    _user: Dict[str, Any] = Depends(
        _auth_router_module.require_role("operator")
    ),
) -> Dict[str, Any]:
    """Compute auto-match suggestions for a (source_server,
    dest_server) pair from the mirror DB. Returns one entry per
    source library, with best + alternatives + confidence per the
    4-tier matcher.

    Body: ``{"source_server_id": str, "dest_server_id": str,
             "persist_high_confidence": bool}``
    When ``persist_high_confidence`` is True, suggestions whose
    confidence >= 0.50 are saved as ``source='auto'`` rows. Below
    that threshold are returned as suggestions only — operator
    confirms via /save. Default False so the matcher is purely
    read-only unless the caller opts in to auto-save."""
    from services import library_mapper
    from server import library_mapping_db
    source_server_id = str(body.get("source_server_id") or "").strip()
    dest_server_id = str(body.get("dest_server_id") or "").strip()
    persist = bool(body.get("persist_high_confidence") or False)
    if not source_server_id or not dest_server_id:
        raise HTTPException(
            status_code=400,
            detail="source_server_id and dest_server_id are required",
        )
    results = library_mapper.auto_match_libraries(
        source_server_id, dest_server_id,
    )
    wire = [library_mapper.serialize_match_result(r) for r in results]
    saved = 0
    if persist:
        for r in results:
            if r.best is None:
                continue
            if not library_mapper.confidence_is_auto_apply(
                r.best.confidence
            ):
                continue
            # Skip when an operator-confirmed mapping already
            # exists for this source library — don't override
            # operator decisions.
            existing = library_mapping_db.get_mapping(
                source_server_id=source_server_id,
                source_library_id=r.source_library.library_id,
                dest_server_id=dest_server_id,
            )
            if existing and existing.get("source") == "operator":
                continue
            ok = library_mapping_db.upsert_mapping(
                source_server_id=source_server_id,
                source_library_id=r.source_library.library_id,
                source_library_name=r.source_library.library_name,
                dest_server_id=dest_server_id,
                dest_library_id=r.best.dest_library.library_id,
                dest_library_name=r.best.dest_library.library_name,
                source="auto",
                confidence=r.best.confidence,
                tier=r.best.tier,
                notes=None,
            )
            if ok:
                saved += 1
    return {
        "source_server_id": source_server_id,
        "dest_server_id":   dest_server_id,
        "results":          wire,
        "auto_saved":       saved,
    }


@router.get("/api/library-mapping/list")
def get_library_mapping_list(
    source_server_id: str,
    dest_server_id: str,
    _user: Dict[str, Any] = Depends(
        _auth_router_module.require_role("viewer")
    ),
) -> Dict[str, Any]:
    """All saved mappings between two servers, ordered by source
    library id. Viewer-gated because mappings carry only library
    names and ids (no credentials)."""
    from server import library_mapping_db
    if not source_server_id or not dest_server_id:
        raise HTTPException(
            status_code=400,
            detail="source_server_id and dest_server_id are required",
        )
    return {
        "source_server_id": source_server_id,
        "dest_server_id":   dest_server_id,
        "mappings": library_mapping_db.list_mappings_for_pair(
            source_server_id, dest_server_id,
        ),
    }


@router.put("/api/library-mapping/save")
def put_library_mapping_save(
    body: Dict[str, Any],
    _user: Dict[str, Any] = Depends(
        _auth_router_module.require_role("operator")
    ),
) -> Dict[str, Any]:
    """Operator save (or confirm a suggestion).
    Body: ``{source_server_id, source_library_id, dest_server_id,
             dest_library_id, source, notes?}``
    ``source`` must be ``"operator"`` for the human-confirmed path
    or ``"auto"`` when bulk-saving suggestions. Empty
    ``dest_library_id`` means "explicit skip" — the restorer will
    bypass this source library for this server pair."""
    from server import library_mapping_db
    required = (
        "source_server_id", "source_library_id",
        "dest_server_id",
    )
    for k in required:
        if not str(body.get(k) or "").strip():
            raise HTTPException(
                status_code=400, detail=f"{k} is required",
            )
    source = str(body.get("source") or "operator").lower()
    if source not in ("operator", "auto"):
        raise HTTPException(
            status_code=400,
            detail="source must be 'operator' or 'auto'",
        )
    ok = library_mapping_db.upsert_mapping(
        source_server_id=body["source_server_id"],
        source_library_id=body["source_library_id"],
        source_library_name=str(body.get("source_library_name") or ""),
        dest_server_id=body["dest_server_id"],
        dest_library_id=str(body.get("dest_library_id") or ""),
        dest_library_name=str(body.get("dest_library_name") or ""),
        source=source,
        confidence=float(body.get("confidence") or 0.0),
        tier=str(body.get("tier") or ""),
        notes=body.get("notes"),
    )
    return {"ok": bool(ok)}


@router.delete(
    "/api/library-mapping/{source_server_id}/"
    "{source_library_id}/{dest_server_id}"
)
def delete_library_mapping_one(
    source_server_id: str,
    source_library_id: str,
    dest_server_id: str,
    _user: Dict[str, Any] = Depends(
        _auth_router_module.require_role("operator")
    ),
) -> Dict[str, Any]:
    """Drop one mapping. Useful for "reset this row" from the UI
    before re-running automap. The restorer falls back to exact-
    name matching once the row is gone."""
    from server import library_mapping_db
    deleted = library_mapping_db.delete_mapping(
        source_server_id, source_library_id, dest_server_id,
    )
    return {"deleted": deleted}


@router.get("/api/library-mapping/sides")
def get_library_mapping_sides(
    source_server_id: str,
    dest_server_id: str,
    _user: Dict[str, Any] = Depends(
        _auth_router_module.require_role("operator")
    ),
) -> Dict[str, Any]:
    """Return EACH server's library list (id, name, type,
    item_count) alongside the currently-saved mapping table for
    the pair. Drives the side-by-side Library Mapping UI so the
    operator sees both servers' libraries laid out as columns
    even before the mirror has been synced.

    Resolution order per server:
      1. Mirror DB (when warm; fastest, no Plex round-trips).
      2. Live adapter ``list_libraries()`` (fallback for cold
         mirror; one cheap API call per server).
      3. Empty list if neither path yields anything.

    The response shape is intentionally flat so the React side
    can render two columns without re-shaping. ``mappings`` is
    the same payload ``/list`` returns, included here so the UI
    does one fetch per server-pair selection."""
    from server import library_mapping_db, server_mirror_db
    from server import server_registry

    if not source_server_id or not dest_server_id:
        raise HTTPException(
            status_code=400,
            detail="source_server_id and dest_server_id are required",
        )

    def _top_count_index(server_id: str) -> Dict[str, int]:
        """Index the registry's cached top-level library count by
        library key. ``last_libraries[*].count`` is Plex's
        ``section.totalSize`` snapshotted at the last Refresh —
        the AUTHORITATIVE top-level count (artists for a music
        library, shows for a TV library, movies for a movie
        library). Used as the fallback for ``item_count`` when
        the mirror's ``live_total_size`` is NULL.

        The mirror's ``COUNT(mirror_items)`` is a LEAF count
        (mirror_items stores tracks / episodes / movies, never
        the artist / show parent rows). Using it as ``item_count``
        would report "12,005 artists" for a 1,100-artist library.
        The correct top-level number is live_total_size OR this
        registry count."""
        try:
            row = server_registry.get_server_by_id(
                server_id, include_token=False,
            ) or {}
            cached = row.get("last_libraries") or []
        except Exception:
            return {}
        out: Dict[str, int] = {}
        for entry in cached:
            if not isinstance(entry, dict):
                continue
            key = str(entry.get("key") or entry.get("library_id") or "")
            if not key:
                continue
            try:
                c = int(entry.get("count") or 0)
            except (TypeError, ValueError):
                c = 0
            if c > 0:
                out[key] = c
        return out

    def _leaf_counts_index(server_id: str) -> Dict[str, Dict[str, int]]:
        """Index leaf_counts by library key. Three data sources
        merged in priority order:

          1. Registry's cached ``last_libraries`` — updated on every
             Refresh + library walk; gives episodes / seasons /
             tracks / albums / collections without a fresh
             round-trip.
          2. ``collection_cache.db`` — when warmed via the bulk
             cache flow, replaces the registry's collection count
             with the comprehensive cached value (the registry's
             size-0 probe may have been stale).
          3. ``playlist_cache.db`` — adds per-library playlist
             count via the cache's ``primary_library_id`` index.
             The registry doesn't track this, so the cache is
             the only source today.

        All sources are best-effort; a cold cache returns an empty
        dict and falls through to whichever upstream source has data."""
        try:
            row = server_registry.get_server_by_id(
                server_id, include_token=False,
            ) or {}
            cached = row.get("last_libraries") or []
        except Exception:
            cached = []
        out: Dict[str, Dict[str, int]] = {}
        for entry in cached:
            if not isinstance(entry, dict):
                continue
            key = str(entry.get("key") or entry.get("library_id") or "")
            if not key:
                continue
            leaf = entry.get("leaf_counts")
            if isinstance(leaf, dict):
                out[key] = {
                    k: int(v) for k, v in leaf.items()
                    if isinstance(v, (int, float)) and int(v) > 0
                }
        # Enrich with collection_cache.db (cache-first; replaces
        # registry's collection count which may be stale).
        try:
            from server import collection_cache_db
            coll_idx = collection_cache_db.count_per_section(server_id)
            for sec_id, n in coll_idx.items():
                if n > 0:
                    out.setdefault(sec_id, {})["collections"] = n
        except Exception:
            pass
        # Enrich with playlist_cache.db (per-library playlist count;
        # registry doesn't track this).
        try:
            from server import playlist_cache_db
            pl_idx = playlist_cache_db.count_per_library(server_id)
            for lib_id, n in pl_idx.items():
                if n > 0:
                    out.setdefault(lib_id, {})["playlists"] = n
        except Exception:
            pass
        return out

    def _libs_for(server_id: str) -> List[Dict[str, Any]]:
        leaf_idx = _leaf_counts_index(server_id)
        top_idx = _top_count_index(server_id)
        # Mirror-first.
        try:
            conn = server_mirror_db.get_connection()
            # Select live_total_size (Plex's section.totalSize =
            # the top-level count) instead of COUNT(mirror_items)
            # which is a leaf count. mirror_items only stores
            # leaf rows (tracks / episodes / movies); counting
            # them gives "12,005 artists" for a 1,100-artist
            # library. COUNT(mirror_items) is kept as ``leaf_n``
            # purely as a last-resort fallback.
            rows = conn.execute(
                "SELECT s.section_id, s.name, s.section_type, "
                "       s.live_total_size AS live_total, "
                "       COUNT(i.rating_key) AS leaf_n "
                "FROM mirror_library_sections s "
                "LEFT JOIN mirror_items i "
                "  ON i.server_id = s.server_id "
                " AND i.section_id = s.section_id "
                "WHERE s.server_id = ? "
                "GROUP BY s.section_id, s.name, s.section_type, "
                "         s.live_total_size "
                "ORDER BY s.section_id",
                (server_id,),
            ).fetchall()
            if rows:
                out_rows: List[Dict[str, Any]] = []
                for r in rows:
                    sec_id = str(r["section_id"] or "")
                    # item_count priority:
                    #   1. live_total_size (mirror sync; Plex
                    #      section.totalSize — the real top count)
                    #   2. registry last_libraries count (library
                    #      walk's section.totalSize snapshot)
                    #   3. COUNT(mirror_items) leaf count — last
                    #      resort; wrong unit for music/TV but
                    #      better than reporting zero.
                    live_total = r["live_total"]
                    if live_total is not None and int(live_total) > 0:
                        item_count = int(live_total)
                    elif top_idx.get(sec_id):
                        item_count = top_idx[sec_id]
                    else:
                        item_count = int(r["leaf_n"] or 0)
                    out_rows.append({
                        "library_id":   sec_id,
                        "library_name": str(r["name"] or ""),
                        "library_type": str(r["section_type"] or ""),
                        "item_count":   item_count,
                        "leaf_counts":  leaf_idx.get(sec_id, {}),
                        "source":       "mirror",
                    })
                return out_rows
        except Exception:
            pass
        # Live fallback via the adapter when the mirror is empty.
        try:
            conn = server_registry.connect_registered_server(
                server_id, log,
            )
            libs = conn.adapter.list_libraries() or []
            return [
                {
                    "library_id":   str(getattr(lib, "library_id", "") or ""),
                    "library_name": str(getattr(lib, "name", "") or ""),
                    "library_type": str(getattr(lib, "type", "") or ""),
                    "item_count":   int(getattr(lib, "item_count", 0) or 0),
                    "leaf_counts":  leaf_idx.get(
                        str(getattr(lib, "library_id", "") or ""), {},
                    ),
                    "source":       "live",
                }
                for lib in libs
            ]
        except Exception as exc:
            log.warning(
                "library-mapping sides: live list_libraries "
                "failed for server=%s: %s", server_id, exc,
            )
            return []

    return {
        "source_server_id": source_server_id,
        "dest_server_id":   dest_server_id,
        "source_libraries": _libs_for(source_server_id),
        "dest_libraries":   _libs_for(dest_server_id),
        "mappings": library_mapping_db.list_mappings_for_pair(
            source_server_id, dest_server_id,
        ),
    }


@router.post("/api/library-mapping/preflight")
def post_library_mapping_preflight(
    body: Dict[str, Any],
    _user: Dict[str, Any] = Depends(
        _auth_router_module.require_role("viewer")
    ),
) -> Dict[str, Any]:
    """Pre-submit check that the Run Job form fires once the
    operator picks source/destination servers + a library list.
    Returns:

      - which selected libraries have a saved operator-confirmed
        destination mapping (will route correctly);
      - which have an auto-suggested mapping (Replace-mode
        should warn - operator hasn't confirmed equivalence);
      - which have NO mapping at all (will be skipped by the
        engine when ignore_library_mapping is False);
      - whether cross-backend Replace mode would be refused by
        the job-handler guard.

    Same-server jobs short-circuit to a "no warnings"
    response - exact-name matching always wins for same-server
    snapshot/restore.

    Body shape:
      {
        "source_server_id": str,
        "dest_server_id":   str,
        "library_names":    [str, ...],
        "mode":             "merge" | "replace",
        "ignore_library_mapping": bool   (optional, defaults False),
      }
    """
    src_id = str(body.get("source_server_id") or "").strip()
    dst_id = str(body.get("dest_server_id") or "").strip()
    names_in = body.get("library_names") or []
    if not isinstance(names_in, list):
        names_in = []
    names = [str(n).strip() for n in names_in if str(n).strip()]
    mode = str(body.get("mode") or "merge").lower()
    ignore_mapping = bool(body.get("ignore_library_mapping") or False)
    # Per-run override map. When set, an entry for a source
    # library makes the preflight treat that library as
    # "overridden" (no warning) rather than consulting the saved
    # table - the same precedence the engine applies.
    # Empty-string values are operator-confirmed skips for this
    # run.
    overrides_in = body.get("library_mapping_overrides") or {}
    if not isinstance(overrides_in, dict):
        overrides_in = {}
    overrides = {
        str(k): str(v) if v is not None else ""
        for k, v in overrides_in.items()
    }

    out: Dict[str, Any] = {
        "same_server": False,
        "cross_backend_replace_refusal": None,
        "library_warnings": [],
        "any_unmapped": False,
    }

    if not src_id or not dst_id or not names:
        return out

    # Same-server short-circuit. No mapping consult needed; exact-
    # name match always wins.
    if src_id == dst_id:
        out["same_server"] = True
        return out

    # Look up service types for the cross-backend refusal check.
    from server import server_registry
    servers = server_registry.list_servers() or []
    src_row = next((r for r in servers if r.get("id") == src_id), None)
    dst_row = next((r for r in servers if r.get("id") == dst_id), None)
    src_svc = (src_row or {}).get("service_type") or "plex"
    dst_svc = (dst_row or {}).get("service_type") or "plex"

    # Same helper the job handler uses; gives the operator the
    # same error copy pre-submit.
    from server.jobs import _cross_backend_replace_requires_mappings
    refusal = _cross_backend_replace_requires_mappings(
        source_server_id=src_id,
        source_service_type=src_svc,
        dest_server_id=dst_id,
        dest_service_type=dst_svc,
        mode=mode,
        ignore_library_mapping=ignore_mapping,
    )
    out["cross_backend_replace_refusal"] = refusal

    # Per-library mapping status. When ignore_mapping is True the
    # warnings still surface (the operator should know what they're
    # overriding) but they're informational, not blocking.
    from server import library_mapping_db
    rows = library_mapping_db.list_mappings_for_pair(
        src_id, dst_id,
    ) or []
    # Index by source library name (case-insensitive).
    by_src_name = {
        (r.get("source_library_name") or "").strip().lower(): r
        for r in rows
    }
    # Pre-index overrides by source library name (case-
    # insensitive) for consistency with how the engine matches.
    overrides_lc = {k.lower(): v for k, v in overrides.items()}

    warnings = []
    for lib_name in names:
        key = lib_name.lower()
        # Per-run override consult fires FIRST (matches engine
        # precedence). When the operator has declared an override
        # for this library, the saved-table classification is
        # bypassed entirely.
        if key in overrides_lc:
            ov = overrides_lc[key]
            if ov == "":
                warnings.append({
                    "library": lib_name,
                    "status": "per_run_skip",
                    "detail": (
                        "Per-run override: this library is set to "
                        "skip for this run only. The shared "
                        "Library Mapping table is unchanged."
                    ),
                })
            else:
                # Per-run route. Not a warning per se, but surface
                # the routing decision so the operator sees what
                # the engine will do.
                warnings.append({
                    "library": lib_name,
                    "status": "per_run_route",
                    "detail": (
                        f"Per-run override: this run only, route "
                        f"to {ov!r}. The shared Library Mapping "
                        f"table is unchanged."
                    ),
                })
            continue
        row = by_src_name.get(key)
        if row is None:
            warnings.append({
                "library": lib_name,
                "status": "unmapped",
                "detail": (
                    "No saved mapping. The engine will fall back "
                    "to exact-name match on the destination; if "
                    "no library with this name exists on the "
                    "destination, this library will be skipped."
                ),
            })
            continue
        source = (row.get("source") or "").lower()
        dest_id = row.get("dest_library_id") or ""
        dest_name = row.get("dest_library_name") or ""
        if not dest_id:
            # Operator-confirmed skip sentinel.
            warnings.append({
                "library": lib_name,
                "status": "operator_skip",
                "detail": (
                    "This library is mapped as an explicit skip: "
                    "the engine won't write any data for it on "
                    "the destination. Clear the mapping under "
                    "Server Syncing > Library Mapping to re-enable."
                ),
            })
            continue
        if source == "operator":
            # Operator-confirmed mapping; no warning needed.
            continue
        # Auto-suggested mapping; warn on Replace because we
        # don't want destructive writes against an unconfirmed
        # equivalence.
        if mode == "replace":
            warnings.append({
                "library": lib_name,
                "status": "auto_unconfirmed",
                "detail": (
                    f"Auto-suggested mapping ({source}) → "
                    f"{dest_name!r}. Replace mode is destructive; "
                    f"confirm or override this mapping under "
                    f"Server Syncing > Library Mapping before "
                    f"running."
                ),
            })

    out["library_warnings"] = warnings
    # per_run_route + per_run_skip + auto_unconfirmed are NOT
    # "unmapped" — the engine has a clear instruction for each.
    # Only genuinely unmapped or library-mapping-table skips
    # count toward the any_unmapped banner color.
    out["any_unmapped"] = any(
        w["status"] in ("unmapped", "operator_skip")
        for w in warnings
    )
    return out


@router.post("/api/library-mapping/invalidate-auto")
def post_library_mapping_invalidate_auto(
    body: Dict[str, Any],
    _user: Dict[str, Any] = Depends(
        _auth_router_module.require_role("operator")
    ),
) -> Dict[str, Any]:
    """Bulk-drop ``source='auto'`` rows. ``source_server_id`` +
    ``dest_server_id`` filters narrow the wipe; both omitted wipes
    every auto row. Operator-confirmed rows are PRESERVED."""
    from server import library_mapping_db
    source_id = str(body.get("source_server_id") or "").strip() or None
    dest_id = str(body.get("dest_server_id") or "").strip() or None
    n = library_mapping_db.invalidate_auto_mappings(
        source_server_id=source_id, dest_server_id=dest_id,
    )
    return {"deleted": n}
