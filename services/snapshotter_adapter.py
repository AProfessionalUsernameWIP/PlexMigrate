"""
Backend-agnostic snapshot engine. Used for Jellyfin / Emby sources;
Plex still routes through ``services/snapshotter.py`` to keep its
perf-tuned plexapi-specific code paths untouched (a deliberate
two-engine split).

Known follow-ups:

  * **Bulk-walk consolidation.** Today ``_capture_watch_history`` and
    ``_capture_ratings`` each call ``adapter.iter_items()`` separately
    even though both could share a single full-library walk per
    (user, library). The response carries ``UserData.PlayCount`` AND
    ``UserData.Rating`` AND ``UserData.IsFavorite`` in one go; we just
    iterate it twice. Halving the walk count (2 -> 1 per user per
    library) would meaningfully reduce snapshot time on large servers.
    Pattern: introduce a ``_capture_user_state_bulk`` that walks the
    library once and yields both watch_history + ratings tuples.

  * **Episode parent-context GUIDs.** Emby episodes often carry TVDB
    or TMDB IDs on the SERIES, not the episode itself. The current
    GUID extraction in ``_jellyfin_item_to_snapshot`` reads only the
    episode's ProviderIds. Cross-server episode matching degrades
    when only one side has provider IDs. The fix is to also fetch
    the parent Series item (one extra call per series, cacheable
    per run) and emit the series GUIDs alongside the episode's
    show_title for the matcher. Out of scope for this pass.

  * **Backend version awareness.** Emby vs Jellyfin diverge on
    occasional response shapes; specific Emby releases also change
    UserData field semantics. The adapter has no version
    introspection. A future hardening pass should record the
    server's reported version at server_identity() time and gate
    quirk-fixes on it.


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
from services.backend_translation import affinity_row_is_meaningful


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

    # Dashboard wiring: the adapter
    # path had ZERO state.get_dashboard() calls, which is why the
    # Run dashboard's per-library card, progress bars, run totals,
    # and activity feed all stayed empty during Emby / Jellyfin
    # snapshot runs. The Plex path emits these events throughout
    # ``services/snapshotter.py``; this block mirrors the same
    # contract so the dashboard updates identically on both engines.
    from services import state as _state
    _dash = _state.get_dashboard()
    if _dash is not None:
        _dash.set_library_phase(library_name, "Capturing snapshot…")

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
        if _dash is not None:
            n = len(watch_history)
            if n:
                _dash.add_batch_total("watch", n)
                _dash.add_run_total(n)
                for _ in range(n):
                    _dash.inc_watch()
            _dash.set_library_phase(library_name, "Watch History ✓")
            _dash.advance_library(library_name)

    if include_ratings:
        ratings = _capture_ratings(
            adapter, library_id, library_name, user_context,
            user_label, stop_event, log_,
        )
        if _dash is not None:
            n = len(ratings)
            if n:
                _dash.add_batch_total("rating", n)
                _dash.add_run_total(n)
                for _ in range(n):
                    _dash.inc_rating()
            _dash.set_library_phase(library_name, "Ratings ✓")
            _dash.advance_library(library_name)

    playlists: List[Dict[str, Any]] = []
    collections: List[Dict[str, Any]] = []
    if include_playlists:
        playlists = _capture_playlists(
            adapter, library_name, user_context, user_label, log_,
        )
        if _dash is not None:
            n = len(playlists)
            if n:
                _dash.add_batch_total("playlist", n)
                _dash.add_run_total(n)
                for _ in range(n):
                    _dash.inc_playlist()
            _dash.set_library_phase(library_name, "Playlists ✓")
            _dash.advance_library(library_name)
    if include_collections:
        collections = _capture_collections(
            adapter, library_id, library_name, log_,
        )
        if _dash is not None:
            n = len(collections)
            if n:
                _dash.add_batch_total("collection", n)
                _dash.add_run_total(n)
                for _ in range(n):
                    _dash.inc_collection()
            _dash.set_library_phase(library_name, "Collections ✓")
            _dash.advance_library(library_name)

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
    # Diagnostic counters. Original goal was to surface
    # whether 0 captured rows meant "API returned nothing" vs "API
    # returned items but none had PlayCount > 0." A later iteration
    # also tracks ``max_view_count`` seen across the
    # full walk so we can tell apart "user genuinely has 0 plays on
    # this server" (max=0 across 1000s of items) from "API is leaking
    # the wrong user's UserData" (some items show >0 if the request
    # is actually scoped to the URL user).
    total_seen = 0
    skipped_no_view_count = 0
    max_view_count_seen = 0
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
            total_seen += 1
            vc = int(snap.view_count or 0)
            if vc > max_view_count_seen:
                max_view_count_seen = vc
            if vc <= 0:
                # Client-side filter: snapshot rows must reflect
                # "actually watched." On Jellyfin/Emby this is the
                # ONLY filter; we walk the full
                # library and pick out rows with PlayCount > 0.
                #
                # KNOWN LIMITATION (SNAP-04, operator-confirmed
                # 2026-05-21, by design): an item with a resume offset
                # but zero completed plays is intentionally NOT
                # captured. "Watch history" here means completed plays
                # only; in-progress / resume-point fidelity is out of
                # scope for the snapshot.
                skipped_no_view_count += 1
                continue
            out.append(item_snapshot_to_engine_dict(snap, user=user_label))
            # Parity with Plex media.log:
            # emit one line per captured item to the per-run
            # ``media.log`` surface. The Plex snapshot path does this
            # via ``state._media_logger.debug(_fmt_media_line(...))``;
            # the adapter path was silent, leaving the operator with
            # only summary counters to verify capture. Including the
            # hierarchy fields lets the operator tail media.log and
            # watch episodes / tracks stream by with full show /
            # season / artist context.
            try:
                from services import state as _state
                from services.logging_ops import _fmt_media_line
                if getattr(_state, "_media_logger", None) is not None:
                    _state._media_logger.debug(_fmt_media_line(
                        "EXPORT", library_name, snap.type,
                        snap.title or "(no title)",
                        user=user_label,
                        plays=vc,
                        show=snap.show_title or None,
                        season=snap.season_index,
                        episode=snap.episode_index,
                        artist=snap.artist or None,
                        album=snap.album or None,
                        path=snap.file_path or None,
                    ))
            except Exception:
                # Media-log emit is observability only. A failure here
                # must not break the capture loop.
                pass
    except Exception as exc:
        # Do NOT swallow the error and return the partial list: a
        # snapshot that silently drops part of a library's watch
        # history is indistinguishable from a clean one, and if used
        # as a Replace-mode restore source it writes incomplete data
        # to the destination. Re-raise so the snapshot job fails
        # loudly and the operator re-runs - registering a
        # half-captured snapshot as a success is the dangerous
        # outcome. A user-requested stop is handled by the break
        # above and never reaches here.
        logger.error(
            "[%s] watch-history capture failed after %d row(s): %s",
            library_name, len(out), exc,
        )
        raise
    logger.info(
        "[%s] watch-history captured %d row(s) (user=%s, total_items=%d, "
        "skipped_no_play=%d, max_play_count_seen=%d)",
        library_name, len(out), user_label, total_seen, skipped_no_view_count,
        max_view_count_seen,
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
            is_fav = bool(snap.is_favorite)
            # Keep the row when it carries a real rating OR is
            # favorited. affinity_row_is_meaningful is the shared
            # predicate (see backend_translation) so this capture
            # filter matches the Plex capture and both ingest paths.
            if not affinity_row_is_meaningful(rating, is_fav):
                continue
            out.append(item_snapshot_to_engine_dict(snap, user=user_label))
            # Parity with Plex media.log for the ratings
            # capture surface. See the watch-history loop above for
            # the rationale.
            try:
                from services import state as _state
                from services.logging_ops import _fmt_media_line
                if getattr(_state, "_media_logger", None) is not None:
                    _state._media_logger.debug(_fmt_media_line(
                        "EXPORT-RATING", library_name, snap.type,
                        snap.title or "(no title)",
                        user=user_label,
                        rating=rating,
                        show=snap.show_title or None,
                        season=snap.season_index,
                        episode=snap.episode_index,
                        artist=snap.artist or None,
                        album=snap.album or None,
                        path=snap.file_path or None,
                    ))
            except Exception:
                pass
    except Exception as exc:
        # See _capture_watch_history: swallowing the error and
        # returning a partial list yields a silently incomplete
        # ratings set that looks like a clean capture. Re-raise so
        # the snapshot job fails loudly instead.
        logger.error(
            "[%s] ratings capture failed after %d row(s): %s",
            library_name, len(out), exc,
        )
        raise
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
            # CONSOLE-08: preserve the raw GUID library id so the
            # snapshot->mirror writethrough can key section_id on it,
            # matching the live mirror sync (which uses the GUID).
            library_guid=library_id,
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
            except Exception as exc:
                logger.debug(
                    "[%s] upsert_server_item failed for %r: %s",
                    library_name, row.get("title"), exc,
                )
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
        # Keep the row when it has a real rating OR is favorited. A
        # favorite-only row (Jellyfin/Emby item with no numeric
        # rating) is stored with rating 0.0 + is_favorite 1.
        # affinity_row_is_meaningful is the shared capture/ingest
        # predicate (see backend_translation).
        _fav_raw = row.get("is_favorite")
        is_fav = None if _fav_raw is None else bool(_fav_raw)
        if not affinity_row_is_meaningful(rating_val, is_fav):
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
                rating=float(rating_val) if rating_val is not None else 0.0,
                is_favorite=is_fav,
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

    # The owner-phase used to run
    # unconditionally for EVERY snapshot regardless of user_filter
    # - on Emby/Jellyfin servers with multiple admins, that meant
    # selecting one admin still captured the other's data because
    # the registry's primary-owner happened to be the un-filtered
    # admin.
    #
    # ``owner_in_filter`` is the single gate used downstream to
    # decide whether to capture the owner's watch_history /
    # ratings / playlists. When the filter is None (capture
    # everyone), the gate opens. When the filter is supplied and
    # the owner's username is one of the selected entries, the
    # gate opens. Otherwise the owner-phase still runs (so the
    # library scaffold + per-user fan-out attach point is built)
    # but with capture flags forced false - no Ares rows land in
    # the snapshot when the operator only wanted Kai.
    if user_filter is None:
        owner_in_filter = True
    else:
        _filter_set = {
            u.strip() for u in user_filter
            if isinstance(u, str) and u.strip()
        }
        owner_in_filter = (owner_ctx.username or "") in _filter_set
    if not owner_in_filter:
        log_.info(
            "run_snapshot_adapter: owner %r not in user_filter %r; "
            "owner-phase will run as a scaffolding pass (no "
            "watch_history / ratings / playlists captured for the "
            "owner). Per-user fan-out will capture filtered users "
            "normally.",
            owner_ctx.username, user_filter,
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
            adapter, admin_token, user_filter,
            primary_owner_id=identity.owner_user_id or "",
            server_id=server_id or identity.machine_id or "",
            logger=log_,
        )

    log_.info(
        "run_snapshot_adapter: %d/%d libraries selected on %s (%s); "
        "%d managed users in scope.",
        len(target_libraries), len(all_libraries),
        identity.name or "?", service_type,
        len(managed_user_contexts),
    )

    # Register each library with the dashboard up front so
    # the Libraries panel shows the full list with totals before any
    # phase work begins. Mirrors snapshotter.py:2501-2504 for the
    # Plex path.
    #
    # The per-library total must match what advance_library actually
    # gets called for, otherwise the bar caps short of 100% even
    # though the library is functionally complete. The breakdown:
    #
    #   * Virtual libraries (boxsets/playlists CollectionType) carry
    #     no leaf items - watch/rating capture is skipped, per-user
    #     fan-out is skipped. Total = 1 (so the bar can complete via
    #     finish_library after the anchor attach).
    #   * Real libraries get one advance per: watch_history (if
    #     include_watch_history), ratings (if include_ratings),
    #     anchor collection attach (only on anchor library, if
    #     include_collections AND server has collections), anchor
    #     playlist attach (same conditions), plus n_user_tasks for
    #     the per-user fan-out.
    from services import state as _state
    _run_dash = _state.get_dashboard()
    n_user_tasks = len(managed_user_contexts)

    def _lib_total(_lib: Any) -> int:
        is_virtual = _lib.type in ("boxsets", "playlists")
        is_anchor = _lib.name == collections_anchor_name
        if is_virtual:
            return 1
        total = 0
        if include_watch_history:
            total += 1
        if include_ratings:
            total += 1
        if is_anchor and include_collections and server_wide_collections:
            total += 1
        if is_anchor and include_playlists and owner_playlists:
            total += 1
        total += n_user_tasks
        return max(1, total)

    # set_user_count drives RunCoverage's "Users (incl. owner)" stat.
    # Mirrors snapshotter.py:2501 for the Plex path. Pre-fix the
    # adapter path never called this, so the panel always showed 0
    # users regardless of how many managed users were captured.
    #
    # Library registration is deferred to after server-wide
    # collections + playlists + anchor selection have been computed,
    # because ``_lib_total`` depends on those values to know whether
    # a given library will receive the anchor-attach advance_library
    # tick (otherwise the bar would cap short of 100%).
    if _run_dash is not None:
        _run_dash.set_user_count(1 + n_user_tasks)

    # Jellyfin / Emby BoxSets AND
    # playlists are SERVER-WIDE — they don't belong to any single
    # library. Pre-fix the per-library loop called
    # ``adapter.list_collections(library_id=lib.id)`` and
    # ``adapter.list_playlists(user_context)`` for each library; both
    # adapters ignored the library scope (correctly, since they're
    # server-wide) and returned the SAME full lists every time. With N libraries selected the operator's snapshot
    # ended up with the same 333 BoxSets duplicated 4x in the payload
    # (once per library), and the snapshot.db builder then wrote them
    # under N different ``section_key`` values, inflating row counts
    # by Nx.
    #
    # Fix: capture server-wide collections ONCE here, suppress the
    # per-library collection capture entirely, and attach the result
    # to exactly ONE library payload after the loop. This matches the
    # operator's stated rule: "globally accessible collections are
    # accounted ONCE under an admin." Per-user fan-out continues to
    # skip collections (managed users have no private collections in
    # the Emby/Jellyfin model by default).
    #
    # For the snapshot.db builder the collections need a section_key
    # to anchor against. We attach them to the FIRST selected library;
    # if the operator's selection includes the synthetic
    # "Collections" / "BoxSets" library, prefer that as the anchor
    # since it's semantically the right home. The chosen anchor is
    # logged so the operator can see where the BoxSets landed.
    server_wide_collections: List[Dict[str, Any]] = []
    if include_collections and target_libraries:
        try:
            specs = adapter.list_collections() or []
            for spec in specs:
                server_wide_collections.append({
                    "name": spec.name,
                    "source_library": "(server-wide)",
                    "source_library_id": None,
                    "library_id": spec.library_id,
                    "items": [
                        {
                            "title": ref.title,
                            "guids": list(ref.guids),
                            "rating_key": ref.backend_item_id,
                        }
                        for ref in (spec.items or ())
                    ],
                })
            log_.info(
                "server-wide collections captured: %d (will attach to one library "
                "payload to avoid per-library duplication)",
                len(server_wide_collections),
            )
        except Exception as exc:
            log_.warning(
                "server-wide collections capture failed: %s; continuing without collections.",
                exc,
            )

    # Owner-phase playlists: capture once per run. Jellyfin/Emby
    # playlists are server-wide and per-user-owned; the owner sees
    # their own playlists via list_playlists(owner_ctx). Pre-fix this
    # was called per library inside snapshot_library_adapter, producing
    # the same N-fold duplication as collections.
    #
    # The owner-phase playlist
    # capture is gated by ``owner_in_filter``. When the operator
    # filtered to a non-owner user (e.g. "Kai only" with Ares as
    # the registry primary owner), capturing Ares's playlists too
    # was the same scope leak as the watch_history capture below.
    owner_playlists: List[Dict[str, Any]] = []
    if include_playlists and target_libraries and owner_in_filter:
        try:
            owner_playlists = _capture_playlists(
                adapter, "(server-wide)", owner_ctx, owner_ctx.username or "Owner", log_,
            )
        except Exception as exc:
            log_.warning(
                "owner playlists capture failed: %s; continuing without playlists.",
                exc,
            )

    # Managed-user playlists: capture once per managed user (not per
    # library). Each user has their own private playlists in Emby/
    # Jellyfin; this captures them all and attaches to that user's
    # entry under the anchor library's ``users`` dict below.
    managed_user_playlists: Dict[str, List[Dict[str, Any]]] = {}
    if include_playlists and managed_user_contexts:
        for _ctx in managed_user_contexts:
            try:
                managed_user_playlists[_ctx.username] = _capture_playlists(
                    adapter, "(server-wide)", _ctx, _ctx.username or "Managed", log_,
                )
            except Exception as exc:
                log_.warning(
                    "managed user %r playlists capture failed: %s",
                    _ctx.username, exc,
                )
                managed_user_playlists[_ctx.username] = []

    # Decide which library payload anchors the server-wide
    # collections + playlists. Both attach to the SAME anchor so all
    # the server-wide content lives in one place. Prefer a library
    # whose name strongly suggests it's the BoxSet / Collections
    # virtual library; fall back to the first selected one. The
    # anchor is set whenever there's ANY server-wide content to
    # attach (collections OR owner playlists OR managed-user
    # playlists), not only when collections are present.
    collections_anchor_name: Optional[str] = None
    _have_server_wide_content = bool(
        server_wide_collections
        or owner_playlists
        or any(managed_user_playlists.values())
    )
    if _have_server_wide_content:
        _collections_keywords = ("collection", "boxset", "box set", "boxsets", "box sets")
        for _lib in target_libraries:
            if any(kw in _lib.name.lower() for kw in _collections_keywords):
                collections_anchor_name = _lib.name
                break
        if collections_anchor_name is None:
            collections_anchor_name = target_libraries[0].name
        log_.info(
            "server-wide content anchored to library %r "
            "(collections=%d, owner_playlists=%d, managed_user_playlists=%d)",
            collections_anchor_name,
            len(server_wide_collections),
            len(owner_playlists),
            sum(len(v) for v in managed_user_playlists.values()),
        )

    # Register each library with the dashboard now that
    # ``collections_anchor_name`` / ``server_wide_collections`` /
    # ``owner_playlists`` are all known. ``_lib_total`` reads those
    # closures to decide whether the anchor library's bar should
    # include +1 for the collection attach and/or +1 for the playlist
    # attach, so the bar can reach 100% on every library at run end.
    if _run_dash is not None:
        for lib in target_libraries:
            _run_dash.add_library(lib.name, total=_lib_total(lib))

    per_library: List[Dict[str, Any]] = []
    for lib in target_libraries:
        if _run_dash is not None:
            _run_dash.set_library_status(lib.name, "active")
            _run_dash.push_activity(
                "started", lib.name, "Snapshot started",
            )
        if stop_event is not None and stop_event.is_set():
            log_.info("run_snapshot_adapter: stop requested; halting.")
            break
        # Skip watch/rating capture for Jellyfin/Emby
        # virtual libraries (``boxsets`` is the auto-created BoxSets
        # library, ``playlists`` is the auto-created Playlists
        # library). These libraries contain BoxSet / Playlist
        # entities, not leaf items, so the ``/Users/{uid}/Items?ParentId=X``
        # walk returns BoxSets / playlists that have no view_count
        # semantics. Running watch_history / ratings against them is
        # wasted work; the per-user fan-out is similarly skipped.
        # The library still receives the anchor attachment for
        # server-wide collections (matches the operator's mental
        # model of "Collections" being where collections live).
        _is_virtual_library = lib.type in ("boxsets", "playlists")
        lib_payload = snapshot_library_adapter(
            adapter,
            library_id=lib.library_id,
            library_name=lib.name,
            library_type=lib.type,
            server_id=server_id or identity.machine_id,
            user_context=owner_ctx,
            # owner-phase data capture is gated by
            # ``owner_in_filter``. The library scaffold (id, name,
            # type, anchor for collections + playlists, attachment
            # point for the per-user fan-out) is always built; only
            # the owner's PER-USER data (watch_history + ratings)
            # is suppressed when the operator's filter excluded
            # them. Pre-fix this ran unconditionally and captured
            # the wrong admin's data on multi-admin Emby installs.
            include_watch_history=(
                include_watch_history and not _is_virtual_library
                and owner_in_filter
            ),
            include_ratings=(
                include_ratings and not _is_virtual_library
                and owner_in_filter
            ),
            # Per-library playlist + collection capture is suppressed
            # here: we captured server-wide playlists + BoxSets once
            # above and will attach them to a single anchor library
            # after the loop.
            include_playlists=False,
            include_collections=False,
            logger=log_,
            stop_event=stop_event,
        )
        # Per-user fan-out for this library. The library payload's
        # ``users`` dict gets one entry per managed user captured.
        # Skipped for virtual libraries (boxsets/playlists) since they
        # carry no per-user leaf state.
        if managed_user_contexts and not _is_virtual_library:
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
                # Per-user advance: each managed-user pass counts as
                # one step in the library's progress bar (matches the
                # ``4 + n_user_tasks`` total declared in add_library).
                if _run_dash is not None:
                    _run_dash.advance_library(lib.name)
                # Attach managed user's once-captured playlists on the
                # anchor library only. Other libraries' per-user
                # blocks keep playlists=[] (server-wide content lives
                # in one place).
                if lib.name == collections_anchor_name:
                    per_user_block[ctx.username]["playlists"] = list(
                        managed_user_playlists.get(ctx.username, [])
                    )
            lib_payload["users"] = per_user_block
        # attach the once-per-run server-wide collections
        # AND playlists to the chosen anchor library, BEFORE the
        # owner-block mirror below so they flow into both the top-
        # level shape (for the JSON sidecar) and the users[owner]
        # shape (for the snapshot.db builder). Every other library
        # payload has empty collections/playlists, which is correct:
        # BoxSets and playlists are server-wide, not per-library, and
        # double-counting them under every library is what produced
        # the operator-reported 4x duplication.
        if lib.name == collections_anchor_name:
            if server_wide_collections:
                lib_payload["collections"] = list(server_wide_collections)
                if _run_dash is not None:
                    n = len(server_wide_collections)
                    if n:
                        _run_dash.add_batch_total("collection", n)
                        _run_dash.add_run_total(n)
                        for _ in range(n):
                            _run_dash.inc_collection()
                    # advance the library step for the collection
                    # attach (matches the +1 in _lib_total above when
                    # is_anchor+include_collections+server has collections)
                    _run_dash.advance_library(lib.name)
            if owner_playlists:
                lib_payload["playlists"] = list(owner_playlists)
                if _run_dash is not None:
                    n = len(owner_playlists)
                    if n:
                        _run_dash.add_batch_total("playlist", n)
                        _run_dash.add_run_total(n)
                        for _ in range(n):
                            _run_dash.inc_playlist()
                    # advance for the playlist attach step
                    _run_dash.advance_library(lib.name)

        # Mirror owner-phase data into the ``users`` dict under a
        # role="owner" entry. The snapshot.db build path
        # (``server/snapshot_capture.py::build_snapshot_db_from_payloads``)
        # walks ``payload["users"][handle]`` blocks only - its
        # ``_find_owner_block`` looks for an entry whose ``role`` is
        # "owner". The adapter previously emitted owner-phase data at
        # the TOP LEVEL of the payload only, so the consumer found an
        # empty owner block and wrote zero rows for items / watch
        # events / ratings / playlists / collections, producing an
        # effectively empty snapshot.db. The top-level fields stay
        # populated because the JSON sidecar serialiser reads that
        # shape; the ``users[owner]`` block is the new addition
        # consumed by the snapshot.db builder.
        owner_handle = owner_ctx.username or "Owner"
        users_dict = lib_payload.setdefault("users", {})
        # Collision-safe key. A managed user captured into
        # ``per_user_block`` above can share the owner's username on a
        # multi-admin Emby/Jellyfin server. Writing the owner-mirror
        # block under a colliding key would silently overwrite that
        # managed user's captured watch/ratings with the owner's data.
        # The snapshot.db builder finds the owner block by
        # ``role == "owner"``, not by key, so a suffixed key is safe.
        owner_key = owner_handle
        _owner_suffix = 2
        while owner_key in users_dict:
            owner_key = f"{owner_handle} ({_owner_suffix})"
            _owner_suffix += 1
        users_dict[owner_key] = {
            "role": "owner",
            "display_name": owner_ctx.username or "Owner",
            "backend_user_id": owner_ctx.backend_user_id or "",
            "watch_history": list(lib_payload.get("watch_history") or []),
            "ratings": list(lib_payload.get("ratings") or []),
            "playlists": list(lib_payload.get("playlists") or []),
            "collections": list(lib_payload.get("collections") or []),
        }
        per_library.append(lib_payload)
        # Library complete: ``finish_library`` snaps completed=total
        # AND sets status=done. Using set_library_status alone (the
        # earlier pass) left completed at whatever number
        # advance_library had bumped it to, so the bar stayed at
        # ~71% but the status flipped to "done" which turned the bar
        # green. The two contracts disagreed -> operator-reported
        # "bar turning green at 71% as if it were 100%." Mirrors
        # snapshotter.py:1912 (Plex path).
        if _run_dash is not None:
            _run_dash.finish_library(lib.name, error=False)
            _run_dash.push_activity(
                "done", lib.name, "Library complete",
            )
        # Bridge to the Rule-1 payload-direct collector so
        # the post-run wrapper (``server/jobs.py::_capture_snapshot_db``)
        # can build the snapshot.db file. The Plex path appends at
        # ``services/snapshotter.py:2144``; the adapter path was
        # missing this contract, which is why Emby/Jellyfin snapshot
        # runs completed with "no per-library payload" warnings even
        # though the engine had captured everything successfully.
        try:
            from services import state as _state
            _state._snapshot_payloads.append(lib_payload)
        except Exception:
            log_.exception(
                "[%s] failed to append payload to state collector; "
                "snapshot.db build will skip this library.",
                lib.name,
            )

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
    primary_owner_id: str,
    server_id: str,
    logger: logging.Logger,
) -> List[Any]:
    """Enumerate the source's managed users + return one
    :class:`UserContext` per user that passes the optional filter.
    The admin token is reused for each context because Jellyfin /
    Emby admin tokens can read any user's UserData via
    ``/Users/{userId}/Items`` - no per-user authentication required.

    Skip rules:

    * The **primary owner** (the user whose ``backend_user_id``
      matches ``primary_owner_id``) is skipped because the owner-phase
      pass in :func:`run_snapshot_adapter` already captures that
      user's state.
    * Users without a ``backend_user_id`` are skipped (can't address
      them on the API).
    * Users not in ``user_filter`` (when supplied) are skipped.

    Non-primary admin users are CAPTURED. Emby / Jellyfin allow
    multiple administrators on a single server; pre-fix, the broad
    ``is_admin`` skip silently dropped every non-primary admin from
    the fan-out, which meant any data those users had (watch state,
    ratings, playlists) was never captured. The primary owner is the
    only user that needs to be skipped to avoid double-capture, not
    every admin."""
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
        if primary_owner_id and user.backend_user_id == primary_owner_id:
            continue  # captured by owner-phase
        if not user.backend_user_id:
            logger.debug(
                "user %r has no backend_user_id; skipping fan-out.",
                user.username,
            )
            continue
        if filter_set is not None and user.username not in filter_set:
            continue
        # Prefer the user's OWN stored
        # auth_token over the admin's. Operator reported the Emby
        # admin-impersonation flow returning UserData populated for
        # the admin instead of the URL user even after the
        # Authorization-header UserId override. The bullet-proof fix
        # is to authenticate AS the target user with their own token.
        # ``services.playlist_copy._lookup_plex_home_token`` does the
        # same managed_users.auth_token_enc lookup; reuse the helper
        # so the storage contract stays in one place. Falls back to
        # the admin token when no per-user token is stored (the
        # legacy admin-impersonation path still works with all the
        # fixes if the operator hasn't supplied a
        # per-user token yet).
        per_user_token = admin_token
        per_user_token_source = "admin (no per-user token stored)"
        # Inspect the row state directly so we can distinguish:
        #   (a) no row matching (server_id, username) at all
        #   (b) row exists but auth_token_enc is NULL
        #   (c) row exists, column populated, decrypt silently failed
        # All three currently surface as "no per-user token stored" via
        # the helper; this extra log makes the distinction obvious in
        # the run log without leaking the actual ciphertext.
        try:
            from server import media_db as _media_db
            _conn = _media_db._require_conn()
            _row = _conn.execute(
                "SELECT auth_token_enc IS NOT NULL AS has_token, "
                "       length(auth_token_enc) AS enc_len "
                "FROM managed_users WHERE server_id = ? AND username = ?",
                (server_id, user.username),
            ).fetchone()
            if _row is None:
                _row_state = (
                    f"no row matching (server_id={server_id!r}, "
                    f"username={user.username!r})"
                )
            elif not _row["has_token"]:
                _row_state = "row found but auth_token_enc is NULL"
            else:
                _row_state = (
                    f"row found with auth_token_enc populated "
                    f"(enc_len={_row['enc_len']})"
                )
            logger.info(
                "managed user %r: managed_users row state = %s",
                user.username, _row_state,
            )
        except Exception:
            logger.exception(
                "managed user %r: row-state diagnostic query failed.",
                user.username,
            )

        try:
            from services.playlist_copy import _lookup_plex_home_token
            stored = _lookup_plex_home_token(server_id, user.username)
            if stored:
                per_user_token = stored
                per_user_token_source = (
                    f"per-user token from managed_users.auth_token_enc "
                    f"(len={len(stored)})"
                )
        except Exception:
            logger.exception(
                "managed user %r: per-user token lookup failed; "
                "falling back to admin token + URL-path impersonation.",
                user.username,
            )
        logger.info(
            "managed user %r: auth source = %s (server_id=%r)",
            user.username, per_user_token_source, server_id,
        )
        contexts.append(_UserContext(
            backend_user_id=user.backend_user_id,
            username=user.username,
            auth_token=per_user_token,
            is_admin=bool(user.is_admin),
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
        # Identity fields the snapshot.db builder reads when writing
        # the ``server_users`` row for this per-user block. Pre-fix
        # these were missing, so the builder fell back to
        # ``display_name=handle`` and ``backend_user_id=None``.
        "role": "managed",
        "display_name": user_context.username or "",
        "backend_user_id": user_context.backend_user_id or "",
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
