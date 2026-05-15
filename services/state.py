"""
Shared module-level state for PlexMigrate.

All mutable globals and module-level singletons live here so every service
module can access or mutate them via:

    import services.state as state
    state._dashboard = DashboardState(...)   # write (still valid)
    if state.get_dashboard():                # read (use the helper)
        state.get_dashboard().advance_library(...)

Run-scoped fields like ``_dashboard``, ``_plex_base_url``,
``_plex_token``, the per-library accumulators, etc. are backed by
``contextvars.ContextVar`` so each fan-out destination thread sees
its own values. Reads and writes use bare attribute syntax - the
module-level ``__getattr__`` and the class-swapped ``__setattr__``
route them through the matching ContextVar. CLI and
single-destination web jobs run in a single context and behave
exactly as before.

Immutable constants (VERSION, PLEX_PORT, etc.) may be imported directly:
    from services.state import VERSION, PLEX_PORT
"""

import contextvars
import logging
import os
import sys
import threading
import types
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
from rich.console import Console
from rich.progress import Progress

# ── Version ───────────────────────────────────────────────────────────────────
# v0.8.0: introduced the optional Docker + FastAPI + React web layer.
# v0.9.0: introduced the multi-server registry, server-name-prefixed log dirs
#         and snapshot filenames, and direct server-to-server transfer mode.
# v0.9.1: explicit server selection in every API request, lightweight 30 s
#         ping polling for live status, chained-snapshot fallback when the
#         direct transfer path fails per-library.
# v0.9.2: four small UI items - Current Job ETA / progress now read from the
#         same source the Libraries section uses, thread-pool pills carry
#         plain-English descriptions, settings-page migration banner removed.
# v0.10.0: thread-local dashboard foundation + Feature 1 (Fan-Out Transfer) -
#         multi-destination direct transfer and import jobs that copy from
#         one source to several targets in parallel, each with its own
#         DashboardState surfaced as a destination card in the web UI.
# v0.11.0: Feature 2 (Application Authentication) - opt-in JWT-based login
#         for the web UI. Gated by PLEXMIGRATE_AUTH_ENABLED; off by default
#         so existing installs are unchanged. Auth state lives in its own
#         server_data/auth.db (separate from the planned media.db).
# v0.12.0: Networking tab + Feature 3 foundations. A process-lifetime,
#         server-keyed HTTP collector replaces the per-job network telemetry,
#         giving the new Networking tab live data in every state (idle,
#         single-job, fan-out). server/media_db.py ships the SQLite schema
#         (items, watch_events, ratings, playlists, collections, servers)
#         with migration scaffolding and a /api/db/stats endpoint -
#         engine integration (DB-primary writes, DB-first reads, resolver
#         tier 0) is staged for v0.12.1.
# v0.12.1: Engine integration for Feature 3 + Hard Stop. Direct-transfer
#         and chained-fallback paths now ingest source-side payloads into
#         media.db via ingest_snapshot_payload(), which enforces the
#         server-wide vs user-private dedup discipline by rating_key
#         (server-wide playlists/collections land once under user_handle="";
#         per-user variants are subtracted by rating_key before writing).
#         Resolver gains Tier-0 - a DB-cached ratingKey lookup ahead of
#         the existing GUID/path/title tiers - that bypasses Plex's slow
#         getByGuid round-trip on items the cache has seen before. New
#         "Hard Stop (force)" button on the dashboard tears down the
#         shared HTTP session so the engine bails out within seconds
#         when the soft Stop is hung. v2 schema migration adds
#         server_items(item_id, server_id, rating_key).
# The engine itself is unchanged across these releases - these VERSION bumps
# label the release that ships the new wrapper layer.
VERSION = "0.12.3"

# ── Thread Pool Size ──────────────────────────────────────────────────────────
# MAX_WORKERS caps how many parallel threads the script may run at once.
# Overridden by --workers at runtime (main() reassigns this module attribute).
MAX_WORKERS = min(32, (os.cpu_count() or 4) * 4)

# ── Scrobble Concurrency Limit ────────────────────────────────────────────────
# SCROBBLE_WORKERS caps simultaneous /:/scrobble write calls.
# Overridden by --scrobble-workers at runtime.
SCROBBLE_WORKERS = 8

# ── Default Directory Paths ───────────────────────────────────────────────────
DEFAULT_OUTPUT_DIR = "./snapshots"
DEFAULT_LOG_DIR = "./plex_logs"

