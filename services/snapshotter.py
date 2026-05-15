"""
Snapshot pipeline for PlexMigrate.

snapshot_watch_history / snapshot_playlists / snapshot_collections / snapshot_ratings
gather data from a live Plex server. snapshot_library orchestrates them
concurrently into a .plexexport.json file. run_snapshot drives the full
multi-library snapshot with progress display.
"""

import concurrent.futures
import json
import logging
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from plexapi.server import PlexServer
from rich.live import Live

import services.state as state
from services.state import console
from services.dashboard import (
    DashboardState,
    _advance_lib,
    _build_dashboard,
    _check_terminal_size,
    _current_item,
    _http_lib_var,
    _keyboard_thread,
    _make_progress,
    _thread_category,
    submit_with_context,
)
from services.logging_ops import _fmt_media_line
from services.resolver import (
    _all_guids,
    _disable_autoreload,
    _safe_file_path,
    serialize_item,
    serialize_playlist,
    serialize_collection,
)
from services.auth import get_home_users


def _log_serialize_diag(
    logger: logging.Logger,
    lib_name: str,
    phase: str,
    item_count: int,
    loop_t0: float,
    loop_http0: int,
) -> None:
    """
    DIAGNOSTIC: report a per-item serialize loop's wall time and the
    number of Plex HTTP calls it made.

    ``loop_t0`` / ``loop_http0`` are ``time.monotonic()`` and
    ``state.get_http_count()`` captured immediately before the loop.
    The bulk fetch already happened before that point, so any HTTP
    calls counted here were made *during* serialization - i.e. plexapi
    per-item ``.reload()`` round-trips (an N+1). A calls/item ratio
    near 0 means items came back fully populated; a ratio near (or
    above) 1 means every item is costing a blocking round-trip, which
    is the likely reason ``show`` libraries snapshot slower than
    ``artist`` ones despite the same loop shape.
    """
    try:
        elapsed = time.monotonic() - loop_t0
        http_delta = state.get_http_count() - loop_http0
        ratio = (http_delta / item_count) if item_count else 0.0
        verdict = (
            " - PER-ITEM RELOAD (N+1): every item is a blocking HTTP round-trip"
            if ratio >= 0.5
            else " - items pre-populated, loop is CPU-bound (threading won't help)"
            if item_count
            else ""
        )
        logger.info(
            "[%s] %s serialize loop: %d item(s) in %.1fs, %d Plex HTTP call(s) "
            "during loop (%.2f calls/item)%s",
            lib_name, phase, item_count, elapsed, http_delta, ratio, verdict,
        )
    except Exception:  # pragma: no cover (diagnostic must never break a run)
        pass


# ── Snapshot Functions ──────────────────────────────────────────────────────────

_VALID_WR_STRATEGIES = ("smart", "force_bulk", "force_server_side")


def _should_cache_payload_to_media_db(
    server_id: str,
    lib_name: str,
    logger: logging.Logger,
) -> bool:
    """
    Decide whether this snapshot run's per-library payload should be
    ingested into media.db.

    Resolution rule:
      1. Operator explicitly opted in via the
         ``cache_snapshot_payloads_to_media_db`` tunable → cache.
      2. Tunable left at default false, but media.db has NO rows
         tagged with this server_id → auto-seed (one-shot ingest to
         populate the resolver Tier 0 GUID cache).
      3. Tunable false AND server already has rows → skip the ingest.

    Failures of either lookup are treated as "skip" - we never want
    to accidentally cache a run when the configured state is unclear.
    """
    try:
        from services import tunables
        if tunables.cache_snapshot_payloads_to_media_db():
            logger.info(
                "[%s] media.db caching: ENABLED via tunable (operator opt-in)",
                lib_name,
            )
            return True
    except Exception:
        return False

    if not server_id:
        return False

    try:
        from server import media_db
        seeded = media_db.has_any_items_for_server(server_id)
    except Exception:
        return False

    if not seeded:
        logger.info(
            "[%s] media.db caching: AUTO-SEED (first run for server %r - "
            "ingesting to populate the resolver Tier 0 cache; subsequent "
            "runs will skip unless the operator flips the tunable on)",
            lib_name, server_id,
        )
        return True

    return False


def _resolve_watch_ratings_strategy(
    *,
    server_id: Optional[str],
    logger: logging.Logger,
) -> str:
    """
    Resolve the owner-phase watch+ratings capture strategy for one
    snapshot run.

    Resolution chain (first match wins):

      0. Per-job override from
         ``state._watch_ratings_strategy_override_var`` - set by the
         job runner from the JobIn / ScheduleIn payload when the
         operator picked something other than "Inherit" on the
         Per-Run Settings ▸ Advanced sub-tab.
      1. Per-server override at
         ``settings.snapshot_defaults_per_server[server_id]
         .watch_ratings_filter_strategy``
      2. Global default at ``settings.watch_ratings_filter_strategy``
      3. Built-in default ``"smart"``

    Unknown / malformed values at any tier fall through to the next
    tier rather than raising, so a stale settings.json from before
    this setting existed Just Works.
    """
    # Tier 0: per-job override carried on the ContextVar. Empty
    # string = "no override" (the default).
    try:
        per_job = state._watch_ratings_strategy_override_var.get()
    except LookupError:
        per_job = ""
    if isinstance(per_job, str) and per_job.lower() in _VALID_WR_STRATEGIES:
        return per_job.lower()

    try:
        from server.persistence import load_settings
        settings = load_settings() or {}
    except Exception:
        return "smart"

    if server_id:
        per_server = settings.get("snapshot_defaults_per_server") or {}
        override = (per_server.get(server_id) or {}).get("watch_ratings_filter_strategy")
        if isinstance(override, str) and override.lower() in _VALID_WR_STRATEGIES:
            return override.lower()

    glob = settings.get("watch_ratings_filter_strategy")
    if isinstance(glob, str) and glob.lower() in _VALID_WR_STRATEGIES:
        return glob.lower()

    return "smart"


def _bulk_fetch_for_filters(
    section, logger: logging.Logger, want_shows: bool = False,
) -> Dict[str, Any]:
    """
    Perf #2 helper: fetch every leaf-level item in ``section`` (and the
    show-level container list for show libraries when ``want_shows``)
    in **one** Plex round-trip per list. Watch-history and ratings then
    filter the same in-memory list locally instead of issuing separate
    server-side ``viewCount__gt=0`` and ``userRating__gt=0`` filter
    scans.

    Why this is faster: Plex's filter engine scans the full library
    table for every filtered ``search*`` call. With both watch and
    ratings enabled the section is scanned 2-3 times depending on type
    (3 for shows: episode-watched, show-rated, episode-rated). One
    unfiltered fetch returns the same data in a single scan, and the
    inline XML response already carries ``viewCount`` and ``userRating``
    so the local filter is a trivial attribute read.

    Returns ``{"items": [...], "shows": [...] | None, "fetch_seconds":
    float, "http_calls": int, "ok": bool}``. On any exception the
    caller falls back to the per-task server-side filter path, so this
    is a soft-fail optimisation - a failure never breaks a snapshot.
    """
    libtype = getattr(section, "type", "")
    t0 = time.monotonic()
    http0 = state.get_http_count()
    items: List = []
    shows: Optional[List] = None
    try:
        if libtype == "artist":
            items = section.searchTracks()
        elif libtype == "show":
            items = section.searchEpisodes()
            if want_shows:
                shows = section.search()  # show-level container
        else:
            items = section.search()
        ok = True
    except Exception as exc:
        logger.warning(
            "[%s] bulk-fetch for shared watch+ratings filter failed (%s) "
            "- gathers will use per-task server-side filter scans instead",
            section.title, exc,
        )
        ok = False
    # Perf #2 follow-up: disable plexapi's autoreload on the freshly-
    # fetched lists BEFORE the per-attribute filter loops in
    # ``snapshot_watch_history`` / ``snapshot_ratings`` touch them.
    # The bulk responses are partial objects; reading ``viewCount`` /
    # ``userRating`` on an unwatched / unrated item would otherwise
    # trip ``PlexPartialObject.__getattribute__``'s auto-reload, which
    # bundles ``includeMarkers + includeChapters`` and triggers Plex
    # intro/chapter analysis on shows that haven't been analysed -
    # potentially 20-30s per item. On a 6 000-episode library with
    # mostly-unrated content that's hours, not seconds.
    #
    # Best-effort: the helper is silent on objects that don't expose
    # ``_autoReload`` and a no-op when the tunable
    # ``plexapi_autoreload_enabled`` is true (operator opt-in to
    # vanilla plexapi behaviour for diagnostics).
    if ok:
        try:
            _disable_autoreload(*items)
            if shows is not None:
                _disable_autoreload(*shows)
        except Exception:
            pass
    return {
        "items": items,
        "shows": shows,
        "fetch_seconds": time.monotonic() - t0,
        "http_calls": state.get_http_count() - http0,
        "ok": ok,
    }


