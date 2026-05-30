"""End user-facing audit trail for every playlist-cache refresh attempt."""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path
from typing import Optional


# Default rotation. Matches db_access.log: 50 MB max, 5 archives kept.
_DEFAULT_ROTATE_MAX_BYTES = 50 * 1024 * 1024
_DEFAULT_ROTATE_BACKUP_COUNT = 5


def _resolve_rotate_settings() -> tuple:
    """Read ``log_rotate_max_size_mb`` + ``log_rotate_backup_count`` from persisted settings, falling back to defaults."""
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


_LOGGER_NAME = "plexmigrate.playlist_cache"
_FILE_NAME = "playlist_cache.log"

_handler_attached = False


def _target_dir() -> Path:
    """Return the server_data directory for the playlist cache log file."""
    try:
        from server.persistence import get_data_dir
        return get_data_dir()
    except Exception:
        return Path(".")


def _get_logger() -> logging.Logger:
    """Return the dedicated playlist-cache logger, attaching the RotatingFileHandler once per process."""
    global _handler_attached
    log_ = logging.getLogger(_LOGGER_NAME)
    if _handler_attached:
        return log_
    log_.setLevel(logging.INFO)
    log_.propagate = False  # don't double-print into runtime.log
    target = _target_dir() / _FILE_NAME
    target.parent.mkdir(parents=True, exist_ok=True)
    max_bytes, backup_count = _resolve_rotate_settings()
    try:
        handler: logging.Handler = logging.handlers.RotatingFileHandler(
            str(target),
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
    except Exception:
        # File-handler attach failed (read-only filesystem, perms, etc.).
        # Fall back to a NullHandler so callers don't crash; the audit
        # trail goes silent but the engine keeps working.
        handler = logging.NullHandler()
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s [%(levelname)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    log_.addHandler(handler)
    _handler_attached = True
    return log_


def _format_kv(**fields) -> str:
    """Render ``key=value`` pairs space-separated, with None values dropped."""
    parts = []
    for k, v in fields.items():
        if v is None:
            continue
        s = str(v)
        if any(c in s for c in (' ', '"', '\t')):
            s = '"' + s.replace('"', '\\"') + '"'
        parts.append(f"{k}={s}")
    return " ".join(parts)


def log_bulk_refresh_start(
    *, server_id: str, user_count: int, source: Optional[str] = None,
) -> None:
    """Log the start of a bulk per-server refresh."""
    _get_logger().info(_format_kv(
        server=server_id, action="bulk-refresh-start",
        users=user_count, source=source,
    ))


def log_bulk_refresh_end(
    *, server_id: str, ok: int, errors: int, elapsed_s: float,
    source: Optional[str] = None,
) -> None:
    """Log the end of a bulk refresh with outcome counts and elapsed time."""
    _get_logger().info(_format_kv(
        server=server_id, action="bulk-refresh-end",
        ok=ok, errors=errors, elapsed_s=round(float(elapsed_s), 2),
        source=source,
    ))


def log_user_refresh(
    *, server_id: str, user_id: str, ok: bool, elapsed_ms: int,
    playlists: int = 0, items: int = 0, error: Optional[str] = None,
) -> None:
    """Log a per-user refresh result with optional error details."""
    level = logging.INFO if ok else logging.WARNING
    payload = _format_kv(
        server=server_id, user=user_id,
        action="user-refresh-end" if ok else "user-refresh-error",
        playlists=playlists if ok else None,
        items=items if ok else None,
        elapsed_ms=elapsed_ms,
        error=error if not ok else None,
    )
    _get_logger().log(level, payload)


def log_auth_chain_step(
    *, server_id: str, user_id: str, step: str, outcome: str,
    role: str = "source", detail: Optional[str] = None,
) -> None:
    """Log a per-step trace for the combined per-user auth chain.
    
    outcome: "hit" (token obtained), "miss" (step failed), "used" (fallback applied), "refused" (strict-mode).
    """
    level = logging.INFO if outcome in ("hit", "used") else logging.WARNING
    payload = _format_kv(
        server=server_id, user=user_id,
        action="auth-chain",
        step=step, outcome=outcome, role=role,
        detail=detail,
    )
    _get_logger().log(level, payload)