# ── Plex Server Port ──────────────────────────────────────────────────────────
PLEX_PORT = 32400

# ── Shared HTTP Session ───────────────────────────────────────────────────────
# Created once in main() and reused for every direct API call. Shared
# across fan-out destinations - ``requests.Session`` is internally
# thread-safe and its connection pool routes by URL so distinct
# destinations get their own pooled connections without interference.
_session: Optional[requests.Session] = None

# ── Per-run context (ContextVar-backed) ──────────────────────────────────────
#
# Every field in this section is backed by a ``contextvars.ContextVar``
# rather than a plain module global, so each fan-out destination thread
# can hold its own value without trampling the others. Reads via
# ``state._plex_base_url`` route through the module-level
# ``__getattr__`` defined below; writes via ``state._plex_base_url = X``
# route through the ``__setattr__`` installed by the class-swap at the
# bottom of this module. To the engine and CLI both reads and writes
# look exactly like accessing a regular attribute - no call-site
# changes are required.
#
# Why ContextVars and not threading.local?
# ----------------------------------------
# The engine uses :func:`services.dashboard.submit_with_context` to
# spawn ThreadPoolExecutor workers. That helper calls
# ``contextvars.copy_context()`` so every ContextVar set on the parent
# thread propagates into the worker. A bare ``threading.local`` would
# stop at the destination thread's boundary; engine workers would fall
# back to the module global and pick up the *wrong* destination's URL.
#
# Default semantics
# -----------------
# When no context has set a value (CLI starts cold, server worker_loop
# hasn't bound a run yet), reads return the ContextVar's ``default=``
# value below - which matches the previous module-level default. The
# CLI's pattern of ``state._plex_base_url = url`` at startup sets the
# value in the main thread's context once and the rest of the run
# inherits it via submit_with_context.
_dashboard_var: contextvars.ContextVar = contextvars.ContextVar(
    "plexmigrate_dashboard", default=None,
)
_plex_base_url_var: contextvars.ContextVar = contextvars.ContextVar(
    "plexmigrate_plex_base_url", default=None,
)
# Plaintext Plex auth token for the duration of one job.
#
# v0.9.5 - encryption at rest (server/secrets.py) covers the token on
# disk (servers.json and the legacy settings.json). It does NOT cover
# this in-memory copy: python-plexapi's PlexServer instance necessarily
# holds the decrypted token to sign every HTTP request, and several
# direct-HTTP helpers (/:/scrobble, /:/rate, /:/progress) read this
# variable rather than the PlexServer object. Accepted residual
# exposure: encryption at rest protects against host-disk / volume /
# export theft; it does not protect against a compromised running
# process that can read another process's memory.
_plex_token_var: contextvars.ContextVar = contextvars.ContextVar(
    "plexmigrate_plex_token", default=None,
)
_plex_owner_name_var: contextvars.ContextVar = contextvars.ContextVar(
    "plexmigrate_plex_owner_name", default="Plex Owner",
)
# v0.9.6: owner's Plex.tv email - the canonical "identifier" the
# dashboard uses for the run-owner phases of current_user. Distinct
# from _plex_owner_name (the myPlexUsername / handle) because the
# user_display_names map is keyed by email for owners and by username
# for managed users (single dict, two kinds of keys).
_plex_owner_email_var: contextvars.ContextVar = contextvars.ContextVar(
    "plexmigrate_plex_owner_email", default="",
)
_run_log_dir_var: contextvars.ContextVar = contextvars.ContextVar(
    "plexmigrate_run_log_dir", default=None,
)
_run_timestamp_var: contextvars.ContextVar = contextvars.ContextVar(
    "plexmigrate_run_timestamp",
    # Initialised lazily after import (see _initialise_run_timestamp
    # below) because import-time evaluation of datetime.now() runs once
    # per process and we want a fresh stamp per fan-out destination
    # context anyway.
    default="",
)
# v0.13.x: MDC-style destination context for fan-out logging. Set at
# the top of each fan-out destination worker so every log record
# emitted in that worker's thread carries an identifying tag. The
# DestinationContextFilter in services/logging_ops.py stamps records
# from this ContextVar; per-destination FileHandlers filter on the
# stamp to route their records into per-destination files without the
# cross-contamination caveat that the prior shared-handler design had.
# Empty default = "not in fan-out" - records emitted in single-job
# mode get an empty destination tag and land in the normal run log.
_destination_var: contextvars.ContextVar = contextvars.ContextVar(
    "plexmigrate_destination", default="",
)
# v0.9.7 Item 4: gate for the dashboard's ``current_user`` field.
# True only during a direct-transfer run with a non-empty
# ``user_filter`` (i.e. the operator explicitly narrowed the run to
# specific users). In every other case - standard snapshot, standard
# import, direct transfer with no filter - the four ``set_current_user``
# call sites in importer/snapshotter become no-ops and the field stays
# null so the dashboard header doesn't render it.
_current_user_visible_var: contextvars.ContextVar = contextvars.ContextVar(
    "plexmigrate_current_user_visible", default=False,
)
# Per-run accumulators populated by _record_success / _record_failure
# in logging_ops.py. Each fan-out destination needs its OWN dict so
# end-of-library success/fail log writers and the troubleshoot.log
# generator only see entries belonging to that destination.
_lib_successes_var: contextvars.ContextVar = contextvars.ContextVar(
    "plexmigrate_lib_successes", default=None,
)
_lib_failures_var: contextvars.ContextVar = contextvars.ContextVar(
    "plexmigrate_lib_failures", default=None,
)
_failure_categories_var: contextvars.ContextVar = contextvars.ContextVar(
    "plexmigrate_failure_categories", default=None,
)