def snapshot_watch_history(
    section, logger: logging.Logger, user: str = "Plex Owner",
    stop_event: Optional[threading.Event] = None,
    # Perf #2: shared library-item list pre-fetched by
    # ``_bulk_fetch_for_filters``. When supplied, skip the server-side
    # ``viewCount__gt=0`` filter scan and walk the shared list locally.
    # ``None`` keeps the per-task server-side-filter path - a
    # first-class strategy for runs where only one of watch / ratings
    # is wanted (avoids over-fetching the full library to filter for a
    # single type) and for home-user gathers (each user's section is
    # distinct, so sharing across users wouldn't help).
    prefetched_items: Optional[List] = None,
) -> List[Dict]:
    """
    Fetches all watched items from a library section.

    We only snapshot items that have actually been watched (viewCount > 0).
    Capturing snapshot unwatched items would add noise and isn't useful for migration.

    Args:
        section: A python-plexapi LibrarySection.
        logger (Logger): Shared logger.
        user (str): Username label for the media log.

    Returns:
        List of serialized item dicts for watched items.
    """
    watched = []
    try:
        libtype = section.type
        if prefetched_items is not None:
            # Perf #2: filter the shared bulk-fetched list locally.
            # No new Plex round-trip; the attribute read is in-memory
            # because the inline XML response already carries
            # ``viewCount``.
            all_items = [
                it for it in prefetched_items
                if getattr(it, "viewCount", 0)
            ]
            logger.info(
                "[%s] watch-history: filtering shared bulk-fetched list "
                "(%d candidates → %d watched)",
                section.title, len(prefetched_items), len(all_items),
            )
        elif libtype == "artist":
            try:
                all_items = section.searchTracks(viewCount__gt=0)
            except Exception as _filter_exc:
                logger.warning(
                    "[%s] watch-history: server-side viewCount filter "
                    "failed (%s) - falling back to full searchTracks() walk",
                    section.title, _filter_exc,
                )
                all_items = [t for t in section.searchTracks()
                             if getattr(t, "viewCount", 0)]
        elif libtype == "show":
            try:
                all_items = section.searchEpisodes(viewCount__gt=0)
            except Exception as _filter_exc:
                logger.warning(
                    "[%s] watch-history: server-side viewCount filter "
                    "failed (%s) - falling back to full searchEpisodes() walk",
                    section.title, _filter_exc,
                )
                all_items = [ep for ep in section.searchEpisodes()
                             if getattr(ep, "viewCount", 0)]
        else:
            try:
                all_items = section.search(viewCount__gt=0)
            except Exception as _filter_exc:
                logger.warning(
                    "[%s] watch-history: server-side viewCount filter "
                    "failed (%s) - falling back to full section.all() walk",
                    section.title, _filter_exc,
                )
                all_items = [m for m in section.all()
                             if getattr(m, "viewCount", 0)]

        # Discover-don't-predict: register the real watched-item count
        # the moment we've enumerated it. The watch batch + the
        # total-run tracker both grow by exactly this much, so the
        # Process List bar and headline ETR work off measured numbers.
        _watched_count = sum(
            1 for it in all_items if getattr(it, "viewCount", 0)
        )
        if state.get_dashboard() and _watched_count:
            state.get_dashboard().add_batch_total("watch", _watched_count)
            state.get_dashboard().add_run_total(_watched_count)

        # DIAGNOSTIC: measure the per-item serialize loop. If the Plex
        # HTTP call count climbs ~1:1 with items serialized, plexapi is
        # doing a per-item ``.reload()`` (an N+1) - the prime suspect
        # for ``show`` libraries snapshotting far slower than ``artist``.
        _loop_t0 = time.monotonic()
        _loop_http0 = state.get_http_count()
        for item in all_items:
            # Stop P3: item-level checkpoint. Soft Stop sets this
            # event; bailing here means a multi-thousand-episode TV
            # library halts within one item instead of running to the
            # next library boundary (which is effectively never).
            if stop_event is not None and stop_event.is_set():
                logger.info(
                    "[%s] watch-history: stop requested - halting after "
                    "%d item(s)", section.title, len(watched),
                )
                break
            plays = getattr(item, "viewCount", 0) or 0
            if plays:
                with _current_item(section.title, item.type, item.title, phase="capturing"):
                    watched.append(serialize_item(item))
                if state.get_dashboard():
                    state.get_dashboard().inc_watch()
                if state._media_logger:
                    state._media_logger.debug(_fmt_media_line(
                        "EXPORT", section.title, item.type, item.title,
                        user=user,
                        plays=plays,
                        rating=getattr(item, "userRating", None),
                        path=_safe_file_path(item),
                    ))
        _log_serialize_diag(logger, section.title, "watch-history",
                            len(watched), _loop_t0, _loop_http0)
        logger.info(f"[{section.title}] Play Count snapshot: {len(watched)} item(s) found (user: {user})")
    except Exception as e:
        logger.error(f"Error fetching Play Count for {section.title}: {e}")
    return watched


def _resolve_server_owner_ids(
    admin_server: PlexServer,
    home_users: List[Tuple[str, str, PlexServer]],
    logger: logging.Logger,
) -> Dict[int, int]:
    """
    Map each user-bound ``PlexServer`` instance (by ``id()``) to that
    user's *local server user-id* - the same number Plex puts in the
    ``<Playlist userID="…">`` attribute.

    Plex's local user-ids come from ``server.systemAccounts()`` (the
    ``/accounts`` endpoint) and are distinct from Plex.tv account-ids.
    The owner is typically ``SystemAccount.id == 1``; home users get
    2, 3, 4… We match by ``SystemAccount.name`` against the home
    user's title and against the admin's ``MyPlexAccount.username``
    so a future migration to an account that wasn't id=1 still works.

    Returns an empty dict if ``systemAccounts()`` is unavailable; the
    caller treats a missing entry as "owner_id unknown" and walks
    every playlist (the prior behaviour), so this is a soft-fail
    optimisation rather than a hard requirement.
    """
    try:
        sys_accts = admin_server.systemAccounts()
    except Exception as e:
        logger.debug(f"systemAccounts() unavailable: {e} - playlist owner filter disabled")
        return {}

    by_name: Dict[str, int] = {}
    for a in sys_accts:
        name = getattr(a, "name", None)
        aid = getattr(a, "id", None)
        if name and aid is not None:
            by_name[str(name)] = int(aid)

    out: Dict[int, int] = {}

    # Owner: try MyPlexAccount username first, then fall back to the
    # SystemAccount with the lowest id (Plex's owner is conventionally id=1).
    try:
        owner_name = admin_server.myPlexAccount().username
    except Exception:
        owner_name = None
    owner_id = by_name.get(str(owner_name)) if owner_name else None
    if owner_id is None and sys_accts:
        owner_id = min((int(getattr(a, "id", 0)) for a in sys_accts if getattr(a, "id", None) is not None), default=None)
    if owner_id is not None:
        out[id(admin_server)] = owner_id

    for (title, _, user_server) in home_users:
        uid = by_name.get(str(title))
        if uid is not None:
            out[id(user_server)] = uid
        else:
            logger.debug(f"No SystemAccount match for home user '{title}' - will snapshot all visible playlists")

    return out


def _playlist_owner_id(pl) -> Optional[int]:
    """
    Best-effort extraction of the local server user-id that owns ``pl``.

    Plex's ``<Playlist userID="…">`` attribute maps to ``SystemAccount.id``
    on the server (not the global Plex.tv account id). python-plexapi
    has used both ``userID`` and ``ownerID`` over the years; we try the
    historical name first and fall back. Returns ``None`` if neither is
    present (e.g. on auto-generated server playlists) so callers can
    treat that as "unknown - don't filter".
    """
    raw = getattr(pl, "userID", None)
    if raw is None:
        raw = getattr(pl, "ownerID", None)
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def build_playlist_cache(
    server: PlexServer,
    logger: logging.Logger,
    owner_id: Optional[int] = None,
) -> List[Tuple[Any, List]]:
    """
    Fetch every playlist owned by ``owner_id`` on this server, once.

    Returns a list of ``(playlist_obj, items)`` tuples. Playlists whose
    ``pl.items()`` call errors (e.g. Plex 500s on auto-generated
    "Recently Played" / "All Music" entries) are recorded with an empty
    item list and a single summary INFO line - not per-playlist DEBUG
    spam.

    Ownership filter (v0.9.5): when ``owner_id`` is supplied, playlists
    whose owner does not match are skipped without fetching
    ``pl.items()`` - Plex's server-wide ``/playlists`` response under a
    user's token can include playlists *shared to* that user, and
    walking them once per recipient was the dominant cost in mixed-
    content snapshots. Now each playlist is fetched exactly once, by its
    actual owner. Auto-generated playlists with no userID attribute
    are still walked (we can't tell who owns them; better to over-fetch
    than to silently drop them).

    Each export run reuses this cache across libraries so the
    O(libraries × playlists × items) blow-up in :func:`snapshot_playlists`
    collapses to one fetch per (server, owner) pair.

    The per-playlist ``pl.items()`` call is the slow part (one network
    round-trip each) so we surface it on the dashboard's Currently
    Processing panel under phase ``"fetching"`` - for a server with
    hundreds of playlists this gives the user a visible heartbeat
    during what would otherwise look like a stalled warmup.
    """
    cache: List[Tuple[Any, List]] = []
    skipped_500 = 0
    skipped_not_owned = 0
    skipped_smart = 0
    try:
        all_playlists = server.playlists()
    except Exception as e:
        logger.warning(f"Could not list playlists from server: {e}")
        return cache

    server_label = getattr(server, "friendlyName", "") or "(server-wide)"
    if state.get_dashboard():
        state.get_dashboard().push_activity(
            "phase", "-",
            f"Warming playlist cache for '{server_label}' ({len(all_playlists)} playlists)…",
        )
    for pl in all_playlists:
        # Throttle-reduction: skip smart playlists entirely. ``pl.items()``
        # on a smart playlist makes Plex *run the filter* server-side -
        # a real round-trip - yet the snapshotter can't migrate a smart
        # playlist anyway (the restorer records them as "recreate
        # manually" and never imports their members). Fetching their
        # members during the warm was pure wasted request volume, and
        # request volume is what gets the whole run rate-limited (429 +
        # Retry-After). ``server.playlists()`` itself populated the
        # ``.smart`` attribute, so this check costs nothing.
        if getattr(pl, "smart", False):
            skipped_smart += 1
            continue

        if owner_id is not None:
            pl_owner = _playlist_owner_id(pl)
            # pl_owner is None for auto-generated playlists (no userID
            # attribute). We let those through rather than guess.
            if pl_owner is not None and pl_owner != owner_id:
                skipped_not_owned += 1
                continue

        pl_title = getattr(pl, "title", "?")
        try:
            with _current_item(server_label, "playlist", pl_title, phase="fetching"):
                cache.append((pl, list(pl.items())))
        except Exception as e:
            cache.append((pl, []))
            # Plex's auto-generated playlists routinely 500 on .items();
            # demote individual errors to DEBUG and emit one summary
            # INFO line at the end so the run log stays scannable.
            logger.debug(f"playlist '{pl_title}' returned no items: {e}")
            skipped_500 += 1
    if skipped_500:
        logger.info(
            f"Playlist enumeration: {skipped_500} playlist(s) returned errors on .items() "
            f"and were treated as empty. See DEBUG runtime log for per-playlist details."
        )
    if skipped_smart:
        logger.info(
            f"[{server_label}] Skipped {skipped_smart} smart playlist(s) - "
            f"filter-based, not migratable, and fetching their members would "
            f"only add throttle-inducing request volume."
        )
    if skipped_not_owned:
        logger.info(
            f"[{server_label}] Skipped {skipped_not_owned} playlist(s) shared to this "
            f"user - they will be exported once under their actual owner."
        )
    return cache


