"""
Export pipeline for PlexMigrate.

export_watch_history / export_playlists / export_collections / export_ratings
gather data from a live Plex server. export_library orchestrates them
concurrently into a .plexbackup.json file. run_export drives the full
multi-library export with progress display.
"""

import concurrent.futures
import json
import logging
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

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
    _keyboard_thread,
    _make_progress,
    _thread_category,
)
from services.logging_ops import _fmt_media_line
from services.resolver import (
    _all_guids,
    _safe_file_path,
    serialize_item,
    serialize_playlist,
    serialize_collection,
)
from services.auth import get_home_users


# ── Export Functions ──────────────────────────────────────────────────────────

def export_watch_history(section, logger: logging.Logger, user: str = "Plex Owner") -> List[Dict]:
    """
    Fetches all watched items from a library section.

    We only export items that have actually been watched (viewCount > 0).
    Exporting unwatched items would add noise and isn't useful for migration.

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
        if libtype == "artist":
            try:
                all_items = section.searchTracks(viewCount__gt=0)
            except Exception:
                all_items = [t for t in section.searchTracks()
                             if getattr(t, "viewCount", 0)]
        elif libtype == "show":
            try:
                all_items = section.searchEpisodes(viewCount__gt=0)
            except Exception:
                all_items = [ep for ep in section.searchEpisodes()
                             if getattr(ep, "viewCount", 0)]
        else:
            try:
                all_items = section.search(viewCount__gt=0)
            except Exception:
                all_items = [m for m in section.all()
                             if getattr(m, "viewCount", 0)]

        for item in all_items:
            plays = getattr(item, "viewCount", 0) or 0
            if plays:
                with _current_item(section.title, item.type, item.title, phase="exporting"):
                    watched.append(serialize_item(item))
                if state._dashboard:
                    state._dashboard.inc_watch()
                if state._media_logger:
                    state._media_logger.debug(_fmt_media_line(
                        "EXPORT", section.title, item.type, item.title,
                        user=user,
                        plays=plays,
                        rating=getattr(item, "userRating", None),
                        path=_safe_file_path(item),
                    ))
        logger.info(f"[{section.title}] Play Count export: {len(watched)} item(s) found (user: {user})")
    except Exception as e:
        logger.error(f"Error fetching Play Count for {section.title}: {e}")
    return watched


def build_playlist_cache(server: PlexServer, logger: logging.Logger) -> List[Tuple[Any, List]]:
    """
    Fetch every playlist on a server and its items, once.

    Returns a list of ``(playlist_obj, items)`` tuples. Playlists whose
    ``pl.items()`` call errors (e.g. Plex 500s on auto-generated
    "Recently Played" / "All Music" entries) are recorded with an empty
    item list and a single summary INFO line — not per-playlist DEBUG
    spam.

    Each backup run reuses this cache across libraries (and across home
    users, one cache per user-token connection) so the N×M×P×items
    blow-up in :func:`export_playlists` collapses to a single fetch per
    server connection. See P1-1 in the code review.

    The per-playlist ``pl.items()`` call is the slow part (one network
    round-trip each) so we surface it on the dashboard's Currently
    Processing panel under phase ``"fetching"`` — for a server with
    hundreds of playlists this gives the user a visible heartbeat
    during what would otherwise look like a stalled warmup.
    """
    cache: List[Tuple[Any, List]] = []
    skipped_500 = 0
    try:
        all_playlists = server.playlists()
    except Exception as e:
        logger.warning(f"Could not list playlists from server: {e}")
        return cache

    server_label = getattr(server, "friendlyName", "") or "(server-wide)"
    if state._dashboard:
        state._dashboard.push_activity(
            "phase", "—",
            f"Warming playlist cache for '{server_label}' ({len(all_playlists)} playlists)…",
        )
    for pl in all_playlists:
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
    return cache


def export_playlists(
    server: PlexServer,
    section_key: str,
    logger: logging.Logger,
    lib_name: str = "",
    playlist_cache: Optional[List[Tuple[Any, List]]] = None,
) -> List[Dict]:
    """
    Fetches playlists that contain at least one item from the given library.

    Plex playlists are server-wide (not per-library), but we want to export
    each library's playlists with that library's backup file. We filter by
    checking whether any playlist item belongs to this library section.

    Args:
        server (PlexServer): Active server connection (only used when
            ``playlist_cache`` is None and we need to fetch on the fly).
        section_key (str): The library section's key (integer ID as string).
        logger (Logger): Shared logger.
        lib_name (str): Library name for log labels.
        playlist_cache (list, optional): Pre-fetched
            ``[(playlist, items), ...]`` shared across libraries and users
            to avoid an O(libraries × users × playlists × items) fetch
            blow-up. Built once per server connection in run_export.

    Returns:
        List of serialized playlist dicts.
    """
    result = []
    label = lib_name or f"section:{section_key}"
    try:
        if playlist_cache is None:
            playlist_cache = build_playlist_cache(server, logger)

        for pl, items in playlist_cache:
            try:
                if any(
                    str(getattr(i, "librarySectionID", "")) == str(section_key)
                    for i in items
                ):
                    with _current_item(label, "playlist", pl.title, phase="exporting"):
                        result.append(serialize_playlist(pl, prefetched_items=items))
                    if state._dashboard:
                        state._dashboard.inc_playlist()
                    if state._media_logger:
                        state._media_logger.debug(_fmt_media_line(
                            "EXPORT", label, "playlist", pl.title,
                            items=len(items),
                        ))
            except Exception as e:
                logger.debug(f"Skipping playlist '{pl.title}': {e}")
        logger.info(f"[{label}] Playlist export: {len(result)} playlist(s) found")
    except Exception as e:
        logger.error(f"Error fetching playlists: {e}")
    return result


def export_collections(section, logger: logging.Logger) -> List[Dict]:
    """
    Fetches all collections from a library section.

    Collections are per-library, so we fetch them directly from the section.
    Each collection groups related items (e.g., a film franchise).

    Args:
        section: A python-plexapi LibrarySection.
        logger (Logger): Shared logger.

    Returns:
        List of serialized collection dicts.
    """
    result = []
    try:
        for coll in section.collections():
            with _current_item(section.title, "collection", coll.title, phase="exporting"):
                result.append(serialize_collection(coll))
            if state._dashboard:
                state._dashboard.inc_collection()
            if state._media_logger:
                try:
                    n_members = len(coll.items())
                except Exception:
                    n_members = None
                state._media_logger.debug(_fmt_media_line(
                    "EXPORT", section.title, "collection", coll.title,
                    items=n_members,
                ))
        logger.info(
            f"[{section.title}] Collection export: {len(result)} collection(s) found"
        )
    except Exception as e:
        logger.error(f"Error fetching collections for {section.title}: {e}")
    return result


def export_ratings(section, logger: logging.Logger, user: str = "Plex Owner") -> List[Dict]:
    """
    Fetches all items in a library that have a user star rating.

    User ratings (1–10 stars) are separate from Play Count. We export them
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
        if libtype == "artist":
            try:
                all_items = section.searchTracks(userRating__gt=0)
            except Exception:
                all_items = [t for t in section.searchTracks()
                             if getattr(t, "userRating", None) is not None]
        elif libtype == "show":
            try:
                show_items = section.search(userRating__gt=0)
            except Exception:
                show_items = [s for s in section.all()
                              if getattr(s, "userRating", None) is not None]
            try:
                ep_items = section.searchEpisodes(userRating__gt=0)
            except Exception:
                ep_items = [ep for ep in section.searchEpisodes()
                            if getattr(ep, "userRating", None) is not None]
            all_items = show_items + ep_items
        else:
            try:
                all_items = section.search(userRating__gt=0)
            except Exception:
                all_items = section.all()

        for item in all_items:
            rating = getattr(item, "userRating", None)
            if rating is not None:
                with _current_item(section.title, item.type, item.title, phase="exporting"):
                    entry: Dict[str, Any] = {
                        "title": item.title,
                        "type": item.type,
                        "guids": _all_guids(item),
                        "filepath": _safe_file_path(item),
                        "user_rating": rating,
                    }
                    if item.type == "track":
                        entry["artist"] = getattr(item, "grandparentTitle", "")
                        entry["album"] = getattr(item, "parentTitle", "")
                    elif item.type == "episode":
                        entry["show_title"] = getattr(item, "grandparentTitle", "")
                    rated.append(entry)
                if state._dashboard:
                    state._dashboard.inc_rating()
                if state._media_logger:
                    state._media_logger.debug(_fmt_media_line(
                        "EXPORT", section.title, item.type, item.title,
                        user=user,
                        rating=rating,
                        artist=entry.get("artist") or entry.get("show_title"),
                        path=_safe_file_path(item),
                    ))
        logger.info(f"[{section.title}] Ratings export: {len(rated)} item(s) found (user: {user})")
    except Exception as e:
        logger.error(f"Error fetching ratings for {section.title}: {e}")
    return rated


