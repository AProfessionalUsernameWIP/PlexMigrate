"""
Logging setup and result recording for PlexMigrate.

Contains setup_logging() (builds all file and console handlers), the
thread-safe _record_success() / _record_failure() accumulators, and the
log-file writers called at the end of each library import.
"""

import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List

from rich.logging import RichHandler

import services.state as state
from services.state import (
    TROUBLESHOOT_CATEGORIES,
    console,
    _log_lock,
)
from services.dashboard import _action_type_from_record


# v0.10.0: ``_lib_successes``, ``_lib_failures``, and
# ``_failure_categories`` are no longer plain module globals - they're
# ContextVar-backed in :mod:`services.state` so parallel fan-out
# destinations each have isolated accumulator dicts. Importing them by
# name at module load would freeze a reference to whichever dict was
# current at import time; we route through ``state.X`` instead so
# every read picks up the calling context's dict.


# ── Utility: Timestamped Local Time ───────────────────────────────────────────

def _tz_now() -> str:
    """
    Returns the current local time as a formatted string with UTC offset.

    Returns:
        str: Formatted timestamp, e.g. "2026-05-09 17:33:00 -0700"
    """
    now = datetime.now().astimezone()
    return now.strftime("%Y-%m-%d %H:%M:%S %z")


# ── Run-log scope filter ─────────────────────────────────────────────────────

class _EngineOnlyFilter(logging.Filter):
    """
    Drop records whose logger name starts with one of the configured
    control-plane prefixes. Attached to the per-run FileHandlers below
    so the FastAPI server's housekeeping output (server-registry calls
    triggered by the open Servers / User Management panels, scheduler
    chatter, route handlers) does not bleed into the per-run log dir
    while a job is active.

    The same records still propagate to the console / app log; we just
    don't write them into the run's runtime.log or errors.log.
    """
    # ``plexmigrate.server`` covers app.py, server_registry,
    # managed_users_router, snapshot_registry, network_collector, etc.
    # ``plexmigrate.db_access`` writes its OWN per-run file via
    # services/db_access_log.py and sets propagate=False, so excluding
    # it here is belt-and-braces.
    #
    # Exception: ``plexmigrate.server.jobs`` IS kept in the per-run
    # log. The post-job snapshot-capture hook (``_capture_snapshot_after_run``)
    # is engine-adjacent work whose success / failure belongs in the
    # run log next to the engine output. Pre-fix, hook failures only
    # surfaced in uvicorn's stdout and the operator had no visible
    # signal that the snapshot artifact wasn't produced.
    _EXCLUDED_PREFIXES = ("plexmigrate.server", "plexmigrate.db_access")
    _ALLOWED_PREFIXES = ("plexmigrate.server.jobs",)

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name.startswith(self._ALLOWED_PREFIXES):
            return True
        return not record.name.startswith(self._EXCLUDED_PREFIXES)


# ── Logging Setup ─────────────────────────────────────────────────────────────

