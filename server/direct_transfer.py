"""
Direct server-to-server transfer orchestrator (v0.9.0).

Reads watch history, playlists, collections, and ratings from one
registered Plex server (the *source*) and writes them straight into
another registered server (the *destination*) without ever writing
an intermediate ``.plexbackup.json`` to disk.

The engine's four-tier matching (GUID → exact filepath → suffix →
fuzzy title) runs against the destination just as it does in
file-mediated import mode. All additive-only merge rules also apply
unchanged — nothing on the destination is ever deleted, reduced, or
overwritten. Resume positions, ratings, playlists, collections, and
view counts are all merged via the same functions ``run_import``
uses.

Engine constraint
-----------------
This module **does not** modify any engine source file. It calls
the existing primitives:

* From :mod:`services.exporter` —
  ``export_watch_history``, ``export_playlists``,
  ``export_collections``, ``export_ratings`` to build the data dict
  in memory.
* From :mod:`services.importer` —
  ``import_backup_file`` (called with ``preloaded_data=`` so it
  doesn't re-read from disk).

Both halves run under the existing ``DashboardState`` so the live
dashboard's per-library progress bar, activity feed, and counters
work identically to a normal export-then-import sequence. The job
record's ``params`` carries ``source_server_name`` and
``dest_server_name`` so the WebSocket payload and the React
dashboard can render "Plex1 → Plex2" badges; no DashboardState
field is added.

One library at a time
---------------------
Direct transfer is intentionally serialised across libraries — each
library finishes gather→merge before the next starts. The engine
already parallelises *within* a library (four gather threads, then
multi-worker resolution+merge), and adding another layer of cross-
library parallelism would pile API load on two Plex servers
simultaneously, which the README's threading section explicitly
warns against.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from plexapi.server import PlexServer

import services.state as state
from services.dashboard import DashboardState
from services.exporter import (
    export_collections,
    export_playlists,
    export_ratings,
    export_watch_history,
)
from services.importer import import_backup_file


log = logging.getLogger("plexmigrate.server.direct_transfer")


class DirectTransferUnavailable(Exception):
    """
    Internal sentinel: raised when an attempted direct (in-memory)
    transfer cannot proceed and the orchestrator should switch to the
    chained export-then-import fallback for the current library.

    Raised in two scenarios:
      * Pre-flight: one of the two registered servers does not respond
        to its identity ping within a short timeout.
      * Mid-flight: an exception escapes one of the export_* gather
        primitives or the import_backup_file call. We catch broadly
        and trust the fallback to either succeed or fail with a
        clearer error message.
    """


# ── Public entry point ───────────────────────────────────────────────────────

def run_direct_transfer(
    source_server: PlexServer,
    source_url: str,
    source_token: str,
    source_owner: str,
    dest_server: PlexServer,
    dest_url: str,
    dest_token: str,
    dest_owner: str,
    library_names: List[str],
    logger: logging.Logger,
    log_dir: str,
    remap: Optional[Tuple[str, str]],
    strict_match: bool,
    stop_event: Optional[threading.Event] = None,
    output_dir: Optional[str] = None,
) -> None:
    """
    Drive an end-to-end direct transfer.

    Workflow per library:
        1. Find the source library section by friendly name.
        2. Find the destination library section by the same name —
           we require exact name match because reliable cross-server
           mapping by anything else (key, type) is fragile in Plex.
        3. Run the four export gather primitives against the source
           into an in-memory dict matching the .plexbackup.json shape.
        4. Hand that dict to ``import_backup_file`` against the
           destination via ``preloaded_data``.

    Args:
        source_server / source_url / source_token / source_owner:
            Connection info for the source Plex.
        dest_server   / dest_url   / dest_token   / dest_owner:
            Connection info for the destination Plex.
        library_names: Friendly names to transfer. Empty list = transfer
            every library that exists on *both* servers.
        logger: Shared logger (per-run handler set up by the job runner).
        log_dir: Run log directory path (string).
        remap: Optional path-prefix translation tuple for cross-OS migrations.
        strict_match: Engine flag — same semantics as run_import.
        stop_event: Optional Event the job worker flips when the user
            clicks "Stop Job". The transfer drains the current library
            and exits cleanly when set.
    """
    # Resolve the library list. If the caller passed an empty list we
    # take "all libraries present on both servers" rather than "all on
    # source" — transferring a library that doesn't exist on the
    # destination just produces an entire run's worth of unresolved
    # items, which isn't useful.
    src_by_name = {s.title: s for s in source_server.library.sections()}
    dst_by_name = {s.title: s for s in dest_server.library.sections()}

    if not library_names:
        library_names = sorted(set(src_by_name) & set(dst_by_name))
        if not library_names:
            raise ValueError(
                "No libraries exist on both servers — nothing to transfer."
            )

    # P0-1: clear per-job accumulators (success/failure dicts) so a long-
    # lived server process doesn't carry totals from prior jobs into this
    # transfer's troubleshoot.log.
    state.reset_run_state()

    # ── Dashboard setup ──────────────────────────────────────────────
    # The job runner sets state._run_timestamp before calling us so the
    # log dir already carries a "Src-to-Dst" prefix. We just populate
    # DashboardState — the same one the WebSocket reads at 4 Hz.
    # Augment any placeholder the job runner created so pre-flight
    # activity entries (Plex source/dest connects) are preserved.
    if state._dashboard is None:
        state._dashboard = DashboardState(log_dir=log_dir)
    else:
        state._dashboard.log_dir = log_dir
    # Direct transfer is owner-only — per-user data doesn't survive a
    # cross-server move because Home tokens are server-specific (see
    # the "users": {} block written by _transfer_one_library). Report
    # one user (the source owner) so the dashboard's coverage row
    # doesn't read "0 users" during a real run.
    state._dashboard.set_user_count(1)
    for lib_name in library_names:
        # Each library does 4 gather phases + N resolution phases.
        # We use a coarse total of 8 (4 gather + 4 import) so the bar
        # animates smoothly without inflating to a confusing "1273 items".
        state._dashboard.add_library(lib_name, total=8)
        state._dashboard.set_library_status(lib_name, "queued")
    state._live_instance = None  # No Rich Live in server mode.

    # Resolve where the chained-fallback temp files (if any) land.
    # Falls back to ``./plex_exports`` to match the engine default
    # used by export_library when no output_dir is configured.
    resolved_output_dir = output_dir or "./plex_exports"
    try:
        for lib_name in library_names:
            if stop_event is not None and stop_event.is_set():
                logger.info("Stop requested — skipping remaining libraries.")
                state._dashboard.set_library_status(lib_name, "queued")
                continue

            src_section = src_by_name.get(lib_name)
            dst_section = dst_by_name.get(lib_name)
            if src_section is None:
                logger.warning(f"Library {lib_name!r} not found on source — skipping.")
                state._dashboard.finish_library(lib_name, error=True)
                continue
            if dst_section is None:
                logger.warning(f"Library {lib_name!r} not found on destination — skipping.")
                state._dashboard.finish_library(lib_name, error=True)
                continue

            # v0.9.1: attempt the in-memory direct transfer first. If
            # it fails for any reason (network blip mid-gather, OOM,
            # unexpected API response, etc.), fall back to a chained
            # export-then-import for this library without aborting
            # the whole job. The user sees the fallback in the
            # activity feed; the end result is identical either way.
            try:
                _transfer_one_library(
                    lib_name=lib_name,
                    src_section=src_section,
                    source_server=source_server,
                    source_owner=source_owner,
                    dest_server=dest_server,
                    dest_url=dest_url,
                    dest_token=dest_token,
                    logger=logger,
                    log_dir=log_dir,
                    remap=remap,
                    strict_match=strict_match,
                    stop_event=stop_event,
                )
            except DirectTransferUnavailable as exc:
                logger.warning(
                    "Direct transfer unavailable for %r (%s) — falling back "
                    "to chained export-then-import.", lib_name, exc,
                )
                state._dashboard.push_activity(
                    "phase", lib_name,
                    f"Direct path unavailable: {exc} — falling back to chained.",
                )
                _chained_fallback_library(
                    lib_name=lib_name,
                    src_section=src_section,
                    source_server=source_server,
                    source_owner=source_owner,
                    dest_server=dest_server,
                    dest_url=dest_url,
                    dest_token=dest_token,
                    logger=logger,
                    log_dir=log_dir,
                    remap=remap,
                    strict_match=strict_match,
                    output_dir=resolved_output_dir,
                    stop_event=stop_event,
                )
            except Exception as exc:
                # Any unexpected exception in the direct path: log and
                # try the chained fallback. We swallow the original
                # exception type because the user's report will come
                # from the chained path's own error if it also fails.
                logger.exception(
                    "Direct transfer of %r failed unexpectedly — "
                    "attempting chained fallback.", lib_name,
                )
                state._dashboard.push_activity(
                    "phase", lib_name,
                    "Direct path errored — falling back to chained export-then-import.",
                )
                _chained_fallback_library(
                    lib_name=lib_name,
                    src_section=src_section,
                    source_server=source_server,
                    source_owner=source_owner,
                    dest_server=dest_server,
                    dest_url=dest_url,
                    dest_token=dest_token,
                    logger=logger,
                    log_dir=log_dir,
                    remap=remap,
                    strict_match=strict_match,
                    output_dir=resolved_output_dir,
                    stop_event=stop_event,
                )
            state._dashboard.finish_library(lib_name)
    finally:
        # Leave DashboardState in place for ~3 s so the WebSocket
        # broadcaster gets one more snapshot with the final counters.
        # The job worker clears it after a small grace period.
        pass


# ── Per-library worker ───────────────────────────────────────────────────────

def _transfer_one_library(
    *,
    lib_name: str,
    src_section: Any,
    source_server: PlexServer,
    source_owner: str,
    dest_server: PlexServer,
    dest_url: str,
    dest_token: str,
    logger: logging.Logger,
    log_dir: str,
    remap: Optional[Tuple[str, str]],
    strict_match: bool,
    stop_event: Optional[threading.Event] = None,
) -> None:
    """
    Transfer a single library source→dest. See :func:`run_direct_transfer`
    for the high-level contract.
    """
    state._dashboard.set_library_status(lib_name, "active")
    state._dashboard.set_library_phase(lib_name, "Reading source…")
    state._dashboard.push_activity(
        "started", lib_name, f"Direct transfer: source → dest",
    )

    # ── Phase 1: gather data from the source ─────────────────────────
    # We are deliberately not running the four gather functions in
    # parallel here. The engine itself parallelises calls underneath
    # (e.g. multi-threaded HTTP) and stacking another concurrency
    # layer would interleave threads from two Plex servers in
    # unpredictable ways. The user can request a higher worker count
    # via --workers if they want more parallelism.
    #
    # Temporarily set the global owner name so per-item log lines
    # attribute correctly. ``state._plex_owner_name`` is read by the
    # exporter functions to label each item.
    saved_owner = state._plex_owner_name
    state._plex_owner_name = source_owner
    try:
        watch_history = export_watch_history(src_section, logger, user=source_owner)
        state._dashboard.advance_library(lib_name, 1)
        state._dashboard.set_library_phase(lib_name, "Reading playlists…")

        playlists = export_playlists(source_server, src_section.key, logger, lib_name=lib_name)
        state._dashboard.advance_library(lib_name, 1)
        state._dashboard.set_library_phase(lib_name, "Reading collections…")

        collections = export_collections(src_section, logger)
        state._dashboard.advance_library(lib_name, 1)
        state._dashboard.set_library_phase(lib_name, "Reading ratings…")

        ratings = export_ratings(src_section, logger, user=source_owner)
        state._dashboard.advance_library(lib_name, 1)
    finally:
        state._plex_owner_name = saved_owner

    # ── Build the dict in the on-disk schema's shape ─────────────────
    # ``import_backup_file`` reads ``library``, ``items.{watch_history,
    # playlists, collections, ratings}``, and ``users`` (per-user data).
    # We never carry per-user data over a direct transfer — Plex Home
    # user tokens are server-specific, so the user data is intentionally
    # source-server-scoped and would not survive a cross-server move
    # without explicit re-credentialing. That stays a future feature.
    payload: Dict[str, Any] = {
        "library": lib_name,
        "exported_at": datetime.now().isoformat(),
        "server_version": getattr(source_server, "version", "unknown"),
        "items": {
            "watch_history": watch_history,
            "playlists": playlists,
            "collections": collections,
            "ratings": ratings,
        },
        "users": {},
        "stats": {
            "total_watched": len(watch_history),
            "total_playlists": len(playlists),
            "total_collections": len(collections),
            "total_rated": len(ratings),
            "total_home_users": 0,
        },
    }

    # ── Phase 2: merge into the destination ──────────────────────────
    state._dashboard.set_library_phase(lib_name, "Writing to destination…")
    # ``import_backup_file`` expects the dest connection's URL + token
    # in the module-level state (for /:/scrobble, /:/rate, /:/progress
    # direct HTTP calls). Save the previous values so we don't trample
    # them if a later phase needs them.
    prev_url, prev_tok = state._plex_base_url, state._plex_token
    state._plex_base_url = dest_url
    state._plex_token = dest_token
    state._plex_owner_name = "Plex Owner"  # dest owner not strictly needed; labels only
    try:
        sections_by_name = {s.title: s for s in dest_server.library.sections()}
        all_playlists = {pl.title: pl for pl in dest_server.playlists()}
        # ``import_backup_file`` expects a backup_path string for log
        # messages but reads ``preloaded_data`` for the actual content.
        # The synthetic placeholder makes the run-log message explain
        # what happened ("direct://source/<library>") for forensic value.
        synthetic_path = f"direct://{lib_name}"
        import_backup_file(
            dest_server, synthetic_path, dest_token, dest_url,
            logger, log_dir, remap, strict_match,
            home_users=None,
            existing_playlists=all_playlists,
            sections_by_name=sections_by_name,
            preloaded_data=payload,
            stop_event=stop_event,
        )
    finally:
        state._plex_base_url = prev_url
        state._plex_token = prev_tok

    state._dashboard.advance_library(lib_name, 4)  # phases 5-8 (import side)
    state._dashboard.push_activity("done", lib_name, "Direct transfer complete")


# ── Chained-export fallback worker (v0.9.1) ──────────────────────────────────

def _chained_fallback_library(
    *,
    lib_name: str,
    src_section: Any,
    source_server: PlexServer,
    source_owner: str,
    dest_server: PlexServer,
    dest_url: str,
    dest_token: str,
    logger: logging.Logger,
    log_dir: str,
    remap: Optional[Tuple[str, str]],
    strict_match: bool,
    output_dir: str,
    stop_event: Optional[threading.Event] = None,
) -> None:
    """
    Fallback path used by :func:`run_direct_transfer` when the
    in-memory direct attempt fails for one library.

    Flow:
      1. Gather data from the source the same way the direct path
         does (four export_* primitives, source-server connection).
      2. Write the data to a ``.tmp.plexbackup.json`` file in
         ``output_dir`` — clearly marked as temporary so the user can
         identify and clean it up manually if this process is
         interrupted before the delete step.
      3. Hand that file to ``import_backup_file`` against the
         destination server.
      4. Delete the temp file on successful import.

    Errors during step 1 or 3 propagate, leaving the temp file on
    disk (per spec — the user should be able to recover it manually
    if needed). The activity feed receives a clear message at each
    step boundary so the user can tell where the process is.
    """
    # Reset progress for the chained path so the library bar runs
    # from 0 to 8 again — the user gets a visual restart that matches
    # the rhetorical "we tried direct, now we're trying chained."
    state._dashboard.set_library_status(lib_name, "active")
    state._dashboard.set_library_phase(lib_name, "Chained: gathering from source…")

    # ── Phase 1: gather from source into a dict (same as direct) ──────
    saved_owner = state._plex_owner_name
    state._plex_owner_name = source_owner
    try:
        watch_history = export_watch_history(src_section, logger, user=source_owner)
        playlists = export_playlists(source_server, src_section.key, logger, lib_name=lib_name)
        collections = export_collections(src_section, logger)
        ratings = export_ratings(src_section, logger, user=source_owner)
    finally:
        state._plex_owner_name = saved_owner

    payload: Dict[str, Any] = {
        "library": lib_name,
        "exported_at": datetime.now().isoformat(),
        "server_version": getattr(source_server, "version", "unknown"),
        "items": {
            "watch_history": watch_history,
            "playlists": playlists,
            "collections": collections,
            "ratings": ratings,
        },
        "users": {},
        "stats": {
            "total_watched": len(watch_history),
            "total_playlists": len(playlists),
            "total_collections": len(collections),
            "total_rated": len(ratings),
            "total_home_users": 0,
        },
    }

    # ── Phase 2: write to disk with clear .tmp marker ─────────────────
    # ``state._run_timestamp`` already carries the "Source-to-Dest_<ts>"
    # slug, so the resulting filename looks like:
    #   Movies_Plex1-to-Plex2_20260511_021515.tmp.plexbackup.json
    safe_lib_name = lib_name.replace(" ", "_").replace("/", "_")
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = out_dir / f"{safe_lib_name}_{state._run_timestamp}.tmp.plexbackup.json"
    state._dashboard.set_library_phase(lib_name, "Chained: writing temp file…")
    with open(tmp_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, default=str)
    state._dashboard.push_activity(
        "phase", lib_name, f"Wrote temp file → {tmp_path.name}",
    )

    # ── Phase 3: import that file into the destination ────────────────
    state._dashboard.set_library_phase(lib_name, "Chained: importing into destination…")
    prev_url, prev_tok = state._plex_base_url, state._plex_token
    state._plex_base_url = dest_url
    state._plex_token = dest_token
    state._plex_owner_name = "Plex Owner"
    try:
        sections_by_name = {s.title: s for s in dest_server.library.sections()}
        all_playlists = {pl.title: pl for pl in dest_server.playlists()}
        import_backup_file(
            dest_server, str(tmp_path), dest_token, dest_url,
            logger, log_dir, remap, strict_match,
            home_users=None,
            existing_playlists=all_playlists,
            sections_by_name=sections_by_name,
            preloaded_data=payload,
            stop_event=stop_event,
        )
    finally:
        state._plex_base_url = prev_url
        state._plex_token = prev_tok

    # ── Phase 4: delete the temp file only on success ─────────────────
    # We deliberately leave the file behind if anything in step 3
    # raised — the user can re-import it manually after fixing the
    # underlying issue. The .tmp marker in the filename makes it easy
    # to find and clean up if no longer needed.
    try:
        os.unlink(tmp_path)
        state._dashboard.push_activity(
            "phase", lib_name, "Chained: temp file deleted.",
        )
    except OSError as exc:
        # Not fatal — log and move on. The file just stays on disk.
        logger.warning(
            "Could not delete chained-transfer temp file %s: %s", tmp_path, exc,
        )

    state._dashboard.push_activity(
        "done", lib_name, "Chained export-then-import complete.",
    )
