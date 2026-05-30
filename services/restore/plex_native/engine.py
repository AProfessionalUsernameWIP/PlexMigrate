"""
Restore-engine orchestrators for the Plex-direct pipeline.

Owns ``restore_export_file`` (the per-export-file driver that loads JSON,
finds the destination section, and dispatches the four restore primitives)
and ``run_restore`` (the top-level fan-out across export files with the
dashboard / progress display and the Replace-mode dest-only playlist
sweep). The per-kind restore functions live in their dedicated submodules
(``watch``, ``playlists``, ``collections``, ``ratings``) and are imported
here through normal absolute imports.
"""

import concurrent.futures
import json
import logging
import threading
import time
from typing import Any, Dict, List, Optional, Set, Tuple

from plexapi.server import PlexServer

import services.state as state
from services.state import console
# NOTE: never ``from services.state import _lib_successes`` or
# ``_lib_failures`` - those names are ContextVar-backed via
# ``state.__getattr__`` (PEP 562). A ``from`` import evaluates the
# proxy ONCE at module load and freezes the resolved value (None at
# that moment) in this module's namespace forever, so subsequent
# ``reset_run_state`` writes are invisible. Always access them via
# ``state._lib_successes`` / ``state._lib_failures`` at use sites.
from services.auth import get_home_users
from services.dashboard import (
    DashboardState,
    _build_dashboard,
    _http_lib_var,
    _keyboard_thread,
    _thread_category,
    submit_with_context,
)
from services.logging_ops import (
    write_troubleshoot_log,
    write_unresolved_log,
)
from services.resolver import _build_scan_cache
from services.restore.replace_sweep import purge_dest_only_playlists

from rich.live import Live

from services.restore.plex_native.collections import restore_collections
from services.restore.plex_native.playlists import restore_playlists
from services.restore.plex_native.ratings import restore_ratings
from services.restore.plex_native.watch import restore_watch_history


