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
    _lib_successes,
    _lib_failures,
    _failure_categories,
)
from services.dashboard import _action_type_from_record


# ── Utility: Timestamped Local Time ───────────────────────────────────────────

def _tz_now() -> str:
    """
    Returns the current local time as a formatted string with UTC offset.

    Returns:
        str: Formatted timestamp, e.g. "2026-05-09 17:33:00 -0700"
    """
    now = datetime.now().astimezone()
    return now.strftime("%Y-%m-%d %H:%M:%S %z")


# ── Logging Setup ─────────────────────────────────────────────────────────────

def setup_logging(log_dir: str, verbose: bool) -> logging.Logger:
    """
    Creates and configures the shared loggers used throughout the script.

    Three log files are written per run:
        runtime.log — all runtime events (DEBUG+) from the main logger.
        errors.log  — ERROR+ from both the main logger and the media logger.
        media.log   — per-item export and import events (DEBUG+).

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

    # ── Shared error file handler (ERROR+) ────────────────────────────────────
    fh_errors = logging.FileHandler(errors_log_file, encoding="utf-8")
    fh_errors.setLevel(logging.ERROR)
    fh_errors.setFormatter(file_fmt)

    # ── Main runtime logger ────────────────────────────────────────────────────
    logger = logging.getLogger("plexmigrate")
    logger.setLevel(logging.DEBUG)

    fh_runtime = logging.FileHandler(runtime_log_file, encoding="utf-8")
    fh_runtime.setLevel(logging.DEBUG)
    fh_runtime.setFormatter(file_fmt)
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

    # ── Media logger (per-item export / import data) ──────────────────────────
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
    # uncaught exceptions funnelled into this per-job logger — they belong
    # to the long-lived server, not to one engine run.
    if not state.HEADLESS_MODE:
        def _excepthook(exc_type, exc_value, exc_tb):
            if issubclass(exc_type, KeyboardInterrupt):
                sys.__excepthook__(exc_type, exc_value, exc_tb)
                return
            logger.critical(
                "Unhandled exception — script crashed",
                exc_info=(exc_type, exc_value, exc_tb),
            )

        sys.excepthook = _excepthook

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
    with _log_lock:
        _lib_successes.setdefault(library, []).append(record)

    if state._dashboard:
        action_type = _action_type_from_record(record)
        if action_type == "skipped":
            state._dashboard.inc_skipped()
        else:
            state._dashboard.inc_completed()
            state._dashboard.push_activity(action_type, library, record.get("title", ""))


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
    """
    with _log_lock:
        _lib_failures.setdefault(library, []).append(record)
        _failure_categories.setdefault(category, []).append(record)

    if state._dashboard:
        _resolution_failure_cats = {
            "no_tier_match", "local_guid_no_match",
            "file_path_not_found", "ambiguous_title_match",
        }
        if category in _resolution_failure_cats:
            state._dashboard.inc_unresolved()
            state._dashboard.push_activity("unresolved", library, record.get("title", ""))
        else:
            state._dashboard.inc_failed()
            state._dashboard.push_activity("failed", library, record.get("title", ""))


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

    successes = _lib_successes.get(library, [])
    failures = _lib_failures.get(library, [])
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
    if not _failure_categories:
        return

    log_path = Path(log_dir)
    tpath = log_path / "troubleshoot.log"

    with open(tpath, "w", encoding="utf-8") as f:
        f.write(f"PlexMigrate Troubleshooting Log — {_tz_now()}\n")
        f.write("=" * 60 + "\n\n")

        for cat_key, items in _failure_categories.items():
            cat = TROUBLESHOOT_CATEGORIES.get(cat_key, {
                "title": cat_key,
                "explanation": "An unexpected error category — check the run log.",
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
            "2. After fixing, re-run the import — already-successful items will be skipped.\n"
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
    unresolved = _failure_categories.get("no_tier_match", [])
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
