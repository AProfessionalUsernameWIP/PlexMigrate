"""
PR-13 follow-up - database access audit log.

A small helper that writes a dedicated ``db_access.log`` alongside
the existing per-run log files. Captures every read / write the
engine performs against the local databases (``media.db``,
``auth.db``, ``snapshots.db``) plus the end user-driven credential
fetches inside :func:`services.auth.get_home_users`.

Why a separate log?
-------------------
The run log is dominated by per-track / per-item engine activity.
Credential reads and other database hits are sparse and operationally
important - the end user needs to be able to answer "did we actually
use the stored PIN for Crystal Jean?" without grepping through tens
of thousands of track lines.

A separate ``db_access.log`` makes the audit trail trivially
greppable and easy to ship to a SIEM later if anyone needs that.

Destination
-----------
* If the engine has published an active run log directory via
  :data:`services.state._run_log_dir`, the file is written into that
  directory (``<run_log_dir>/db_access.log``). This keeps the audit
  trail co-located with the run's other artefacts so the Logs panel
  surfaces it next to ``run_*.log``.
* Otherwise (e.g. end user clicks Download in the Backups panel
  outside of a run) it falls back to ``server_data/db_access.log``
  so the trail never goes silently nowhere.

Concurrency
-----------
Python's :mod:`logging` is thread-safe; the singleton logger we
return below uses a single FileHandler whose write lock is internal
to the logging module. Multiple snapshot job threads hammering the
log at the same time will serialise on the handler.
"""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path
from typing import Any, Dict, Optional


# Default rotation limits applied when the end user hasn't configured
# the tunables (or the settings.json read fails). Sized so a chatty
# DB-access trail can still reach a useful history without ballooning
# disk usage: 50 MB max per file, 5 archives kept (~250 MB ceiling).
_DEFAULT_ROTATE_MAX_BYTES = 50 * 1024 * 1024
_DEFAULT_ROTATE_BACKUP_COUNT = 5


def _resolve_rotate_settings() -> tuple:
    """
    Read ``settings.log_rotate_max_size_mb`` (converted to bytes) and
    ``settings.log_rotate_backup_count`` from the persisted settings,
    falling back to the defaults above on any read failure. Tunables
    are clamped at sane bounds (size >= 1 MB, count >= 0) so a typo
    in settings.json can't disable rotation by writing 0 / negative.
    """
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


_LOGGER_NAME = "plexmigrate.db_access"
_FILE_NAME = "db_access.log"


def _resolve_target_dir() -> Path:
    """
    Choose the directory the log file should land in. Prefers the
    active run log directory (so each snapshot's audit trail ships
    inside its run-log bundle), falls back to ``server_data/``.
    """
    try:
        import services.state as _state
        rd = getattr(_state, "_run_log_dir", "") or ""
        if rd:
            p = Path(rd)
            if p.is_dir():
                return p
    except Exception:
        pass
    try:
        from server.persistence import get_data_dir
        return get_data_dir()
    except Exception:
        return Path(".")


# Track the directory the current FileHandler is attached to so a new
# run with a different ``_run_log_dir`` swaps the handler over rather
# than appending to the previous run's log forever.
_active_target: Optional[Path] = None