def restore_export_file(
    server: PlexServer,
    export_path: str,
    token: str,
    base_url: str,
    logger: logging.Logger,
    log_dir: str,
    remap: Optional[Tuple[str, str]],
    strict_match: bool,
    home_users: Optional[List[Tuple[str, str, PlexServer]]] = None,
    existing_playlists: Optional[Dict[str, Any]] = None,
    sections_by_name: Optional[Dict[str, Any]] = None,
    preloaded_data: Optional[dict] = None,
    stop_event: Optional[threading.Event] = None,
    include_playlists: bool = True,
    # Additional include_* flags for the other three data types.
    # Defaults are all-True so an omitting caller restores everything.
    include_watch_history: bool = True,
    include_ratings: bool = True,
    include_collections: bool = True,
    # Per-library metric map. When provided AND this library has an
    # entry, the entry's flags override the include_* booleans for
    # this library only. Keys are library names. Same pattern as
    # ``services.snapshot.plex_native.snapshotter.snapshot_library``.
    library_metrics: Optional[Dict[str, Dict[str, bool]]] = None,
    # Per-library section selector for reconstructed payloads.
    # A reconstructed payload (from ``snapshot_serializer.build_payload_from_db``)
    # carries a top-level ``libraries`` array - one entry per library
    # section the snapshot captured, each shaped like a normal flat
    # per-library export. When ``library_section_id`` is supplied,
    # ``restore_export_file`` finds the matching entry by
    # ``library_section_id`` and uses it as the per-library payload for
    # the rest of the function. ``run_restore`` emits one task per
    # library entry with its real section_id and resolved target name.
    #
    # Legacy single-library files (``library`` + ``users`` at the top
    # level, no ``libraries`` array) ignore this argument and use the
    # flat shape directly.
    library_section_id: Optional[int] = None,
    # Optional target-name override. When supplied, takes precedence
    # over the picked entry's ``library`` field for destination
    # routing - the end user may have renamed the library on the
    # destination. ``run_restore`` resolves this against the live
    # destination section list before submitting the task.
    target_section_name_override: Optional[str] = None,
    # v0.13.x restore mode. "merge" = legacy additive behaviour (never
    # destroys data); "replace" = true point-in-time, overwrites view
    # counts / ratings / playlist+collection membership to match the
    # snapshot exactly. Forwarded to every restore_* primitive.
    mode: str = "merge",
    # v0.13.x: sub-strategy for Merge mode's watch-count math. "higher"
    # (default) = destination ends at max(stored, current); "sum" =
    # destination ends at current + stored. Ignored when mode=="replace"
    # since Replace overwrites unconditionally. See restore_watch_history.
    merge_watch_strategy: str = "higher",
    # v0.14 - per-job user filter. None = import every user the
    # payload carries that also exists on the destination (historical
    # default). When supplied (set of Plex identifiers - owner email
    # + managed usernames), the importer drops payload users whose
    # handle isn't in the set BEFORE running their per-user restore.
    user_filter: Optional[List[str]] = None,
    # USER-MGMT-IDENTITY-AUDIT R-1: server identity for the
    # cross-server user resolution chain. When BOTH are provided, the
    # per-user fan-out below routes each source user via
    # ``services.identity.user_resolution.resolve_destination_user`` (the
    # 5-step chain: per-job override -> identity_map -> backend_user_id
    # direct -> case-insensitive name -> single-admin owner). When
    # either is None or empty, the per-user fan-out preserves the
    # legacy direct-name-match behaviour. Defaults preserve every
    # existing caller.
    source_server_id: Optional[str] = None,
    dest_server_id: Optional[str] = None,
) -> None:
    """
    Imports a single .plexexport.json file into the target server.

    Reads the file, finds the matching library on the target server, and
    delegates the four data types to their respective import functions.
    If the export contains per-user data (the "users" key), each user's
    Play Count, playlists, and ratings are imported using that user's own
    token and server connection so the data lands in the correct profile.

    Args:
        server (PlexServer): Active connection to the target server (admin).
        export_path (str): Filesystem path to the .plexexport.json file.
        token (str): Admin Plex auth token.
        base_url (str): Plex server base URL for direct API calls.
        logger (Logger): Shared logger.
        log_dir (str): Where per-library logs will be written.
        remap (tuple, optional): Path root translation.
        strict_match (bool): Whether to require exactly one fuzzy match.
        home_users (list, optional): List of (username, token, server) tuples.
        existing_playlists (dict, optional): Pre-fetched {title: Playlist} map.
        sections_by_name (dict, optional): Pre-built {title: section} lookup.
        preloaded_data (dict, optional): Already-parsed JSON from run_restore.
    """
    if preloaded_data is not None:
        data = preloaded_data
    else:
        with open(export_path, encoding="utf-8") as f:
            data = json.load(f)

    # USER-MGMT-IDENTITY-AUDIT R-1: when the caller didn't supply
    # source_server_id explicitly (the common case for snapshot-file
    # restores; jobs.py usually only knows the destination), peek at
    # the payload's snapshot_meta to recover the source. Best-effort:
    # legacy payloads without snapshot_meta leave source_server_id at
    # None and the resolver chain falls through to backend_user_id /
    # username matching (legacy behaviour).
    if not source_server_id:
        meta = data.get("snapshot_meta") or data.get("meta") or {}
        if isinstance(meta, dict):
            inferred = (meta.get("server_id") or meta.get("source_server_id") or "").strip()
            if inferred:
                source_server_id = inferred

    # v0.15: if the payload is a reconstructed multi-library wrapper
    # (``libraries`` array at the top level), select the entry matching
    # the supplied ``library_section_id`` and treat THAT as ``data`` for
    # the rest of the function. The entry's shape is identical to a
    # legacy flat per-library export, so every downstream read
    # (``data["users"]`` etc.) continues to work without changes.
    wrapper_libraries = data.get("libraries")
    if isinstance(wrapper_libraries, list) and library_section_id is not None:
        picked: Optional[Dict[str, Any]] = None
        for entry in wrapper_libraries:
            if not isinstance(entry, dict):
                continue
            try:
                if int(entry.get("library_section_id") or 0) == int(library_section_id):
                    picked = entry
                    break
            except (TypeError, ValueError):
                continue
        if picked is None:
            logger.error(
                "restore_export_file: wrapper payload %s has no entry with "
                "library_section_id=%s; aborting this task.",
                export_path, library_section_id,
            )
            return
        data = picked

    if target_section_name_override:
        lib_name = target_section_name_override
        logger.info(
            f"Importing from {export_path} → reconstructed payload "
            f"(section_id={library_section_id}), target library: {lib_name}"
        )
    else:
        lib_name = data.get("library", "Unknown")
        logger.info(f"Importing from {export_path} → library: {lib_name}")

    # If a per-library metric map was passed in AND this library has
    # an entry, override the include_* booleans for this library
    # only. Every downstream restore_* call inside this function
    # reads from the local include_* names; only this rebind point
    # changes.
    #
    # library_metrics is keyed by the SOURCE library name (the
    # snapshot's captured library list - what the operator ticked in
    # the UI). lib_name above may have been routed to a different
    # DESTINATION name by a library mapping / rename, so the lookup
    # uses the source name, which is always data["library"] - whether
    # data is a picked wrapper entry or a legacy flat payload.
    source_lib_name = data.get("library", lib_name)
    if library_metrics and source_lib_name in library_metrics:
        _lm_row = library_metrics[source_lib_name]
        if isinstance(_lm_row, dict):
            include_watch_history = bool(_lm_row.get("watch_history", include_watch_history))
            include_ratings = bool(_lm_row.get("ratings", include_ratings))
            include_playlists = bool(_lm_row.get("playlists", include_playlists))
            include_collections = bool(_lm_row.get("collections", include_collections))
            logger.info(
                "Per-library metric override (restore) for source library %r "
                "(restoring into %r): watch_history=%s ratings=%s playlists=%s collections=%s",
                source_lib_name, lib_name,
                include_watch_history, include_ratings, include_playlists, include_collections,
            )

    # v0.9.6 Feature 2: tag every Plex API call this library makes with
    # the library name so the Network panel can attribute traffic
    # per-library. Set on the task-local context (the restore_export_file
    # call is itself dispatched via submit_with_context from run_restore,
    # so each library task has its own context - no leakage across
    # libraries).
    _http_lib_var.set(lib_name)
    # v0.9.6 Feature 1: the per-library work the OWNER does (admin's
    # watch history, playlists, etc) is attributed to the owner's
    # email. Per-user blocks inside the loop below override this
    # temporarily and restore it on exit.
    # v0.9.7 Item 4: gate on ``state._current_user_visible`` so the
    # field stays null during standard imports and unscoped direct
    # transfers. Set only when the user explicitly narrowed the run.
    if state.get_dashboard() and state._current_user_visible:
        state.get_dashboard().set_current_user(state._plex_owner_email or None)

    def _set_phase(name: str) -> None:
        if state.get_dashboard():
            state.get_dashboard().set_library_phase(lib_name, name)
            state.get_dashboard().push_activity("phase", lib_name, f"→ {name}")

    if state.get_dashboard():
        state.get_dashboard().set_library_status(lib_name, "active")
        state.get_dashboard().push_activity("started", lib_name, "Import started")

    if sections_by_name is not None:
        section = sections_by_name.get(lib_name)
        if section is None:
            # Plex may have hidden a library mid-refresh during the
            # one-shot fetch at run start (or returned a partial list
            # while warming up). One self-healing retry: refetch the
            # section list and update the shared cache in place so any
            # subsequent libraries benefit from the warmer response too.
            logger.warning(
                "Library '%s' missing from initial section cache - "
                "refetching from destination...", lib_name,
            )
            try:
                time.sleep(1)
                refreshed = {s.title: s for s in server.library.sections()}
            except Exception as exc:
                logger.error(
                    "Section refetch failed for library '%s': %s", lib_name, exc,
                )
                refreshed = None
            if refreshed:
                sections_by_name.clear()
                sections_by_name.update(refreshed)
                section = sections_by_name.get(lib_name)
                if section is not None:
                    logger.info(
                        "Library '%s' found after refetch (initial fetch was "
                        "partial - destination was likely warming up).",
                        lib_name,
                    )
    else:
        section = next(
            (s for s in server.library.sections() if s.title == lib_name), None
        )

    if section is None:
        logger.error(
            f"Library '{lib_name}' not found on target server. "
            f"Ensure the library exists and has been scanned before importing."
        )
        return

    # Unified users map. The owner is identified by ``role == 'owner'``
    # in the per-user block; the four restore_* calls below read
    # ``owner_block`` for the owner's data.
    users_map = data.get("users") or {}
    owner_handle: Optional[str] = None
    owner_block: Dict[str, Any] = {}
    for _h, _ub in users_map.items():
        if isinstance(_ub, dict) and _ub.get("role") == "owner":
            owner_handle = _h
            owner_block = _ub
            break

    scan_cache: Dict[str, Any] = {}
    scan_lock = threading.Lock()

    # v0.9.5: build the scan cache synchronously on this thread before
    # launching the watch-history resolver pool. Workers only read
    # from the cache during resolution - they never write - so the
    # shared coordination machinery in _build_scan_cache (claim
    # handshake, __building__ marker, spin-wait loop) is only
    # exercised during this single up-front call. By the time the
    # ThreadPoolExecutor inside restore_watch_history fires up, every
    # subsequent _build_scan_cache call sees __ready__ on the first
    # lock-free dict read and returns immediately - no lock acquired,
    # no spin-wait. The shared coordinator stays in resolver.py as a
    # defensive fallback for any future call site that doesn't
    # pre-build, but on this path it's a no-op.
    #
    # Trade-off: each library's watch-history phase now has a hard
    # ``t_build`` lower bound (30-60 s on a big TV library) before
    # the first resolver runs. For libraries with thousands of
    # watched items the saving from removing the spin-wait dominates;
    # for tiny libraries the build dominates. Net positive on real
    # workloads.
    try:
        _build_scan_cache(section, scan_cache, scan_lock, logger)
    except Exception as e:
        logger.warning(
            f"[scan_cache] Build failed for '{section.title}': {e} - "
            f"resolvers will fall back to fuzzy matching."
        )

    def _stopped() -> bool:
        return stop_event is not None and stop_event.is_set()

    if include_watch_history:
        _set_phase("Play Count" if section.type == "artist" else "Watched")
        restore_watch_history(
            server, section, owner_block.get("watch_history", []),
            token, base_url, logger, log_dir, remap, strict_match,
            scan_cache, scan_lock, user=state._plex_owner_name,
            stop_event=stop_event,
            mode=mode,
            merge_watch_strategy=merge_watch_strategy,
        )
    else:
        # Per-library skip notices are INFO: expected
        # normal-operations output that confirms the end user's
        # filter choice on a per-library basis, not noise.
        logger.info(
            "[%s] Watch history import skipped (include_watch_history=False)", lib_name,
        )
    if _stopped():
        logger.info(f"Stop requested - skipping remaining phases for '{lib_name}'.")
        return
    if include_playlists:
        _set_phase("Playlists")
        restore_playlists(
            server, section, owner_block.get("playlists", []),
            logger, log_dir, remap, strict_match,
            scan_cache, scan_lock,
            existing_playlists,
            stop_event=stop_event,
            mode=mode,
        )
    else:
        logger.info(
            "[%s] Playlist import skipped (include_playlists=False)", lib_name,
        )
    if _stopped():
        logger.info(f"Stop requested - skipping remaining phases for '{lib_name}'.")
        return
    if include_collections:
        _set_phase("Collections")
        restore_collections(
            server, section, owner_block.get("collections", []),
            logger, log_dir, remap, strict_match,
            scan_cache, scan_lock,
            stop_event=stop_event,
            mode=mode,
        )
    else:
        logger.info(
            "[%s] Collection import skipped (include_collections=False)", lib_name,
        )
    if _stopped():
        logger.info(f"Stop requested - skipping remaining phases for '{lib_name}'.")
        return
    if include_ratings:
        _set_phase("Ratings")
        restore_ratings(
            server, section, owner_block.get("ratings", []),
            token, base_url, logger, remap, strict_match,
            scan_cache, scan_lock, user=state._plex_owner_name,
            stop_event=stop_event,
            mode=mode,
        )
    else:
        logger.info(
            "[%s] Ratings import skipped (include_ratings=False)", lib_name,
        )

    # Managed users only - the owner block was already restored
    # above via the role lookup. Filter against owner_handle (the
    # JSON key) AND any explicit role marker so a payload with two
    # owner rows (shouldn't happen, but defensive) doesn't accidentally
    # treat the second one as managed.
    users_data = {
        h: ub for h, ub in users_map.items()
        if h != owner_handle
        and isinstance(ub, dict)
        and ub.get("role") != "owner"
    }
    # v0.14 - per-job user filter. When the end user picked a subset
    # of users on the Restore form, drop everyone else here BEFORE the
    # per-user fan-out. The owner row was already handled by the role
    # lookup above; the matching filter for owner is the email
    # check in run_restore (managed_filter setup) - at this point the
    # filter list only matters for managed users.
    if user_filter is not None:
        filter_set = {str(s).strip() for s in user_filter if str(s).strip()}
        before = len(users_data)
        users_data = {h: ub for h, ub in users_data.items() if h in filter_set}
        if before != len(users_data):
            logger.info(
                "[%s] user_filter applied: %d managed user(s) in payload → %d "
                "selected for restore",
                lib_name, before, len(users_data),
            )
    if users_data and not home_users:
        logger.info(
            f"Export contains data for {len(users_data)} home user(s), but no "
            f"home users are available on this server (requires Plex.tv account). "
            f"Per-user data will be skipped."
        )

    user_lookup: Dict[str, Tuple[str, PlexServer]] = {
        uname: (utok, usrv) for uname, utok, usrv in (home_users or [])
    }
    # USER-MGMT-IDENTITY-AUDIT R-1: build a UserSpec-like view of the
    # destination's available home users so resolve_destination_user
    # can apply the 5-step priority chain (per-job override ->
    # identity_map -> backend_user_id direct -> case-insensitive name
    # -> single-admin owner). When source_server_id + dest_server_id
    # are BOTH provided, the resolver runs; otherwise this stays in
    # legacy direct-name-match mode.
    class _UserView:
        """Adapter shim: resolve_destination_user reads ``.username``,
        ``.role``, ``.backend_user_id``, and ``.service_type``. The
        Plex home_users list only carries usernames, so the latter
        three default to safe values that let steps 0/1/4 work while
        step 2 (backend_user_id direct match) naturally short-circuits
        because Plex home users don't expose backend_user_id here."""
        __slots__ = ("username", "role", "backend_user_id", "service_type")
        def __init__(self, username: str):
            self.username = username
            self.role = "managed"
            self.backend_user_id = ""
            self.service_type = "plex"
    _resolver_enabled = bool(source_server_id and dest_server_id)
    _dest_by_username_lc: Dict[str, _UserView] = (
        {n.lower(): _UserView(n) for n in user_lookup}
        if _resolver_enabled else {}
    )

    def _restore_user(username: str, user_data: dict) -> str:
        """Import one home user's watch history, playlists, and ratings."""
        # Identity resolution: when the 5-step resolver is enabled,
        # route the source username through it and use the matched
        # destination handle to look up the (token, server) tuple.
        # Legacy mode (resolver disabled) is identical to the prior
        # ``if username not in user_lookup`` skip.
        resolved_dest = username
        if _resolver_enabled:
            try:
                from services.identity.user_resolution import resolve_destination_user
                match = resolve_destination_user(
                    source_username=username,
                    source_role="managed",
                    dest_by_username=_dest_by_username_lc,
                    dest_admins=[],
                    source_server_id=source_server_id or "",
                    dest_server_id=dest_server_id or "",
                    logger=logger,
                )
            except Exception as exc:
                logger.debug(
                    "user_resolution call failed for %r; falling back to "
                    "direct-name match. cause: %s", username, exc,
                )
                match = _dest_by_username_lc.get(username.lower())
            if match is None:
                from services.identity.user_display import display_for_logging
                logger.info(
                    f"Home user "
                    f"'{display_for_logging(source_server_id, username)}' "
                    f"did not resolve to any user on the destination "
                    f"(no identity_map row, no name match) - skipped."
                )
                return "skipped"
            resolved_dest = match.username
        if resolved_dest not in user_lookup:
            from services.identity.user_display import display_for_logging
            logger.info(
                f"Home user "
                f"'{display_for_logging(source_server_id, username)}' is in "
                f"the export but not on this server - skipped. Add them to "
                f"Plex Home and re-run to import their data."
            )
            return "skipped"

        user_token, user_server = user_lookup[resolved_dest]

        user_section = next(
            (s for s in user_server.library.sections() if s.title == lib_name),
            None,
        )
        if user_section is None:
            logger.info(
                f"Library '{lib_name}' not accessible to home user '{username}' - skipped"
            )
            return "skipped"

        logger.info(
            f"Importing home user '{username}' - "
            f"library '{lib_name}': "
            f"{len(user_data.get('watch_history', []))} watch history, "
            f"{len(user_data.get('playlists', []))} playlist(s), "
            f"{len(user_data.get('collections', []))} personal collection(s), "
            f"{len(user_data.get('ratings', []))} rating(s)"
        )

        # v0.9.6 Feature 1: tag the header with this user for the
        # duration of their block. Restored to the owner's identifier
        # (or None) on exit so subsequent libraries' owner phases
        # don't show this user.
        # v0.9.7 Item 4: gated by ``_current_user_visible``. When
        # False the header field stays null for the whole run.
        prev_user = state.get_dashboard().current_user if state.get_dashboard() else None
        if state.get_dashboard() and state._current_user_visible:
            state.get_dashboard().set_current_user(username)
        # v0.9.7 Item 5: register this orchestrator thread under
        # "home_user" so the Thread Pool panel shows live counts
        # during the per-user phase. Inner pools spawned by the
        # import_* primitives still register under their own
        # categories (watched / playlists / collections / ratings).
        thread_ctx = _thread_category("home_user")
        thread_ctx.__enter__()
        try:
            if include_watch_history:
                restore_watch_history(
                    user_server, user_section, user_data.get("watch_history", []),
                    user_token, base_url, logger, log_dir, remap, strict_match,
                    scan_cache, scan_lock, user=username,
                    stop_event=stop_event,
                    mode=mode,
                    merge_watch_strategy=merge_watch_strategy,
                )
            if include_playlists:
                restore_playlists(
                    user_server, user_section, user_data.get("playlists", []),
                    logger, log_dir, remap, strict_match,
                    scan_cache, scan_lock,
                    existing_playlists=None,
                    stop_event=stop_event,
                    mode=mode,
                    user=username,
                )
            # v0.9.7 Item 9: restore this user's personal collections
            # (Plex Pass feature - collections that live in their
            # profile, not at the library level). Older exports
            # without the field map to an empty list via .get's
            # default, making this a no-op for pre-v0.9.7 snapshots.
            if include_collections:
                restore_collections(
                    user_server, user_section, user_data.get("collections", []),
                    logger, log_dir, remap, strict_match,
                    scan_cache, scan_lock,
                    stop_event=stop_event,
                    mode=mode,
                    user=username,
                )
            if include_ratings:
                restore_ratings(
                    user_server, user_section, user_data.get("ratings", []),
                    user_token, base_url, logger, remap, strict_match,
                    scan_cache, scan_lock, user=username,
                    stop_event=stop_event,
                    mode=mode,
                )
        finally:
            # Restore the prior current_user (owner email or None) so
            # the next user's block has a clean starting point. Safe
            # to run even when gating is off - the prior value was
            # also null in that case, so this is a no-op.
            if state.get_dashboard() and state._current_user_visible:
                state.get_dashboard().set_current_user(prev_user)
            # v0.9.7 Item 5: unregister the "home_user" thread tag.
            thread_ctx.__exit__(None, None, None)
        return "imported"

    if users_data:
        imported_users: List[str] = []
        skipped_users: List[str] = []
        n_user_workers = min(4, len(users_data))
        with concurrent.futures.ThreadPoolExecutor(max_workers=n_user_workers) as user_pool:
            futs = {
                submit_with_context(user_pool, _restore_user, uname, udata): uname
                for uname, udata in users_data.items()
            }
            for fut in concurrent.futures.as_completed(futs):
                uname = futs[fut]
                exc = fut.exception()
                if exc:
                    logger.warning(f"Home user '{uname}' import error: {exc}")
                    skipped_users.append(uname)
                elif fut.result() == "imported":
                    imported_users.append(uname)
                else:
                    skipped_users.append(uname)

        if imported_users:
            logger.info(
                f"Home user import complete - {len(imported_users)} imported: "
                f"{sorted(imported_users)}"
            )
        if skipped_users:
            logger.info(
                f"Home user import - {len(skipped_users)} skipped (not on target server): "
                f"{sorted(skipped_users)}. "
                f"Add them to Plex Home and re-run to import their data."
            )