# Direct-transfer resolver-tier policy. The four-tier resolver
# (services/resolver.py) reads these to decide whether to attempt
# the Tier 2 (filepath suffix) and Tier 3 (fuzzy title) fallbacks.
# Tiers 0 and 1 are always attempted.
#
# Default values are the permissive "every tier on" current behaviour
# so snapshot / import paths (which never write to these vars) keep
# working unchanged. Direct transfer sets these at run start from
# settings.transfer_resolution; reset_run_state restores defaults so
# a subsequent non-transfer run inherits no overrides.
_resolver_allow_filepath_var: contextvars.ContextVar = contextvars.ContextVar(
    "plexmigrate_resolver_allow_filepath", default=True,
)
_resolver_allow_fuzzy_var: contextvars.ContextVar = contextvars.ContextVar(
    "plexmigrate_resolver_allow_fuzzy", default=True,
)

# Per-job watch+ratings filter strategy override. Set by the job
# runner at run start from the JobIn / ScheduleIn payload; cleared on
# finally. ``""`` (the default) means "inherit from per-server / global
# / built-in default" - same semantics as the empty radio option in
# the JobFormPanel UI. Valid non-empty values: "smart", "force_bulk",
# "force_server_side". Read by
# ``services.snapshotter._resolve_watch_ratings_strategy``.
_watch_ratings_strategy_override_var: contextvars.ContextVar = contextvars.ContextVar(
    "plexmigrate_watch_ratings_strategy_override", default="",
)

# Per-snapshot-run in-memory payload collector.
#
# Rule 1: media.db is never the source of truth for snapshot content.
# Each call to ``services.snapshotter.snapshot_library`` appends its
# live-fetched ``export_data`` dict (one library per entry) to this
# list. ``server.jobs._capture_snapshot_after_run`` reads the list at
# the end of the run and hands it to
# ``server.snapshot_capture.build_snapshot_db_from_payloads`` which
# writes the .db file directly from this in-memory data.
#
# Why NOT a ContextVar:
# ---------------------
# Per-library snapshot workers in the full-dashboard branch are
# submitted via ``pool.submit`` (not ``submit_with_context``) - each
# worker starts with a fresh empty context, so a ContextVar-backed
# list written by ``reset_run_state`` in the parent context would
# stay invisible to the workers. The workers would then either see
# the ContextVar default and quietly drop their payload, or set their
# own local copy that the post-run reader (back in the parent
# context) never observes. That is exactly the silent-drop we hit
# the first time this collector was wired up.
#
# A snapshot job is a single-source / single-destination operation -
# no fan-out destination ever appends here. So plain module-level
# state with a list mutated in place is correct, race-safe (CPython
# list.append is atomic for a single append), and immune to how
# downstream callers spawn their workers. ``reset_run_state`` clears
# the list in place via slice-assignment so any caller holding a
# reference (the snapshotter, the post-run wrapper) sees the same
# emptied list at the start of every run.
_snapshot_payloads: List[Dict[str, Any]] = []

# ── Non-TLS module globals (shared across all contexts) ──────────────────────
_console_handler: Optional[logging.Handler] = None
_media_logger: Optional[logging.Logger] = None
_live_instance: Optional[Any] = None      # active Live context; set in run_snapshot/run_restore

