"""
Backend-agnostic snapshot engine. Used for Jellyfin / Emby sources;
Plex still routes through ``services/snapshotter.py`` to keep its
perf-tuned plexapi-specific code paths untouched (deliberate two-engine
split documented in Plan[MULTI-BACKEND]-2026-05-15.md section 11.5).

Scope (MVP for PR-Backends Phase 1)
-----------------------------------
- Per-library watch_history + ratings capture, owner-phase only.
- Output payload shape matches what
  ``services/snapshotter.py:snapshot_library`` emits so downstream
  consumers (snapshot.db writer, restore engine) don't branch on
  backend.
- Per-user fan-out (home-user equivalent), playlist + collection
  capture, advanced perf strategies (bulk vs server-side filter) are
  out of scope for this initial cut and follow in PR-CrossPolish.

Flow (single library)
---------------------
1. Caller resolves the destination ``ServerConnection`` (see
   ``server/server_registry.py:connect_registered_server``).
2. ``snapshot_library_adapter(connection, library_id, ...)`` iterates
   items via ``connection.adapter.iter_items(library_id, ...)``.
3. For each ``ItemSnapshot`` returned, the helper writes:
     * one ``items`` row (upserted by canonical GUID set)
     * one ``server_items`` row (server-local backend_item_id ->
       items.id mapping)
     * one ``watch_events`` row (per-user view count + offset +
       last_viewed_at) when ``view_count > 0``
     * one ``ratings`` row when ``user_rating > 0``
4. Output: a per-library dict matching the legacy ``snapshot_library``
   shape (consumed unchanged by ``server/snapshot_serializer.py``).

DB section_key handling
-----------------------
v0.15 invariant requires ``section_key > 0`` (positive int) on every
per-server row. Jellyfin / Emby library ids are GUID strings; SQLite
type affinity stores them in INTEGER columns without complaint, but
the Python-level ``record_watch_event`` enforcement currently checks
``isinstance(section_key, int)``. The adapter engine hashes the
GUID library_id to a stable positive int (CRC32, masked to 31 bits)
for the section_key fields. The ``library_sections`` table receives
the same hash as section_key and the GUID as section_title so the
original is preserved.
"""

from __future__ import annotations

import logging
import threading
import time
import zlib
from typing import Any, Dict, List, Optional

from services.adapters import (
    ItemSnapshot,
    MediaServerAdapter,
    UserContext,
    item_snapshot_to_engine_dict,
)


log = logging.getLogger("plexmigrate.services.snapshotter_adapter")


def stable_section_key(library_id: str) -> int:
    """Convert a GUID-or-numeric library identifier to a stable
    positive 31-bit int suitable for the v0.15 ``section_key`` slot.

    Numeric library ids (Plex section keys passed through to this
    engine for cross-backend dry-runs) round-trip unchanged. GUID
    strings hash via CRC32; the mask drops the sign bit so the
    invariant ``section_key > 0`` holds. Collision probability is
    negligible at typical registry scale (a few dozen libraries
    per server)."""
    s = str(library_id or "")
    if not s:
        return 0
    if s.lstrip("-").isdigit():
        try:
            v = int(s)
            if v > 0:
                return v
        except ValueError:
            pass
    return (zlib.crc32(s.encode("utf-8")) & 0x7FFFFFFF) or 1