def _get_logger() -> logging.Logger:
    """
    Return the dedicated DB-access logger. Idempotent: handler is
    attached only once per target directory, but re-targets when the
    active run log dir changes (a new snapshot job kicks off).
    """
    global _active_target
    log = logging.getLogger(_LOGGER_NAME)
    target = _resolve_target_dir()
    if _active_target == target:
        return log

    # Drop any previous handler attached to a different directory.
    for h in list(log.handlers):
        log.removeHandler(h)
        try:
            h.close()
        except Exception:
            pass

    log.setLevel(logging.INFO)
    log.propagate = False  # don't double-print into the main run log
    file_path = target / _FILE_NAME
    max_bytes, backup_count = _resolve_rotate_settings()
    try:
        # RotatingFileHandler rolls the active file to
        # ``db_access.log.1`` (then .2, .3 …) when it crosses
        # ``maxBytes``. Older archives beyond ``backupCount`` are
        # deleted by the handler. This bounds disk usage without
        # the end user having to babysit the file. Tunables read at
        # handler-attach time; a settings change requires a server
        # restart or a new run (the per-run target re-attaches a
        # fresh handler) to take effect.
        handler: logging.Handler = logging.handlers.RotatingFileHandler(
            str(file_path),
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
    except OSError as exc:
        # Defensive: failure to open the file shouldn't fail the
        # caller. The logger stays without a handler and effectively
        # no-ops; callers don't care about delivery. But a destructive
        # op then "succeeds" with no audit record - so warn loudly on
        # the main run log that the audit trail is NOT being written.
        logging.getLogger("plexmigrate").warning(
            "db_access_log: could not open audit log at %s (%s) - "
            "DB-access events for this run will NOT be recorded.",
            file_path, exc,
        )
        _active_target = target
        return log
    handler.setFormatter(logging.Formatter(
        "[%(asctime)s] [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    log.addHandler(handler)
    _active_target = target
    return log


def _fmt_where(where: Dict[str, Any]) -> str:
    """Render a small kv-pair dict as ``key=val key=val`` for the log line."""
    if not where:
        return ""
    parts = []
    for k, v in where.items():
        s = "" if v is None else str(v)
        # Truncate ridiculously long values (e.g. a full JSON blob
        # passed in by accident) so one bad call doesn't poison the
        # whole log file.
        if len(s) > 200:
            s = s[:197] + "..."
        parts.append(f"{k}={s}")
    return " ".join(parts)


# ── Enable/disable flag ──────────────────────────────────────────────────────
# End user can disable the audit trail via the db_admin-gated endpoint
# /api/settings/audit-log-toggle. When disabled all log_* calls no-op.
# The flag is cached at module level (no settings.json read per call);
# the toggle endpoint refreshes the cache and lifespan startup
# initialises it from settings.
#
# Self-documenting transitions: the toggle endpoint should call
# ``log_audit_disabled(user)`` BEFORE flipping to False so the final
# entry records who switched it off and when, and call
# ``log_audit_enabled(user)`` AFTER flipping back to True so the first
# entry on re-enable records who switched it back on. The audit trail
# always shows that disablement was an explicit, attributable act.
_enabled: bool = True


def set_enabled(value: bool) -> None:
    """Refresh the in-process enabled flag. Called by the toggle endpoint."""
    global _enabled
    _enabled = bool(value)


def is_enabled() -> bool:
    return _enabled


def log_audit_disabled(user: str) -> None:
    """
    Write the *final* audit entry before switching the flag off. Call
    this BEFORE :func:`set_enabled(False)` so the line is recorded.
    """
    try:
        _get_logger().warning(
            "[EVENT] audit logging DISABLED by user=%r - subsequent "
            "DB-access events will NOT be recorded until re-enabled",
            user,
        )
        # Flush so the line lands even if the process dies before the
        # next write.
        for h in logging.getLogger(_LOGGER_NAME).handlers:
            try:
                h.flush()
            except Exception:
                pass
    except Exception:
        pass


def log_audit_enabled(user: str) -> None:
    """
    Write the *first* audit entry after switching the flag back on.
    Call this AFTER :func:`set_enabled(True)`.
    """
    try:
        _get_logger().warning(
            "[EVENT] audit logging RE-ENABLED by user=%r - recording "
            "DB-access events resumes from this line",
            user,
        )
    except Exception:
        pass


# ── Public API ──────────────────────────────────────────────────────────────

def log_read(
    *,
    table: str,
    field: str = "",
    where: Optional[Dict[str, Any]] = None,
    intent: str = "",
) -> None:
    """
    Record a database read. ``intent`` is a one-line end user-readable
    reason (e.g. ``"per-user PIN lookup for snapshot impersonation"``).
    """
    if not _enabled:
        return
    _get_logger().info(
        "[READ] table=%s%s %s%s",
        table,
        f" field={field}" if field else "",
        _fmt_where(where or {}),
        f" intent={intent}" if intent else "",
    )


def log_write(
    *,
    table: str,
    field: str = "",
    where: Optional[Dict[str, Any]] = None,
    intent: str = "",
    affected_rows: Optional[int] = None,
) -> None:
    """
    Record a database write. ``affected_rows`` is logged when known so
    bulk operations show their footprint at a glance.
    """
    if not _enabled:
        return
    rows_part = "" if affected_rows is None else f" rows={affected_rows}"
    _get_logger().info(
        "[WRITE] table=%s%s %s%s%s",
        table,
        f" field={field}" if field else "",
        _fmt_where(where or {}),
        rows_part,
        f" intent={intent}" if intent else "",
    )


def log_event(message: str, *args: Any) -> None:
    """
    Free-form audit line for end user-meaningful events that don't
    fit a strict read/write shape (e.g. "PIN successfully authorised
    home-user sign-in for Crystal Jean").
    """
    if not _enabled:
        return
    _get_logger().info("[EVENT] " + message, *args)