# Cross-thread mirror of the currently-active DashboardState.
#
# ContextVar.get() returns the default (None) when read from a thread
# that didn't write the var - so the WebSocket broadcaster, the
# /api/job REST handler, and any other reader living *outside* the
# engine call's thread tree would see None instead of the active
# dashboard. We mirror every write of ``_dashboard`` here so those
# cross-thread readers can find the current dashboard via
# ``get_dashboard()``'s fallback path.
#
# In fan-out mode this mirror races between destinations - but the
# WS broadcaster renders the ``fan_out`` array instead of the
# top-level dashboard whenever ``len(fan_out) > 1``, so the race is
# unobservable. The mirror exists for the single-destination /
# CLI / placeholder paths where exactly one dashboard is active at
# a time.
_dashboard_global: Optional[Any] = None

# M18: guards the cross-thread handoff of ``_dashboard_global``. The
# job worker writes it (via the ``__setattr__`` mirror below) while the
# WebSocket broadcaster reads it through ``get_dashboard()`` every
# ~250 ms - a genuine unsynchronised cross-thread access. The lock
# makes the publish/consume of the active-dashboard reference a
# well-defined ordered handoff rather than relying on CPython
# attribute-access atomicity by accident.
_dashboard_global_lock = threading.Lock()

# ── Diagnostic: per-thread Plex HTTP call counter ────────────────────────────
# Incremented by ``services.auth._http_response_hook`` on every Plex
# HTTP response. Thread-local so each concurrent gather phase counts
# only the calls made on its own thread. The snapshotter snapshots
# this before/after a per-item serialize loop to measure whether the
# loop is doing a per-item ``.reload()`` (an N+1) - if the call count
# climbs ~1:1 with item count, plexapi is round-tripping per item.
_http_count_tls = threading.local()


def bump_http_count() -> None:
    """Increment this thread's Plex HTTP response counter. Never raises."""
    try:
        _http_count_tls.n = getattr(_http_count_tls, "n", 0) + 1
    except Exception:  # pragma: no cover (defensive - telemetry only)
        pass


def get_http_count() -> int:
    """Read this thread's cumulative Plex HTTP response count."""
    return int(getattr(_http_count_tls, "n", 0))


# ── Run-trigger labels (v0.9.5) ──────────────────────────────────────────────
# Filled by server/jobs.py at job start so services/snapshotter.py can stamp
# the resulting .plexexport.json with how the run was initiated. "manual"
# for an API call from the GUI, "schedule" for the background scheduler,
# "cli" / "" for direct invocations. Schedule fires also fill
# _run_schedule_name with the schedule's display name. Shared across
# fan-out destinations because the run-trigger is a property of the
# parent job, not of any one destination.
_run_trigger: str = ""
_run_schedule_name: str = ""

# PR-13 fix #3 - registered server id (from the registry's
# ``servers.json``) of the source the engine is snapshotting. Set
# by the job runner and the CLI before ``run_snapshot`` fires;
# read inside ``services.snapshotter.snapshot_library`` so the
# per-library payload can be ingested directly into
# ``media.db`` via ``media_db.ingest_snapshot_payload(server_id, ...)``.
# Empty string means "no registered server" - the engine refuses to
# persist data when this is empty so the operator gets a hard error
# rather than a silent drop.
_snapshot_server_id: str = ""


# ── ContextVar attribute routing ─────────────────────────────────────────────
# Reads of ``state._dashboard`` etc. route through ``__getattr__`` to
# the ContextVar; writes route through the class-swapped __setattr__
# at the bottom of this module. Engine code keeps using bare attribute
# access - no call-site changes are required.

_TLS_BACKED: Dict[str, contextvars.ContextVar] = {
    "_dashboard": _dashboard_var,
    "_plex_base_url": _plex_base_url_var,
    "_plex_token": _plex_token_var,
    "_plex_owner_name": _plex_owner_name_var,
    "_plex_owner_email": _plex_owner_email_var,
    "_run_log_dir": _run_log_dir_var,
    "_run_timestamp": _run_timestamp_var,
    "_current_user_visible": _current_user_visible_var,
    "_lib_successes": _lib_successes_var,
    "_lib_failures": _lib_failures_var,
    "_failure_categories": _failure_categories_var,
    "_resolver_allow_filepath": _resolver_allow_filepath_var,
    "_resolver_allow_fuzzy": _resolver_allow_fuzzy_var,
    "_destination": _destination_var,
}