def export_library(
    server: PlexServer,
    section,
    output_dir: str,
    logger: logging.Logger,
    home_users: Optional[List[Tuple[str, str, PlexServer]]] = None,
    playlist_caches: Optional[Dict[int, List[Tuple[Any, List]]]] = None,
    stop_event: Optional[threading.Event] = None,
) -> str:
    """
    Exports a single library to a .plexbackup.json file.

    Each library gets its own backup file so the user can choose which
    libraries to import individually. The four data-gathering tasks
    (Play Count, playlists, collections, ratings) run concurrently
    within the library to reduce total export time.

    Args:
        server (PlexServer): Active admin server connection.
        section: A python-plexapi LibrarySection to export.
        output_dir (str): Directory where the .plexbackup.json will be written.
        logger (Logger): Shared logger.
        home_users (list, optional): List of (username, token, server) tuples.
        playlist_caches (dict, optional): Pre-fetched playlist caches keyed
            by ``id(server)``. Built once in run_export and shared across
            libraries + users to avoid repeated server.playlists() round-trips.

    Returns:
        Absolute path to the written .plexbackup.json file as a string.
    """
    lib_name = section.title
    logger.info(f"Exporting library: {lib_name}")
    if state._dashboard:
        state._dashboard.push_activity("started", lib_name, "Export started")
        state._dashboard.set_library_phase(lib_name, "Exporting…")

    results: Dict[str, List] = {
        "watch_history": [],
        "playlists": [],
        "collections": [],
        "ratings": [],
    }

    def _advance():
        _advance_lib(lib_name)

    def gather_watch():
        cat = "play_count" if section.type == "artist" else "watched"
        with _thread_category(cat):
            results["watch_history"] = export_watch_history(section, logger, user=state._plex_owner_name)
        if state._dashboard:
            state._dashboard.set_library_phase(lib_name, "Watch History ✓")
            state._dashboard.push_activity("phase", lib_name, f"Watch History → {len(results['watch_history'])} items")
        _advance()

    def gather_playlists():
        cache = playlist_caches.get(id(server)) if playlist_caches else None
        with _thread_category("playlists"):
            results["playlists"] = export_playlists(
                server, section.key, logger, lib_name=lib_name, playlist_cache=cache,
            )
        if state._dashboard:
            state._dashboard.set_library_phase(lib_name, "Playlists ✓")
            state._dashboard.push_activity("phase", lib_name, f"Playlists → {len(results['playlists'])} items")
        _advance()

    def gather_collections():
        with _thread_category("collections"):
            results["collections"] = export_collections(section, logger)
        if state._dashboard:
            state._dashboard.set_library_phase(lib_name, "Collections ✓")
            state._dashboard.push_activity("phase", lib_name, f"Collections → {len(results['collections'])} items")
        _advance()

    def gather_ratings():
        with _thread_category("ratings"):
            results["ratings"] = export_ratings(section, logger, user=state._plex_owner_name)
        if state._dashboard:
            state._dashboard.set_library_phase(lib_name, "Ratings ✓")
            state._dashboard.push_activity("phase", lib_name, f"Ratings → {len(results['ratings'])} items")
        _advance()

    def gather_user(username: str, user_server: PlexServer):
        """Thread task: fetch one home user's Play Count, playlists, and ratings."""
        try:
            user_section = next(
                (s for s in user_server.library.sections() if s.title == lib_name),
                None,
            )
            if user_section is None:
                logger.warning(
                    f"Library '{lib_name}' not visible to home user '{username}' — skipped"
                )
                return
            user_cache = playlist_caches.get(id(user_server)) if playlist_caches else None
            with _thread_category("home_user"):
                u_watch = export_watch_history(user_section, logger, user=username)
                u_ratings = export_ratings(user_section, logger, user=username)
                u_playlists = export_playlists(
                    user_server, user_section.key, logger,
                    lib_name=lib_name, playlist_cache=user_cache,
                )
            users_data[username] = {
                "watch_history": u_watch,
                "ratings": u_ratings,
                "playlists": u_playlists,
            }
            logger.info(
                f"Home user '{username}' — {lib_name}: "
                f"{len(u_watch)} watched, {len(u_ratings)} rated, "
                f"{len(u_playlists)} playlist(s)"
            )
        except Exception as e:
            logger.warning(f"Could not export data for home user '{username}': {e}")
        finally:
            _advance()

    users_data: Dict[str, Dict] = {}
    n_user_tasks = len(home_users or [])
    n_total_tasks = 4 + n_user_tasks

    # If stop was requested before this library even started, exit
    # before opening the pool so the user's click takes effect at the
    # next library boundary instead of running this one to completion.
    if stop_event is not None and stop_event.is_set():
        logger.info(f"Stop requested — skipping library '{lib_name}'.")
        if state._dashboard:
            state._dashboard.finish_library(lib_name, error=False)
        return ""

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, n_total_tasks)) as pool:
        futures = [
            pool.submit(gather_watch),
            pool.submit(gather_playlists),
            pool.submit(gather_collections),
            pool.submit(gather_ratings),
        ]
        # Home-user gather threads check stop_event before launching so
        # a stop signalled mid-library at least prevents the (often very
        # slow) per-user playlist scans from starting.
        for username, _, user_server in (home_users or []):
            if stop_event is not None and stop_event.is_set():
                logger.info(
                    f"Stop requested — skipping home user '{username}' "
                    f"for library '{lib_name}'."
                )
                continue
            futures.append(pool.submit(gather_user, username, user_server))

        for f in concurrent.futures.as_completed(futures):
            exc = f.exception()
            if exc:
                logger.error(f"Error in gather thread for {lib_name}: {exc}")

    # Embed source-server identity so the Exports browser can label
    # each file with the server that produced it. Reads from state
    # populated by the CLI / job runner before the export starts. All
    # three fields are best-effort — missing values fall back to "".
    export_data = {
        "library": lib_name,
        "exported_at": datetime.now().isoformat(),
        "server_version": server.version,
        # v0.9.3: source server identity (new)
        "source_server_name": getattr(server, "friendlyName", "") or "",
        "source_server_url": state._plex_base_url or "",
        "source_server_machine_id": getattr(server, "machineIdentifier", "") or "",
        "items": results,
        "users": users_data,
        "stats": {
            "total_watched": len(results["watch_history"]),
            "total_playlists": len(results["playlists"]),
            "total_collections": len(results["collections"]),
            "total_rated": len(results["ratings"]),
            "total_home_users": len(users_data),
        },
    }

    Path(output_dir).mkdir(parents=True, exist_ok=True)
    safe_name = lib_name.replace(" ", "_").replace("/", "_")
    filename = f"{safe_name}_{state._run_timestamp}.plexbackup.json"
    out_path = Path(output_dir) / filename

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(export_data, f, indent=2, default=str)

    logger.info(
        f"Exported {lib_name}: "
        f"{export_data['stats']['total_watched']} watched, "
        f"{export_data['stats']['total_playlists']} playlists, "
        f"{export_data['stats']['total_collections']} collections → {out_path}"
    )
    return str(out_path)


