"""
Sync engine audit log.

Sibling of :mod:`services.collection_cache_log` and
:mod:`services.playlist_cache_log`. Carries every record the sync
worker (and any helpers it calls into, including the playlist copy
orchestrator when the sync worker is the initiator) emits during a
poll cycle.

Why this exists:
  Sync activity must NEVER bleed into a running job's runtime.log.
  Before this module existed, every sync_worker log line propagated
  to the ``plexmigrate`` root logger. When a snapshot/restore job
  was simultaneously running with its own runtime.log FileHandler
  attached to ``plexmigrate``, sync records landed in that job's
  per-run log and polluted it. The same was true for playlist
  copies the sync worker initiated — the playlist_copy module's
  logger propagated to ``plexmigrate`` and the active job's
  handler caught those records too.

  This logger fixes both:
    * ``propagate = False`` so records DO NOT bubble up to
      ``plexmigrate`` and never reach a running job's handlers.
    * A dedicated RotatingFileHandler writing to
      ``<data_dir>/sync.log`` so operators have one place to read
      cycle-by-cycle sync activity.
    * Same rotation knobs as the other audit logs
      (collection_cache.log, db_access.log, playlist_cache.log)
      so an operator who sizes one sizes them all.

Format mirrors the other audit logs so the log-viewer renders it
identically::

    2026-05-19 10:30:00 [INFO] [sync-worker] sub=12 cycle=ab12 ...
    2026-05-19 10:30:05 [INFO] [sync-worker] sub=12 playlist=Workout merged=1 already=18 ...

Manual / operator-initiated playlist copies (the Playlist Management
flow and Restore-mode playlists) are UNAFFECTED — they don't pass
this logger in, so playlist_copy keeps using its module logger which
propagates to runtime.log as before.
"""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path
from typing import Optional


_DEFAULT_ROTATE_MAX_BYTES = 50 * 1024 * 1024
_DEFAULT_ROTATE_BACKUP_COUNT = 5

_LOGGER_NAME = "plexmigrate.sync"
_FILE_NAME = "sync.log"

_handler_attached = False
_log_path: Optional[Path] = None


def _resolve_rotate_settings() -> tuple:
    """Share rotation knobs with the other audit logs so one tunable
    sizes them all. Defaults are conservative."""
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


def get_sync_logger() -> logging.Logger:
    """Return the dedicated sync logger. Idempotent.

    The logger:
      * Uses namespace ``plexmigrate.sync``.
      * Has ``propagate = False`` so records do NOT reach
        ``plexmigrate`` (and therefore do NOT land in a running
        job's runtime.log).
      * Writes to ``<data_dir>/sync.log`` via a RotatingFileHandler.
      * Goes through the token-scrubber chain so admin tokens never
        leak into the file.

    On any setup failure the function still returns a usable Logger
    (with NullHandler) so caller code never has to special-case the
    None path.
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
        # Token scrubber install (best-effort).
        try:
            from server.log_scrubber import install_on_handler
            install_on_handler(handler)
        except Exception:
            pass
        log_.addHandler(handler)
    finally:
        _handler_attached = True
    return log_


def get_sync_log_path() -> Optional[Path]:
    """Return the sync.log file path, or None if setup failed."""
    return _log_path


def _close_for_tests() -> None:
    """Reset module state. Tests use this to force a fresh
    setup against a tmp_path data dir."""
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