def __getattr__(name: str) -> Any:  # PEP 562
    """
    Route reads of ContextVar-backed names through their var.

    Python only invokes module-level ``__getattr__`` when normal
    attribute lookup misses, so the names in ``_TLS_BACKED`` MUST NOT
    appear as module-level variables - they live as ContextVars only.
    Anything else falls through to a normal ``AttributeError`` so a
    typo on the caller's side still surfaces.
    """
    var = _TLS_BACKED.get(name)
    if var is not None:
        return var.get()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def get_dashboard() -> Optional[Any]:
    """
    Convenience accessor for the active DashboardState.

    Resolution order:
      1. ``_dashboard_var.get()`` - the calling thread's ContextVar
         value. Set when the caller is in an engine call's thread
         tree (worker_loop, fan-out destination, or a child worker
         spawned via ``submit_with_context``).
      2. ``_dashboard_global`` - cross-thread mirror, used by the
         WebSocket broadcaster and REST handlers that live on
         different threads from the engine.

    Both forms (``state._dashboard`` and ``state.get_dashboard()``)
    return the same value; the helper exists because many engine call
    sites reach for it directly and the explicit name is friendlier in
    diffs.
    """
    ctx_val = _dashboard_var.get()
    if ctx_val is not None:
        return ctx_val
    with _dashboard_global_lock:
        return _dashboard_global

# ── Small-Terminal Fallback Progress State ────────────────────────────────────
# Used when the terminal is < 80×22 (Rich Progress bars instead of dashboard).
_live_progress: Optional[Progress] = None
_lib_task_ids: Dict[str, Any] = {}

# ── OS-Specific Plex Data Directory Paths ────────────────────────────────────
PLEX_DB_PATHS: Dict[str, List[str]] = {
    "win32": [
        os.path.expandvars(r"%LOCALAPPDATA%\Plex Media Server"),
    ],
    "linux": [
        os.path.expandvars("$PLEX_HOME/Library/Application Support/Plex Media Server"),
        "/var/lib/plexmediaserver/Library/Application Support/Plex Media Server",
    ],
    "darwin": [
        os.path.expandvars("~/Library/Application Support/Plex Media Server"),
    ],
}

