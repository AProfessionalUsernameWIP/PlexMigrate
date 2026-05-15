"""
PR-13 follow-up - database access audit log.

A small helper that writes a dedicated ``db_access.log`` alongside
the existing per-run log files. Captures every read / write the
engine performs against the local databases (``media.db``,
``auth.db``, ``snapshots.db``) plus the operator-driven credential
fetches inside :func:`services.auth.get_home_users`.

Why a separate log?
-------------------
The run log is dominated by per-track / per-item engine activity.
Credential reads and other database hits are sparse and operationally
important - the operator needs to be able to answer "did we actually
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
* Otherwise (e.g. operator clicks Download in the Backups panel
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
from pathlib import Path
from typing import Any, Dict, Optional


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
    try:
        handler = logging.FileHandler(str(file_path), encoding="utf-8")
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


# ── Public API ──────────────────────────────────────────────────────────────

def log_read(
    *,
    table: str,
    field: str = "",
    where: Optional[Dict[str, Any]] = None,
    intent: str = "",
) -> None:
    """
    Record a database read. ``intent`` is a one-line operator-readable
    reason (e.g. ``"per-user PIN lookup for snapshot impersonation"``).
    """
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
    Free-form audit line for operator-meaningful events that don't
    fit a strict read/write shape (e.g. "PIN successfully authorised
    home-user sign-in for Crystal Jean").
    """
    _get_logger().info("[EVENT] " + message, *args)
