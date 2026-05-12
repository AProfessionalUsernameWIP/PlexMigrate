"""
Shared module-level state for PlexMigrate.

All mutable globals and module-level singletons live here so every service
module can access or mutate them via:

    import services.state as state
    state._dashboard = DashboardState(...)   # mutate
    if state._dashboard:                     # read
        state._dashboard.advance_library(...)

Immutable constants (VERSION, PLEX_PORT, etc.) may be imported directly:
    from services.state import VERSION, PLEX_PORT
"""

import logging
import os
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
from rich.console import Console
from rich.progress import Progress

# ── Version ───────────────────────────────────────────────────────────────────
# v0.8.0: introduced the optional Docker + FastAPI + React web layer.
# v0.9.0: introduced the multi-server registry, server-name-prefixed log dirs
#         and export filenames, and direct server-to-server transfer mode.
# v0.9.1: explicit server selection in every API request, lightweight 30 s
#         ping polling for live status, chained-export fallback when the
#         direct transfer path fails per-library.
# v0.9.2: four small UI items — Current Job ETA / progress now read from the
#         same source the Libraries section uses, thread-pool pills carry
#         plain-English descriptions, settings-page migration banner removed.
# The engine itself is unchanged across these releases — these VERSION bumps
# label the release that ships the new wrapper layer.
VERSION = "0.9.2"

# ── Thread Pool Size ──────────────────────────────────────────────────────────
# MAX_WORKERS caps how many parallel threads the script may run at once.
# Overridden by --workers at runtime (main() reassigns this module attribute).
MAX_WORKERS = min(32, (os.cpu_count() or 4) * 4)

# ── Scrobble Concurrency Limit ────────────────────────────────────────────────
# SCROBBLE_WORKERS caps simultaneous /:/scrobble write calls.
# Overridden by --scrobble-workers at runtime.
SCROBBLE_WORKERS = 8

# ── Default Directory Paths ───────────────────────────────────────────────────
DEFAULT_OUTPUT_DIR = "./plex_exports"
DEFAULT_LOG_DIR = "./plex_logs"

# ── Plex Server Port ──────────────────────────────────────────────────────────
PLEX_PORT = 32400

# ── Shared HTTP Session ───────────────────────────────────────────────────────
# Created once in main() and reused for every direct API call.
_session: Optional[requests.Session] = None

# ── Dashboard / Display State ─────────────────────────────────────────────────
# _dashboard is the DashboardState instance during a full-terminal run.
# _console_handler is the RichHandler from setup_logging() (for V key toggle).
# _live_instance is the active Rich Live context (for R key refresh).
_dashboard: Optional[Any] = None
_console_handler: Optional[logging.Handler] = None
_media_logger: Optional[logging.Logger] = None
_run_log_dir: Optional[Path] = None       # per-run subdirectory; set by setup_logging()
_plex_base_url: Optional[str] = None      # server URL; set in main() for [S] key
# Plaintext Plex auth token for the duration of one job.
#
# v0.9.5 — encryption at rest (server/secrets.py) covers the token on
# disk (servers.json and the legacy settings.json). It does NOT cover
# this module-level alias: python-plexapi's PlexServer instance
# necessarily holds the decrypted token in memory to sign every HTTP
# request, and several direct-HTTP helpers (/:/scrobble, /:/rate,
# /:/progress) read this variable rather than the PlexServer object.
# Cleared at the end of every run by reset_run_state(). Accepted
# residual exposure: encryption at rest protects against host-disk /
# volume / backup theft; it does not protect against a compromised
# running process that can read another process's memory.
_plex_token: Optional[str] = None
_plex_owner_name: str = "Plex Owner"      # actual myPlexUsername; set after connect
# v0.9.6: owner's Plex.tv email — the canonical "identifier" the
# dashboard uses for the run-owner phases of current_user. Distinct
# from _plex_owner_name (which is the myPlexUsername / handle) because
# the user_display_names map is keyed by email for owners and by
# username for managed users (single dict, two kinds of keys).
_plex_owner_email: str = ""
# v0.9.7 Item 4: gate for the dashboard's ``current_user`` field.
# True only during a direct-transfer run with a non-empty
# ``user_filter`` (i.e. the operator explicitly narrowed the run to
# specific users). In every other case — standard export, standard
# import, direct transfer with no filter — the four ``set_current_user``
# call sites in importer/exporter become no-ops and the field stays
# null so the dashboard header doesn't render it. Cleared at the
# start of every run by ``reset_run_state``; flipped True by
# ``server.jobs._run_direct`` after it resolves the filter.
_current_user_visible: bool = False
_live_instance: Optional[Any] = None      # active Live context; set in run_export/run_import