# ── Troubleshooting Category Lookup Table ─────────────────────────────────────
# HARDCODED intentionally - dynamic generation could produce wrong advice.
TROUBLESHOOT_CATEGORIES: Dict[str, Dict] = {
    "local_guid_no_match": {
        "title": "local:// GUID - No MusicBrainz Match",
        "explanation": (
            "This track was never matched to MusicBrainz on the old server, "
            "so it has no universal ID. Without a universal ID, the script cannot "
            "reliably find this track on a different server."
        ),
        "steps": [
            "Open Plex on the old server and navigate to the Music library.",
            "Right-click the album containing the unmatched track.",
            'Select "Fix Match" from the context menu.',
            "Search for the correct album on MusicBrainz and select it.",
            "Wait for Plex to finish matching (may take a few minutes per album).",
            "Re-run plexmigrate.py --snapshot to capture the updated GUIDs.",
        ],
    },
    "file_path_not_found": {
        "title": "File Path Not Found on New Server",
        "explanation": (
            "The file exists on the old server but could not be found at the same "
            "path on the new one. This usually means your media drive is mounted at "
            "a different location, or the folder structure changed during the move. "
            "PlexMigrate automatically attempts suffix matching (comparing the last "
            "2–3 path components without the root prefix) so cross-platform moves "
            "between Windows and Linux are often resolved without any configuration. "
            "If this item still failed, the tail of the path may have also changed."
        ),
        "steps": [
            "Check that your media drive is connected and mounted.",
            "Compare the file path shown below with where your files actually live.",
            "If only the root changed (e.g., C:\\Media → /mnt/plex), suffix matching "
            "should have caught it automatically - verify the item exists in Plex.",
            "If the root AND some intermediate folders changed, re-run with: "
            "--remap-path /old/root /new/root to translate the stored root prefix.",
            "If paths match but files still aren't found, check drive permissions.",
        ],
    },
    "ambiguous_title_match": {
        "title": "Ambiguous Title Match - Multiple Results",
        "explanation": (
            "A search by title returned more than one result, so the script "
            "couldn't safely pick one. This happens when you have duplicate "
            "entries or similarly named items in your library."
        ),
        "steps": [
            "Open Plex and search for the item title shown below.",
            "Check for duplicate entries and remove the extras.",
            "Re-run the import after removing duplicates.",
            "Alternatively, re-run with --no-strict-match to allow best-guess "
            "selection (use carefully - may match the wrong item).",
        ],
    },
    "api_error": {
        "title": "API Error During Import",
        "explanation": (
            "The Plex server returned an error when the script tried to update "
            "this item. This may be a permissions issue, a network problem, or a "
            "temporary server hiccup."
        ),
        "steps": [
            "Verify your Plex token has admin access to the server.",
            "Check that the Plex server is running and reachable.",
            "Open the run log and search for the HTTP error code for this item.",
            "Try re-running the import - transient errors often resolve on retry.",
        ],
    },
    "playlist_item_already_present": {
        "title": "Playlist Item Already Present - Skipped",
        "explanation": (
            "This item was already in the playlist on the target server and was "
            "skipped to avoid duplicates. This is expected behaviour - "
            "PlexMigrate never adds duplicate items to existing playlists."
        ),
        "steps": [
            "No action required - this item is already correctly in the playlist.",
            "If you believe the playlist is wrong, review it directly in Plex.",
        ],
    },
    "collection_member_already_present": {
        "title": "Collection Member Already Present - Skipped",
        "explanation": (
            "This item was already a member of the collection on the target server "
            "and was skipped to avoid duplicates."
        ),
        "steps": [
            "No action required - this item is already correctly in the collection.",
        ],
    },
    "rating_already_set": {
        "title": "Rating Already Set - Skipped",
        "explanation": (
            "This item already has a star rating on the target server. "
            "PlexMigrate treats the target rating as authoritative and never "
            "overwrites it, even if the export contains a different value."
        ),
        "steps": [
            "No action required - the existing rating is preserved.",
            "If you want to change the rating, do so directly in Plex.",
        ],
    },
    "smart_playlist_skipped": {
        "title": "Smart Playlist - Requires Manual Recreation",
        "explanation": (
            "Smart playlists are defined by a saved filter query that contains "
            "server-specific library section IDs. Those IDs are different on "
            "every Plex installation, so the filter cannot be transferred "
            "automatically. The playlist must be recreated manually on the "
            "target server using the same filter criteria."
        ),
        "steps": [
            "Open Plex on the target server and go to the library shown below.",
            "Choose 'New Smart Playlist' from the playlist menu.",
            "Re-enter the same filter rules the playlist used on the old server.",
            "The original filter URL is recorded in the run log for this export "
            "(search for the playlist name alongside 'smart_content:').",
        ],
    },
    "playlist_type_conflict": {
        "title": "Destination Playlist Type Conflict",
        "explanation": (
            "The playlist already exists on the destination server but is of "
            "a different media type ('audio' vs 'video' vs 'photo') than the "
            "snapshot's items. Plex enforces a single media type per playlist "
            "and rejects appends across the boundary. Almost always traces to "
            "a prior restore run that fanned out into the wrong library and "
            "left a polluted playlist on the destination."
        ),
        "steps": [
            "Open Plex on the destination server.",
            "Find each affected playlist by name (the run log lists them).",
            "Delete the playlist on the destination - it has the wrong "
            "media type and will keep blocking future restores.",
            "Re-run the restore. The engine will create a fresh playlist "
            "with the correct items.",
            "If you keep seeing this, check that the snapshot's source "
            "library name actually exists on the destination - mismatched "
            "names cause cross-library fan-out.",
        ],
    },
    "no_tier_match": {
        "title": "No Match Found - All Tiers Exhausted",
        "explanation": (
            "The script tried GUID lookup, exact file path, suffix path matching "
            "(last 2–3 path components, cross-platform), and title search, but "
            "could not find this item on the target server. The item may not have "
            "been added to the new library yet, or may have a different title."
        ),
        "steps": [
            "Verify the item exists in your Plex library on the new server.",
            "If the file was renamed, use Fix Match in Plex to give it a stable plex:// GUID.",
            "Re-run the import after the item is confirmed present.",
            "If it still fails, restore Play Count for this item manually in Plex.",
        ],
    },
}

# ── Linux Terminal Compatibility ──────────────────────────────────────────────
# Must happen before Console() is constructed so Rich picks up the corrected value.
if sys.platform.startswith("linux") and os.environ.get("TERM", "") not in (
    "xterm-256color", "xterm", "screen-256color", "xterm-kitty"
):
    os.environ["TERM"] = "xterm-256color"