def _primary_section_for_playlist(items) -> Optional[str]:
    """
    Return the ``librarySectionID`` (as string) that holds the majority
    of items in this playlist, or ``None`` if no item carries one.

    Used by :func:`snapshot_playlists` to assign each playlist to a single
    "primary" library export instead of duplicating it into every
    library that has at least one item - see the v0.9.5 changelog note
    on the mixed-content playlist fan-out.

    Tie-breaking: ``max()`` picks the first-encountered max which gives
    a stable but arbitrary winner. Stability matters only across runs
    of the same dataset (so the same library always "owns" the
    playlist), which is satisfied because the iteration order of
    ``pl.items()`` is deterministic per Plex response.
    """
    counts: Dict[str, int] = {}
    for i in items:
        sec_id = getattr(i, "librarySectionID", None)
        if sec_id is None:
            continue
        key = str(sec_id)
        counts[key] = counts.get(key, 0) + 1
    if not counts:
        return None
    return max(counts, key=lambda k: counts[k])


def snapshot_playlists(
    server: PlexServer,
    section_key: str,
    logger: logging.Logger,
    lib_name: str = "",
    playlist_cache: Optional[List[Tuple[Any, List]]] = None,
    skip_playlists: bool = False,
    run_caches: Optional[Dict[int, List[Tuple[Any, List]]]] = None,
    stop_event: Optional[threading.Event] = None,
) -> List[Dict]:
    """
    Fetches playlists whose *primary* library is this one.

    Plex playlists are server-wide (not per-library) and can span
    multiple libraries (e.g. a mixed movies+TV playlist). Pre-v0.9.5
    we serialised the whole playlist into *every* library that had at
    least one matching item - meaning an 800-item mixed playlist
    landed in both the Movies and the TV export files (1600 items
    on disk, 1600 resolves on import). Each playlist is now serialised
    into exactly one library: the section that holds the most items.
    Cross-section items still restore correctly on import via Tier 1
    GUID matching (``server.library.getByGuid`` is section-agnostic).

    Args:
        server (PlexServer): Active server connection (only used when
            ``playlist_cache`` is None and we need to fetch on the fly).
        section_key (str): The library section's key (integer ID as string).
        logger (Logger): Shared logger.
        lib_name (str): Library name for log labels.
        playlist_cache (list, optional): Pre-fetched
            ``[(playlist, items), ...]`` shared across libraries and users
            to avoid an O(libraries × users × playlists × items) fetch
            blow-up. Built once per server connection in run_snapshot.
        skip_playlists (bool): Return empty list immediately - no API calls
            at all. Takes priority over everything else.
        run_caches (dict, optional): Shared per-run dict keyed by
            ``id(server)``. When ``playlist_cache`` is None, this is
            checked first before calling ``build_playlist_cache``, and the
            newly built cache is stored back so subsequent calls for the
            same server re-use it. This is the lazy-sharing path used by
            ``skip_playlist_prebuild`` mode: the upfront parallel warm is
            skipped but each server's cache is still built at most once
            per run instead of once per library × user.

    Returns:
        List of serialized playlist dicts.
    """
    if skip_playlists:
        return []
    result = []
    label = lib_name or f"section:{section_key}"
    section_key_s = str(section_key)
    try:
        if playlist_cache is None:
            # Lazy path: check the run-level shared cache before hitting
            # the Plex API again. Two threads for the same server may race
            # here; the last writer wins (both results are equivalent) and
            # the extra build is wasted work but not a correctness issue.
            if run_caches is not None:
                server_id = id(server)
                playlist_cache = run_caches.get(server_id)
                if playlist_cache is None:
                    playlist_cache = build_playlist_cache(server, logger)
                    run_caches[server_id] = playlist_cache
            else:
                playlist_cache = build_playlist_cache(server, logger)

        # Discover-don't-predict: register the real count of playlists
        # this section will contribute to the "playlist" batch. Smart
        # playlists are excluded - they carry no static member list,
        # so capturing one is near-zero work and it does not tick the
        # batch below. Counting them in the total would leave the
        # Process List bar permanently short of 100%.
        _section_playlist_count = sum(
            1 for pl, items in playlist_cache
            if _primary_section_for_playlist(items) == section_key_s
            and not getattr(pl, "smart", False)
        )
        if state.get_dashboard() and _section_playlist_count:
            state.get_dashboard().add_batch_total("playlist", _section_playlist_count)
            state.get_dashboard().add_run_total(_section_playlist_count)

        for pl, items in playlist_cache:
            # Stop P3: item-level checkpoint (see snapshot_watch_history).
            if stop_event is not None and stop_event.is_set():
                logger.info(
                    "[%s] playlists: stop requested - halting after "
                    "%d playlist(s)", label, len(result),
                )
                break
            try:
                primary = _primary_section_for_playlist(items)
                # Skip playlists with no items carrying a librarySectionID
                # - those are empty playlists or auto-generated entries
                # whose pl.items() failed during cache warm-up. An empty
                # playlist has no meaningful content to migrate and
                # silently dropping it here avoids the alternative of
                # duplicating its name into every selected library.
                if primary is None:
                    continue
                if primary != section_key_s:
                    continue

                with _current_item(label, "playlist", pl.title, phase="capturing"):
                    result.append(serialize_playlist(pl, prefetched_items=items))
                # Only non-smart playlists tick the batch - this must
                # match the ``_section_playlist_count`` registered
                # above or the bar never reaches 100%.
                if state.get_dashboard() and not getattr(pl, "smart", False):
                    state.get_dashboard().inc_playlist()
                if state._media_logger:
                    state._media_logger.debug(_fmt_media_line(
                        "EXPORT", label, "playlist", pl.title,
                        items=len(items),
                    ))
            except Exception as e:
                logger.debug(f"Skipping playlist '{pl.title}': {e}")
        logger.info(f"[{label}] Playlist snapshot: {len(result)} playlist(s) found")
    except Exception as e:
        logger.error(f"Error fetching playlists: {e}")
    return result


