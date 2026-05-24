"""
One canonical SQLite connection helper for the ``server/*_db.py``
persistence modules.

Each module previously hand-rolled the same connect + ``row_factory`` +
``set_journal_mode`` + pragma + optional-chmod sequence. They now call
``open_db`` and pass the flags matching their own configuration. The
helper's defaults are the conservative set; every call site passes its
flags explicitly so the per-module configuration stays visible at the
call.

``media.db`` is intentionally NOT a caller - it carries its own
schema-migration machinery and a divergent connection path.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Iterable, Tuple, Union

from server._sqlite_journal import set_journal_mode


def open_db(
    path: Union[str, Path],
    *,
    label: str,
    check_same_thread: bool = True,
    foreign_keys: bool = False,
    synchronous_normal: bool = True,
    chmod_sidecars: bool = False,
) -> sqlite3.Connection:
    """
    Open a SQLite connection with the project's standard configuration.

    Always applied: ``timeout=30.0``, ``isolation_level=None``
    (autocommit; callers manage transactions explicitly),
    ``row_factory = sqlite3.Row``, and ``set_journal_mode`` (WAL with a
    DELETE fallback for bind mounts that can't host WAL's shared-memory
    file).

    Flag-gated, each call site passing its own value:
      * ``check_same_thread`` - forwarded to ``sqlite3.connect``. Pass
        ``False`` for a connection shared across threads.
      * ``chmod_sidecars`` - restrict the ``.db`` and its ``-wal`` /
        ``-shm`` sidecars to ``0o600`` (POSIX-only; best-effort). Done
        before ``set_journal_mode`` so the order matches the original
        per-module code (the sidecars may not exist yet on first boot).
      * ``synchronous_normal`` - apply ``PRAGMA synchronous=NORMAL``.
      * ``foreign_keys`` - apply ``PRAGMA foreign_keys=ON``.
    """
    conn = sqlite3.connect(
        str(path),
        timeout=30.0,
        isolation_level=None,
        check_same_thread=check_same_thread,
    )
    conn.row_factory = sqlite3.Row
    if chmod_sidecars:
        p = Path(path)
        for _p in (p, p.with_name(p.name + "-wal"), p.with_name(p.name + "-shm")):
            try:
                if _p.exists():
                    os.chmod(_p, 0o600)
            except OSError:
                pass
    set_journal_mode(conn, db_label=label)
    if synchronous_normal:
        conn.execute("PRAGMA synchronous=NORMAL")
    if foreign_keys:
        conn.execute("PRAGMA foreign_keys=ON")
    return conn


def apply_additive_columns(
    conn: sqlite3.Connection,
    columns: Iterable[Tuple[str, str]],
) -> None:
    """Apply additive ``ALTER TABLE ... ADD COLUMN`` migrations idempotently.

    ``columns`` is an iterable of ``(table_name, column_definition)``
    pairs, e.g. ``("playlist_cache", "auth_kind TEXT")``. A
    ``PRAGMA table_info`` lookup is the single idempotency guard: the
    ALTER runs only when the table exists and the column is genuinely
    absent, so a ``duplicate column name`` error can never fire and
    needs no catch. A table that does not exist yet is skipped - a
    fresh ``CREATE TABLE`` from the schema script includes every
    current column, so there is nothing to migrate. Any ALTER that
    still fails is a real schema fault and is allowed to propagate.

    Safe to call before or after the schema script: callers whose
    schema references the new column in an index (so the ALTER must
    precede ``executescript``) rely on the absent-table skip above.
    """
    for table, column_def in columns:
        info = conn.execute(f"PRAGMA table_info({table})").fetchall()
        if not info:
            continue  # table absent; the schema script creates it complete
        existing = {row[1] for row in info}
        if column_def.split()[0] in existing:
            continue
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column_def}")