# ── Windows VT Processing ─────────────────────────────────────────────────────
# Enable ANSI/VT escape codes on Windows before Rich creates its Console so that
# cursor-rewind sequences (used by Live to overwrite in place) actually work.
# Without this, Rich falls back to legacy Windows Console API mode and each
# Live.update() appends a new block instead of overwriting the previous one.
if sys.platform == "win32":
    try:
        import ctypes
        _kernel32 = ctypes.windll.kernel32
        _stdout_handle = _kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        _con_mode = ctypes.c_ulong()
        if _kernel32.GetConsoleMode(_stdout_handle, ctypes.byref(_con_mode)):
            _kernel32.SetConsoleMode(_stdout_handle, _con_mode.value | 0x0004)
    except Exception:
        pass

# ── Rich Console (module-level singleton) ─────────────────────────────────────
console = Console(force_terminal=True) if sys.platform.startswith("linux") else Console()

# ── Run Timestamp ─────────────────────────────────────────────────────────────
# Captured once at startup; used in log filenames so all files from one run share
# the same suffix. Stored on the ContextVar so each fan-out destination
# can carry its own per-destination stamp into setup_logging.
_run_timestamp_var.set(datetime.now().strftime("%Y%m%d_%H%M%S"))

# ── Thread-Safety Lock ────────────────────────────────────────────────────────
# Guards the per-library result accumulator dicts when multiple worker
# threads inside ONE engine call mutate them concurrently. Fan-out
# parallel destinations each have their own dicts (ContextVar-backed
# above), so cross-destination contention runs through distinct dicts
# and this lock is only contended within a single destination's
# in-engine worker pool.
_log_lock = threading.Lock()


# ── Per-library result accumulators: empty default dict factory ───────────
# The ContextVars above default to ``None``; a caller that reads them
# before any setter has run gets ``None``. ``_ensure_accumulators``
# below lazily installs empty dicts in the *current* context the first
# time a recorder or writer accesses them. This is what gives CLI and
# single-destination jobs their old "shared module-global dicts"
# behaviour, while fan-out destinations get isolated dicts because
# their threads start with a fresh context.
def _ensure_accumulators() -> None:
    """
    Make sure the per-run accumulator dicts exist in the calling
    context. Called from :func:`reset_run_state` and from the
    ``_record_*`` helpers in logging_ops.py before any append.
    """
    if _lib_successes_var.get() is None:
        _lib_successes_var.set({})
    if _lib_failures_var.get() is None:
        _lib_failures_var.set({})
    if _failure_categories_var.get() is None:
        _failure_categories_var.set({})


# ── Scrobble view-count increment cap ────────────────────────────────────────
# Per-item upper bound on the number of /:/scrobble calls fired during
# import. Each call adds one to the target's viewCount; this caps how far
# we push the count in a single run. Re-running the import advances another
# batch. Bounded to keep a runaway export (e.g. a stale viewCount of 99999)
# from hammering the target server.
VIEWCOUNT_INCREMENT_CAP = 50


# ── Headless mode flag ───────────────────────────────────────────────────────
# Set to True by server/runtime_patches.py so per-run setup_logging() can
# skip rebinding sys.excepthook (which is owned by uvicorn in server mode).
HEADLESS_MODE = False