def snapshot_collections(
    section,
    logger: logging.Logger,
    skip_rating_keys: Optional[Set] = None,
    fast_owner_detection: bool = False,
    stop_event: Optional[threading.Event] = None,
) -> List[Dict]:
    """
    Fetches collections from a library section with three-layer optimisation
    for the per-user call path (v0.12.3).

    **Layer 1 - Early exit** (always active when ``skip_rating_keys`` is
    provided): fetch the lightweight collection list once, compare rating-key
    sets. If every collection the user can see is already in
    ``skip_rating_keys``, this user has zero personal collections - return
    an empty list immediately without touching any collection object. On a
    server with 12 users and 300 library-wide collections this fires on
    almost every user pass and costs exactly one API call.

    **Layer 2 - skip_rating_keys** (always active when provided): inside
    the per-item loop, skip any collection whose ``ratingKey`` is in the
    owner's set before ``coll.items()``, ``serialize_collection()``, the
    media-log write, or the dashboard counter are invoked. This eliminates
    the N × M redundant API calls that were inflating run time and logs.

    **Layer 3 - fast_owner_detection** (opt-in, requires modern Plex):
    check ``coll.librarySectionUserID`` before the rating-key lookup. On
    Plex servers ≥ ~1.32 this attribute is populated:
    ``None`` / ``0`` = library-wide (admin-owned), any other value =
    personal collection owned by a managed user. When available this makes
    the per-collection decision a single attribute read rather than a set
    lookup - marginally faster, but more importantly it works even if
    ``skip_rating_keys`` is not fully populated. Falls back to the
    rating-key check automatically when the attribute is absent so it is
    safe to enable on mixed-version Plex environments; the worst outcome
    is that a personal collection that looks library-wide (because
    ``librarySectionUserID`` is None on old Plex) is caught by the
    ``skip_rating_keys`` fallback. Disable this flag on Plex servers older
    than 1.32 to be safe.

    Args:
        section:              python-plexapi LibrarySection.
        logger:               Shared logger.
        skip_rating_keys:     Set of ratingKey values already processed
                              under the owner context. Absent = owner-level
                              call, so process everything.
        fast_owner_detection: Try ``librarySectionUserID`` before falling
                              back to ``skip_rating_keys``. Default False.

    Returns:
        List of serialised collection dicts. Personal-only when
        ``skip_rating_keys`` is provided; all collections otherwise.
    """
    result = []
    skipped = 0
    # PR-2 / Phase C (auth refactor - collections dedup as safety net):
    # the auth doc's premise is that user-scoped tokens make the
    # rating-key dedup at Layer 2 unnecessary because the Plex API
    # already returns only what the user sees. We keep the dedup as a
    # safety net AND count how often it fires so we can later decide
    # whether it can be removed. ``layer2_skipped`` tracks rating-key
    # dedup hits specifically (separately from Layer 3 fast-detection)
    # so the metric isn't muddled by both layers.
    layer2_skipped = 0
    try:
        raw: List[Any] = list(section.collections())

        # ── Layer 1: early exit ───────────────────────────────────────
        # Build rating-key set from the cheap metadata we already have
        # (ratingKey is always present on the list response - no extra
        # API call needed). If every key is library-wide, this user has
        # no personal collections at all; skip the entire loop.
        if skip_rating_keys:
            user_keys: Set[Any] = {c.ratingKey for c in raw}
            if user_keys <= skip_rating_keys:
                logger.debug(
                    "[%s] No personal collections for this user - skipped (%d library-wide)",
                    section.title, len(user_keys),
                )
                return []

        # Discover-don't-predict: pre-count the collections this pass
        # will actually process. The Layer 2/3 skip checks below are
        # cheap attribute reads, so replicating the decision here costs
        # nothing and gives the "collection" batch a real denominator
        # that matches the ``inc_collection`` ticks in the loop.
        _eligible = 0
        for _c in raw:
            if fast_owner_detection:
                _oid = getattr(_c, "librarySectionUserID", None)
                if _oid is not None and not _oid:
                    continue
            if skip_rating_keys is not None and _c.ratingKey in skip_rating_keys:
                continue
            _eligible += 1
        if state.get_dashboard() and _eligible:
            state.get_dashboard().add_batch_total("collection", _eligible)
            state.get_dashboard().add_run_total(_eligible)

        # DIAGNOSTIC: see _log_serialize_diag / snapshot_watch_history.
        # For collections each iteration also calls ``coll.items()``
        # (once in serialize_collection, again for the media log), so a
        # high calls/item ratio here is expected - the useful signal is
        # the absolute call count vs collection count.
        _loop_t0 = time.monotonic()
        _loop_http0 = state.get_http_count()
        for coll in raw:
            # Stop P3: item-level checkpoint (see snapshot_watch_history).
            if stop_event is not None and stop_event.is_set():
                logger.info(
                    "[%s] collections: stop requested - halting after "
                    "%d collection(s)", section.title, len(result),
                )
                break
            # ── Layer 3: fast owner-detection via librarySectionUserID ─
            if fast_owner_detection:
                owner_id = getattr(coll, "librarySectionUserID", None)
                if owner_id is not None:
                    # Attribute is populated on this Plex build.
                    # ``0`` or falsy == library-wide (admin-created).
                    # Any non-zero value == personal (user-created).
                    if not owner_id:
                        skipped += 1
                        continue  # library-wide - skip all expensive work
                    # Non-zero: personal collection, fall through to process
                else:
                    # Attribute absent - this Plex build doesn't expose it.
                    # Fall back to rating-key check (Layer 2 below).
                    pass

            # ── Layer 2: rating-key dedup ─────────────────────────────
            if skip_rating_keys is not None and coll.ratingKey in skip_rating_keys:
                skipped += 1
                layer2_skipped += 1
                continue  # library-wide confirmed - skip all expensive work

            # This collection is either owner-level or genuinely personal
            # - process it fully.
            with _current_item(section.title, "collection", coll.title, phase="capturing"):
                result.append(serialize_collection(coll))
            if state.get_dashboard():
                state.get_dashboard().inc_collection()
            if state._media_logger:
                try:
                    n_members = len(coll.items())
                except Exception:
                    n_members = None
                state._media_logger.debug(_fmt_media_line(
                    "EXPORT", section.title, "collection", coll.title,
                    items=n_members,
                ))

        _log_serialize_diag(logger, section.title, "collections",
                            len(result), _loop_t0, _loop_http0)
        logger.info(
            "[%s] Collection snapshot: %d collection(s) found%s",
            section.title, len(result),
            f" ({skipped} library-wide skipped)" if skipped else "",
        )
        # PR-2 / Phase C: explicit signal when Layer 2 (rating-key
        # dedup) actually catches anything, so we can later judge
        # whether the safety net is still earning its keep after the
        # per-user-token refactor is in place. If this never logs
        # across a representative run, the dedup is fully redundant
        # and a future release can remove it.
        if layer2_skipped:
            logger.info(
                "[%s] Collections dedup safety-net fired: "
                "Layer 2 (rating-key) caught %d library-wide collection(s) "
                "that the user-scoped token returned. If this line never "
                "appears in normal runs the dedup can be retired.",
                section.title, layer2_skipped,
            )
    except Exception as e:
        logger.error("Error fetching collections for %s: %s", section.title, e)
    return result


def snapshot_ratings(
    section, logger: logging.Logger, user: str = "Plex Owner",
    stop_event: Optional[threading.Event] = None,
    # Perf #2: shared library-item list pre-fetched by
    # ``_bulk_fetch_for_filters``. ``prefetched_items`` is the leaf
    # list (tracks / episodes / movies); ``prefetched_shows`` is the
    # show-level container list, only meaningful for show libraries
    # where ratings can live on both shows and episodes. Either may
    # be ``None`` to keep the per-task server-side-filter path - a
    # first-class strategy for runs where only one of watch / ratings
    # is wanted (no point over-fetching the whole library for one
    # type) and for home-user gathers (per-user sections differ, so
    # cross-user sharing doesn't apply).
    prefetched_items: Optional[List] = None,
    prefetched_shows: Optional[List] = None,
) -> List[Dict]:
    """
    Fetches all items in a library that have a user star rating.

    User ratings (1–10 stars) are separate from Play Count. We snapshot them
    independently so they can be restored even if the watch count on the target
    is already higher than what we saved.

    Args:
        section: A python-plexapi LibrarySection.
        logger (Logger): Shared logger.
        user (str): Username label for the media log.

    Returns:
        List of dicts, each containing the item's identifiers and its rating.
    """
    rated = []
    try:
        libtype = section.type
        if prefetched_items is not None:
            # Perf #2: filter the shared bulk-fetched list locally.
            leaf_rated = [
                it for it in prefetched_items
                if getattr(it, "userRating", None) is not None
            ]
            if libtype == "show" and prefetched_shows is not None:
                show_rated = [
                    s for s in prefetched_shows
                    if getattr(s, "userRating", None) is not None
                ]
                all_items = show_rated + leaf_rated
                logger.info(
                    "[%s] ratings: filtering shared bulk-fetched lists "
                    "(%d show candidates → %d rated; "
                    "%d episode candidates → %d rated)",
                    section.title,
                    len(prefetched_shows), len(show_rated),
                    len(prefetched_items), len(leaf_rated),
                )
            else:
                all_items = leaf_rated
                logger.info(
                    "[%s] ratings: filtering shared bulk-fetched list "
                    "(%d candidates → %d rated)",
                    section.title, len(prefetched_items), len(leaf_rated),
                )
        elif libtype == "artist":
            try:
                all_items = section.searchTracks(userRating__gt=0)
            except Exception as _filter_exc:
                logger.warning(
                    "[%s] ratings: server-side userRating filter failed "
                    "(%s) - falling back to full searchTracks() walk",
                    section.title, _filter_exc,
                )
                all_items = [t for t in section.searchTracks()
                             if getattr(t, "userRating", None) is not None]
        elif libtype == "show":
            try:
                show_items = section.search(userRating__gt=0)
            except Exception as _filter_exc:
                logger.warning(
                    "[%s] ratings: server-side userRating filter failed "
                    "(%s) - falling back to full section.all() show walk",
                    section.title, _filter_exc,
                )
                show_items = [s for s in section.all()
                              if getattr(s, "userRating", None) is not None]
            try:
                ep_items = section.searchEpisodes(userRating__gt=0)
            except Exception as _filter_exc:
                logger.warning(
                    "[%s] ratings: server-side userRating filter failed "
                    "(%s) - falling back to full searchEpisodes() walk",
                    section.title, _filter_exc,
                )
                ep_items = [ep for ep in section.searchEpisodes()
                            if getattr(ep, "userRating", None) is not None]
            all_items = show_items + ep_items
        else:
            try:
                all_items = section.search(userRating__gt=0)
            except Exception as _filter_exc:
                logger.warning(
                    "[%s] ratings: server-side userRating filter failed "
                    "(%s) - falling back to full section.all() walk",
                    section.title, _filter_exc,
                )
                all_items = section.all()

        # Discover-don't-predict: register the real rated-item count.
        _rated_count = sum(
            1 for it in all_items if getattr(it, "userRating", None) is not None
        )
        if state.get_dashboard() and _rated_count:
            state.get_dashboard().add_batch_total("rating", _rated_count)
            state.get_dashboard().add_run_total(_rated_count)

        # DIAGNOSTIC: see _log_serialize_diag / snapshot_watch_history.
        _loop_t0 = time.monotonic()
        _loop_http0 = state.get_http_count()
        for item in all_items:
            # Stop P3: item-level checkpoint (see snapshot_watch_history).
            if stop_event is not None and stop_event.is_set():
                logger.info(
                    "[%s] ratings: stop requested - halting after %d item(s)",
                    section.title, len(rated),
                )
                break
            rating = getattr(item, "userRating", None)
            if rating is not None:
                with _current_item(section.title, item.type, item.title, phase="capturing"):
                    # Use the same serialise helper watch_history uses
                    # so the record carries ``rating_key`` and the rest
                    # of the canonical shape. Pre-fix this function
                    # built the dict by hand and omitted ``rating_key``;
                    # ``ingest_snapshot_payload`` then skipped every
                    # rating because its ``rating_key_to_item_id`` map
                    # had no entry for the per-user rating walk.
                    #
                    # ``rating`` is the canonical key the engine's
                    # media.db ingest reads. ``user_rating`` (set by
                    # serialize_item from item.userRating) is the
                    # legacy key the JSON-file importer reads. Both
                    # carry the same value so the round-trip works
                    # regardless of which path the consumer takes.
                    entry = serialize_item(item, user=user)
                    entry["rating"] = rating
                    rated.append(entry)
                if state.get_dashboard():
                    state.get_dashboard().inc_rating()
                if state._media_logger:
                    state._media_logger.debug(_fmt_media_line(
                        "EXPORT", section.title, item.type, item.title,
                        user=user,
                        rating=rating,
                        artist=entry.get("artist") or entry.get("show_title"),
                        path=_safe_file_path(item),
                    ))
        _log_serialize_diag(logger, section.title, "ratings",
                            len(rated), _loop_t0, _loop_http0)
        logger.info(f"[{section.title}] Ratings snapshot: {len(rated)} item(s) found (user: {user})")
    except Exception as e:
        logger.error(f"Error fetching ratings for {section.title}: {e}")
    return rated


