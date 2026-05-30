"""Dedicated logger for the user-activity sweeper audit log."""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path
from typing import Optional


_DEFAULT_ROTATE_MAX_BYTES = 50 * 1024 * 1024
_DEFAULT_ROTATE_BACKUP_COUNT = 5

_LOGGER_NAME = "plexmigrate.user_activity"
_FILE_NAME = "user_activity.log"

_handler_attached = False
_log_path: Optional[Path] = None


def _resolve_rotate_settings() -> tuple:
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


def get_user_activity_logger() -> logging.Logger:
    """Return the user-activity logger (idempotent, propagate=False)."""
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


def _close_for_tests() -> None:
    """Reset module state for test isolation."""
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