def snapshot_library_adapter(
    adapter: MediaServerAdapter,
    *,
    library_id: str,
    library_name: str,
    library_type: str,
    server_id: str,
    user_context: UserContext,
    include_watch_history: bool = True,
    include_ratings: bool = True,
    # PR-CrossPolish (Phase 2): include playlists / collections in
    # the captured payload. Defaults match the engine API:
    # ``True`` so legacy callers don't lose data; explicit ``False``
    # turns each off.
    include_playlists: bool = True,
    include_collections: bool = True,
    logger: Optional[logging.Logger] = None,
    stop_event: Optional[threading.Event] = None,
) -> Dict[str, Any]:
    """Capture one library's per-user state via the adapter.

    ``server_id`` is the registry id of the source server; required
    for the media.db writes. ``user_context`` identifies which user's
    state to snapshot (owner-phase only in this MVP).

    Returns the per-library payload dict in the shape
    ``services/snapshotter.py:snapshot_library`` emits, so the
    snapshot.db writer doesn't need a non-Plex branch:

    {
      "library":              <library_name>,
      "library_section_id":   <stable_section_key>,
      "library_section_type": <library_type>,
      "captured_at":          <iso>,
      "watch_history":        [serialize_item dicts],
      "ratings":              [serialize_item dicts],
      "playlists":            [],   # deferred to follow-up
      "collections":          [],   # deferred to follow-up
      "users":                {},   # deferred (owner-phase only here)
    }
    """
    log_ = logger or log
    section_key = stable_section_key(library_id)

    log_.info(
        "[%s] snapshot start (adapter=%s, library_id=%s, section_key=%d)",
        library_name, adapter.backend, library_id, section_key,
    )

    watch_history: List[Dict[str, Any]] = []
    ratings: List[Dict[str, Any]] = []

    # Determine which read passes we actually need. The adapter
    # supports include_watched_only + include_rated_only as
    # server-side filters; we run them as separate passes so the
    # snapshot dict's watch_history and ratings lists are
    # independently populated (matches Plex snapshotter's contract).
    user_label = user_context.username or "Owner"

    if include_watch_history:
        watch_history = _capture_watch_history(
            adapter, library_id, library_name, user_context,
            user_label, stop_event, log_,
        )

    if include_ratings:
        ratings = _capture_ratings(
            adapter, library_id, library_name, user_context,
            user_label, stop_event, log_,
        )

    playlists: List[Dict[str, Any]] = []
    collections: List[Dict[str, Any]] = []
    if include_playlists:
        playlists = _capture_playlists(
            adapter, library_name, user_context, user_label, log_,
        )
    if include_collections:
        collections = _capture_collections(
            adapter, library_id, library_name, log_,
        )

    # Stamp media.db with what we captured. Best-effort: the legacy
    # JSON output is still produced even if the DB write fails (the
    # end user can re-run later to backfill).
    try:
        _ingest_to_media_db(
            server_id=server_id,
            library_id=library_id,
            library_name=library_name,
            library_type=library_type,
            section_key=section_key,
            user_context=user_context,
            watch_history=watch_history,
            ratings=ratings,
            logger=log_,
        )
    except Exception:
        log_.exception(
            "[%s] media.db ingest failed; JSON payload still produced.",
            library_name,
        )

    return {
        "library": library_name,
        "library_section_id": section_key,
        "library_section_type": library_type,
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "watch_history": watch_history,
        "ratings": ratings,
        "playlists": playlists,
        "collections": collections,
        # Per-user fan-out (Plex Home user equivalent) is the
        # next-up PR-CrossPolish item; owner-only for now.
        "users": {},
    }