def snapshot_library(
    server: PlexServer,
    section,
    output_dir: str,
    logger: logging.Logger,
    home_users: Optional[List[Tuple[str, str, PlexServer]]] = None,
    playlist_caches: Optional[Dict[int, List[Tuple[Any, List]]]] = None,
    stop_event: Optional[threading.Event] = None,
    skip_collections: bool = False,
    fast_collection_detection: bool = False,
    skip_playlists: bool = False,
    run_lazy_caches: Optional[Dict[int, List[Tuple[Any, List]]]] = None,
    # v0.14 - when False, the owner-phase gather pool is skipped
    # entirely and only home-user data is captured. ``run_snapshot``
    # derives this from the user_filter (False when owner email is
    # absent from the filter list). Default True preserves historical
    # behaviour for ad-hoc / unfiltered runs.
    owner_included: bool = True,
    # PR-3 / Phase D - four-flag data-type filter. ``skip_*`` is folded
    # into the include_* form by ``run_snapshot`` before this is called,
    # so the per-library gather only needs to consult the include_*
    # flags. The legacy args stay accepted for callers that haven't
    # migrated yet.
    include_watch_history: bool = True,
    include_ratings: bool = True,
    include_playlists: bool = True,
    include_collections: bool = True,
    # ── Testability seams ────────────────────────────────────────────
    # Both keyword-only, both default to None and fall back to the
    # services.state run-state globals, so the positional call sites in
    # run_snapshot are byte-for-byte unaffected. Passing them explicitly
    # lets a unit test drive snapshot_library end-to-end without poking
    # module globals: ``server_id`` is the function's one required
    # input, ``payload_sink`` is its output collector.
    *,
    server_id: Optional[str] = None,
    payload_sink: Optional[List[Dict]] = None,
) -> str:
    """
    Snapshots a single library to a .plexexport.json file.

    Each library gets its own export file so the user can choose which
    libraries to import individually. The four data-gathering tasks
    (Play Count, playlists, collections, ratings) run concurrently
    within the library to reduce total snapshot time.

    Args:
        server (PlexServer): Active admin server connection.
        section: A python-plexapi LibrarySection to snapshot.
        output_dir (str): Directory where the .plexexport.json will be written.
        logger (Logger): Shared logger.
        home_users (list, optional): List of (username, token, server) tuples.
        playlist_caches (dict, optional): Pre-fetched playlist caches keyed
            by ``id(server)``. Built once in run_snapshot and shared across
            libraries + users to avoid repeated server.playlists() round-trips.

    Returns:
        Absolute path to the written .plexexport.json file as a string.
    """
    lib_name = section.title
    # Server label for log + activity-feed context, so multi-server /
    # fan-out runs make it obvious which server's data a line belongs
    # to (and which home users are being assigned to which server).
    server_name = getattr(server, "friendlyName", "") or "?"
    logger.info(f"Capturing snapshot library: {lib_name} [server: {server_name}]")
    # v0.9.6 Feature 2: tag every Plex API call this library makes
    # with the library name so the Network panel can attribute traffic
    # per-library. Set on the task-local context (each snapshot_library
    # call runs in its own copied context via submit_with_context from
    # run_snapshot, so the var is library-scoped).
    _http_lib_var.set(lib_name)
    if state.get_dashboard():
        state.get_dashboard().push_activity("started", lib_name, "Snapshot started")
        state.get_dashboard().set_library_phase(lib_name, "Capturing snapshot…")
        # v0.9.6 Feature 1: attribute owner-phase work to the owner
        # email. gather_user temporarily overrides this while its
        # block runs.
        # v0.9.7 Item 4: gated - standard snapshots never narrow the
        # run to one user, so the header field stays null.
        if state._current_user_visible:
            state.get_dashboard().set_current_user(state._plex_owner_email or None)

    results: Dict[str, List] = {
        "watch_history": [],
        "playlists": [],
        "collections": [],
        "ratings": [],
    }

    def _advance():
        _advance_lib(lib_name)

    # Perf #2: watch+ratings capture strategy. Resolves
    # per-server override → global default → "smart".
    #
    #   "smart"             - bulk-fetch when BOTH watch+ratings wanted,
    #                         server-side filter when only one wanted.
    #   "force_bulk"        - always bulk-fetch + local filter, even
    #                         for single-type runs (best when Plex is
    #                         rate-limited / hits 429s).
    #   "force_server_side" - always server-side filter (no shared
    #                         prefetch). Best when wire-traffic back
    #                         from the server is the constraint.
    strategy = _resolve_watch_ratings_strategy(server_id=server_id, logger=logger)
    if strategy == "force_server_side":
        # Skip the prefetch entirely; each gather will use its
        # per-task server-side-filter path.
        shared_prefetch: Optional[Dict[str, Any]] = None
        logger.info(
            "[%s] watch+ratings strategy=force_server_side - skipping shared bulk-fetch",
            lib_name,
        )
    else:
        want_prefetch = (
            strategy == "force_bulk"
            or (include_watch_history and include_ratings)
        )
        shared_prefetch = None
        if want_prefetch:
            # Only show libraries need the show-level container in
            # addition to episodes; artist/movie ratings live on the
            # same leaf list watch-history reads. For force_bulk we
            # still bulk-fetch even when only one type is wanted -
            # the user opted into the API-call-saving trade-off.
            _want_shows = (section.type == "show")
            shared_prefetch = _bulk_fetch_for_filters(
                section, logger, want_shows=_want_shows,
            )
            if shared_prefetch.get("ok"):
                logger.info(
                    "[%s] shared bulk-fetch for watch+ratings "
                    "(strategy=%s): %d item(s)%s in %.1fs, %d Plex HTTP call(s)",
                    lib_name, strategy,
                    len(shared_prefetch.get("items") or []),
                    f" + {len(shared_prefetch.get('shows') or [])} show(s)"
                        if shared_prefetch.get("shows") else "",
                    shared_prefetch.get("fetch_seconds", 0.0),
                    shared_prefetch.get("http_calls", 0),
                )
            else:
                # Failed bulk fetch → don't pass partial / empty lists
                # to the gathers; let them use the per-task
                # server-side-filter path so a transient Plex hiccup
                # doesn't silently lose data.
                shared_prefetch = None

    def gather_watch():
        if not include_watch_history:
            # PR-6: per-library skip notices are INFO. The operator
            # uncheckd this type and seeing one confirmation per
            # library is the expected normal-operations output -
            # not noise. The redundant top-level summary line is
            # what was removed in PR-6, not these.
            logger.info("[%s] Watch history skipped (include_watch_history=False)", lib_name)
            _advance()
            return
        cat = "play_count" if section.type == "artist" else "watched"
        with _thread_category(cat):
            results["watch_history"] = snapshot_watch_history(
                section, logger, user=state._plex_owner_name,
                stop_event=stop_event,
                prefetched_items=(
                    shared_prefetch.get("items") if shared_prefetch else None
                ),
            )
        if state.get_dashboard():
            state.get_dashboard().set_library_phase(lib_name, "Watch History ✓")
            state.get_dashboard().push_activity("phase", lib_name, f"Watch History → {len(results['watch_history'])} items")
        _advance()

    def gather_playlists():
        # PR-3 / Phase D: skip_playlists (legacy) and include_playlists
        # are honoured together - either disables the gather.
        if skip_playlists or not include_playlists:
            logger.info("[%s] Playlists skipped (include_playlists=False)", lib_name)
            _advance()
            return
        cache = playlist_caches.get(id(server)) if playlist_caches else None
        with _thread_category("playlists"):
            results["playlists"] = snapshot_playlists(
                server, section.key, logger, lib_name=lib_name, playlist_cache=cache,
                skip_playlists=skip_playlists, run_caches=run_lazy_caches,
                stop_event=stop_event,
            )
        if state.get_dashboard():
            state.get_dashboard().set_library_phase(lib_name, "Playlists ✓")
            state.get_dashboard().push_activity("phase", lib_name, f"Playlists → {len(results['playlists'])} items")
        _advance()

    def gather_collections():
        if skip_collections or not include_collections:
            logger.info("[%s] Collections skipped (include_collections=False)", lib_name)
            _advance()
            return
        with _thread_category("collections"):
            results["collections"] = snapshot_collections(
                section, logger, stop_event=stop_event,
            )
        if state.get_dashboard():
            state.get_dashboard().set_library_phase(lib_name, "Collections ✓")
            state.get_dashboard().push_activity("phase", lib_name, f"Collections → {len(results['collections'])} items")
        _advance()

    def gather_ratings():
        if not include_ratings:
            logger.info("[%s] Ratings skipped (include_ratings=False)", lib_name)
            _advance()
            return
        with _thread_category("ratings"):
            results["ratings"] = snapshot_ratings(
                section, logger, user=state._plex_owner_name,
                stop_event=stop_event,
                prefetched_items=(
                    shared_prefetch.get("items") if shared_prefetch else None
                ),
                prefetched_shows=(
                    shared_prefetch.get("shows") if shared_prefetch else None
                ),
            )
        if state.get_dashboard():
            state.get_dashboard().set_library_phase(lib_name, "Ratings ✓")
            state.get_dashboard().push_activity("phase", lib_name, f"Ratings → {len(results['ratings'])} items")
        _advance()

    def gather_user(username: str, user_server: PlexServer):
        """Thread task: fetch one home user's Play Count, playlists, ratings, and personal collections."""
        # v0.9.6 Feature 1: surface this user in the dashboard header
        # while their block runs. Restored on exit so a sibling
        # gather_user that completes after this one doesn't show this
        # username instead of its own.
        # v0.9.7 Item 4: gated. Standard snapshots always run with this
        # off; the field stays null for the whole run.
        prev_user = state.get_dashboard().current_user if state.get_dashboard() else None
        if state.get_dashboard() and state._current_user_visible:
            state.get_dashboard().set_current_user(username)
        try:
            user_section = next(
                (s for s in user_server.library.sections() if s.title == lib_name),
                None,
            )
            if user_section is None:
                logger.warning(
                    f"Library '{lib_name}' not visible to home user '{username}' - skipped"
                )
                return
            user_cache = playlist_caches.get(id(user_server)) if playlist_caches else None
            with _thread_category("home_user"):
                # PR-3 / Phase D - each include_* flag gates its
                # corresponding per-user gather. Defaults preserve
                # pre-Phase-D behaviour exactly.
                u_watch = (
                    snapshot_watch_history(
                        user_section, logger, user=username,
                        stop_event=stop_event,
                    )
                    if include_watch_history else []
                )
                u_ratings = (
                    snapshot_ratings(
                        user_section, logger, user=username,
                        stop_event=stop_event,
                    )
                    if include_ratings else []
                )
                u_playlists = (
                    snapshot_playlists(
                        user_server, user_section.key, logger,
                        lib_name=lib_name, playlist_cache=user_cache,
                        skip_playlists=skip_playlists, run_caches=run_lazy_caches,
                        stop_event=stop_event,
                    )
                    if include_playlists else []
                )
                # v0.9.7 Item 9 / v0.12.3 optimisation: per-user
                # personal collections. ``snapshot_collections`` now takes
                # ``skip_rating_keys`` + ``fast_owner_detection`` and
                # applies three layers of filtering BEFORE any expensive
                # per-collection work fires:
                #   1. Early exit  - if all user keys ⊆ owner_coll_keys
                #      return [] immediately (no iteration at all).
                #   2. skip_rating_keys - skip coll.items() / serialize /
                #      log for library-wide collections mid-loop.
                #   3. fast_owner_detection (opt-in) - check
                #      librarySectionUserID to skip without a set lookup.
                # The post-loop list comprehension is gone; the function
                # already returns personal-only collections.
                u_collections = (
                    [] if (skip_collections or not include_collections)
                    else snapshot_collections(
                        user_section, logger,
                        skip_rating_keys=owner_coll_keys,
                        fast_owner_detection=fast_collection_detection,
                        stop_event=stop_event,
                    )
                )
            users_data[username] = {
                "watch_history": u_watch,
                "ratings": u_ratings,
                "playlists": u_playlists,
                "collections": u_collections,
            }
            logger.info(
                f"Home user '{username}' [server: {server_name}] - {lib_name}: "
                f"{len(u_watch)} watched, {len(u_ratings)} rated, "
                f"{len(u_playlists)} playlist(s), "
                f"{len(u_collections)} personal collection(s)"
            )
            # Surface home-user work in the dashboard activity feed -
            # one combined entry per user (per-phase would flood the
            # 8-slot feed with 4x the home-user count). The owner's
            # gathers each push their own phase entry above; this is
            # the home-user equivalent so the feed isn't owner-only.
            if state.get_dashboard():
                state.get_dashboard().push_activity(
                    "phase", lib_name,
                    f"Home user '{username}' [{server_name}] → "
                    f"{len(u_watch)} watched · {len(u_ratings)} rated · "
                    f"{len(u_playlists)} playlist(s) · "
                    f"{len(u_collections)} collection(s)",
                )
        except Exception as e:
            logger.warning(f"Could not snapshot data for home user '{username}': {e}")
        finally:
            if state.get_dashboard() and state._current_user_visible:
                state.get_dashboard().set_current_user(prev_user)
            _advance()

    users_data: Dict[str, Dict] = {}
    # v0.9.7 Item 9: the per-user gather subtracts the owner's
    # collection rating-keys to isolate personal collections. Built
    # after the owner-side gather pool finishes (below) so the set
    # is guaranteed populated before any gather_user runs. Captured
    # in this closure so gather_user can read it without arg-threading.
    owner_coll_keys: Set[Any] = set()
    n_user_tasks = len(home_users or [])

    # If stop was requested before this library even started, exit
    # before opening the pool so the user's click takes effect at the
    # next library boundary instead of running this one to completion.
    if stop_event is not None and stop_event.is_set():
        logger.info(f"Stop requested - skipping library '{lib_name}'.")
        if state.get_dashboard():
            state.get_dashboard().finish_library(lib_name, error=False)
        return ""

    # v0.9.7 Item 9: TWO-PHASE gather (Q1 confirmed).
    # Phase 1 - owner-side: watch / playlists / collections / ratings
    #   run in parallel. Library-level data lives in ``results``.
    # Phase 2 - per-user: each managed user reads their own data via
    #   their token-bound server connection. Per-user personal
    #   collections need the owner's collection set computed from
    #   Phase 1's output, so Phase 2 can't start until Phase 1 ends.
    #
    # v0.14 - when ``owner_included`` is False (the operator excluded
    # the owner via user_filter), Phase 1 is skipped entirely. Phase
    # 2 still fires for every managed user that survived the filter.
    if owner_included:
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            owner_futures = [
                submit_with_context(pool, gather_watch),
                submit_with_context(pool, gather_playlists),
                submit_with_context(pool, gather_collections),
                submit_with_context(pool, gather_ratings),
            ]
            for f in concurrent.futures.as_completed(owner_futures):
                exc = f.exception()
                if exc:
                    logger.error(f"Error in owner gather thread for {lib_name}: {exc}")
    else:
        logger.info(
            "[%s] Owner excluded by user_filter - skipping Phase 1 (library-wide gather). "
            "Phase 2 (per-user) will still fire for the %d included managed user(s).",
            lib_name, len(home_users or []),
        )
        # Advance the library counter for the four owner-phase tasks
        # that didn't run; the dashboard's per-library bar otherwise
        # stops at "0/X" forever waiting for ticks that never come.
        for _ in range(4):
            _advance()

    # Phase 1 done - owner_coll_keys is now safe to populate from the
    # owner's collections result.
    owner_coll_keys = {
        c.get("rating_key")
        for c in (results.get("collections") or [])
        if c.get("rating_key") is not None
    }

    # Phase 2 - per-user gathers, only if any users exist for this run.
    if n_user_tasks > 0:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, n_user_tasks)) as pool:
            futures = []
            for username, _, user_server in (home_users or []):
                if stop_event is not None and stop_event.is_set():
                    logger.info(
                        f"Stop requested - skipping home user '{username}' "
                        f"for library '{lib_name}'."
                    )
                    continue
                futures.append(submit_with_context(pool, gather_user, username, user_server))

            for f in concurrent.futures.as_completed(futures):
                exc = f.exception()
                if exc:
                    logger.error(f"Error in gather thread for {lib_name}: {exc}")

    # v0.13.0: unified users map. The owner is folded into ``users``
    # with ``role='owner'``; managed users are ``role='managed'``.
    # There's no longer a top-level ``items`` block - that owner-vs-
    # users asymmetry forced every downstream consumer to maintain
    # two code paths. Now the restorer iterates one map and reads
    # the role to decide which token to use.
    owner_display = state._plex_owner_name or "Plex Owner"
    owner_json_key = owner_display or "Plex Owner"
    # Collision-safe: if a managed user shares the owner's display
    # name, suffix the owner's JSON key until unique. Highly unlikely
    # in practice but free defense.
    _used_keys = set(users_data.keys())
    _bk, _n = owner_json_key, 2
    while owner_json_key in _used_keys:
        owner_json_key = f"{_bk} ({_n})"
        _n += 1

    unified_users: Dict[str, Any] = {
        owner_json_key: {
            "role": "owner",
            "display_name": owner_display,
            "backend_user_id": None,  # populated when MyPlexAccount.id is known
            "watch_history": results.get("watch_history", []),
            "ratings":       results.get("ratings", []),
            "playlists":     results.get("playlists", []),
            "collections":   results.get("collections", []),
        },
    }
    for u_handle, u_block in users_data.items():
        if not isinstance(u_block, dict):
            continue
        unified_users[u_handle] = {
            "role": "managed",
            "display_name": u_handle,
            "backend_user_id": None,
            "watch_history": u_block.get("watch_history", []),
            "ratings":       u_block.get("ratings", []),
            "playlists":     u_block.get("playlists", []),
            "collections":   u_block.get("collections", []),
        }

    # v0.15 integrity-anchor contract: every per-library payload MUST
    # carry library_section_id (Plex's numeric section key) and
    # library_section_type. ``ingest_snapshot_payload`` asserts on
    # these and refuses to write rows without them. See
    # ``server/media_db.py`` schema migration v8 for the rationale.
    export_data = {
        "library": lib_name,
        "library_section_id": int(getattr(section, "key", 0) or 0),
        "library_section_type": str(getattr(section, "type", "") or ""),
        "captured_at": datetime.now().isoformat(),
        # v0.13.0: server identity + run provenance consolidated into
        # snapshot_meta to match the serializer's shape. ``backend``
        # is the forward-looking field that lets future Jellyfin /
        # Emby adapters write payloads readable by the same engine.
        "snapshot_meta": {
            "server_id": getattr(state, "_snapshot_server_id", "") or "",
            "server_name": getattr(server, "friendlyName", "") or "",
            "server_url": state._plex_base_url or "",
            "server_machine_id": getattr(server, "machineIdentifier", "") or "",
            "server_version": server.version,
            "backend": "plex",
            # how this run was triggered - "manual" for a GUI / API
            # submission, "schedule" for a scheduler fire.
            # schedule_name is the display name when trigger=="schedule".
            "trigger": state._run_trigger or "",
            "schedule_name": state._run_schedule_name or "",
        },
        "users": unified_users,
        "stats": {
            "total_watched": len(results["watch_history"]),
            "total_playlists": len(results["playlists"]),
            "total_collections": len(results["collections"]),
            "total_rated": len(results["ratings"]),
            "total_home_users": len(users_data),
        },
    }

    # PR-13 fix #3: the engine is media.db-primary now. The per-
    # library payload we just built in memory is ingested directly
    # into media.db via :func:`server.media_db.ingest_snapshot_payload`,
    # which enforces the server-wide vs user-private dedup discipline
    # for playlists and collections. No JSON file is written here -
    # the on-demand serialiser in ``server/snapshot_serializer.py``
    # rebuilds the JSON shape from the snapshot ``.db`` when the
    # operator clicks Download in the Exports panel.
    #
    # ``server_id`` comes from ``state._snapshot_server_id`` (the
    # job runner / CLI sets it before run_snapshot fires). When it's
    # missing - which should never happen because the job runner
    # rejects unregistered servers, and the CLI now requires
    # ``--source-server`` for ``--snapshot`` - we log a hard error
    # and bail rather than silently losing the run's data.
    # ``server_id`` may be injected (unit tests); otherwise fall back to
    # the module global the job runner / CLI sets before run_snapshot
    # fires. A caller that explicitly passes "" still hits the bail below.
    if server_id is None:
        server_id = getattr(state, "_snapshot_server_id", "") or ""
    if not server_id:
        logger.error(
            "Snapshot of library %r could not be persisted: no "
            "registered server_id in state. The job runner / CLI "
            "must set state._snapshot_server_id before run_snapshot "
            "fires. Library data is in memory and will be lost when "
            "this function returns.",
            lib_name,
        )
        return ""

    try:
        from server import media_db
    except Exception as e:
        logger.error("media_db import failed: %s; cannot persist snapshot", e)
        return ""

    # v0.14 - media.db caching is now opt-in via the
    # ``cache_snapshot_payloads_to_media_db`` tunable, with a one-shot
    # auto-seed for any server that hasn't been ingested yet. See
    # ``_should_cache_payload_to_media_db`` for the resolution rule.
    # Note: the .db snapshot artifact + JSON sidecar are payload-
    # direct (Rule 1) so they're unaffected by this decision.
    if _should_cache_payload_to_media_db(server_id, lib_name, logger):
        try:
            counters = media_db.ingest_snapshot_payload(server_id, export_data)
        except Exception:
            logger.exception(
                "ingest_snapshot_payload failed for library %r on server %r",
                lib_name, server_id,
            )
            return ""
    else:
        # Skipping the ingest. Build an empty counter dict so the
        # downstream code path (logging, payload collector) keeps
        # working without a media.db write.
        counters = {}
        logger.info(
            "[%s] media.db caching skipped (cache_snapshot_payloads_to_media_db=false, "
            "server already seeded) - snapshot.db + JSON sidecar are unaffected.",
            lib_name,
        )

    # Rule 1: append the live-fetched payload to the run-scoped
    # collector. ``_capture_snapshot_after_run`` reads this list to
    # build the snapshot.db directly - media.db is no longer the
    # source of truth for snapshot content. The ingest above remains
    # a side-effect cache so the resolver Tier-0 / Tier-1 lookups on
    # the next run still benefit, but it does NOT feed snapshot
    # rendering.
    #
    # ``state._snapshot_payloads`` is a plain module-level list (not
    # a ContextVar). Some snapshotter call sites submit per-library
    # workers via ``pool.submit`` rather than ``submit_with_context``,
    # so a ContextVar-backed collector would be invisible to those
    # workers. The list is reset in place by ``reset_run_state`` so
    # every run starts empty.
    # ``payload_sink`` may be injected (unit tests); otherwise fall back
    # to the module-global collector that _capture_snapshot_after_run reads.
    sink = state._snapshot_payloads if payload_sink is None else payload_sink
    sink.append(export_data)

    # The "-> media.db" tail only makes sense when we actually wrote
    # to media.db. When the cache-decision short-circuited (counters
    # is empty), report the payload collection without the misleading
    # "items=0, ..." breakdown.
    if counters:
        logger.info(
            "Snapshot[%s]: %d watched, %d playlists, %d collections -> "
            "media.db (items=%d, watch_events=%d, ratings=%d, playlists=%d, collections=%d)",
            lib_name,
            export_data["stats"]["total_watched"],
            export_data["stats"]["total_playlists"],
            export_data["stats"]["total_collections"],
            counters.get("items", 0),
            counters.get("watch_events", 0),
            counters.get("ratings", 0),
            counters.get("playlists", 0),
            counters.get("collections", 0),
        )
    else:
        logger.info(
            "Snapshot[%s]: %d watched, %d playlists, %d collections "
            "(payload captured to snapshot.db; media.db caching skipped)",
            lib_name,
            export_data["stats"]["total_watched"],
            export_data["stats"]["total_playlists"],
            export_data["stats"]["total_collections"],
        )
    # Returning the library name keeps the contract "non-empty string
    # means success" for the few callers that test the return value;
    # ``out_path`` no longer exists since nothing was written.
    return lib_name


