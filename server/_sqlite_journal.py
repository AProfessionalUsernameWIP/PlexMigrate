"""Shared SQLite journal-mode initialization helper.

Recovers from a crash-loop where SQLite's
``PRAGMA journal_mode=WAL`` raised ``OperationalError: disk I/O error``
on every database open. Root cause: Docker Desktop on Windows
sometimes can't establish WAL's required shared-memory mapping
(-shm file mmap + fcntl byte-range locks) over a bind-mounted host
path. The host filesystem itself is fine — host-side SQLite opens
the same DBs in WAL mode without issue — but the in-container view
loses the mmap support after some Docker / WSL2 state transitions.

This helper tries WAL first (the desired mode for concurrency) and
falls back to DELETE journal mode on failure so the engine boots
even when the bind-mount can't support WAL. DELETE mode is
single-writer + single-reader (rollback journal instead of WAL),
which on this app means slightly more lock contention during writes
but full correctness. Operators on Linux hosts / docker-volume
backends will get WAL as usual.
"""

from __future__ import annotations

import logging
import sqlite3

log = logging.getLogger("plexmigrate.server._sqlite_journal")


def set_journal_mode(
    conn: sqlite3.Connection,
    *,
    db_label: str = "",
) -> str:
    """Set the connection's journal mode, preferring WAL but
    falling back to DELETE on disk I/O errors that indicate the
    underlying filesystem can't support WAL's shared-memory file.

    Returns the journal_mode string actually set ("wal" / "delete" /
    "memory" / etc.) so callers can log the outcome.

    ``db_label`` is used only for log messages; pass the friendly
    db filename so the operator can grep for which DBs fell back.
    """
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        # Confirm — some FSes silently swallow the change.
        row = conn.execute("PRAGMA journal_mode").fetchone()
        mode = (row[0] if row else "").lower()
        if mode == "wal":
            return mode
        log.warning(
            "sqlite[%s]: requested WAL but got %r; falling back to DELETE.",
            db_label or "?", mode,
        )
    except sqlite3.OperationalError as exc:
        # The canonical signature for "filesystem can't support WAL"
        # on Docker Desktop / WSL2 bind mounts: "disk I/O error" raised
        # by the SHM file's mmap call. Don't propagate — fall through
        # to DELETE so the engine boots. Any OTHER OperationalError
        # would also be a recoverable case (locked, busy, etc.) — the
        # right answer is still "try DELETE", which uses a rollback
        # journal instead of an mmap'd SHM.
        log.warning(
            "sqlite[%s]: WAL init failed (%s); falling back to DELETE "
            "journal mode. This usually means the bind-mounted host "
            "filesystem can't support WAL's shared-memory file "
            "(common on Docker Desktop / Windows). Engine still works "
            "correctly under DELETE; concurrency is slightly reduced.",
            db_label or "?", exc,
        )
    # Fallback path. DELETE is universally supported.
    try:
        conn.execute("PRAGMA journal_mode=DELETE")
        row = conn.execute("PRAGMA journal_mode").fetchone()
        mode = (row[0] if row else "").lower()
        return mode or "delete"
    except sqlite3.OperationalError as exc:
        log.error(
            "sqlite[%s]: DELETE journal mode also failed: %s. The "
            "filesystem is genuinely broken; further operations will "
            "fail. Check disk space + permissions on the bind mount.",
            db_label or "?", exc,
        )
        raise
