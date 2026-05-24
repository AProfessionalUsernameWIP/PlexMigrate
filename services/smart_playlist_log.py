"""Smart Playlist Migration job log.

Sibling of :mod:`services.sync_log` / :mod:`services.collection_cache_log`.
Carries every record a Smart Playlist Migration job emits - filter
decode, id->name translation, preflight warnings, re-create outcomes.

Why this exists:
  The migration job must give live feedback on its own job page, and
  its activity must NEVER bleed into another running job's
  runtime.log. A dedicated logger with ``propagate = False`` keeps
  the records out of the ``plexmigrate`` root, and a RotatingFile
  handler at ``<data_dir>/smart_playlist.log`` gives the in-panel
  live log view (and the Application Logs UI) one place to read.

Format mirrors the other audit logs so the log viewer renders it
identically.
"""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path
from typing import Optional


_DEFAULT_ROTATE_MAX_BYTES = 50 * 1024 * 1024
_DEFAULT_ROTATE_BACKUP_COUNT = 5

_LOGGER_NAME = "plexmigrate.smartplaylist"
_FILE_NAME = "smart_playlist.log"

_handler_attached = False
_log_path: Optional[Path] = None


def _resolve_rotate_settings() -> tuple:
    """Share rotation knobs with the other audit logs so one tunable
    sizes them all."""
    max_bytes = _DEFAULT_ROTATE_MAX_BYTES
    backup_count = _DEFAULT_ROTATE_BACKUP_COUNT
    try:
        from server.persistence import load_settings
        settings = load_settings() or {}
        raw_mb = settings.get("log_rotate_max_size_mb")
        if isinstance(raw_mb, (int, float)) and raw_mb >= 1:
            max_bytes = int(raw_mb * 1024 * 1024)
        raw_count = settings.get("log_rotate_backup_count")
        if isinstance(raw_count, int) and raw_count >= 0:
            backup_count = raw_count
    except Exception:
        pass
    return max_bytes, backup_count


def _target_dir() -> Path:
    try:
        from server.persistence import get_data_dir
        return get_data_dir()
    except Exception:
        return Path(".")


def get_smart_playlist_logger() -> logging.Logger:
    """Return the dedicated Smart Playlist Migration logger. Idempotent.

    The logger uses namespace ``plexmigrate.smartplaylist``, has
    ``propagate = False`` so records never reach a running job's
    runtime.log, writes to ``<data_dir>/smart_playlist.log`` via a
    RotatingFileHandler, and runs through the token scrubber.

    On any setup failure it still returns a usable Logger (with a
    NullHandler) so callers never special-case None.
    """
    global _handler_attached, _log_path
    log_ = logging.getLogger(_LOGGER_NAME)
    if _handler_attached:
        return log_
    log_.setLevel(logging.DEBUG)
    log_.propagate = False
    try:
        target = _target_dir() / _FILE_NAME
        target.parent.mkdir(parents=True, exist_ok=True)
        max_bytes, backup_count = _resolve_rotate_settings()
        handler: logging.Handler
        try:
            handler = logging.handlers.RotatingFileHandler(
                str(target),
                maxBytes=max_bytes,
                backupCount=backup_count,
                encoding="utf-8",
            )
            _log_path = target
        except Exception:
            handler = logging.NullHandler()
        handler.setFormatter(
            logging.Formatter(
                fmt="%(asctime)s [%(levelname)s] [%(threadName)s] %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        try:
            from server.log_scrubber import install_on_handler
            install_on_handler(handler)
        except Exception:
            pass
        log_.addHandler(handler)
    finally:
        _handler_attached = True
    return log_


def get_smart_playlist_log_path() -> Optional[Path]:
    """Return the smart_playlist.log path, or None if setup failed."""
    return _log_path


def _close_for_tests() -> None:
    """Reset module state so a test can force fresh setup against a
    tmp_path data dir."""
    global _handler_attached, _log_path
    log_ = logging.getLogger(_LOGGER_NAME)
    for h in list(log_.handlers):
        try:
            h.close()
        except Exception:
            pass
        log_.removeHandler(h)
    _handler_attached = False
    _log_path = None