def reset_run_state() -> None:
    """
    Clear per-run accumulators so a single long-lived process can run many
    sequential jobs without state from earlier jobs bleeding into the
    troubleshoot.log / unresolved.log / per-library success+fail logs of
    later jobs.

    The CLI exits after one run so this is effectively a no-op there. In
    server mode every run_snapshot / run_restore / run_direct_transfer call
    invokes this at its top to start clean.

    Fan-out (v0.10.0): each destination thread runs ``reset_run_state``
    in its own context, which resets THAT context's accumulators only.
    Sibling destinations are unaffected - **for the ContextVar-backed
    fields**: ``_lib_successes``, ``_lib_failures``,
    ``_failure_categories``, and the resolver flags.

    L2 caveat: ``_snapshot_payloads`` and ``_lib_task_ids`` are NOT
    ContextVars - they are plain process-global containers, and the
    two lines below mutate them in place. Under fan-out, N destination
    threads clear these same globals concurrently. This is harmless
    *only* because neither is written on the server-mode fan-out
    paths: ``_snapshot_payloads`` is appended to exclusively by
    ``snapshotter.snapshot_library`` (not exercised by fan-out
    direct/restore), and ``_lib_task_ids`` is populated only in the
    terminal / Rich-progress branch (never in server/dashboard mode).
    If either ever starts being written on a concurrent server-mode
    path, it must be promoted to a ContextVar first or it will race.
    """
    # Install fresh empty dicts in the calling context's accumulators
    # (rather than ``.clear()`` on whatever was there - clearing would
    # affect any other context still holding a reference to the same
    # dict, which is exactly the cross-destination bleed we're avoiding).
    _lib_successes_var.set({})
    _lib_failures_var.set({})
    _failure_categories_var.set({})
    # Resolver-tier overrides revert to permissive defaults so a
    # snapshot/import run following a direct-transfer doesn't inherit
    # the transfer's Tier-3 OFF flip. Direct transfer re-applies its
    # settings-derived values at its own run start.
    _resolver_allow_filepath_var.set(True)
    _resolver_allow_fuzzy_var.set(True)
    # Rule 1: fresh empty list per run. snapshot_library appends its
    # live-fetched payloads; capture-after-run reads them. Sequential
    # jobs in server mode (snapshot → import → snapshot) all start
    # clean here so payloads from the prior run can't leak.
    # Slice-assignment instead of rebinding so any module attribute
    # already holding a reference to the list sees the cleared state -
    # this is what makes module-level state correct across threads
    # that read ``state._snapshot_payloads`` outside this function.
    _snapshot_payloads[:] = []
    _lib_task_ids.clear()
    # PR-13 fix #3 hotfix: ``_run_trigger``, ``_run_schedule_name`` and
    # ``_snapshot_server_id`` are PUBLISHED into state by the job
    # runner (``server/jobs.py``) and the CLI (``plexmigrate.py``)
    # BEFORE ``run_snapshot`` / ``run_restore`` / ``run_direct_transfer``
    # fires. ``reset_run_state`` runs at the TOP of those engine
    # entrypoints, so clearing these fields here would wipe the values
    # the caller just set and the engine would read empty strings.
    # Leave them untouched - the caller's set is the source of truth.
    # (Stale values from a previous run aren't a hazard because the
    # caller always re-sets before invoking the engine.)
    # v0.9.6: owner email is re-populated at the next job start.
    _plex_owner_email_var.set("")
    # v0.9.7 Item 4: ``current_user`` visibility opts in per-run. The
    # next ``_run_direct`` call sets this True iff a user filter is
    # active; standard snapshot / import always leaves it False.
    _current_user_visible_var.set(False)
    # v0.13.x note: ``_destination`` is intentionally NOT reset here.
    # Fan-out workers set it BEFORE the engine entrypoint runs, and
    # the engine entrypoint calls reset_run_state at its top; clearing
    # ``_destination`` would wipe the worker's MDC tag. Single-job
    # mode never sets ``_destination`` so it stays at its default
    # empty string anyway, and a fresh ContextVar context (every
    # fan-out worker gets one) inherits the empty default until the
    # worker sets its own value.
    # v0.9.6: wipe HTTP telemetry on the dashboard. The dashboard
    # instance is owned by the next-run setup; if one is already
    # attached (server mode reuses placeholders) clear its state too.
    dash = _dashboard_var.get()
    if dash is not None:
        try:
            dash.reset_http_telemetry()
        except Exception:
            # Defensive: a malformed dashboard shouldn't block reset.
            pass


# ── Module class swap: route attribute writes for TLS-backed names ───────────
# Python invokes a module-level ``__getattr__`` only when normal lookup
# fails - but it never routes attribute writes through a hook unless we
# swap the module's class. ``types.ModuleType`` subclasses support
# ``__setattr__``; assigning to ``sys.modules[__name__].__class__``
# installs the subclass for every subsequent attribute write. Callers
# keep using ``state._plex_base_url = X`` syntax; we intercept and
# write to the ContextVar instead so each fan-out destination context
# stores its own value.

class _StateModule(types.ModuleType):
    def __setattr__(self, name: str, value: Any) -> None:
        var = _TLS_BACKED.get(name)
        if var is not None:
            var.set(value)
            # Mirror the dashboard write to the cross-thread global so
            # readers outside the engine's thread tree (WS broadcaster,
            # REST handlers) can find the active dashboard. The mirror
            # races in fan-out mode - the WS broadcaster handles that
            # by reading ``fan_out`` array instead. See note on
            # ``_dashboard_global`` above for details.
            if name == "_dashboard":
                global _dashboard_global
                with _dashboard_global_lock:
                    _dashboard_global = value
            return
        super().__setattr__(name, value)


sys.modules[__name__].__class__ = _StateModule
