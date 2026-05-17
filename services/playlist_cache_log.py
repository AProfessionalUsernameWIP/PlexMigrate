"""
Playlist-cache audit log.

End user-facing audit trail for every playlist-cache refresh attempt
the orchestrator runs. Mirrors the design of
:mod:`services.db_access_log` (RotatingFileHandler under
``server_data/playlist_cache.log``) so the existing Application Logs
panel can surface it without bespoke plumbing.

Why a dedicated log:
The end user clicks "Refresh" on a server in Servers > Overview and
expects to see proof that the playlist cache rebuild ran (per-user
results, errors, durations). Live-tailing runtime.log works but it's
noisy with engine traces. A dedicated file keeps the audit trail
short, structured, and trivially greppable.

Format
------
Each line is a single structured event::

    2026-05-16 14:32:08 [INFO] server=plex_a1b2c3 action=bulk-refresh-start users=5
    2026-05-16 14:32:18 [INFO] server=plex_a1b2c3 action=bulk-refresh-end ok=4 errors=1 elapsed_s=9.8
    2026-05-16 14:32:18 [WARNING] server=plex_a1b2c3 user=nlovlyn action=user-refresh-error error="429 Too Many Requests"

Tags are space-separated key=value pairs so a future SIEM pipeline can
pull them without re-parsing. ``user_id`` may be a username or an
``app_user_uuid`` depending on how the caller addressed the user — the
mixed key reflects the same dual-key reality the cache itself stores
post-schema-v2 (2026-05-16).
"""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path
from typing import Optional


# Default rotation. Matches db_access.log: 50 MB max, 5 archives kept.
_DEFAULT_ROTATE_MAX_BYTES = 50 * 1024 * 1024
_DEFAULT_ROTATE_BACKUP_COUNT = 5


def _resolve_rotate_settings() -> tuple:
    """Read ``log_rotate_max_size_mb`` + ``log_rotate_backup_count``
    from persisted settings, falling back to the defaults above on any
    read failure. Same tunables ``db_access.log`` reads — kept in sync
    so end users can size both audit trails with one knob set."""
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
    """Always land in ``server_data/`` regardless of any run-log dir.
    Cache refresh isn't a "run" event — it's end user-driven server
    maintenance, so the audit trail belongs at the project root rather
    than inside a per-run log bundle."""
    try:
        from server.persistence import get_data_dir
        return get_data_dir()
    except Exception:
        return Path(".")


def _get_logger() -> logging.Logger:
    """Return the dedicated playlist-cache logger. Idempotent — the
    RotatingFileHandler is attached only once per process lifetime."""
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
    """Render ``key=value`` pairs space-separated. Values containing
    spaces or quotes get double-quoted; None values are dropped."""
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
    """Emitted at the start of a bulk per-server refresh (the kind
    triggered by Servers ▸ Refresh and by the Playlist Mgmt column
    Refresh button). ``source`` is a free-form tag describing what
    triggered it (e.g. ``"servers-refresh"`` or ``"playlist-mgmt-panel"``)."""
    _get_logger().info(_format_kv(
        server=server_id, action="bulk-refresh-start",
        users=user_count, source=source,
    ))


def log_bulk_refresh_end(
    *, server_id: str, ok: int, errors: int, elapsed_s: float,
    source: Optional[str] = None,
) -> None:
    """Emitted at the end of a bulk refresh. ``ok`` + ``errors`` count
    per-user refresh outcomes; ``elapsed_s`` is wall-clock for the whole
    bulk operation."""
    _get_logger().info(_format_kv(
        server=server_id, action="bulk-refresh-end",
        ok=ok, errors=errors, elapsed_s=round(float(elapsed_s), 2),
        source=source,
    ))


def log_user_refresh(
    *, server_id: str, user_id: str, ok: bool, elapsed_ms: int,
    playlists: int = 0, items: int = 0, error: Optional[str] = None,
) -> None:
    """Per-user refresh result. Called by the orchestrator's
    ``refresh_user_cache`` after each user (whether part of a bulk
    refresh or a one-off single-user refresh)."""
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
    """Per-step trace for the combined per-user auth chain in
    :func:`services.playlist_copy._user_context_for`. One line per
    attempt so the end user can grep the audit log and see exactly which
    path produced the token used for a given user / server / role.

    Parameters
    ----------
    step : str
        Which chain step ran. Stable vocabulary: ``saved_token``,
        ``pin_token``, ``admin_fallback``.
    outcome : str
        ``hit`` = the step produced a usable token; ``miss`` = the step
        ran but produced nothing (caller will try the next step);
        ``used`` = the admin/owner fallback was actually applied for
        this user; ``refused`` = strict-mode raised on missing token.
    role : str
        ``"source"`` or ``"dest"`` — same meaning as the parent role arg.
    detail : str, optional
        Extra context (e.g. ``"no managed_users row"``,
        ``"signInHomeUser threw"``) for the warning paths.
    """
    level = logging.INFO if outcome in ("hit", "used") else logging.WARNING
    payload = _format_kv(
        server=server_id, user=user_id,
        action="auth-chain",
        step=step, outcome=outcome, role=role,
        detail=detail,
    )
    _get_logger().log(level, payload)
