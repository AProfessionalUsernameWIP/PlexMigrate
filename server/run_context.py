"""
Shared per-run logger, run-timestamp, and run-directory helpers.

Used by the job runners in ``server/jobs.py`` and the fan-out path in
``server/fan_out.py`` so both share one canonical implementation of the
per-run logger setup, handler teardown, the PASS/FAIL run-dir rename,
and the slug/timestamp derivation that prefixes run-log directories
and snapshot filenames.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

import services.state as state
from services.logging_ops import setup_logging
from server.persistence import load_settings


def _build_logger(log_dir: str, verbose: bool) -> Tuple[logging.Logger, str]:
    """
    Set up the per-run logger via ``setup_logging``.
    Returns ``(logger, run_log_dir)``. When the end user has disabled
    run logging via the global ``run_logging_enabled`` setting,
    ``setup_logging`` skips the per-run directory entirely and returns
    a console-only logger; ``run_log_dir`` is then the empty string so
    downstream finalize / library-log writers know to no-op.
    """
    # Resolve the global toggle (default true). Per-server is
    # intentionally out of scope for run logging - one global switch.
    run_logging_enabled = bool(
        (load_settings() or {}).get("run_logging_enabled", True)
        if (load_settings() or {}).get("run_logging_enabled") is not None
        else True
    )
    logger = setup_logging(log_dir, verbose, run_logging_enabled=run_logging_enabled)
    if state._run_log_dir is None:
        return logger, ""
    return logger, str(state._run_log_dir)


def _close_logger(logger: logging.Logger, run_log_dir: str) -> None:
    """
    Close and detach the per-run file handlers from the logger.

    The engine's setup_logging adds rotating file handlers to two
    named loggers; if we don't close them here, the per-run log
    directory rename below fails on Windows because the files are
    still open.

    Handlers tagged with ``_pm_app_log`` (installed at app startup by
    :func:`server.app._install_app_log_handler` to write the
    server-wide ``app.log``) are NOT detached here - they're meant to
    persist across job lifetimes so the unified app log keeps
    receiving records.
    """
    for lg in (logging.getLogger("plexmigrate"), logging.getLogger("plexmigrate.media")):
        for h in lg.handlers[:]:
            if getattr(h, "_pm_app_log", False):
                continue  # persistent app-log handler; leave attached
            try:
                h.close()
            finally:
                lg.removeHandler(h)


def _finalise_run_dir(run_log_dir: str) -> None:
    """
    Apply the end-of-run PASS/FAIL rename to the run log directory.
    Best-effort: a rename failure on a locked file is logged and
    ignored - the logs themselves are still readable.
    """
    # Run-logging-disabled path: no run dir to rename.
    if not run_log_dir:
        return
    p = Path(run_log_dir)
    if not p.exists():
        return
    errors_file = p / "errors.log"
    passed = not (errors_file.exists() and errors_file.stat().st_size > 0)
    suffix = "PASS" if passed else "FAIL"
    final = p.parent / f"{p.name}_{suffix}"
    try:
        p.rename(final)
    except OSError:
        # On Windows, a still-open handle blocks the rename. We've
        # already closed our handlers, but any background scan-cache
        # thread the engine may have spawned could still hold one.
        # Leaving the directory under its temporary name is acceptable.
        pass


def _finalize_run(logger: logging.Logger, run_log_dir: str) -> None:
    """
    End-of-run teardown: close the per-run file handlers, then apply
    the PASS/FAIL rename to the run directory.

    This is the two-step teardown the engine-runner methods in
    ``server/jobs.py`` previously hand-rolled at each exit path -
    ``_close_logger`` must run before ``_finalise_run_dir`` so the
    directory rename isn't blocked by an open log handle on Windows.
    """
    _close_logger(logger, run_log_dir)
    _finalise_run_dir(run_log_dir)


# ── Run-timestamp / slug derivation ──────────────────────────────────

_LIB_SLUG_STRIP = re.compile(r'[\\/:*?"<>|]+')
_LIBRARIES_IN_SLUG = 3  # cap before "+N" overflow kicks in


def _safe_lib_slug(name: str) -> str:
    """Sanitize one library name for use inside a filesystem path.

    Replaces whitespace with hyphens and strips characters that would
    cause trouble on Windows / macOS / Linux. Empty input -> "".
    """
    if not name:
        return ""
    cleaned = _LIB_SLUG_STRIP.sub("", name).strip()
    return re.sub(r"\s+", "-", cleaned) or ""


def _libraries_slug(libraries: Optional[List[str]]) -> str:
    """Compose a short, readable libraries fragment for the run-dir name.

    Caps the visible list at ``_LIBRARIES_IN_SLUG`` and appends ``+N``
    for the rest so long lists don't blow the dir name into something
    unreadable. Returns ``""`` when no libraries are supplied (the
    caller's slug then falls back to server-only).

    Example: ``["Movies", "TV Shows", "Audio-Books", "Music"]`` ->
    ``"Movies_TV-Shows_Audio-Books+1"``.
    """
    if not libraries:
        return ""
    parts: List[str] = []
    for name in libraries:
        slug = _safe_lib_slug(str(name))
        if slug:
            parts.append(slug)
    if not parts:
        return ""
    if len(parts) <= _LIBRARIES_IN_SLUG:
        return "_".join(parts)
    overflow = len(parts) - _LIBRARIES_IN_SLUG
    return "_".join(parts[:_LIBRARIES_IN_SLUG]) + f"+{overflow}"


def _set_run_timestamp(
    slug: str,
    libraries: Optional[List[str]] = None,
) -> None:
    """
    Re-derive ``state._run_timestamp`` so the current run's log dir
    and snapshot filenames are prefixed with the server's slug and,
    when known, the libraries the run is about.

    The engine reads ``state._run_timestamp`` lazily inside
    :func:`services.logging_ops.setup_logging` and
    :func:`services.snapshot.plex_native.snapshotter.snapshot_library`, so we can reassign it
    here without touching either of those modules.

    Examples:
      ``slug="Plex1", libraries=None`` ->
          log dir   : plex_logs/run_Plex1_20260510_135425/
          filename  : Movies_Plex1_20260510_135425.plexexport.json

      ``slug="Plex1", libraries=["Movies","TV Shows"]`` ->
          log dir   : plex_logs/run_Plex1_Movies_TV-Shows_20260510_135425/

      ``slug="Plex1", libraries=["Movies","TV","Audio","Music","Photos"]`` ->
          log dir   : plex_logs/run_Plex1_Movies_TV_Audio+2_20260510_135425/
    """
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    libs_part = _libraries_slug(libraries)
    if slug and slug != "adhoc":
        if libs_part:
            state._run_timestamp = f"{slug}_{libs_part}_{ts}"
        else:
            state._run_timestamp = f"{slug}_{ts}"
    else:
        state._run_timestamp = f"{libs_part}_{ts}" if libs_part else ts
