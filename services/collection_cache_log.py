"""
Collection-cache audit log.

Sibling of :mod:`services.playlist_cache_log`. End user-facing audit
trail for every collection-children-cache warm/clear attempt (per-server
or bulk-all) the orchestrator runs. The bulk-cache UI control in
Servers > Overview surfaces per-server FAILURE reasons inline; this
file is the durable counterpart so the same reasons can be reviewed
later under Application Logs without scrolling through runtime.log.

Format mirrors playlist_cache.log so the existing log-viewer plumbing
renders it identically::

    2026-05-18 18:50:09 [INFO] server=plex_a action=warm-start source=servers-bulk
    2026-05-18 18:50:12 [INFO] server=plex_a action=warm-end ok_collections=316 elapsed_s=2.1 source=servers-bulk
    2026-05-18 18:50:12 [WARNING] server=plex_b action=warm-error source=servers-bulk error="server unreachable: HTTPConnectionPool ..."
    2026-05-18 18:50:12 [INFO] server=plex_a action=clear deleted=316

Tags are space-separated key=value pairs.
"""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path
from typing import Optional


_DEFAULT_ROTATE_MAX_BYTES = 50 * 1024 * 1024
_DEFAULT_ROTATE_BACKUP_COUNT = 5

_LOGGER_NAME = "plexmigrate.collection_cache"
_FILE_NAME = "collection_cache.log"

_handler_attached = False


def _resolve_rotate_settings() -> tuple:
    """Share the rotation knobs with db_access.log + playlist_cache.log
    so an operator who sizes one audit trail sizes them all."""
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


def _get_logger() -> logging.Logger:
    """Return the dedicated collection-cache logger. Idempotent."""
    global _handler_attached
    log_ = logging.getLogger(_LOGGER_NAME)
    if _handler_attached:
        return log_
    log_.setLevel(logging.INFO)
    log_.propagate = False
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
    parts = []
    for k, v in fields.items():
        if v is None:
            continue
        s = str(v)
        if any(c in s for c in (' ', '"', '\t')):
            s = '"' + s.replace('"', '\\"') + '"'
        parts.append(f"{k}={s}")
    return " ".join(parts)


def log_warm_start(
    *, server_id: str, source: Optional[str] = None,
) -> None:
    """Emitted at the start of a per-server warm. ``source`` is a free
    text tag (``"servers-bulk"`` / ``"servers-overview-row"`` / ``"api"``)
    so the same operator-grep convention as playlist_cache.log works."""
    _get_logger().info(_format_kv(
        server=server_id, action="warm-start", source=source,
    ))


def log_warm_end(
    *, server_id: str, ok_collections: int, ok_items: int,
    skipped: int, errors: int, elapsed_s: float,
    source: Optional[str] = None,
) -> None:
    """Emitted at the end of a successful per-server warm. ``errors`` is
    the per-collection error count (one collection failed to fetch /
    serialize, the rest succeeded). Use :func:`log_warm_error` for the
    server-level failure (connection refused, library walk failed, etc.)."""
    _get_logger().info(_format_kv(
        server=server_id, action="warm-end",
        ok_collections=ok_collections, ok_items=ok_items,
        skipped=skipped, errors=errors,
        elapsed_s=round(float(elapsed_s), 2),
        source=source,
    ))


def log_warm_error(
    *, server_id: str, error: str,
    source: Optional[str] = None,
) -> None:
    """Server-level warm failure: the warm couldn't even enumerate
    libraries (server unreachable, registry says no such server, etc.).
    This is what the bulk-cache UI failure list surfaces and what the
    operator greps for after a red row appears in Servers > Overview."""
    _get_logger().warning(_format_kv(
        server=server_id, action="warm-error",
        source=source,
        error=error,
    ))


def log_collection_warm_error(
    *, server_id: str, library: str, collection_title: str,
    error: str,
) -> None:
    """Per-collection warm failure inside a server pass: the server is
    reachable, the library walked, but THIS collection's .items() /
    serialize raised. Logged at WARNING so failures stand out in the
    audit trail without drowning out the per-server ok/error rows."""
    _get_logger().warning(_format_kv(
        server=server_id, library=library,
        action="collection-warm-error",
        collection=collection_title,
        error=error,
    ))


def log_clear(
    *, server_id: str, deleted: int,
) -> None:
    """Emitted when the operator clears a server's collection cache
    (per-row Clear button) or invalidates the whole table. Mirrors the
    audit-trail intent of the playlist cache: every operator-driven
    mutation leaves a line."""
    _get_logger().info(_format_kv(
        server=server_id, action="clear",
        deleted=deleted,
    ))


def _close_for_tests() -> None:
    """Detach the handler so tests that patch the data dir don't
    accumulate handlers across runs. Mirrors playlist_cache_log's test
    helper (test fixtures call this in autouse cleanup)."""
    global _handler_attached
    log_ = logging.getLogger(_LOGGER_NAME)
    for h in list(log_.handlers):
        try:
            h.close()
        except Exception:
            pass
        log_.removeHandler(h)
    _handler_attached = False