# ── Snapshot Runner ─────────────────────────────────────────────────────────────

def run_snapshot(
    server: PlexServer,
    selected_libs: List,
    output_dir: str,
    logger: logging.Logger,
    log_dir: str,
    base_url: str,
    skip_collections: bool = False,
    fast_collection_detection: bool = False,
    skip_playlists: bool = False,
    skip_playlist_prebuild: bool = False,
    # PR-3 / Phase D - four-flag data-type filter. Defaults preserve
    # pre-Phase-D behaviour exactly. ``skip_*`` legacy flags are
    # honoured alongside (either disables the corresponding type).
    include_watch_history: bool = True,
    include_ratings: bool = True,
    include_playlists: bool = True,
    include_collections: bool = True,
    # v0.14 - per-job user filter. None = capture every user the
    # source server reports (historical default). When supplied, the
    # owner email being absent excludes owner-level data (library-
    # wide watch / playlists / collections); managed usernames
    # absent excludes those users' data. Empty list excludes
    # everyone - legal but unusual.
    user_filter: Optional[List[str]] = None,
    # v0.13.x: library-level concurrency cap, decoupled from the
    # per-library HTTP worker pool (``state.MAX_WORKERS``). ``0``
    # (default) inherits from ``state.MAX_WORKERS`` so the legacy
    # behavior is preserved for any caller that hasn't been updated
    # yet. A positive value caps libraries-in-parallel without
    # touching the per-library worker count.
    library_workers: int = 0,
) -> None:
    """
    Runs the full multi-threaded snapshot pipeline for all selected libraries.

    Each library is exported concurrently (one thread per library). On large
    terminals (≥ 80×22) a full htop-style dashboard is displayed; on small
    terminals the Rich Progress bars are used instead.

    Stop semantics: the [Q] keyboard shortcut (CLI) and /api/job/stop
    (server) both flip ``stop_event``. The orchestrator finishes the
    libraries currently running inside the ThreadPoolExecutor and stops
    starting new ones; ``f.cancel()`` is best-effort and only takes
    effect on futures that have not yet been picked up by a worker
    thread. Mid-library cancellation is not supported.

    Args:
        server (PlexServer): Active server connection.
        selected_libs (List): List of LibrarySection objects to snapshot.
        output_dir (str): Where to write the .plexexport.json files.
        logger (Logger): Shared logger.
        log_dir (str): Log directory (for keyboard shortcuts).
        base_url (str): Plex server base URL, used to authenticate as home users.
    """
    # One-line summary of what this run covers. The per-library /
    # per-user "skipped" notices are DEBUG so this is the only INFO
    # surface for the operator's data-type filter choices.
    _included = [
        n for n, v in (
            ("watch_history", include_watch_history),
            ("ratings",       include_ratings),
            ("playlists",     include_playlists and not skip_playlists),
            ("collections",   include_collections and not skip_collections),
        ) if v
    ]
    logger.info(
        "Snapshot data types: %s%s",
        ", ".join(_included) if _included else "(none)",
        " (fast_collection_detection on)" if fast_collection_detection else "",
    )
    # Wipe accumulators from any previous run so this run's totals,
    # troubleshoot.log, and unresolved.log only describe this run.
    state.reset_run_state()

    # v0.13.x: resolve the effective libraries-in-parallel pool size.
    # 0 (the default) inherits from state.MAX_WORKERS so a caller that
    # hasn't been updated for the new tunable gets today's behavior
    # unchanged. Any positive value is taken as-is, then clamped to
    # >=1 (a negative slip-through never disables the pool entirely)
    # and capped at the actual library count so the pool can't spawn
    # idle workers.
    _effective_lib_workers = int(library_workers) if library_workers > 0 else int(state.MAX_WORKERS)
    _effective_lib_workers = max(1, min(_effective_lib_workers, max(1, len(selected_libs))))
    logger.info(
        "Snapshot library concurrency: %d (from %s)",
        _effective_lib_workers,
        "settings.snapshot_library_workers" if library_workers > 0 else "settings.workers",
    )

    home_users = get_home_users(server, base_url, logger)
    # v0.14 - apply the operator's user filter. The owner is handled
    # by ``owner_included`` below (it isn't in home_users to begin
    # with - owner-level data flows through the library-wide gather
    # paths). When user_filter is None, every user on the server is
    # included (historical default). When it's a list, only managed
    # usernames present in the list survive.
    owner_included = True
    if user_filter is not None:
        filter_set = {str(s).strip() for s in user_filter if str(s).strip()}
        # Owner is keyed by the source server's myPlexAccount email;
        # any other entry in the list is a managed-user username.
        try:
            owner_email = str(getattr(server.myPlexAccount(), "email", "") or "").strip()
        except Exception:
            owner_email = ""
        owner_included = bool(owner_email) and (owner_email in filter_set)
        before = len(home_users)
        home_users = [
            (title, token, user_server)
            for (title, token, user_server) in home_users
            if str(title) in filter_set
        ]
        logger.info(
            "user_filter applied: owner=%s (%s), managed=%d→%d",
            "included" if owner_included else "EXCLUDED",
            owner_email or "(no email)",
            before, len(home_users),
        )
    n_user_tasks = len(home_users)

    # Timing engine: zero-init the rolling tracker. Totals are
    # discovered as each snapshot phase enumerates its real work
    # (discover-don't-predict) - there is no pre-run estimate.
    dash = state.get_dashboard()
    if dash is not None:
        dash.init_etr_tracker()

    # ── Map each user-bound server to its local server user-id ────────────
    # v0.9.5: ``server.playlists()`` returns every playlist a token can
    # *see* - which includes playlists shared TO the user, not just ones
    # they own. The pre-existing per-server cache therefore walked the
    # same big shared playlist once per recipient (admin + every home
    # user it was shared with), and that fan-out was the dominant cost
    # for mixed-content snapshots. We feed ``build_playlist_cache`` each
    # user's *local* ``SystemAccount.id`` so it can drop shared copies
    # - every playlist is then walked exactly once, by its actual owner.
    server_owner_ids: Dict[int, int] = _resolve_server_owner_ids(server, home_users, logger)

    # ── Pre-fetch playlists once per server connection ─────────────────────
    # Without this cache, snapshot_playlists fires server.playlists() once
    # per library *and* once per library per home user, each call also
    # invoking pl.items() on every playlist. For a 4-library / 11-user
    # server that is ~48 full playlist enumerations per snapshot. The cache
    # collapses it to one fetch per unique server connection (owner +
    # one per home user), built here in parallel before per-library work
    # starts.
    #
    # Playlist cache strategy (v0.12.x + Phase D fix):
    #   include_playlists=False    → no playlist work at all; empty dicts passed down
    #   skip_playlists=True        → equivalent to above (legacy flag)
    #   skip_playlist_prebuild=True → warm block skipped; snapshot_playlists lazily
    #                                  populates run_lazy_caches on first call per
    #                                  server so each server is still only fetched once
    #   all defaults               → parallel pre-warm: one fetch per server upfront,
    #                                  results shared across all libraries × users
    playlist_caches: Dict[int, List[Tuple[Any, List]]] = {}
    run_lazy_caches: Optional[Dict[int, List[Tuple[Any, List]]]] = None

    # Bug fix: the top-level warm used to only check ``skip_playlists``.
    # When the Phase D filter unchecks playlists, the frontend sends
    # ``include_playlists=False`` but leaves the legacy ``skip_playlists``
    # flag at its default False - so this gate fell through and warmed
    # the cache once per managed-user token, producing the "Warming
    # playlist cache for 'My Server' (N playlists)…" lines we shouldn't
    # be seeing on a watch-history-only run. Honour both gates now.
    #
    # PR-6: the previous "Playlist processing skipped" INFO line was
    # removed here - it was redundant noise next to the per-library
    # "[Music] Playlists skipped" lines emitted by gather_playlists(),
    # which already convey the same information with per-library
    # precision.
    if skip_playlists or not include_playlists:
        pass  # nothing to do at the top level - per-library lines cover it
    elif skip_playlist_prebuild:
        # Lazy path: allocate the shared cache dict that snapshot_playlists
        # will populate on first call per server. No upfront API calls.
        run_lazy_caches = {}
        logger.info("Playlist pre-build skipped (skip_playlist_prebuild=True) - fetching lazily per server")
    else:
        unique_servers: List[PlexServer] = [server]
        for entry in home_users:
            unique_servers.append(entry[2])

        def _warm(srv: PlexServer) -> Tuple[int, List[Tuple[Any, List]]]:
            # Register the warm thread so the dashboard's Thread Pool
            # panel shows the active workers during this phase. Pre-fix
            # the warm pool ran headless: Currently Processing showed
            # "fetching" but Active Workers stayed at 0 because no
            # category was registered for the thread.
            with _thread_category("playlists"):
                return id(srv), build_playlist_cache(srv, logger, owner_id=server_owner_ids.get(id(srv)))

        with concurrent.futures.ThreadPoolExecutor(
            max_workers=max(1, min(len(unique_servers), 8))
        ) as warm_pool:
            for sid, cache in warm_pool.map(_warm, unique_servers):
                playlist_caches[sid] = cache
        logger.info(
            f"Playlist cache warmed: {sum(len(c) for c in playlist_caches.values())} "
            f"playlist record(s) across {len(playlist_caches)} server connection(s)"
        )

    # Stop coordination - created here so both the dashboard and small-
    # terminal branches share one event. In server mode, the runtime
    # patches' _server_keyboard_stub stashes a reference to this event
    # so /api/job/stop can flip it; in CLI mode, _keyboard_thread reads
    # raw keypresses and sets it on [Q]. snapshot_library checks it
    # before launching per-library work so stops take effect at the
    # next library boundary.
    stop_event = threading.Event()
    kb = threading.Thread(
        target=_keyboard_thread, args=(log_dir, logger, stop_event), daemon=True
    )
    kb.start()

    if _check_terminal_size():
        # ── Small-terminal fallback: Rich Progress bars ────────────────────────
        state._live_progress = _make_progress()
        for sec in selected_libs:
            state._lib_task_ids[sec.title] = state._live_progress.add_task(
                sec.title,
                total=4 + n_user_tasks,
                completed=0,
                fields={"phase": "Capturing snapshot"},
            )
        overall_task = state._live_progress.add_task(
            "Overall", total=len(selected_libs), completed=0, fields={"phase": ""},
        )
        with Live(state._live_progress, console=console, refresh_per_second=8):
            with concurrent.futures.ThreadPoolExecutor(max_workers=_effective_lib_workers) as pool:
                futures = {
                    submit_with_context(
                        pool,
                        snapshot_library, server, sec, output_dir, logger, home_users, playlist_caches,
                        stop_event, skip_collections, fast_collection_detection, skip_playlists, run_lazy_caches,
                        owner_included,
                        # PR-3 / Phase D - pass the four include_* flags
                        # explicitly. ``snapshot_library`` honours both the
                        # legacy skip_* and the new include_* (either
                        # disables a type), so passing both is safe.
                        include_watch_history,
                        include_ratings,
                        include_playlists,
                        include_collections,
                    ): sec.title
                    for sec in selected_libs
                }
                for future in concurrent.futures.as_completed(futures):
                    lib = futures[future]
                    exc = future.exception()
                    if exc:
                        logger.error(f"Snapshot failed for library '{lib}': {exc}")
                    else:
                        logger.info(f"{lib} → {future.result()}")
                    state._live_progress.update(overall_task, advance=1)
        state._live_progress = None
        state._lib_task_ids.clear()

    else:
        # ── Full dashboard mode ────────────────────────────────────────────────
        # If the job runner created a placeholder DashboardState before
        # our pre-flight (Plex connect, home-user auth, playlist cache
        # warm), augment it rather than replacing it - that preserves
        # the activity-feed entries the user already saw and means the
        # frontend never has to render the empty state during start-up.
        if state.get_dashboard() is None:
            state._dashboard = DashboardState(log_dir=log_dir)
        else:
            state.get_dashboard().log_dir = log_dir
        # Owner + every home user we connected to = total users this run covers.
        state.get_dashboard().set_user_count(1 + n_user_tasks)
        for sec in selected_libs:
            state.get_dashboard().add_library(sec.title, total=4 + n_user_tasks)
            state.get_dashboard().set_library_status(sec.title, "active")

        try:
            with Live(console=console, refresh_per_second=4) as live:
                state._live_instance = live
                with concurrent.futures.ThreadPoolExecutor(max_workers=_effective_lib_workers) as pool:
                    futures_map: Dict[Any, str] = {
                        pool.submit(
                            snapshot_library, server, sec, output_dir, logger, home_users, playlist_caches,
                            stop_event, skip_collections, fast_collection_detection, skip_playlists, run_lazy_caches,
                            # PR-3 / Phase D - full-dashboard mode now
                            # also forwards every filter flag. The
                            # previous code silently dropped these,
                            # which meant skip_collections/skip_playlists
                            # only worked in small-terminal mode.
                            include_watch_history,
                            include_ratings,
                            include_playlists,
                            include_collections,
                        ): sec.title
                        for sec in selected_libs
                    }
                    pending = set(futures_map.keys())
                    while pending and not stop_event.is_set():
                        try:
                            live.update(_build_dashboard(state.get_dashboard().to_dashboard_frame(), mode="EXPORT"))
                        except Exception:
                            pass
                        done, pending = concurrent.futures.wait(pending, timeout=0.25)
                        for fut in done:
                            lib = futures_map[fut]
                            exc = fut.exception()
                            if exc:
                                logger.error(f"Snapshot failed for library '{lib}': {exc}")
                                state.get_dashboard().finish_library(lib, error=True)
                                # Per-library activity entry. The server-level
                                # "Snapshot complete" message fires from the
                                # job runner once every library finishes (see
                                # the console.print at end of run_snapshot
                                # and the run-level finalize phase) - here
                                # we say "Library failed" so the feed
                                # reflects what actually finished.
                                state.get_dashboard().push_activity("error", lib, "Library failed")
                            else:
                                logger.info(f"{lib} → {fut.result()}")
                                state.get_dashboard().finish_library(lib)
                                state.get_dashboard().push_activity("done", lib, "Library complete")
                    if stop_event.is_set():
                        for f in pending:
                            f.cancel()
                if not stop_event.is_set():
                    try:
                        live.update(_build_dashboard(state.get_dashboard().to_dashboard_frame(), mode="EXPORT"))
                    except Exception:
                        pass
                    time.sleep(3)
        except Exception as render_err:
            logger.warning(
                f"Dashboard rendering unavailable ({render_err!r}). "
                f"Running without display - see {log_dir}/ for full details."
            )
            with concurrent.futures.ThreadPoolExecutor(max_workers=_effective_lib_workers) as pool:
                futures_map = {
                    submit_with_context(
                        pool,
                        snapshot_library, server, sec, output_dir, logger, home_users, playlist_caches,
                        stop_event, skip_collections, fast_collection_detection, skip_playlists, run_lazy_caches,
                        owner_included,
                        # PR-3 / Phase D - pass include_* flags.
                        include_watch_history,
                        include_ratings,
                        include_playlists,
                        include_collections,
                    ): sec.title
                    for sec in selected_libs
                }
                for fut in concurrent.futures.as_completed(futures_map):
                    lib = futures_map[fut]
                    exc = fut.exception()
                    if exc:
                        logger.error(f"Snapshot failed for library '{lib}': {exc}")
                    else:
                        logger.info(f"{lib} → {fut.result()}")
        finally:
            stop_event.set()
            state._live_instance = None
            # v0.13.x: do NOT clear state._dashboard here. The job worker
            # in server/jobs.py has post-engine work to do (snapshot DB
            # capture, registry insert, JSON-sidecar prebuild, run-dir
            # rename) and uses ``_dash.set_finalizing("…")`` to surface
            # what it's doing - records hit a dashboard nulled here
            # silently. Worker's finally block nulls _dashboard once
            # all of that completes; CLI mode is unaffected (CLI exits
            # after the run, garbage-collecting the reference).

    console.print(f"\n[bold green]Snapshot complete.[/bold green] Files saved to: {output_dir}\n")
