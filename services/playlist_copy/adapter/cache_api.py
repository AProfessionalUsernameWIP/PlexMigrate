"""Cache-aware playlist read / refresh surface.

Powers the Playlist Management UI: lists a user's playlists, reads one
playlist's items, and force-refreshes the local cache. Reads from
``server.playlist_cache_db`` first (when fresh per
``playlist_cache_refresh_interval_seconds``) and falls through to the
adapter's ``list_playlists`` / ``get_playlist_items`` on a cache miss
or a forced refresh, writing the result back into the cache.

Extracted from ``services/playlist_copy.py``; re-exported from there so
existing ``from services.playlist_copy import list_user_playlists``
(etc.) importers are unaffected. The module-level back-import of
``_connect`` and the exception classes from ``playlist_copy`` is safe
because ``playlist_copy`` triggers loading this module AFTER those
names are defined (the re-export line lives below ``_connect``).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from server import playlist_cache_db
from services.adapters import ItemRef, PlaylistSpec as AdapterPlaylistSpec
from services.tunables import (
    playlist_cache_enabled,
    playlist_cache_refresh_interval_seconds,
    playlist_cache_snapshot_threshold_seconds,
)
from services.playlist_copy.adapter.user_auth import (
    _find_user,
    _identity_kit,
    _resolve_app_user_uuid_for_lookup,
    _user_context_for,
)

# Same logger as playlist_copy on purpose: every log line below was
# emitted under "plexmigrate.services.playlist_copy" pre-extraction;
# keep the audit channel unchanged.
log = logging.getLogger("plexmigrate.services.playlist_copy")


# ── Public read API ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _ListResult:
    playlists: List[Dict[str, Any]]
    from_cache: bool
    fetched_at: float


def list_user_playlists(
    *,
    server_id: str,
    user_id: str,
    force_refresh: bool = False,
) -> _ListResult:
    """List a user's playlists. Reads from cache when fresh enough
    (``playlist_cache_refresh_interval_seconds`` tunable) unless the
    caller forces a refresh. Cache failures fall through to live."""
    if not server_id or not user_id:
        return _ListResult(playlists=[], from_cache=False, fetched_at=time.time())

    cache_on = playlist_cache_enabled()
    if cache_on and not force_refresh:
        try:
            interval = playlist_cache_refresh_interval_seconds()
            # Resolve the canonical app_user_uuid early so the cache
            # lookup matches by uuid even when ``user_id`` here is an
            # alias (backend_user_id vs username) that doesn't agree
            # with what the bulk refresh stored. Falls back to the
            # legacy user_id key when no uuid resolves.
            app_uuid = _resolve_app_user_uuid_for_lookup(server_id, user_id)
            marker = playlist_cache_db.get_refresh_marker(
                server_id, user_id, app_user_uuid=app_uuid,
            )
            if marker and not marker.get("error"):
                age = time.time() - float(marker["last_refreshed_at"])
                if age <= float(interval):
                    rows = playlist_cache_db.list_cached_playlists(
                        server_id, user_id, app_user_uuid=app_uuid,
                    )
                    return _ListResult(
                        playlists=rows,
                        from_cache=True,
                        fetched_at=float(marker["last_refreshed_at"]),
                    )
        except Exception:
            log.exception(
                "list_user_playlists cache read failed for (%s,%s); "
                "falling through to live.",
                server_id, user_id,
            )

    # Live fetch + cache write.
    return _live_list_and_cache(server_id, user_id)


def _live_list_and_cache(server_id: str, user_id: str) -> _ListResult:
    """Connect to ``server_id``, list playlists for ``user_id`` via
    the adapter, write the rosters to cache, return the result."""
    from services.playlist_copy import _connect, SourceUnreachable, PlaylistCopyError, DestUserNotFound  # local: cycle-break
    start = time.perf_counter()
    try:
        conn = _connect(server_id, on_fail=SourceUnreachable)
    except PlaylistCopyError as exc:
        # Connection failed before we could resolve a user_spec. Try a
        # best-effort uuid lookup so the error marker still carries the
        # canonical identity tag; falls back to untagged when no row.
        early_uuid = _resolve_app_user_uuid_for_lookup(server_id, user_id)
        try:
            playlist_cache_db.record_refresh(
                server_id=server_id, user_id=user_id,
                last_refresh_ms=int((time.perf_counter() - start) * 1000),
                error=str(exc),
                app_user_uuid=early_uuid,
            )
        except Exception:
            pass
        raise
    adapter = conn.adapter
    user_spec = _find_user(
        adapter, user_id, on_missing=DestUserNotFound,
        this_server_id=server_id,
    )
    # Pass server_id through so the per-user auth chain inside
    # _user_context_for activates. A bare call defaulting
    # server_id=None skips the saved-token + PIN-sign-in steps
    # entirely and silently falls back to admin auth, which returns
    # the admin's view, filtered to 0 rows for managed users.
    ctx = _user_context_for(conn, user_spec, server_id=server_id)
    # Identity / auth tags written onto every cache row this call
    # produces. Resolved once here so all per-playlist + the refresh
    # marker stay in sync (the canonical uuid + auth kind + role flags
    # let cache reads match by app_user_uuid regardless of which legacy
    # user_id alias the caller passed in).
    kit = _identity_kit(server_id, user_spec, ctx)
    try:
        specs: List[AdapterPlaylistSpec] = list(adapter.list_playlists(ctx) or [])
    except Exception as exc:
        try:
            playlist_cache_db.record_refresh(
                server_id=server_id, user_id=user_id,
                last_refresh_ms=int((time.perf_counter() - start) * 1000),
                error=f"list_playlists failed: {exc}",
                **kit,
            )
        except Exception:
            pass
        raise SourceUnreachable(f"list_playlists failed: {exc}") from exc

    out_rows: List[Dict[str, Any]] = []
    cache_uid = user_spec.backend_user_id or user_id
    cache_enabled = playlist_cache_enabled()
    # Wipe every existing row for this user before writing the fresh
    # set. Per-playlist upsert only overwrites matching playlist_ids,
    # so without this delete a stale row from an incorrectly-tagged
    # refresh would survive forever. Matches both the user_id key and
    # the canonical app_user_uuid key so rows written under either
    # cache-key scheme are cleared.
    if cache_enabled:
        try:
            playlist_cache_db.clear_user_cache(
                server_id=server_id,
                user_id=cache_uid,
                app_user_uuid=kit.get("app_user_uuid"),
            )
        except Exception:
            log.exception(
                "playlist_cache clear_user_cache failed for (%s,%s); "
                "stale rows may survive into the next read.",
                server_id, cache_uid,
            )
    for spec in specs:
        # Cache write per playlist; surface as the end user-facing
        # row regardless of cache outcome.
        items_for_cache = [
            {
                "title": ref.title,
                "guids": list(ref.guids),
                "type": "",
            }
            for ref in (spec.items or ())
        ]
        # Forward the adapter's spec.playlist_type / primary_library_*
        # into the cache so the Playlist Mgmt UI can group per-user
        # playlists by source library. Defensive getattr lets adapter
        # implementations that omit those attributes (test stubs)
        # degrade silently to empty values.
        pl_type = getattr(spec, "playlist_type", "") or ""
        primary_lib_id = getattr(spec, "primary_library_id", None)
        primary_lib_name = getattr(spec, "primary_library_name", None)
        if cache_enabled:
            try:
                playlist_cache_db.upsert_playlist(
                    server_id=server_id,
                    user_id=cache_uid,
                    playlist_id=spec.playlist_id,
                    name=spec.name,
                    is_smart=bool(spec.is_smart),
                    items=items_for_cache,
                    playlist_type=pl_type,
                    primary_library_id=primary_lib_id,
                    primary_library_name=primary_lib_name,
                    **kit,
                )
            except Exception:
                log.exception(
                    "playlist_cache upsert failed for (%s,%s,%s); continuing.",
                    server_id, cache_uid, spec.playlist_id,
                )
        out_rows.append({
            "playlist_id": spec.playlist_id,
            "name": spec.name,
            "is_smart": bool(spec.is_smart),
            "item_count": len(spec.items or ()),
            "fetched_at": time.time(),
            "playlist_type": pl_type,
            "primary_library_id": primary_lib_id,
            "primary_library_name": primary_lib_name,
        })

    refreshed_at = time.time()
    if cache_enabled:
        try:
            playlist_cache_db.record_refresh(
                server_id=server_id, user_id=cache_uid,
                last_refreshed_at=refreshed_at,
                last_refresh_ms=int((time.perf_counter() - start) * 1000),
                error=None,
                **kit,
            )
        except Exception:
            log.exception(
                "playlist_cache refresh marker write failed for (%s,%s); continuing.",
                server_id, cache_uid,
            )
    return _ListResult(playlists=out_rows, from_cache=False, fetched_at=refreshed_at)


def get_playlist_detail(
    *,
    server_id: str,
    user_id: str,
    playlist_id: str,
    force_refresh: bool = False,
) -> Dict[str, Any]:
    """Return one playlist's items. Cache-aware in the same way as
    :func:`list_user_playlists`. Returns a dict shape that maps
    directly onto :class:`server.models.PlaylistDetail`."""
    from services.playlist_copy import _connect, SourceUnreachable, DestUserNotFound, PlaylistNotFound  # local: cycle-break
    if not server_id or not user_id or not playlist_id:
        raise PlaylistNotFound("server_id, user_id, playlist_id are all required")

    cache_on = playlist_cache_enabled()
    if cache_on and not force_refresh:
        try:
            interval = playlist_cache_refresh_interval_seconds()
            app_uuid = _resolve_app_user_uuid_for_lookup(server_id, user_id)
            marker = playlist_cache_db.get_refresh_marker(
                server_id, user_id, app_user_uuid=app_uuid,
            )
            cached = playlist_cache_db.get_cached_playlist_items(
                server_id, user_id, playlist_id,
            )
            if cached and marker and not marker.get("error"):
                age = time.time() - float(marker["last_refreshed_at"])
                if age <= float(interval):
                    return {
                        "playlist_id": playlist_id,
                        "name": cached["name"],
                        "is_smart": cached["is_smart"],
                        "items": cached["items"],
                        "fetched_at": float(cached["fetched_at"]),
                        "from_cache": True,
                    }
        except Exception:
            log.exception(
                "get_playlist_detail cache read failed for (%s,%s,%s); "
                "falling through to live.",
                server_id, user_id, playlist_id,
            )

    # Live fetch.
    conn = _connect(server_id, on_fail=SourceUnreachable)
    adapter = conn.adapter
    user_spec = _find_user(
        adapter, user_id, on_missing=DestUserNotFound,
        this_server_id=server_id,
    )
    # Pass server_id so the per-user auth chain (saved token → PIN
    # sign-in → admin fallback) activates; without it the call
    # short-circuits to admin auth.
    ctx = _user_context_for(conn, user_spec, server_id=server_id)
    cache_uid = user_spec.backend_user_id or user_id
    kit = _identity_kit(server_id, user_spec, ctx)

    # Resolve the playlist's name / is_smart flag via list_playlists
    # (single source of truth on the adapter); items via the dedicated
    # get_playlist_items helper.
    name = ""
    is_smart = False
    try:
        specs = list(adapter.list_playlists(ctx) or [])
        match = next((s for s in specs if s.playlist_id == playlist_id), None)
    except Exception as exc:
        raise SourceUnreachable(f"list_playlists failed: {exc}") from exc
    if match is None:
        raise PlaylistNotFound(
            f"playlist {playlist_id!r} not found for user {user_id!r}"
        )
    name = match.name
    is_smart = bool(match.is_smart)

    try:
        items_tuple: Tuple[ItemRef, ...] = adapter.get_playlist_items(
            playlist_id, user_context=ctx,
        )
    except Exception as exc:
        raise SourceUnreachable(f"get_playlist_items failed: {exc}") from exc

    items_out = [
        {
            "title": ref.title,
            "guids": list(ref.guids),
            "type": "",
            "duration_ms": None,
        }
        for ref in (items_tuple or ())
    ]

    # Cache-write side effect.
    if playlist_cache_enabled():
        try:
            playlist_cache_db.upsert_playlist(
                server_id=server_id,
                user_id=cache_uid,
                playlist_id=playlist_id,
                name=name,
                is_smart=is_smart,
                items=items_out,
                **kit,
            )
        except Exception:
            log.exception("playlist_cache upsert failed; continuing.")

    return {
        "playlist_id": playlist_id,
        "name": name,
        "is_smart": is_smart,
        "items": items_out,
        "fetched_at": time.time(),
        "from_cache": False,
    }


# ── Public refresh API ──────────────────────────────────────────────────────


def refresh_user_cache(
    *,
    server_id: str,
    user_id: str,
) -> Dict[str, Any]:
    """Force a live re-fetch + cache rewrite for one user. Returns a
    dict shape matching :class:`server.models.PlaylistCacheRefreshResult`."""
    from services.playlist_copy import PlaylistCopyError  # local: cycle-break
    from services.playlist_copy import log as playlist_cache_log
    start = time.perf_counter()
    try:
        result = _live_list_and_cache(server_id, user_id)
    except PlaylistCopyError as exc:
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        playlist_cache_log.log_user_refresh(
            server_id=server_id, user_id=user_id, ok=False,
            elapsed_ms=elapsed_ms, error=f"{exc.code}: {exc}",
        )
        return {
            "server_id": server_id,
            "user_id": user_id,
            "refreshed_at": time.time(),
            "playlists_count": 0,
            "items_count": 0,
            "duration_ms": elapsed_ms,
            "error": f"{exc.code}: {exc}",
        }
    items_count = sum(int(r.get("item_count") or 0) for r in result.playlists)
    elapsed_ms = int((time.perf_counter() - start) * 1000)
    playlist_cache_log.log_user_refresh(
        server_id=server_id, user_id=user_id, ok=True,
        elapsed_ms=elapsed_ms,
        playlists=len(result.playlists), items=items_count,
    )
    return {
        "server_id": server_id,
        "user_id": user_id,
        "refreshed_at": result.fetched_at,
        "playlists_count": len(result.playlists),
        "items_count": items_count,
        "duration_ms": elapsed_ms,
        "error": None,
    }


def refresh_server_cache(
    *,
    server_id: str,
    source: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Force-refresh every user on ``server_id``. Returns a list of
    :class:`PlaylistCacheRefreshResult`-shaped dicts (one per user)
    plus a final aggregate row with ``user_id=None``.

    ``source`` is an optional free-form tag for the audit log (e.g.
    ``"servers-refresh"`` or ``"playlist-mgmt-panel"``) so the end user
    can tell which UI surface triggered each entry."""
    from services.playlist_copy import _connect, SourceUnreachable, PlaylistCopyError  # local: cycle-break
    from services.playlist_copy import log as playlist_cache_log
    try:
        conn = _connect(server_id, on_fail=SourceUnreachable)
    except PlaylistCopyError as exc:
        playlist_cache_log.log_bulk_refresh_end(
            server_id=server_id, ok=0, errors=1, elapsed_s=0.0, source=source,
        )
        return [{
            "server_id": server_id, "user_id": None,
            "refreshed_at": time.time(),
            "playlists_count": 0, "items_count": 0, "duration_ms": 0,
            "error": f"{exc.code}: {exc}",
        }]
    try:
        users = conn.adapter.list_users() or []
    except Exception as exc:
        playlist_cache_log.log_bulk_refresh_end(
            server_id=server_id, ok=0, errors=1, elapsed_s=0.0, source=source,
        )
        return [{
            "server_id": server_id, "user_id": None,
            "refreshed_at": time.time(),
            "playlists_count": 0, "items_count": 0, "duration_ms": 0,
            "error": f"list_users failed: {exc}",
        }]

    playlist_cache_log.log_bulk_refresh_start(
        server_id=server_id, user_count=len(users), source=source,
    )
    out: List[Dict[str, Any]] = []
    total_playlists = 0
    total_items = 0
    error_count = 0
    aggregate_start = time.perf_counter()
    for user in users:
        uid = user.backend_user_id or user.username
        if not uid:
            continue
        row = refresh_user_cache(server_id=server_id, user_id=uid)
        out.append(row)
        total_playlists += int(row.get("playlists_count") or 0)
        total_items += int(row.get("items_count") or 0)
        if row.get("error"):
            error_count += 1
    elapsed_s = time.perf_counter() - aggregate_start
    playlist_cache_log.log_bulk_refresh_end(
        server_id=server_id,
        ok=len(out) - error_count,
        errors=error_count,
        elapsed_s=elapsed_s,
        source=source,
    )
    out.append({
        "server_id": server_id, "user_id": None,
        "refreshed_at": time.time(),
        "playlists_count": total_playlists,
        "items_count": total_items,
        "duration_ms": int(elapsed_s * 1000),
        "error": None,
    })
    return out


# ── Cache freshness helper (for snapshot path integration) ──────────────────


def snapshot_should_use_cache(
    server_id: str,
    user_id: str,
    *,
    app_user_uuid: Optional[str] = None,
) -> bool:
    """Return True if the snapshot path should consult the playlist
    cache instead of hitting the live API. Reads
    ``playlist_cache_snapshot_threshold_seconds`` and short-circuits to
    False when the cache is disabled or the marker is stale / errored.

    Lives here (not in ``playlist_cache_db``) because the threshold +
    enabled check are tunable-side concerns, not DB-side concerns."""
    if not playlist_cache_enabled():
        return False
    try:
        threshold = playlist_cache_snapshot_threshold_seconds()
    except Exception:
        return False
    return playlist_cache_db.is_fresh(
        server_id, user_id, threshold_seconds=float(threshold),
        app_user_uuid=app_user_uuid,
    )