# ── Export Runner ─────────────────────────────────────────────────────────────

def run_export(
    server: PlexServer,
    selected_libs: List,
    output_dir: str,
    logger: logging.Logger,
    log_dir: str,
    base_url: str,
) -> None:
    """
    Runs the full multi-threaded export pipeline for all selected libraries.

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
        selected_libs (List): List of LibrarySection objects to export.
        output_dir (str): Where to write the .plexbackup.json files.
        logger (Logger): Shared logger.
        log_dir (str): Log directory (for keyboard shortcuts).
        base_url (str): Plex server base URL, used to authenticate as home users.
    """
    # Wipe accumulators from any previous run so this run's totals,
    # troubleshoot.log, and unresolved.log only describe this run.
    state.reset_run_state()

    home_users = get_home_users(server, base_url, logger)
    n_user_tasks = len(home_users)

    # ── Pre-fetch playlists once per server connection ─────────────────────
    # Without this cache, export_playlists fires server.playlists() once
    # per library *and* once per library per home user, each call also
    # invoking pl.items() on every playlist. For a 4-library / 11-user
    # server that is ~48 full playlist enumerations per export. The cache
    # collapses it to one fetch per unique server connection (owner +
    # one per home user), built here in parallel before per-library work
    # starts.
    playlist_caches: Dict[int, List[Tuple[Any, List]]] = {}
    unique_servers: List[PlexServer] = [server]
    for entry in home_users:
        unique_servers.append(entry[2])

    def _warm(srv: PlexServer) -> Tuple[int, List[Tuple[Any, List]]]:
        return id(srv), build_playlist_cache(srv, logger)

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=max(1, min(len(unique_servers), 8))
    ) as warm_pool:
        for sid, cache in warm_pool.map(_warm, unique_servers):
            playlist_caches[sid] = cache
    logger.info(
        f"Playlist cache warmed: {sum(len(c) for c in playlist_caches.values())} "
        f"playlist record(s) across {len(playlist_caches)} server connection(s)"
    )

    # Stop coordination — created here so both the dashboard and small-
    # terminal branches share one event. In server mode, the runtime
    # patches' _server_keyboard_stub stashes a reference to this event
    # so /api/job/stop can flip it; in CLI mode, _keyboard_thread reads
    # raw keypresses and sets it on [Q]. export_library checks it
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
                fields={"phase": "Exporting"},
            )
        overall_task = state._live_progress.add_task(
            "Overall", total=len(selected_libs), completed=0, fields={"phase": ""},
        )
        with Live(state._live_progress, console=console, refresh_per_second=8):
            with concurrent.futures.ThreadPoolExecutor(max_workers=state.MAX_WORKERS) as pool:
                futures = {
                    pool.submit(
                        export_library, server, sec, output_dir, logger, home_users, playlist_caches,
                        stop_event,
                    ): sec.title
                    for sec in selected_libs
                }
                for future in concurrent.futures.as_completed(futures):
                    lib = futures[future]
                    exc = future.exception()
                    if exc:
                        logger.error(f"Export failed for library '{lib}': {exc}")
                    else:
                        logger.info(f"{lib} → {future.result()}")
                    state._live_progress.update(overall_task, advance=1)
        state._live_progress = None
        state._lib_task_ids.clear()

    else:
        # ── Full dashboard mode ────────────────────────────────────────────────
        # If the job runner created a placeholder DashboardState before
        # our pre-flight (Plex connect, home-user auth, playlist cache
        # warm), augment it rather than replacing it — that preserves
        # the activity-feed entries the user already saw and means the
        # frontend never has to render the empty state during start-up.
        if state._dashboard is None:
            state._dashboard = DashboardState(log_dir=log_dir)
        else:
            state._dashboard.log_dir = log_dir
        # Owner + every home user we connected to = total users this run covers.
        state._dashboard.set_user_count(1 + n_user_tasks)
        for sec in selected_libs:
            state._dashboard.add_library(sec.title, total=4 + n_user_tasks)
            state._dashboard.set_library_status(sec.title, "active")

        try:
            with Live(console=console, refresh_per_second=4) as live:
                state._live_instance = live
                with concurrent.futures.ThreadPoolExecutor(max_workers=state.MAX_WORKERS) as pool:
                    futures_map: Dict[Any, str] = {
                        pool.submit(
                            export_library, server, sec, output_dir, logger, home_users, playlist_caches,
                            stop_event,
                        ): sec.title
                        for sec in selected_libs
                    }
                    pending = set(futures_map.keys())
                    while pending and not stop_event.is_set():
                        try:
                            live.update(_build_dashboard(state._dashboard.snapshot(), mode="EXPORT"))
                        except Exception:
                            pass
                        done, pending = concurrent.futures.wait(pending, timeout=0.25)
                        for fut in done:
                            lib = futures_map[fut]
                            exc = fut.exception()
                            if exc:
                                logger.error(f"Export failed for library '{lib}': {exc}")
                                state._dashboard.finish_library(lib, error=True)
                                state._dashboard.push_activity("error", lib, "Export failed")
                            else:
                                logger.info(f"{lib} → {fut.result()}")
                                state._dashboard.finish_library(lib)
                                state._dashboard.push_activity("done", lib, "Export complete")
                    if stop_event.is_set():
                        for f in pending:
                            f.cancel()
                if not stop_event.is_set():
                    try:
                        live.update(_build_dashboard(state._dashboard.snapshot(), mode="EXPORT"))
                    except Exception:
                        pass
                    time.sleep(3)
        except Exception as render_err:
            logger.warning(
                f"Dashboard rendering unavailable ({render_err!r}). "
                f"Running without display — see {log_dir}/ for full details."
            )
            with concurrent.futures.ThreadPoolExecutor(max_workers=state.MAX_WORKERS) as pool:
                futures_map = {
                    pool.submit(
                        export_library, server, sec, output_dir, logger, home_users, playlist_caches,
                        stop_event,
                    ): sec.title
                    for sec in selected_libs
                }
                for fut in concurrent.futures.as_completed(futures_map):
                    lib = futures_map[fut]
                    exc = fut.exception()
                    if exc:
                        logger.error(f"Export failed for library '{lib}': {exc}")
                    else:
                        logger.info(f"{lib} → {fut.result()}")
        finally:
            stop_event.set()
            state._live_instance = None
            state._dashboard = None

    console.print(f"\n[bold green]Export complete.[/bold green] Files saved to: {output_dir}\n")