def setup_logging(log_dir: str, verbose: bool) -> logging.Logger:
    """
    Creates and configures the shared loggers used throughout the script.

    Three log files are written per run:
        runtime.log - all runtime events (DEBUG+) from the main logger.
        errors.log  - ERROR+ from both the main logger and the media logger.
        media.log   - per-item snapshot and import events (DEBUG+).

    Side effects:
        Creates the log directory if it doesn't exist.
        Sets state._console_handler, state._media_logger, state._run_log_dir.
        Installs sys.excepthook so unhandled crashes are recorded in both logs.
    """
    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)
    run_dir = log_path / f"run_{state._run_timestamp}"
    run_dir.mkdir(parents=True, exist_ok=True)
    state._run_log_dir = run_dir

    runtime_log_file = run_dir / "runtime.log"
    errors_log_file  = run_dir / "errors.log"
    media_log_file   = run_dir / "media.log"

    level = logging.DEBUG if verbose else logging.INFO

    file_fmt = logging.Formatter(
        fmt="[%(asctime)s] [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Bug fix: defensively detach any handlers a prior run may have
    # left attached to our named loggers. Without this, each successive
    # job stacked another FileHandler on top of the previous one and
    # every log line was written N times (once per leftover handler).
    # ``_close_logger`` is supposed to detach them on the way out, but
    # any code path that bypasses it - a crash inside the engine, a
    # Hard Stop tearing things down mid-flight, or a fan-out cleanup
    # racing with a new job - could leave residue. Resetting here is
    # idempotent and cheap; one log line == one write, every time.
    for _lg_name in ("plexmigrate", "plexmigrate.media"):
        _lg = logging.getLogger(_lg_name)
        for _h in _lg.handlers[:]:
            try:
                _h.close()
            except Exception:
                pass
            _lg.removeHandler(_h)

    # ── Shared error file handler (ERROR+) ────────────────────────────────────
    fh_errors = logging.FileHandler(errors_log_file, encoding="utf-8")
    fh_errors.setLevel(logging.ERROR)
    fh_errors.setFormatter(file_fmt)

    # Keep control-plane chatter (server tab user-list fetches, scheduler,
    # route handlers) out of the per-run log files. They still flow to
    # the console / uvicorn log via their own logger ancestry; this
    # filter only governs the per-run on-disk view.
    engine_only = _EngineOnlyFilter()
    fh_errors.addFilter(engine_only)

    # ── Main runtime logger ────────────────────────────────────────────────────
    logger = logging.getLogger("plexmigrate")
    logger.setLevel(logging.DEBUG)

    fh_runtime = logging.FileHandler(runtime_log_file, encoding="utf-8")
    fh_runtime.setLevel(logging.DEBUG)
    fh_runtime.setFormatter(file_fmt)
    fh_runtime.addFilter(engine_only)
    logger.addHandler(fh_runtime)
    logger.addHandler(fh_errors)

    ch = RichHandler(
        console=console,
        show_time=False,
        show_path=False,
        markup=False,
        rich_tracebacks=False,
    )
    ch.setLevel(level)
    logger.addHandler(ch)
    state._console_handler = ch

    # ── Media logger (per-item snapshot / import data) ──────────────────────────
    media_logger = logging.getLogger("plexmigrate.media")
    media_logger.setLevel(logging.DEBUG)
    media_logger.propagate = False

    media_fmt = logging.Formatter(
        fmt="[%(asctime)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    fh_media = logging.FileHandler(media_log_file, encoding="utf-8")
    fh_media.setLevel(logging.DEBUG)
    fh_media.setFormatter(media_fmt)
    media_logger.addHandler(fh_media)
    media_logger.addHandler(fh_errors)

    if verbose:
        media_ch = RichHandler(
            console=console,
            show_time=False,
            show_path=False,
            markup=False,
            rich_tracebacks=False,
        )
        media_ch.setLevel(logging.DEBUG)
        media_logger.addHandler(media_ch)

    state._media_logger = media_logger

    # ── Crash handler ─────────────────────────────────────────────────────────
    # P2-5: only rebind sys.excepthook in CLI mode. In the FastAPI process
    # uvicorn (and any background threads it owns) should not have their
    # uncaught exceptions funnelled into this per-job logger - they belong
    # to the long-lived server, not to one engine run.
    if not state.HEADLESS_MODE:
        # Close over the logger *name* (a stable string), not the
        # logger object. Each setup_logging() call detaches and
        # re-attaches handlers; a closure over the object would, after
        # a second CLI run, log a crash through a logger whose file
        # handlers were already detached. Resolving by name at crash
        # time always uses whatever handlers are current.
        _crash_logger_name = logger.name

        def _excepthook(exc_type, exc_value, exc_tb):
            if issubclass(exc_type, KeyboardInterrupt):
                sys.__excepthook__(exc_type, exc_value, exc_tb)
                return
            logging.getLogger(_crash_logger_name).critical(
                "Unhandled exception - script crashed",
                exc_info=(exc_type, exc_value, exc_tb),
            )

        sys.excepthook = _excepthook

    # v0.9.5: install the X-Plex-Token scrubber on every handler that
    # currently exists, including the ones we just attached above and
    # any uvicorn / root handlers from server mode. Re-running on
    # every setup_logging call is fine - install_on_handler is
    # idempotent (it checks for an existing filter first).
    try:
        from server.log_scrubber import install_on_all_handlers
        install_on_all_handlers()
    except Exception:
        # The scrubber is defence-in-depth; a missing module here
        # (CLI-only checkout, partial install) should not stop the
        # logger from being usable.
        pass

    return logger


# ── Media Log Line Formatter ─────────────────────────────────────────────────

def _fmt_media_line(tag: str, library: str, item_type: str, title: str, **attrs) -> str:
    """
    Builds one structured line for the media log.

    Format: [TAG] [Library] type | Title | key: value | key: value ...
    Empty/None attribute values are omitted so lines stay concise.
    """
    parts = [f"[{tag}] [{library}] {item_type} | {title}"]
    for k, v in attrs.items():
        if v is not None and v != "":
            parts.append(f"{k}: {v}")
    return " | ".join(parts)


# ── Thread-Safe Result Recorders ──────────────────────────────────────────────

# v0.12.1 - fields on the success/failure records that may carry an
# exception message verbatim and therefore may carry an
# ``X-Plex-Token=…`` substring. ``reason`` is the dominant offender
# (it's where plexapi exception messages go), but ``action``,
# ``filepath``, and ``detail`` have all been observed to round-trip
# external strings. The log-framework scrubber covers anything routed
# through Python's logging handlers - but ``write_library_logs``
# below writes these dicts to per-library files via plain ``f.write``,
# bypassing the logging framework entirely. The fix is to scrub at
# record-time so both the in-memory accumulators AND the eventual
# on-disk artefacts hold the redacted form. Belt-and-suspenders: the
# scrubber filter on the logging framework still covers everything
# else.
_SCRUB_FIELDS = ("reason", "action", "filepath", "detail", "title", "guid")


def _scrub_record_inplace(record: Dict) -> None:
    """
    Run :func:`server.log_scrubber.scrub` over every string-valued
    field on ``record`` that's known to carry pasted exception text.
    Empty / missing / non-string fields are left untouched. Never
    raises - telemetry must not break the engine path.
    """
    try:
        from server.log_scrubber import scrub
    except Exception:  # pragma: no cover (CLI-only checkout)
        return
    for key in _SCRUB_FIELDS:
        val = record.get(key)
        if isinstance(val, str) and val:
            try:
                record[key] = scrub(val)
            except Exception:
                # Leave the field untouched if the scrubber raises;
                # the framework-level filter still has another shot
                # if the value ever gets logged elsewhere.
                pass


def _record_success(library: str, record: Dict) -> None:
    """
    Thread-safely appends a success record and updates dashboard counters.

    Args:
        library (str): Library name, used as the dict key.
        record (dict): A dict with keys: ts, tier, title, type, action.

    Side effects:
        Appends to _lib_successes[library] under _log_lock.
        Updates state._dashboard counters and activity feed if dashboard is active.
    """
    state._ensure_accumulators()
    _scrub_record_inplace(record)
    with _log_lock:
        state._lib_successes.setdefault(library, []).append(record)

    if state.get_dashboard():
        action_type = _action_type_from_record(record)
        if action_type == "skipped":
            state.get_dashboard().inc_skipped()
        else:
            state.get_dashboard().inc_completed()
            state.get_dashboard().push_activity(action_type, library, record.get("title", ""))


def _record_failure(library: str, record: Dict, category: str) -> None:
    """
    Thread-safely appends a failure record and updates dashboard counters.

    Args:
        library (str): Library name.
        record (dict): Failure record with keys: ts, tier, title, guid,
                       filepath, reason, library, type.
        category (str): One of the TROUBLESHOOT_CATEGORIES keys.

    Side effects:
        Appends to _lib_failures[library] and _failure_categories[category].
        Updates state._dashboard counters and activity feed if dashboard is active.

    Token-scrub note (v0.12.1): we scrub ``reason``, ``action``,
    ``filepath``, and other free-form string fields BEFORE the
    record lands in the in-memory accumulator. ``write_library_logs``
    and ``write_troubleshoot_log`` later flush these dicts straight
    to disk with ``f.write`` - they don't route through the Python
    logging framework, so the scrubber filter installed on the
    handlers never gets to see them. Scrubbing at record-time means
    the on-disk fail logs, the troubleshoot.log, the activity feed
    pushed over the WebSocket, and any future surface that reads
    out of the accumulators all see the redacted form uniformly.
    """
    state._ensure_accumulators()
    _scrub_record_inplace(record)
    with _log_lock:
        state._lib_failures.setdefault(library, []).append(record)
        state._failure_categories.setdefault(category, []).append(record)

    if state.get_dashboard():
        _resolution_failure_cats = {
            "no_tier_match", "local_guid_no_match",
            "file_path_not_found", "ambiguous_title_match",
        }
        if category in _resolution_failure_cats:
            state.get_dashboard().inc_unresolved()
            state.get_dashboard().push_activity("unresolved", library, record.get("title", ""))
        else:
            state.get_dashboard().inc_failed()
            state.get_dashboard().push_activity("failed", library, record.get("title", ""))


# ── Log File Writers ──────────────────────────────────────────────────────────

def write_library_logs(log_dir: str, library: str, total: int) -> None:
    """
    Writes the per-library success and failure log files after processing.

    Args:
        log_dir (str): Directory where log files are written.
        library (str): Library name (used in the filename and summary block).
        total (int): Total items attempted, used to calculate success rate.

    Side effects:
        Creates up to two .log files in log_dir.
    """
    log_path = Path(log_dir)

    state._ensure_accumulators()
    successes = state._lib_successes.get(library, [])
    failures = state._lib_failures.get(library, [])
    succeeded = len(successes)
    failed = len(failures)

    rate = f"{(succeeded / total * 100):.1f}%" if total else "N/A"

    summary = (
        f"\n── Summary ──────────────────────────────\n"
        f"Library       : {library}\n"
        f"Run date      : {_tz_now()}\n"
        f"Total items   : {total}\n"
        f"Succeeded     : {succeeded}\n"
        f"Failed        : {failed}\n"
        f"Success rate  : {rate}\n"
        f"─────────────────────────────────────────\n"
    )

    safe_lib = library.replace(" ", "_").replace("/", "_")

    if successes:
        spath = log_path / f"{safe_lib}_success.log"
        with open(spath, "w", encoding="utf-8") as f:
            for r in successes:
                f.write(
                    f"[{r['ts']}] [SUCCESS] [TIER:{r['tier']}] "
                    f"{r['title']} | {r['type']} | {r['action']}\n"
                )
            f.write(summary)

    if failures:
        fpath = log_path / f"{safe_lib}_fail.log"
        with open(fpath, "w", encoding="utf-8") as f:
            for r in failures:
                f.write(
                    f"[{r['ts']}] [FAIL] [TIER_REACHED:{r.get('tier', 'none')}] "
                    f"{r['title']} | {r.get('guid', 'N/A')} | "
                    f"{r.get('filepath', 'N/A')} | {r['reason']}\n"
                )
            f.write(summary)


def write_troubleshoot_log(log_dir: str) -> None:
    """
    Writes the troubleshooting log grouped by failure category.

    Creates troubleshoot.log in log_dir if any failures exist.
    Does nothing if _failure_categories is empty.
    """
    state._ensure_accumulators()
    if not state._failure_categories:
        return

    log_path = Path(log_dir)
    tpath = log_path / "troubleshoot.log"

    with open(tpath, "w", encoding="utf-8") as f:
        f.write(f"PlexMigrate Troubleshooting Log - {_tz_now()}\n")
        f.write("=" * 60 + "\n\n")

        for cat_key, items in state._failure_categories.items():
            cat = TROUBLESHOOT_CATEGORIES.get(cat_key, {
                "title": cat_key,
                "explanation": "An unexpected error category - check the run log.",
                "steps": ["Review the full run log for details about this error."],
            })

            f.write(f"── {cat['title']} ({'─' * max(0, 54 - len(cat['title']))})\n")
            f.write(f"\nWhat this means:\n  {cat['explanation']}\n\n")
            f.write("Suggested fixes:\n")
            for i, step in enumerate(cat["steps"], 1):
                f.write(f"  {i}. {step}\n")
            f.write(f"\nAffected items ({len(items)}):\n")
            for item in items:
                f.write(
                    f"  • {item.get('library', '?')} | "
                    f"{item.get('title', '?')} | "
                    f"{item.get('reason', '?')}\n"
                )
            f.write("\n" + "─" * 60 + "\n\n")

        f.write(
            "── Next Steps ───────────────────────────\n"
            "1. Review each category above and apply the suggested fixes.\n"
            "2. After fixing, re-run the import - already-successful items will be skipped.\n"
            f"3. If problems persist, check the full runtime log at: "
            f"{log_dir}/runtime.log\n"
            "4. For items that cannot be resolved automatically, restore them manually in Plex.\n"
            "─────────────────────────────────────────\n"
        )


def write_unresolved_log(log_dir: str) -> None:
    """
    Writes the unresolved items index for items that failed all three tiers.

    Creates unresolved.log in log_dir if any items failed all matching tiers.
    Does nothing otherwise.
    """
    state._ensure_accumulators()
    unresolved = state._failure_categories.get("no_tier_match", [])
    if not unresolved:
        return

    log_path = Path(log_dir)
    upath = log_path / "unresolved.log"

    with open(upath, "w", encoding="utf-8") as f:
        f.write(
            "This file lists every item that PlexMigrate could not automatically match on the\n"
            "target server after trying all available methods (GUID lookup, file path lookup,\n"
            "and title search). Use this as a checklist to manually restore these items in Plex.\n"
            "Items are listed one per line in the format:\n"
            "Library | Type | Title | Artist/Show | Album/Season | Failure Reason\n\n"
        )
        f.write("─" * 80 + "\n\n")

        for item in unresolved:
            f.write(
                f"{item.get('library', '?')} | "
                f"{item.get('type', '?')} | "
                f"{item.get('title', '?')} | "
                f"{item.get('parent', '?')} | "
                f"{item.get('grandparent', '?')} | "
                f"{item.get('reason', '?')}\n"
            )