# ── Run-trigger labels (v0.9.5) ──────────────────────────────────────────────
# Filled by server/jobs.py at job start so services/exporter.py can stamp
# the resulting .plexbackup.json with how the run was initiated. "manual"
# for an API call from the GUI, "schedule" for the background scheduler,
# "cli" / "" for direct invocations. Schedule fires also fill
# _run_schedule_name with the schedule's display name.
_run_trigger: str = ""
_run_schedule_name: str = ""

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
# HARDCODED intentionally — dynamic generation could produce wrong advice.
TROUBLESHOOT_CATEGORIES: Dict[str, Dict] = {
    "local_guid_no_match": {
        "title": "local:// GUID — No MusicBrainz Match",
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
            "Re-run plexmigrate.py --export to capture the updated GUIDs.",
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
            "should have caught it automatically — verify the item exists in Plex.",
            "If the root AND some intermediate folders changed, re-run with: "
            "--remap-path /old/root /new/root to translate the stored root prefix.",
            "If paths match but files still aren't found, check drive permissions.",
        ],
    },
    "ambiguous_title_match": {
        "title": "Ambiguous Title Match — Multiple Results",
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
            "selection (use carefully — may match the wrong item).",
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
            "Try re-running the import — transient errors often resolve on retry.",
        ],
    },
    "playlist_item_already_present": {
        "title": "Playlist Item Already Present — Skipped",
        "explanation": (
            "This item was already in the playlist on the target server and was "
            "skipped to avoid duplicates. This is expected behaviour — "
            "PlexMigrate never adds duplicate items to existing playlists."
        ),
        "steps": [
            "No action required — this item is already correctly in the playlist.",
            "If you believe the playlist is wrong, review it directly in Plex.",
        ],
    },
    "collection_member_already_present": {
        "title": "Collection Member Already Present — Skipped",
        "explanation": (
            "This item was already a member of the collection on the target server "
            "and was skipped to avoid duplicates."
        ),
        "steps": [
            "No action required — this item is already correctly in the collection.",
        ],
    },
    "rating_already_set": {
        "title": "Rating Already Set — Skipped",
        "explanation": (
            "This item already has a star rating on the target server. "
            "PlexMigrate treats the target rating as authoritative and never "
            "overwrites it, even if the backup contains a different value."
        ),
        "steps": [
            "No action required — the existing rating is preserved.",
            "If you want to change the rating, do so directly in Plex.",
        ],
    },
    "smart_playlist_skipped": {
        "title": "Smart Playlist — Requires Manual Recreation",
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
            "The original filter URL is recorded in the run log for this backup "
            "(search for the playlist name alongside 'smart_content:').",
        ],
    },
    "no_tier_match": {
        "title": "No Match Found — All Tiers Exhausted",
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
# the same suffix.
_run_timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

# ── Thread-Safety Lock ────────────────────────────────────────────────────────
# Guards _lib_successes, _lib_failures, and _failure_categories.
_log_lock = threading.Lock()

# ── Per-Library Result Accumulators ───────────────────────────────────────────
# Populated by _record_success() and _record_failure() in logging_ops.py.
_lib_successes: Dict[str, List[Dict]] = {}
_lib_failures: Dict[str, List[Dict]] = {}
_failure_categories: Dict[str, List[Dict]] = {}


# ── Scrobble view-count increment cap ────────────────────────────────────────
# Per-item upper bound on the number of /:/scrobble calls fired during
# import. Each call adds one to the target's viewCount; this caps how far
# we push the count in a single run. Re-running the import advances another
# batch. Bounded to keep a runaway backup (e.g. a stale viewCount of 99999)
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
    server mode every run_export / run_import / run_direct_transfer call
    invokes this at its top to start clean.
    """
    with _log_lock:
        _lib_successes.clear()
        _lib_failures.clear()
        _failure_categories.clear()
    _lib_task_ids.clear()
    # Clear the run-trigger labels so a manual run after a scheduled
    # one doesn't inherit the scheduler's "schedule" tag.
    global _run_trigger, _run_schedule_name, _plex_owner_email
    _run_trigger = ""
    _run_schedule_name = ""
    # v0.9.6: owner email is re-populated at the next job start.
    _plex_owner_email = ""
    # v0.9.7 Item 4: ``current_user`` visibility opts in per-run. The
    # next ``_run_direct`` call sets this True iff a user filter is
    # active; standard export / import always leaves it False.
    global _current_user_visible
    _current_user_visible = False
    # v0.9.6: wipe HTTP telemetry on the dashboard. The dashboard
    # instance is owned by the next-run setup; if one is already
    # attached (server mode reuses placeholders) clear its state too.
    if _dashboard is not None:
        try:
            _dashboard.reset_http_telemetry()
        except Exception:
            # Defensive: a malformed dashboard shouldn't block reset.
            pass