def run_restore(
    server: PlexServer,
    export_files: List[str],
    token: str,
    base_url: str,
    logger: logging.Logger,
    log_dir: str,
    remap: Optional[Tuple[str, str]],
    strict_match: bool,
    include_playlists: bool = True,
    # Additional include_* flags.
    include_watch_history: bool = True,
    include_ratings: bool = True,
    include_collections: bool = True,
    # Restore mode. "merge" is the default additive behaviour;
    # "replace" is the opt-in point-in-time overwrite. The job worker
    # in server/jobs.py forwards the user's request body value and
    # runs the pre-Replace auto-capture safety belt before calling this
    # function when mode == "replace".
    mode: str = "merge",
    # Merge-mode watch-count math sub-strategy. Forwarded to
    # restore_export_file → restore_watch_history. Ignored when
    # mode == "replace".
    merge_watch_strategy: str = "higher",
    # Per-job user filter (intersection of snapshot users and
    # destination users). None = restore every user from the payload
    # that also exists on the destination (historical default). When
    # supplied, only Plex identifiers (owner email + managed
    # usernames) in this list survive. Read at the per-user iteration
    # boundary in restore_library; users not in the list are skipped
    # silently with an info log.
    user_filter: Optional[List[str]] = None,
    # Library-level concurrency cap, end user-tunable via settings.
    # The final pool size is ``min(library_workers, len(libraries))``
    # so the tunable is a ceiling, never a floor. Lower when Plex
    # rate-limits multi-library bursts during a restore.
    library_workers: int = 3,
    # Per-library metric map. Forwarded to each ``restore_export_file``
    # call so the restore engine applies the end user's per-library
    # choice.
    library_metrics: Optional[Dict[str, Dict[str, bool]]] = None,
    # Optional source / destination server ids forwarded down to
    # restore_export_file so the per-user fan-out's identity_map
    # resolver actually fires in production. When both are None the
    # resolver short-circuits to the case-insensitive username match.
    # ``source_server_id`` defaults to the value encoded in the
    # snapshot's snapshot_meta when not supplied at this layer
    # (restore_export_file picks it up automatically).
    source_server_id: Optional[str] = None,
    dest_server_id: Optional[str] = None,
    # Power-user override. Default False so the restorer always
    # consults library_mappings unless the operator explicitly asks to
    # ignore them for THIS run. The control is hidden in the UI behind
    # a tunable so it's not a foot-gun by default. Server jobs pass
    # settings['ignore_library_mapping'].
    ignore_library_mapping: bool = False,
    # Per-run library name overrides. Keys are source library names;
    # values are destination library names (empty string for an
    # explicit skip). The engine consults this dict BEFORE falling
    # through to the saved library_mappings table, so an operator
    # restoring from a snapshot with the source server offline can
    # route libraries for this one run without touching the shared
    # mapping table. Set to None when no overrides apply.
    library_mapping_overrides: Optional[Dict[str, str]] = None,
) -> None:
    """
    Runs the full import pipeline for all selected export files.

    Processes multiple export files concurrently (up to 3 at once), then
    writes the troubleshooting and unresolved logs once all are done.

    Stop semantics: pressing [Q] (CLI) or POSTing /api/job/stop (server)
    flips ``stop_event``. The dashboard loop stops dispatching new
    library imports and best-effort cancels queued futures; mid-import
    library work is not interrupted. ThreadPoolExecutor's ``f.cancel()``
    only succeeds on futures that have not yet started running.

    Args:
        server (PlexServer): Active connection to the target server.
        export_files (List[str]): Filesystem paths to .plexexport.json files.
        token (str): Plex auth token.
        base_url (str): Plex server base URL.
        logger (Logger): Shared logger.
        log_dir (str): Where logs will be written.
        remap (tuple, optional): Path root translation.
        strict_match (bool): Whether to require exactly one fuzzy match.
    """
    # P0-1: wipe accumulators from any previous run so this run's totals
    # and troubleshoot.log don't inherit data from a prior job.
    state.reset_run_state()

    # Phase 2: per-run restoration log. Open before any metric handler
    # runs so emissions go to a single file across all libraries. The
    # null-writer shim covers the "run logging disabled" path so the
    # rest of the engine can call writer.restored() etc. unconditionally.
    # The finally-block at the bottom closes it with a summary.
    from services.restore.restoration_log import open_restoration_log
    # Forward dest_server_id so the writer can apply the
    # log_use_display_name substitution (USER-MGMT-IDENTITY-AUDIT
    # cosmetic toggle, off by default). Legacy callers that didn't
    # plumb the kwarg through still see raw usernames in logs.
    state._restoration_log = open_restoration_log(
        log_dir, logger=logger, dest_server_id=dest_server_id or None,
    )

    # One-line summary of which data types this run will import. The
    # per-library skip notices are DEBUG; this is the only INFO line
    # confirming the end user's filter choices for the import side.
    _included = [
        n for n, v in (
            ("watch_history", include_watch_history),
            ("ratings",       include_ratings),
            ("playlists",     include_playlists),
            ("collections",   include_collections),
        ) if v
    ]
    logger.info("Import data types: %s", ", ".join(_included) if _included else "(none)")
    # v0.13.x: restore-mode header. Visible at INFO so the runtime.log
    # forensic trail makes it obvious whether a given run was the safe
    # additive default (merge) or the destructive point-in-time variant
    # (replace). For merge, also surface the sub-strategy.
    if mode == "replace":
        logger.info("Restore mode: REPLACE (point-in-time overwrite - destructive).")
    else:
        if merge_watch_strategy == "sum":
            logger.info(
                "Restore mode: merge (additive, watch counts COMBINE current+stored)."
            )
        else:
            logger.info(
                "Restore mode: merge (additive, watch counts keep HIGHER of stored/current)."
            )

    # Pre-filter the home-user auth burst so we only authenticate
    # users that the operator actually selected for this restore.
    # Authenticating every Plex Home user on every restore is wasteful
    # and confusing in the activity feed when only one user is being
    # restored. The downstream per-user gate still runs as a
    # belt-and-braces check.
    home_users = get_home_users(
        server, base_url, logger,
        user_filter=user_filter,
    )
    home_user_names: Set[str] = {n for n, _, _ in home_users}
    if home_user_names:
        logger.info(
            f"Target server home users available for import ({len(home_user_names)}): "
            f"{sorted(home_user_names)}"
        )
    else:
        logger.info("No home users available on target server - only owner data will be imported")

    # Defer the destination playlists prefetch until we know any
    # payload actually carries playlists AND the end user hasn't
    # disabled the playlist phase. An unconditional prefetch costs a
    # wasted ``server.playlists()`` round-trip on every import even
    # when ``skip_playlists=True`` was set at snapshot time and the
    # export files contain no playlists at all.
    all_playlists: Dict[str, Any] = {}

    # One-shot section enumeration for the whole run. Defensive retry:
    # a Plex server warming up from idle, or one mid-refresh on a
    # library, can return an empty (or partial) section list on the
    # first call - which would silently strand every subsequent
    # library lookup as "not found." Retry once after a short delay,
    # then hard-fail with a clear message rather than the misleading
    # per-library error a downstream lookup would emit.
    sections_by_name: Dict[str, Any] = {s.title: s for s in server.library.sections()}
    if not sections_by_name:
        logger.warning(
            "Destination returned no libraries on first fetch - server may "
            "be warming up from idle. Retrying in 2 seconds..."
        )
        time.sleep(2)
        sections_by_name = {s.title: s for s in server.library.sections()}
        if not sections_by_name:
            raise RuntimeError(
                "Destination server returned no libraries after retry. "
                "Verify the server is awake, the token has library access, "
                "and at least one library is published. Re-run the job once "
                "the destination is fully responsive."
            )
        logger.info(
            "Section list refetched - %d libraries now visible: %s",
            len(sections_by_name), sorted(sections_by_name),
        )

    # P1-2: don't hold every export file in memory at once. Peek each
    # file just long enough to extract its library name, item totals,
    # and user set; then let it go out of scope. restore_export_file
    # will re-open and load the file when its turn comes (it supports
    # this path natively via preloaded_data=None). Peak resident set
    # is now ~n_lib_workers files instead of len(export_files) files.
    def _peek_one_library(udata_map: Dict[str, Any]) -> Tuple[int, Set[str], bool]:
        # v0.13.0: unified users map. Walk every user and accumulate
        # totals; the owner (role='owner') is always counted (we always
        # restore the owner block), managed users only count when the
        # target server actually has them.
        total = 0
        has_playlists = False
        managed_keys: Set[str] = set()
        for handle, udata in udata_map.items():
            if not isinstance(udata, dict):
                continue
            role = udata.get("role") or ("owner" if handle == "" else "managed")
            if udata.get("playlists"):
                has_playlists = True
            if role == "owner":
                total += len(udata.get("watch_history", []))
                total += sum(len(pl.get("items", [])) for pl in udata.get("playlists", []))
                total += len(udata.get("playlists", []))
                total += sum(len(c.get("items", [])) for c in udata.get("collections", []))
                total += len(udata.get("collections", []))
                total += len(udata.get("ratings", []))
            else:
                managed_keys.add(handle)
                if handle in home_user_names:
                    total += len(udata.get("watch_history", []))
                    total += sum(len(pl.get("items", [])) for pl in udata.get("playlists", []))
                    total += len(udata.get("playlists", []))
                    total += len(udata.get("ratings", []))
        return total, managed_keys, has_playlists

    # v0.15: Task = (export_file, library_section_id, lib_name).
    #   * Single-library payload (one library per file):
    #       section_id=None, lib_name=data["library"]
    #   * Reconstructed wrapper payload (v0.15 multi-library):
    #       one task per entry in data["libraries"], with the entry's
    #       library_section_id and resolved destination lib_name.
    # The fan-out + divide-by-N hack the pre-v15 code used for
    # reconstructed payloads is gone - every task carries the EXACT
    # per-library total computed from that library's own users block.
    ImportTask = Tuple[str, Optional[int], str]
    lib_names: Dict[ImportTask, str] = {}
    lib_totals: Dict[ImportTask, int] = {}
    export_users: Set[str] = set()
    readable_tasks: List[ImportTask] = []
    any_payload_has_playlists = False
    # Replace-mode dest-only-playlist sweep state. We accumulate the
    # union of source playlist titles + the set of destination library
    # keys touched while we peek each export file; after every library
    # has been imported, the post-loop sweep deletes dest playlists
    # whose items live entirely inside the restored library set AND
    # whose title is absent from the source. Library-level scoping
    # is required: a Replace run that touched only
    # source-side "Music" must NOT delete playlists from an unrelated
    # dest-only "Audio Files" library, even though both libraries
    # share ``playlistType="audio"``. Merge runs skip the sweep
    # entirely; they're additive by contract.
    _replace_src_playlist_titles_owner: Set[str] = set()
    _replace_src_playlist_titles_by_user: Dict[str, Set[str]] = {}
    _replace_restored_library_keys: Set[str] = set()

    def _accumulate_playlist_titles(udata_map: Dict[str, Any]) -> None:
        for handle, udata in (udata_map or {}).items():
            if not isinstance(udata, dict):
                continue
            for pl in udata.get("playlists", []) or []:
                name = (pl.get("name") or "").strip()
                if not name:
                    continue
                role = udata.get("role") or (
                    "owner" if handle == "" else "managed"
                )
                if role == "owner":
                    _replace_src_playlist_titles_owner.add(name)
                else:
                    _replace_src_playlist_titles_by_user.setdefault(
                        handle, set()
                    ).add(name)
    # Cache the destination section titles once - hitting the live API
    # again per export file would slow startup linearly.
    _target_section_titles: Optional[Set[str]] = None

    def _dest_section_titles() -> Set[str]:
        nonlocal _target_section_titles
        if _target_section_titles is None:
            try:
                _target_section_titles = {
                    s.title for s in server.library.sections()
                }
            except Exception as exc:
                logger.error(
                    f"Could not enumerate target server libraries: {exc}; "
                    "reconstructed payloads will be skipped."
                )
                _target_section_titles = set()
        return _target_section_titles

    for bf in export_files:
        try:
            with open(bf, encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception as e:
            logger.error(f"Could not read export file {bf}: {e}")
            continue

        wrapper_libraries = data.get("libraries")
        is_wrapper = isinstance(wrapper_libraries, list) and bool(wrapper_libraries)

        if is_wrapper:
            # v0.15 reconstructed payload. Emit one task per entry,
            # each with its real per-library total. The destination
            # must have a section with the same title; case-sensitive
            # (renaming on either side is the end user's contract).
            dest_titles = _dest_section_titles()
            if not dest_titles:
                logger.error(
                    f"Reconstructed payload {bf}: target server has no "
                    "visible libraries; nothing to import into."
                )
                continue

            for entry in wrapper_libraries:
                if not isinstance(entry, dict):
                    continue
                entry_name = str(entry.get("library") or "")
                try:
                    sec_id = int(entry.get("library_section_id") or 0)
                except (TypeError, ValueError):
                    sec_id = 0
                if not entry_name or sec_id <= 0:
                    logger.error(
                        "Reconstructed payload %s contains a malformed entry "
                        "(library=%r, library_section_id=%r); skipping.",
                        bf, entry.get("library"), entry.get("library_section_id"),
                    )
                    continue
                # library_mappings consult. When the operator (or the
                # automap) has mapped this source library to a
                # destination library with a different name, the
                # helper returns the dest name to use. Falls through
                # to the plain skip when no mapping exists.
                # The ``ignore_library_mapping`` per-run flag bypasses
                # the lookup entirely. The same-server short-circuit
                # (J.TV → J.TV) also bypasses since exact-name always
                # wins for self-restore. ``library_mapping_overrides``
                # per-run dict overrides BOTH the saved table AND
                # ignore_library_mapping for the named source library.
                # Use case: restoring from a snapshot with the source
                # server offline, where the operator can't open
                # Server Syncing > Library Mapping to declare a
                # permanent mapping. Empty-string value = explicit
                # per-run skip; non-empty string = use that dest name.
                from services.library_mapping import lookup as _lml
                _src_lib_id = str(entry.get("library_section_id") or sec_id)
                _same_server = (
                    bool(source_server_id) and bool(dest_server_id)
                    and source_server_id == dest_server_id
                )
                _override = (library_mapping_overrides or {}).get(entry_name)
                if _override is not None:
                    if _override == "":
                        # Per-run explicit skip — treat as the same
                        # sentinel the saved table uses so the
                        # downstream is_explicit_skip branch handles
                        # it identically.
                        _resolved_dest = ""
                    else:
                        # Per-run override to a specific dest name.
                        # Validate it exists on the destination so a
                        # typo in the override map produces a clear
                        # warning rather than a silent miss.
                        if _override in dest_titles:
                            _resolved_dest = _override
                            logger.info(
                                "library_mappings: per-run override "
                                "routing source %r -> dest %r (does NOT "
                                "persist to the saved mapping table)",
                                entry_name, _override,
                            )
                        else:
                            logger.warning(
                                "library_mappings: per-run override for "
                                "%r targets %r which is not present on "
                                "the destination (destination has: %r). "
                                "Skipping this library; check the per-run "
                                "library mapping overrides on the Run "
                                "Job form.",
                                entry_name, _override, sorted(dest_titles),
                            )
                            _resolved_dest = None
                elif ignore_library_mapping or _same_server:
                    _resolved_dest = (
                        entry_name if entry_name in dest_titles else None
                    )
                else:
                    _resolved_dest = _lml.resolve_dest_library_name(
                        source_server_id=source_server_id or "",
                        source_library_id=_src_lib_id,
                        source_library_name=entry_name,
                        dest_server_id=dest_server_id or "",
                        dest_library_names=set(dest_titles),
                    )
                if _lml.is_explicit_skip(_resolved_dest):
                    logger.info(
                        "library_mappings: operator-confirmed SKIP for "
                        "source library %r; not importing.",
                        entry_name,
                    )
                    continue
                if _resolved_dest is not None and _resolved_dest != entry_name:
                    logger.info(
                        "library_mappings: routing source %r -> dest %r "
                        "via saved mapping",
                        entry_name, _resolved_dest,
                    )
                    entry_name = _resolved_dest
                elif _resolved_dest is None:
                    logger.warning(
                        "Reconstructed payload %s: source library %r has no "
                        "destination counterpart (destination has: %r). "
                        "Skipping this library; rename it on the destination "
                        "to match, set up a Library Mapping under Servers, "
                        "or restore into a different server.",
                        bf, entry_name, sorted(dest_titles),
                    )
                    continue

                entry_total, entry_users, entry_has_pl = _peek_one_library(
                    entry.get("users") or {}
                )
                export_users.update(entry_users)
                if entry_has_pl:
                    any_payload_has_playlists = True
                _accumulate_playlist_titles(entry.get("users") or {})
                # Resolve to destination-side section key so the
                # Replace-mode playlist sweep can scope deletions to
                # actual restored libraries (not the broad playlist-
                # Type family).
                _dest_sec = sections_by_name.get(entry_name)
                if _dest_sec is not None:
                    _replace_restored_library_keys.add(
                        str(getattr(_dest_sec, "key", "") or "")
                    )

                task: ImportTask = (bf, sec_id, entry_name)
                lib_names[task] = entry_name
                lib_totals[task] = entry_total
                readable_tasks.append(task)
        else:
            # Single-library per-library file. One library per file.
            lib_name = str(data.get("library") or "")
            entry_total, entry_users, entry_has_pl = _peek_one_library(
                data.get("users") or {}
            )
            export_users.update(entry_users)
            if entry_has_pl:
                any_payload_has_playlists = True
            _accumulate_playlist_titles(data.get("users") or {})
            _dest_sec = sections_by_name.get(lib_name)
            if _dest_sec is not None:
                _replace_restored_library_keys.add(
                    str(getattr(_dest_sec, "key", "") or "")
                )

            task = (bf, None, lib_name or bf)
            lib_names[task] = lib_name or bf
            lib_totals[task] = entry_total
            readable_tasks.append(task)
        # `data` falls out of scope here and is collectable.

    # Now gate the destination-side playlist prefetch on both signals.
    if include_playlists and any_payload_has_playlists:
        all_playlists = {pl.title: pl for pl in server.playlists()}
        logger.info(
            f"Fetched {len(all_playlists)} existing playlist(s) from target server"
        )
    elif not include_playlists:
        logger.info(
            "Skipping target-server playlist prefetch - playlist import disabled."
        )
    else:
        logger.info(
            "Skipping target-server playlist prefetch - no playlists in any export payload."
        )

    if not readable_tasks:
        logger.error("No readable export files. Aborting import.")
        return

    # Timing engine: zero-init the rolling tracker. Totals are
    # discovered as each per-library restore phase enumerates its
    # real work (discover-don't-predict) - there is no pre-run
    # estimate.
    _dash = state.get_dashboard()
    if _dash is not None:
        _dash.init_etr_tracker()

    if export_users:
        missing = sorted(export_users - home_user_names)
        logger.info(
            f"Export contains data for {len(export_users)} user(s): "
            f"{sorted(export_users)}"
        )
        if missing:
            logger.info(
                f"These export users are not on the target server and will be skipped: "
                f"{missing}"
            )

    # v0.13.x: library-level concurrency is end user-tunable via the
    # ``restore_library_workers`` setting. Clamped at 1 (a 0/negative
    # value would silently disable the pool) and capped at the actual
    # library count so a high setting doesn't spawn idle workers.
    n_lib_workers = max(1, min(int(library_workers), len(readable_tasks)))

    # Stop coordination. ``_keyboard_thread`` registers this event on
    # ``state._active_stop_event`` so ``/api/job/stop`` can flip it.
    stop_event = threading.Event()
    kb = threading.Thread(
        target=_keyboard_thread, args=(log_dir, logger, stop_event), daemon=True
    )
    kb.start()

    def _submit_all(lib_pool):
        fmap: Dict[Any, str] = {}
        for task in readable_tasks:
            bf, section_id, target_name = task
            # For wrapper-shape (v0.15 reconstructed) payloads, both
            # library_section_id (picks the entry) and the target name
            # (the destination section title) are required. For legacy
            # flat per-library files, both stay None and the function
            # uses data["library"] directly.
            override_name = target_name if section_id is not None else None
            # Skip submissions queued after stop was requested.
            # Already-running futures continue until their
            # restore_export_file's per-phase stop check fires.
            if stop_event.is_set():
                logger.info(f"Stop requested - not submitting '{lib_names[task]}'.")
                continue
            fut = submit_with_context(
                lib_pool,
                restore_export_file,
                server, bf, token, base_url, logger, log_dir, remap, strict_match,
                home_users, all_playlists, sections_by_name, None,  # preloaded_data=None: load on demand
                stop_event,
                include_playlists,
                include_watch_history,
                include_ratings,
                include_collections,
                # Phase C: per-library metric map. restore_export_file
                # overrides the include_* booleans for any library
                # listed in this map.
                library_metrics,
                section_id,        # library_section_id
                override_name,     # target_section_name_override
                mode,
                merge_watch_strategy,
                user_filter,
                source_server_id,  # USER-MGMT-IDENTITY-AUDIT R-1 (None-safe)
                dest_server_id,    # USER-MGMT-IDENTITY-AUDIT R-1 (None-safe)
            )
            fmap[fut] = lib_names[task]
        return fmap

    # ── Full dashboard mode ────────────────────────────────────────────────
    # Augment any placeholder the job runner created so the
    # activity-feed entries from pre-flight (Plex connect, home-user
    # auth) are preserved when we paint the structured panels.
    if state.get_dashboard() is None:
        state._dashboard = DashboardState(log_dir=log_dir)
    else:
        state.get_dashboard().log_dir = log_dir
    # Owner + every home user we connected to = total users this run covers.
    state.get_dashboard().set_user_count(1 + len(home_users))
    _added_libs: Set[str] = set()
    for task in readable_tasks:
        lib_name = lib_names[task]
        # Reconstructed payload may add the same library name once
        # per task; the dashboard's add_library is keyed by name
        # and a duplicate add overwrites the total. Skip duplicates.
        if lib_name in _added_libs:
            continue
        state.get_dashboard().add_library(lib_name, total=max(lib_totals[task], 1))
        _added_libs.add(lib_name)

    try:
        with Live(console=console, refresh_per_second=4) as live:
            state._live_instance = live
            with concurrent.futures.ThreadPoolExecutor(max_workers=n_lib_workers) as lib_pool:
                futs_map = _submit_all(lib_pool)
                pending = set(futs_map.keys())
                while pending and not stop_event.is_set():
                    try:
                        live.update(_build_dashboard(state.get_dashboard().to_dashboard_frame(), mode="IMPORT"))
                    except Exception:
                        pass
                    done, pending = concurrent.futures.wait(pending, timeout=0.25)
                    for fut in done:
                        lib_name = futs_map[fut]
                        exc = fut.exception()
                        if exc:
                            logger.error(f"Library import failed: {exc}")
                            state.get_dashboard().finish_library(lib_name, error=True)
                            state.get_dashboard().push_activity("error", lib_name, "Import failed")
                        else:
                            state.get_dashboard().finish_library(lib_name)
                            state.get_dashboard().push_activity("done", lib_name, "Import complete")
                if stop_event.is_set():
                    for f in pending:
                        f.cancel()
            if not stop_event.is_set():
                try:
                    live.update(_build_dashboard(state.get_dashboard().to_dashboard_frame(), mode="IMPORT"))
                except Exception:
                    pass
                time.sleep(3)
    except Exception as render_err:
        logger.warning(
            f"Dashboard rendering unavailable ({render_err!r}). "
            f"Running without display - see {log_dir}/ for full details."
        )
        with concurrent.futures.ThreadPoolExecutor(max_workers=n_lib_workers) as lib_pool:
            futs_map = _submit_all(lib_pool)
            for fut in concurrent.futures.as_completed(futs_map):
                lib_name = futs_map[fut]
                exc = fut.exception()
                if exc:
                    logger.error(f"Library import failed: {exc}")
                else:
                    logger.info(f"Completed import for '{lib_name}'")
    finally:
        stop_event.set()
        state._live_instance = None
        # v0.13.x: do NOT clear state._dashboard here. The job worker
        # in server/jobs.py runs post-engine finalization
        # (``write_troubleshoot_log`` below already needs the
        # dashboard alive for any cleanup hooks; the worker's
        # ``set_finalizing("finalizing run")`` writes to it too).
        # Worker's finally block nulls it once everything completes.

    # Replace-mode dest-only playlist sweep. Runs once per restore job
    # — after every library has imported — because Plex playlists are
    # server-wide objects. Scoping is by destination LIBRARY KEY (not
    # the broad playlistType family) so a Replace run that only
    # touched source-side "Music" never deletes a dest-only "Audio
    # Files" library's playlists.
    #
    # Two passes:
    #   * Owner pass against the admin-authenticated ``server`` using
    #     the union of owner source titles.
    #   * Per-user pass against each home user's user-authenticated
    #     PlexServer using that user's union. Plex playlists belong
    #     to their creating user, so the per-user pass only sees and
    #     can only delete the relevant user's own playlists — no
    #     cross-user leakage even though playlists are server-wide.
    if (
        mode == "replace"
        and include_playlists
        and _replace_restored_library_keys
    ):
        try:
            deleted = purge_dest_only_playlists(
                server,
                _replace_src_playlist_titles_owner,
                _replace_restored_library_keys,
                logger,
            )
            if deleted:
                logger.info(
                    "Replace: removed %d destination-only owner playlist(s) "
                    "absent from source.", deleted,
                )
        except Exception:
            logger.exception(
                "Replace: dest-only owner playlist sweep failed; continuing."
            )
        for _uname, _utoken, _user_server in (home_users or []):
            user_titles = _replace_src_playlist_titles_by_user.get(_uname, set())
            try:
                deleted = purge_dest_only_playlists(
                    _user_server,
                    user_titles,
                    _replace_restored_library_keys,
                    logger,
                )
                if deleted:
                    logger.info(
                        "Replace: removed %d destination-only playlist(s) "
                        "for managed user %r absent from source.",
                        deleted, _uname,
                    )
            except Exception:
                logger.exception(
                    "Replace: per-user dest-only playlist sweep failed "
                    "for %r; continuing.", _uname,
                )

    write_troubleshoot_log(log_dir)
    write_unresolved_log(log_dir)

    # Phase 2: close the per-run restoration log with its summary
    # block. Guarded because the engine has many early-return paths
    # above; we want the summary written regardless. Phase 4: stash
    # the affected-user list on state before close so the jobs.py
    # finalisation can populate run_history.users_affected_list.
    try:
        rlog = getattr(state, "_restoration_log", None)
        if rlog is not None:
            try:
                state._restoration_log_affected_users = list(
                    rlog.affected_user_list() or []
                )
            except Exception:
                state._restoration_log_affected_users = []
            rlog.close_with_summary()
    finally:
        state._restoration_log = None

    successes = state._lib_successes or {}
    failures = state._lib_failures or {}
    total_success = sum(len(v) for v in successes.values())
    total_fail = sum(len(v) for v in failures.values())

    console.print(f"\n[bold green]Import complete.[/bold green]")
    console.print(f"  Succeeded: {total_success}")
    console.print(f"  Failed:    {total_fail}")

    if total_fail:
        console.print(f"  [yellow]See {log_dir}/ for troubleshooting details.[/yellow]")
    console.print()