def _capture_watch_history(
    adapter: MediaServerAdapter,
    library_id: str,
    library_name: str,
    user_context: UserContext,
    user_label: str,
    stop_event: Optional[threading.Event],
    logger: logging.Logger,
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    try:
        for snap in adapter.iter_items(
            library_id,
            include_watched_only=True,
            user_context=user_context,
        ):
            if stop_event is not None and stop_event.is_set():
                logger.info(
                    "[%s] watch-history: stop requested after %d items.",
                    library_name, len(out),
                )
                break
            if not isinstance(snap, ItemSnapshot):
                continue
            if (snap.view_count or 0) <= 0:
                # Adapter's filter wasn't authoritative; double-check
                # because snapshot rows must reflect "actually watched."
                continue
            out.append(item_snapshot_to_engine_dict(snap, user=user_label))
    except Exception as exc:
        logger.error(
            "[%s] watch-history capture failed: %s", library_name, exc,
        )
    logger.info(
        "[%s] watch-history captured %d row(s) (user=%s)",
        library_name, len(out), user_label,
    )
    return out


def _capture_ratings(
    adapter: MediaServerAdapter,
    library_id: str,
    library_name: str,
    user_context: UserContext,
    user_label: str,
    stop_event: Optional[threading.Event],
    logger: logging.Logger,
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    try:
        for snap in adapter.iter_items(
            library_id,
            include_rated_only=True,
            user_context=user_context,
        ):
            if stop_event is not None and stop_event.is_set():
                logger.info(
                    "[%s] ratings: stop requested after %d items.",
                    library_name, len(out),
                )
                break
            if not isinstance(snap, ItemSnapshot):
                continue
            rating = snap.user_rating
            if rating is None or rating <= 0:
                continue
            out.append(item_snapshot_to_engine_dict(snap, user=user_label))
    except Exception as exc:
        logger.error(
            "[%s] ratings capture failed: %s", library_name, exc,
        )
    logger.info(
        "[%s] ratings captured %d row(s) (user=%s)",
        library_name, len(out), user_label,
    )
    return out


def _capture_playlists(
    adapter: MediaServerAdapter,
    library_name: str,
    user_context: UserContext,
    user_label: str,
    logger: logging.Logger,
) -> List[Dict[str, Any]]:
    """Capture every playlist visible to the user_context.

    Playlists in Jellyfin / Emby are server-wide (not library-scoped
    like Plex section.collections()). The MVP captures every playlist
    the user owns; cross-library filtering is the end user's
    responsibility at restore time via the library_metrics map."""
    out: List[Dict[str, Any]] = []
    try:
        specs = adapter.list_playlists(user_context)
    except Exception as exc:
        logger.warning("playlists capture failed: %s", exc)
        return out
    for spec in specs or []:
        if spec.is_smart:
            # Smart playlists' criteria don't port across backends
            # (and often not across same-backend installs either).
            # The dict carries ``smart_filter_json`` for end user
            # reference; the restore engine logs + skips smart rows.
            logger.info(
                "playlist %r is smart; criteria preserved but not restored.",
                spec.name,
            )
        out.append({
            "name": spec.name,
            "is_smart": spec.is_smart,
            "smart_filter_json": spec.smart_filter_json or None,
            "user": user_label,
            "items": [
                {
                    "title": ref.title,
                    "guids": list(ref.guids),
                    "rating_key": ref.backend_item_id,
                }
                for ref in (spec.items or ())
            ],
        })
    logger.info(
        "playlists captured %d (user=%s, library_scope_hint=%s)",
        len(out), user_label, library_name,
    )
    return out


def _capture_collections(
    adapter: MediaServerAdapter,
    library_id: str,
    library_name: str,
    logger: logging.Logger,
) -> List[Dict[str, Any]]:
    """Capture this library's collections (BoxSets on Jellyfin / Emby).

    Plex collections are library-scoped; Jellyfin / Emby BoxSets are
    server-wide. The adapter's list_collections call accepts an
    optional library_id filter; for Plex the adapter returns the
    library's collections only, for Jellyfin / Emby it returns every
    BoxSet on the server (since they're not really scoped). The
    restore engine applies D-COL-SCOPE (library-prefixed name) on
    cross-scope writes to avoid same-name merges.

    ``source_library`` is preserved on each row so the restore engine
    knows what scope the source collection belonged to.
    """
    out: List[Dict[str, Any]] = []
    try:
        specs = adapter.list_collections(library_id=library_id)
    except Exception as exc:
        logger.warning("collections capture failed: %s", exc)
        return out
    for spec in specs or []:
        out.append({
            "name": spec.name,
            "source_library": library_name,
            "source_library_id": library_id,
            "library_id": spec.library_id,   # may be None for server-wide BoxSets
            "items": [
                {
                    "title": ref.title,
                    "guids": list(ref.guids),
                    "rating_key": ref.backend_item_id,
                }
                for ref in (spec.items or ())
            ],
        })
    logger.info(
        "[%s] collections captured %d", library_name, len(out),
    )
    return out


def _ingest_to_media_db(
    *,
    server_id: str,
    library_id: str,
    library_name: str,
    library_type: str,
    section_key: int,
    user_context: UserContext,
    watch_history: List[Dict[str, Any]],
    ratings: List[Dict[str, Any]],
    logger: logging.Logger,
) -> None:
    """Persist captured rows into media.db. Mirrors the Plex
    snapshotter's DB-write contract so the resolver's Tier-0 (DB GUID
    lookup) gets populated for cross-server matching."""
    from server import media_db

    # Ensure the library_sections anchor row exists. ``library_id`` is
    # GUID-form for Jellyfin / Emby; we stash it in section_title to
    # preserve the original identity, while ``section_key`` is the
    # stable hashed int that satisfies the v0.15 invariant.
    try:
        media_db.upsert_library_section(
            server_id=server_id,
            section_key=section_key,
            section_title=library_name or library_id,
            section_type=library_type or "",
        )
    except AttributeError:
        # Older media_db revisions don't expose upsert_library_section
        # as a public helper - fall through to the SQL path used by
        # the Plex engine.
        try:
            conn = media_db._require_conn()
            now = time.time()
            with media_db._DB_LOCK:
                conn.execute(
                    """
                    INSERT INTO library_sections (
                        server_id, section_key, section_title, section_type,
                        first_seen_at, last_seen_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(server_id, section_key) DO UPDATE SET
                        section_title = excluded.section_title,
                        section_type  = excluded.section_type,
                        last_seen_at  = excluded.last_seen_at
                    """,
                    (
                        server_id, section_key,
                        library_name or library_id, library_type or "",
                        now, now,
                    ),
                )
        except Exception:
            logger.exception(
                "[%s] failed to upsert library_sections row.", library_name,
            )

    # Watch events.
    for row in watch_history:
        try:
            item_id = media_db.upsert_item(
                guids=list(row.get("guids") or []),
                title=str(row.get("title") or ""),
                media_type=str(row.get("type") or ""),
                year=row.get("year") if isinstance(row.get("year"), int) else None,
                filepath_suffix=None,
            )
            # Map the source's backend_item_id -> items.id so the
            # resolver Tier-0 cache works.
            try:
                media_db.upsert_server_item(
                    item_id=item_id,
                    server_id=server_id,
                    rating_key=str(row.get("rating_key") or ""),
                    section_key=section_key,
                )
            except Exception:
                pass
            media_db.record_watch_event(
                item_id=item_id,
                server_id=server_id,
                user_handle=user_context.username or "",
                view_count=int(row.get("view_count") or 0),
                section_key=section_key,
                view_offset=int(row.get("view_offset") or 0),
                last_viewed_at=row.get("last_viewed_at"),
                role="owner" if user_context.is_admin else "managed",
                display_name=user_context.username or None,
                backend_user_id=user_context.backend_user_id or None,
            )
        except Exception as exc:
            logger.warning(
                "[%s] watch_event write failed for %r: %s",
                library_name, row.get("title"), exc,
            )

    # Ratings.
    for row in ratings:
        rating_val = row.get("user_rating")
        if rating_val is None:
            continue
        try:
            item_id = media_db.upsert_item(
                guids=list(row.get("guids") or []),
                title=str(row.get("title") or ""),
                media_type=str(row.get("type") or ""),
                year=row.get("year") if isinstance(row.get("year"), int) else None,
                filepath_suffix=None,
            )
            media_db.upsert_rating(
                item_id=item_id,
                server_id=server_id,
                user_handle=user_context.username or "",
                rating=float(rating_val),
                section_key=section_key,
            )
        except Exception as exc:
            logger.warning(
                "[%s] rating write failed for %r: %s",
                library_name, row.get("title"), exc,
            )


def run_snapshot_adapter(
    *,
    connection: Any,
    library_names: Optional[List[str]] = None,
    server_id: str = "",
    include_watch_history: bool = True,
    include_ratings: bool = True,
    include_playlists: bool = True,
    include_collections: bool = True,
    # PR-Phase-3: per-user fan-out. When True (default), after the
    # owner-phase capture finishes for each library, the engine
    # iterates the destination's managed users and captures a
    # parallel per-user payload into the library's ``users`` dict.
    # ``user_filter`` narrows to a specific subset of usernames; None
    # means "every managed user the adapter reports". Owner is always
    # captured in the dedicated owner-phase pass above and never
    # appears in the ``users`` dict.
    include_managed_users: bool = True,
    user_filter: Optional[List[str]] = None,
    logger: Optional[logging.Logger] = None,
    stop_event: Optional[threading.Event] = None,
) -> Dict[str, Any]:
    """Top-level adapter-driven snapshot.

    ``connection`` is a :class:`server.server_registry.ServerConnection`
    for the source server (Jellyfin / Emby). Picks each library the
    end user selected, calls :func:`snapshot_library_adapter` per
    library, and combines results into the per-server payload shape
    that ``server/snapshot_serializer.py`` consumes.

    Per-user fan-out (Phase 3): after the owner-phase capture for
    each library, every managed user gets a parallel capture pass
    against the same library. For Jellyfin / Emby the admin token
    can read any user's UserData via ``/Users/{userId}/Items`` so we
    don't need per-user authentication - we just thread a different
    :class:`UserContext` per pass.

    Returns a dict shaped like::

        {
          "snapshot_meta": {
            "backend": <plex|jellyfin|emby>,
            "server_id": ..., "server_name": ...,
            "captured_at": <iso>,
            "libraries": [<lib name>, ...],
            "users_captured": [<managed username>, ...],
          },
          "libraries": [<per-library dicts; each carries a 'users' map>],
        }
    """
    log_ = logger or log
    adapter = connection.adapter
    service_type = getattr(connection, "service_type", "plex")
    identity = adapter.server_identity()
    admin_token = getattr(connection, "token", "") or ""

    # Build the owner-phase UserContext.
    from services.adapters import UserContext as _UserContext
    owner_ctx = _UserContext(
        backend_user_id=identity.owner_user_id or "",
        username=identity.owner_display or "Owner",
        auth_token=admin_token,
        is_admin=True,
    )

    all_libraries = adapter.list_libraries()
    if library_names:
        wanted = {n.lower() for n in library_names if n}
        target_libraries = [
            lib for lib in all_libraries if lib.name.lower() in wanted
        ]
    else:
        target_libraries = list(all_libraries)

    # Resolve managed users once up front. Filter to the end user's
    # selection when supplied. Owner is excluded from the per-user
    # fan-out (already captured in owner-phase). Empty list disables
    # the fan-out for this run; falsy include_managed_users flag does
    # the same.
    managed_user_contexts: List[Any] = []
    if include_managed_users:
        managed_user_contexts = _resolve_managed_user_contexts(
            adapter, admin_token, user_filter, logger=log_,
        )

    log_.info(
        "run_snapshot_adapter: %d/%d libraries selected on %s (%s); "
        "%d managed users in scope.",
        len(target_libraries), len(all_libraries),
        identity.name or "?", service_type,
        len(managed_user_contexts),
    )

    per_library: List[Dict[str, Any]] = []
    for lib in target_libraries:
        if stop_event is not None and stop_event.is_set():
            log_.info("run_snapshot_adapter: stop requested; halting.")
            break
        lib_payload = snapshot_library_adapter(
            adapter,
            library_id=lib.library_id,
            library_name=lib.name,
            library_type=lib.type,
            server_id=server_id or identity.machine_id,
            user_context=owner_ctx,
            include_watch_history=include_watch_history,
            include_ratings=include_ratings,
            include_playlists=include_playlists,
            include_collections=include_collections,
            logger=log_,
            stop_event=stop_event,
        )
        # Per-user fan-out for this library. The library payload's
        # ``users`` dict gets one entry per managed user captured.
        if managed_user_contexts:
            per_user_block: Dict[str, Dict[str, Any]] = {}
            for ctx in managed_user_contexts:
                if stop_event is not None and stop_event.is_set():
                    break
                per_user_block[ctx.username] = _capture_per_user_block(
                    adapter,
                    library_id=lib.library_id,
                    library_name=lib.name,
                    library_type=lib.type,
                    server_id=server_id or identity.machine_id,
                    user_context=ctx,
                    section_key=stable_section_key(lib.library_id),
                    include_watch_history=include_watch_history,
                    include_ratings=include_ratings,
                    logger=log_,
                    stop_event=stop_event,
                )
            lib_payload["users"] = per_user_block
        per_library.append(lib_payload)

    return {
        "snapshot_meta": {
            "backend": service_type,
            "server_id": server_id or identity.machine_id,
            "server_name": identity.name,
            "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "libraries": [lib.name for lib in target_libraries],
            "users_captured": [c.username for c in managed_user_contexts],
        },
        "libraries": per_library,
    }


def _resolve_managed_user_contexts(
    adapter: MediaServerAdapter,
    admin_token: str,
    user_filter: Optional[List[str]],
    *,
    logger: logging.Logger,
) -> List[Any]:
    """Enumerate the source's managed users + return one
    :class:`UserContext` per user that passes the optional filter.
    The admin token is reused for each context because Jellyfin /
    Emby admin tokens can read any user's UserData via
    ``/Users/{userId}/Items`` - no per-user authentication required.

    Owner / admin rows are skipped (already captured in the
    owner-phase pass)."""
    from services.adapters import UserContext as _UserContext
    try:
        users = adapter.list_users()
    except Exception as exc:
        logger.warning("list_users() failed during fan-out resolve: %s", exc)
        return []
    filter_set: Optional[set] = None
    if user_filter is not None:
        filter_set = {u.strip() for u in user_filter if isinstance(u, str) and u.strip()}
    contexts: List[Any] = []
    for user in users or []:
        if user.role == "owner" or user.is_admin:
            continue
        if filter_set is not None and user.username not in filter_set:
            continue
        if not user.backend_user_id:
            logger.debug(
                "managed user %r has no backend_user_id; skipping fan-out.",
                user.username,
            )
            continue
        contexts.append(_UserContext(
            backend_user_id=user.backend_user_id,
            username=user.username,
            auth_token=admin_token,
            is_admin=False,
        ))
    return contexts


def _capture_per_user_block(
    adapter: MediaServerAdapter,
    *,
    library_id: str,
    library_name: str,
    library_type: str,
    server_id: str,
    user_context: Any,
    section_key: int,
    include_watch_history: bool,
    include_ratings: bool,
    logger: logging.Logger,
    stop_event: Optional[threading.Event] = None,
) -> Dict[str, Any]:
    """Per-user capture for one (library, user) pair. Returns the
    ``{watch_history, ratings, playlists, collections}`` dict the
    library payload's ``users[username]`` entry expects.

    Playlists + collections are not captured per-user here because
    Jellyfin / Emby playlists are owned by the creating user and
    BoxSets are server-wide; we capture them once during the
    owner-phase and let the restore engine attribute them. Watch
    history and ratings ARE per-user."""
    watch_history: List[Dict[str, Any]] = []
    ratings: List[Dict[str, Any]] = []
    if include_watch_history:
        watch_history = _capture_watch_history(
            adapter, library_id, library_name, user_context,
            user_context.username, stop_event, logger,
        )
    if include_ratings:
        ratings = _capture_ratings(
            adapter, library_id, library_name, user_context,
            user_context.username, stop_event, logger,
        )
    # Per-user media.db ingest. The owner-phase already wrote the
    # items + library_sections rows; per-user we only add the
    # watch_events + ratings rows attributed to this user.
    if watch_history or ratings:
        try:
            _ingest_to_media_db(
                server_id=server_id,
                library_id=library_id,
                library_name=library_name,
                library_type=library_type,
                section_key=section_key,
                user_context=user_context,
                watch_history=watch_history,
                ratings=ratings,
                logger=logger,
            )
        except Exception:
            logger.exception(
                "[%s] media.db per-user ingest failed for %r",
                library_name, user_context.username,
            )
    return {
        "watch_history": watch_history,
        "ratings": ratings,
        "playlists": [],
        "collections": [],
    }


__all__ = [
    "snapshot_library_adapter",
    "run_snapshot_adapter",
    "stable_section_key",
]
