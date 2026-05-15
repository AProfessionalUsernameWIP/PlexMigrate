"""
Application-user database for the opt-in web-UI authentication layer (v0.11.0).

Roadmap reference: Part 5 / Feature 2 of ``roadmapplan4.md``.

This database is deliberately kept separate from the (future) ``media.db``
that Feature 3 introduces. The two have entirely different lifecycles:

* ``auth.db`` - operator login accounts for the web UI. Sensitive
  (holds bcrypt hashes), tiny (a handful of rows), backed up
  conservatively.
* ``media.db`` - the media-state cache that replaces the JSON files.
  Much larger, regenerable from a re-snapshot, low-sensitivity.

Keeping them in separate files lets the operator back up the small
auth file frequently and the large media file rarely; restoring one
does not perturb the other.

Activation
----------
This module is **only loaded** when the auth layer is enabled (the
``PLEXMIGRATE_AUTH_ENABLED`` env var is truthy). The default is
disabled - pre-v0.11.0 installs and CLI runs never touch this code.
"""

from __future__ import annotations

import logging
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from server.persistence import get_data_dir


log = logging.getLogger("plexmigrate.server.auth_db")


# ── File location ────────────────────────────────────────────────────────────

_DB_NAME = "auth.db"


def _db_path() -> Path:
    """Absolute path to ``auth.db`` inside the configured data dir."""
    return get_data_dir() / _DB_NAME


# ── Connection management ────────────────────────────────────────────────────
#
# SQLite handles aren't thread-safe by default. We open a fresh
# connection per call and close it at function return - the database
# is tiny (a handful of rows) and concurrency is low (login + setup +
# user-create are all interactive), so the per-call open is well
# within budget. WAL mode lets concurrent readers and one writer
# coexist without blocking, matching media.db's planned configuration.

_init_lock = threading.Lock()
_initialised = False


def _connect() -> sqlite3.Connection:
    """
    Open a connection with WAL mode and a 30-second busy timeout.
    Caller is responsible for closing (use a context manager).
    """
    conn = sqlite3.connect(str(_db_path()), timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    # WAL + foreign_keys are session-level pragmas; setting them on
    # every connection is the documented safe pattern. WAL once set on
    # the file persists across reopens, so the journal_mode call is a
    # no-op after the first run.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_auth_db() -> None:
    """
    Create the ``app_users`` table on first boot. Idempotent - every
    subsequent call is a no-op so the FastAPI startup hook can call
    this unconditionally without checking for prior runs.

    The bcrypt hashes are stored as TEXT (passlib's
    ``$2b$<rounds>$<salt><hash>`` format is ASCII-safe). PR-A1 widens
    the ``role`` CHECK constraint to admit the full five-value space
    used by the multi-user auth system:

    * ``viewer``     - read-only role. Dashboard + Servers (RO) +
                       own Account Settings.
    * ``operator``   - read + start jobs. Cannot stop jobs or edit
                       schedules. Can see Logs + Backups.
    * ``manager``    - operator + stop jobs + edit schedules + view
                       Sync (when Feature 5 ships).
    * ``root_admin`` - full access. Only role that can manage other
                       user accounts.
    * ``db_admin``   - NON-LOGIN special-purpose credential row that
                       gates destructive User Management writes
                       (PR-10). Managed by root_admin via Settings
                       → Accounts → Database Admin Account.

    Migration path for legacy installs:
      * pre-PR-9   schema admitted ``admin`` + ``operator`` only.
      * PR-9.1     widened to ``admin`` + ``db_admin`` + ``operator``.
      * PR-A1      widens further to include ``viewer`` + ``manager``
                   + ``root_admin``, ADDS ``display_name`` and
                   ``last_login`` columns, AND remaps every existing
                   ``role='admin'`` row to ``role='root_admin'`` in
                   the same transaction.

    ``_migrate_schema`` is idempotent - it detects the active
    schema shape via ``sqlite_master`` and only rebuilds when an
    upgrade is required.
    """
    global _initialised
    with _init_lock:
        if _initialised:
            return
        _db_path().parent.mkdir(parents=True, exist_ok=True)
        conn = _connect()
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS app_users (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    username      TEXT UNIQUE NOT NULL,
                    password_hash TEXT NOT NULL,
                    role          TEXT NOT NULL DEFAULT 'operator'
                                  CHECK (role IN (
                                      'viewer', 'operator', 'manager',
                                      'admin', 'root_admin', 'db_admin'
                                  )),
                    display_name  TEXT,
                    last_login    REAL,
                    created_at    REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_app_users_username
                    ON app_users(username);
                CREATE TABLE IF NOT EXISTS refresh_tokens (
                    id          TEXT PRIMARY KEY,
                    username    TEXT NOT NULL,
                    issued_at   REAL NOT NULL,
                    expires_at  REAL NOT NULL,
                    revoked     INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_refresh_tokens_username
                    ON refresh_tokens(username);
                CREATE INDEX IF NOT EXISTS idx_refresh_tokens_expires_at
                    ON refresh_tokens(expires_at);
            """)
            _migrate_schema(conn)
        finally:
            conn.close()
        _initialised = True
        log.info("auth.db initialised at %s", _db_path())


def _migrate_schema(conn: sqlite3.Connection) -> None:
    """
    Bring an existing ``app_users`` table forward to the PR-A1 shape:

      * CHECK constraint covers all five role values
      * ``display_name`` and ``last_login`` columns present
      * Every legacy ``role='admin'`` row remapped to ``role='root_admin'``

    Detection: inspect the stored CREATE TABLE statement in
    ``sqlite_master``. If it doesn't mention ``root_admin``, we know
    the schema is on an older shape and we rebuild. The rebuild runs
    in a single transaction so a crash mid-migration leaves the
    original table intact.

    The CREATE TABLE IF NOT EXISTS above sets the new schema on
    fresh installs; this function handles every other case.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='app_users'"
    ).fetchone()
    if row is None:
        return  # fresh install just got the new schema above
    create_sql = (row["sql"] or "").lower()
    # The CHECK constraint must list every accepted role. The post-
    # PR-A1.1 shape has six values; we detect by looking for the last
    # one added (``'admin'`` as a discrete word - root_admin contains
    # the same substring so ``"'admin'"`` is the unambiguous probe).
    if "'admin'" in create_sql and "root_admin" in create_sql:
        return  # already on the current schema

    log.info(
        "Migrating auth.db schema: widening role CHECK "
        "(viewer/operator/manager/admin/root_admin/db_admin), adding "
        "display_name + last_login columns, remapping legacy admin → root_admin."
    )
    # Detect which columns the existing table has so the INSERT
    # SELECT only references columns that exist. Pre-PR-A1 tables
    # don't have ``display_name`` or ``last_login``; we leave those
    # NULL in the new table.
    legacy_cols = {r["name"] for r in conn.execute(
        "PRAGMA table_info(app_users)"
    ).fetchall()}
    has_display = "display_name" in legacy_cols
    has_last_login = "last_login" in legacy_cols
    display_expr = "display_name" if has_display else "NULL"
    last_login_expr = "last_login" if has_last_login else "NULL"

    # We need a CASE expression that:
    #   * remaps the LEGACY ``admin`` (pre-PR-A1) → ``root_admin``,
    #     BUT only when the new ``admin`` role doesn't already exist
    #     in the schema (i.e. the legacy CHECK constraint had at most
    #     ``admin`` / ``operator`` / ``db_admin``).
    #   * leaves the new ``admin`` role alone on already-PR-A1
    #     installs being upgraded to add the sudo-root admin value.
    #
    # The probe: if the old schema already has ``root_admin`` in its
    # CHECK constraint then the existing ``admin`` rows ARE the new
    # sudo-root admin and must be preserved. Otherwise (pre-PR-A1)
    # there's no ``root_admin`` so any ``admin`` row is the legacy
    # login admin and must be promoted.
    legacy_install = "root_admin" not in create_sql
    role_expr = (
        "CASE WHEN role = 'admin' THEN 'root_admin' ELSE role END"
        if legacy_install else "role"
    )
    conn.executescript(f"""
        BEGIN TRANSACTION;
        CREATE TABLE app_users_new (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            username      TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role          TEXT NOT NULL DEFAULT 'operator'
                          CHECK (role IN (
                              'viewer', 'operator', 'manager',
                              'admin', 'root_admin', 'db_admin'
                          )),
            display_name  TEXT,
            last_login    REAL,
            created_at    REAL NOT NULL
        );
        INSERT INTO app_users_new
            (id, username, password_hash, role, display_name, last_login, created_at)
        SELECT
            id,
            username,
            password_hash,
            {role_expr},
            {display_expr},
            {last_login_expr},
            created_at
        FROM app_users;
        DROP TABLE app_users;
        ALTER TABLE app_users_new RENAME TO app_users;
        CREATE INDEX IF NOT EXISTS idx_app_users_username ON app_users(username);
        COMMIT;
    """)


# ── Public API ───────────────────────────────────────────────────────────────

def has_any_users() -> bool:
    """
    Return True if at least one row exists in ``app_users``.

    Used by ``/api/auth/status`` to compute ``setup_needed`` -
    ``setup_needed = True`` when the auth layer is enabled but no
    admin has been created yet.
    """
    init_auth_db()
    conn = _connect()
    try:
        row = conn.execute("SELECT COUNT(*) AS n FROM app_users").fetchone()
        return bool(row["n"] > 0)
    finally:
        conn.close()


# Maximum password length we accept. bcrypt itself truncates at 72
# bytes; rather than silently let two passwords with the same 72-byte
# prefix collide, we reject anything longer up front. 64 chars is more
# than enough for a strong human-typed passphrase.
_MAX_PASSWORD_LEN = 64

# A throwaway hash used as a "constant-time" target on the
# user-not-found path. Generated once at module load; the bcrypt cost
# matches a real verify so a timing oracle can't distinguish "no such
# user" from "wrong password" by response time alone.
_DUMMY_HASH: Optional[bytes] = None


def _dummy_hash() -> bytes:
    """Lazily generate the constant-time dummy hash."""
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        import bcrypt as _bcrypt
        _DUMMY_HASH = _bcrypt.hashpw(b"dummy", _bcrypt.gensalt())
    return _DUMMY_HASH


# Valid role values for ``create_user`` and ``update_role``. The first
# four are the login-capable roles in the multi-user auth hierarchy;
# ``db_admin`` is a non-login special-purpose credential used to gate
# destructive User Management writes (PR-10) and is created from a
# different surface (Settings → Accounts → Database Admin Account).
_LOGIN_ROLES = ("viewer", "operator", "manager", "admin", "root_admin")
_VALID_ROLES = _LOGIN_ROLES + ("db_admin",)


def create_user(
    username: str,
    password: str,
    role: str = "operator",
    display_name: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Insert a new user row. The password is bcrypt-hashed in this
    function so callers never pass a hash directly; ``verify_password``
    is the only consumer that knows the hash format.

    Raises ``ValueError`` if:
      * ``username`` is empty after stripping
      * ``password`` is shorter than 8 characters or longer than
        :data:`_MAX_PASSWORD_LEN` (bcrypt truncates at 72 bytes -
        rejecting up front prevents silent prefix collisions)
      * ``role`` is not one of ``viewer | operator | manager |
        root_admin | db_admin``
      * a user with that username already exists

    ``display_name`` is optional. ``None`` or an empty string leaves
    it unset (UI falls back to showing the raw username). Stored as
    TEXT; not validated beyond stripping whitespace.

    Returns the new user as a dict (without the hash).
    """
    import bcrypt as _bcrypt

    init_auth_db()
    uname = (username or "").strip()
    if not uname:
        raise ValueError("Username must not be empty.")
    if not isinstance(password, str) or len(password) < 8:
        raise ValueError("Password must be at least 8 characters.")
    if len(password.encode("utf-8")) > _MAX_PASSWORD_LEN:
        raise ValueError(
            f"Password is too long (max {_MAX_PASSWORD_LEN} bytes). "
            "bcrypt only considers the first 72 bytes, so longer "
            "passwords can silently collide on the leading prefix."
        )
    if role not in _VALID_ROLES:
        raise ValueError(
            f"Unknown role {role!r}. Valid roles: {', '.join(_VALID_ROLES)}."
        )

    dn = (display_name or "").strip() or None
    hashed = _bcrypt.hashpw(password.encode("utf-8"), _bcrypt.gensalt()).decode("ascii")
    now = time.time()
    conn = _connect()
    try:
        try:
            cur = conn.execute(
                "INSERT INTO app_users "
                "(username, password_hash, role, display_name, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (uname, hashed, role, dn, now),
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"A user named {uname!r} already exists.") from exc
        return {
            "id": cur.lastrowid,
            "username": uname,
            "role": role,
            "display_name": dn,
            "last_login": None,
            "created_at": now,
        }
    finally:
        conn.close()


def verify_password(username: str, password: str) -> Optional[Dict[str, Any]]:
    """
    Check ``(username, password)`` against the stored hash.

    Returns the user row (without the hash) on success, ``None`` on
    failure - failure cases all collapse to the same return so a
    timing oracle can't distinguish "no such user" from "wrong
    password" from the API surface. The user-not-found path runs a
    dummy bcrypt verify to keep response time uniform.
    """
    import bcrypt as _bcrypt

    init_auth_db()
    uname = (username or "").strip()
    if not uname or not isinstance(password, str):
        # Constant-time placebo so the timing of this rejection
        # matches the "user exists but wrong password" branch.
        _bcrypt.checkpw(b"dummy", _dummy_hash())
        return None

    # bcrypt's password input is limited to 72 bytes; longer inputs
    # would otherwise be silently truncated by the verify routine.
    pw_bytes = password.encode("utf-8")[:_MAX_PASSWORD_LEN]

    conn = _connect()
    try:
        row = conn.execute(
            "SELECT id, username, password_hash, role, display_name, "
            "last_login, created_at "
            "FROM app_users WHERE username = ?",
            (uname,),
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        # Constant-time placebo (see above).
        _bcrypt.checkpw(b"dummy", _dummy_hash())
        return None

    try:
        ok = _bcrypt.checkpw(pw_bytes, row["password_hash"].encode("ascii"))
    except (ValueError, TypeError):
        # Malformed hash on disk - treat as auth failure rather than
        # surfacing an exception that would betray internal state.
        return None
    if not ok:
        return None
    return _row_to_user(row)


def _row_to_user(row: sqlite3.Row) -> Dict[str, Any]:
    """Public-shape user dict (no hash). Used by verify_password,
    list_users, get_user, and the role-specific lookups so the
    serialised shape stays consistent everywhere."""
    return {
        "id": row["id"],
        "username": row["username"],
        "role": row["role"],
        "display_name": row["display_name"] if "display_name" in row.keys() else None,
        "last_login": row["last_login"] if "last_login" in row.keys() else None,
        "created_at": row["created_at"],
    }


def get_user(username: str) -> Optional[Dict[str, Any]]:
    """
    Single-row read by username. Returns the user dict (without the
    hash) or ``None`` if not found.

    Used by:
    * The PR-A2 ``require_role()`` dependency - re-reads the user's
      current role on every request so role changes via
      ``PATCH /api/auth/users/{u}`` take effect immediately without
      requiring the affected user to re-login.
    * ``GET /api/auth/me`` - returns the caller's full identity to the
      frontend.
    * The User Accounts explorer (PR-A5) - drilled-in detail view.
    """
    init_auth_db()
    uname = (username or "").strip()
    if not uname:
        return None
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT id, username, role, display_name, last_login, created_at "
            "FROM app_users WHERE username = ?",
            (uname,),
        ).fetchone()
    finally:
        conn.close()
    return _row_to_user(row) if row is not None else None


def list_users() -> List[Dict[str, Any]]:
    """
    Return every user row (without password hashes), ordered by
    creation time. Includes ``display_name`` and ``last_login``
    columns added in PR-A1 so the User Accounts explorer can render
    them directly. db_admin rows are EXCLUDED - they're a non-login
    credential managed via a different surface (Settings → Accounts
    → Database Admin Account) and should never appear in the login-
    user management table per the PR-9 Sub-PR-A spec.
    """
    init_auth_db()
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT id, username, role, display_name, last_login, created_at "
            "FROM app_users WHERE role != 'db_admin' "
            "ORDER BY created_at ASC"
        ).fetchall()
    finally:
        conn.close()
    return [_row_to_user(r) for r in rows]


# ── PR-A1: mutation helpers for the multi-user system ──────────────────────

def _count_role(conn: sqlite3.Connection, role: str) -> int:
    """Count ``app_users`` rows currently holding ``role``."""
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM app_users WHERE role = ?", (role,)
    ).fetchone()
    return int(row["n"]) if row else 0


def update_role(username: str, new_role: str) -> None:
    """
    Change a user's role. Used by ``PATCH /api/auth/users/{u}`` in
    PR-A2 and the User Accounts explorer in PR-A5.

    Raises ``ValueError`` on:
      * empty username
      * unknown role
      * no matching row
      * attempting to change a row's role to ``db_admin`` from here
        (db_admin is created exclusively via the Database Admin
        Account flow; this guard prevents accidental cross-pollination)
      * demoting the last ``root_admin`` (M17 last-admin floor)
    """
    init_auth_db()
    uname = (username or "").strip()
    if not uname:
        raise ValueError("Username must not be empty.")
    if new_role not in _VALID_ROLES:
        raise ValueError(
            f"Unknown role {new_role!r}. Valid roles: {', '.join(_VALID_ROLES)}."
        )
    if new_role == "db_admin":
        raise ValueError(
            "db_admin is created only via the Database Admin Account flow; "
            "it cannot be assigned to a user through update_role."
        )
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT role FROM app_users WHERE username = ?", (uname,)
        ).fetchone()
        if row is None:
            raise ValueError(f"No user named {uname!r}.")
        # M17 last-admin floor: refuse to demote the final root_admin.
        # The PATCH /users route enforces a stricter policy (it won't
        # change ANY root_admin's role), but pushing the lockout-safety
        # invariant down here means every caller - ad-hoc cleanup,
        # future endpoints - is covered, not just that one route.
        if (
            row["role"] == "root_admin"
            and new_role != "root_admin"
            and _count_role(conn, "root_admin") <= 1
        ):
            raise ValueError(
                "Cannot demote the last root_admin - at least one "
                "root_admin must always exist."
            )
        cur = conn.execute(
            "UPDATE app_users SET role = ? WHERE username = ?",
            (new_role, uname),
        )
        if cur.rowcount == 0:
            raise ValueError(f"No user named {uname!r}.")
    finally:
        conn.close()


def update_display_name(username: str, display_name: Optional[str]) -> None:
    """
    Set or clear a user's display name. ``None`` or empty string
    clears the field (the UI will fall back to showing the raw
    username). Used by the Account Settings panel (PR-A5) and the
    User Accounts explorer.
    """
    init_auth_db()
    uname = (username or "").strip()
    if not uname:
        raise ValueError("Username must not be empty.")
    dn = (display_name or "").strip() or None
    conn = _connect()
    try:
        cur = conn.execute(
            "UPDATE app_users SET display_name = ? WHERE username = ?",
            (dn, uname),
        )
        if cur.rowcount == 0:
            raise ValueError(f"No user named {uname!r}.")
    finally:
        conn.close()


def update_last_login(username: str, ts: Optional[float] = None) -> None:
    """
    Stamp the user's ``last_login`` column. Called from
    ``POST /api/auth/login`` on every successful authentication.
    ``ts`` defaults to ``time.time()``; passing an explicit value is
    only useful for tests. Best-effort - a failure here must not
    block the login response, so callers should swallow exceptions
    if the write fails (the login itself already succeeded).
    """
    init_auth_db()
    uname = (username or "").strip()
    if not uname:
        return
    if ts is None:
        ts = time.time()
    conn = _connect()
    try:
        conn.execute(
            "UPDATE app_users SET last_login = ? WHERE username = ?",
            (ts, uname),
        )
    finally:
        conn.close()


def delete_user(username: str) -> None:
    """
    Remove a user row. Used by ``DELETE /api/auth/users/{u}``.
    Raises ``ValueError`` if no row matches.

    The DELETE /users route refuses to delete ANY root_admin row;
    this function enforces the narrower lockout-safety invariant (M17
    last-admin floor): the *last* root_admin can never be deleted, no
    matter which caller asks. Any non-last root_admin is still
    deletable here for ad-hoc cleanup.
    """
    init_auth_db()
    uname = (username or "").strip()
    if not uname:
        raise ValueError("Username must not be empty.")
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT role FROM app_users WHERE username = ?", (uname,)
        ).fetchone()
        if row is None:
            raise ValueError(f"No user named {uname!r}.")
        if row["role"] == "root_admin" and _count_role(conn, "root_admin") <= 1:
            raise ValueError(
                "Cannot delete the last root_admin - at least one "
                "root_admin must always exist."
            )
        cur = conn.execute(
            "DELETE FROM app_users WHERE username = ?",
            (uname,),
        )
        if cur.rowcount == 0:
            raise ValueError(f"No user named {uname!r}.")
    finally:
        conn.close()


# ── PR-A1 - Role-specific lookups ───────────────────────────────────────────
#
# Two distinct admin rows live in ``app_users``, differentiated by
# their ``role`` column:
#
#   * ``role='root_admin'`` - the application-login admin. Created via
#     ``/api/auth/setup`` on first boot. Authoritative login credential
#     for the web UI. Renamed from ``admin`` in PR-A1.
#   * ``role='db_admin'``   - the Database Admin Account (PR-9). A
#     completely separate credential set whose only purpose is to
#     gate destructive User Management writes (PR-10).
#
# Multiple rows are technically allowed for each role (the schema
# permits it) but the Settings UI surfaces only the earliest-created
# row per role as the canonical one.

def _get_first_by_role(role: str) -> Optional[Dict[str, Any]]:
    """Return the earliest-created row matching ``role``, or None."""
    init_auth_db()
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT id, username, role, display_name, last_login, created_at "
            "FROM app_users WHERE role = ? ORDER BY created_at ASC LIMIT 1",
            (role,),
        ).fetchone()
    finally:
        conn.close()
    return _row_to_user(row) if row is not None else None


def get_db_admin() -> Optional[Dict[str, Any]]:
    """
    Return the canonical Database Admin row (``role='db_admin'``) or
    ``None``. Used by the Accounts sub-tab in Settings (PR-9.1) and
    by every User Management write gate in PR-10.
    """
    return _get_first_by_role("db_admin")


def get_root_admin() -> Optional[Dict[str, Any]]:
    """
    Return the canonical root-admin row (``role='root_admin'``) or
    ``None``. Replaces ``get_login_admin`` from PR-9.1 - the role
    formerly known as ``admin`` is now ``root_admin``.
    """
    return _get_first_by_role("root_admin")


# Back-compat alias for any in-tree caller that still references the
# pre-PR-A1 name. New code should call ``get_root_admin()`` directly.
def get_login_admin() -> Optional[Dict[str, Any]]:
    return get_root_admin()


def update_password(username: str, new_password: str) -> None:
    """
    Replace ``username``'s password with a fresh bcrypt hash. Same
    length validation as :func:`create_user`. Raises ``ValueError`` if
    the password is too short / too long or if no row matches.
    """
    import bcrypt as _bcrypt

    init_auth_db()
    uname = (username or "").strip()
    if not uname:
        raise ValueError("Username must not be empty.")
    if not isinstance(new_password, str) or len(new_password) < 8:
        raise ValueError("Password must be at least 8 characters.")
    if len(new_password.encode("utf-8")) > _MAX_PASSWORD_LEN:
        raise ValueError(
            f"Password is too long (max {_MAX_PASSWORD_LEN} bytes)."
        )
    hashed = _bcrypt.hashpw(
        new_password.encode("utf-8"), _bcrypt.gensalt()
    ).decode("ascii")
    conn = _connect()
    try:
        cur = conn.execute(
            "UPDATE app_users SET password_hash = ? WHERE username = ?",
            (hashed, uname),
        )
        if cur.rowcount == 0:
            raise ValueError(f"No user named {uname!r}.")
    finally:
        conn.close()


def update_username(old_username: str, new_username: str) -> None:
    """
    Rename a user. The new username must be unique. Raises
    ``ValueError`` on empty input, no match, or a collision with
    another row's username.
    """
    init_auth_db()
    old = (old_username or "").strip()
    new = (new_username or "").strip()
    if not old or not new:
        raise ValueError("Both usernames must be non-empty.")
    if old == new:
        return  # no-op
    conn = _connect()
    try:
        try:
            cur = conn.execute(
                "UPDATE app_users SET username = ? WHERE username = ?",
                (new, old),
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError(
                f"A user named {new!r} already exists."
            ) from exc
        if cur.rowcount == 0:
            raise ValueError(f"No user named {old!r}.")
    finally:
        conn.close()


# ── Refresh tokens ──────────────────────────────────────────────────────────
#
# Two-token auth: short-lived JWT access token + opaque long-lived
# refresh token. The refresh token is a 256-bit random string stored
# in this database and shipped to the browser as an HttpOnly cookie
# scoped to ``/api/auth``. The browser cannot read it; only the
# backend can validate it.
#
# Lifecycle:
#   * Issued on successful /login (and /setup, which issues a session
#     immediately).
#   * Consumed by /refresh to mint a new access JWT without re-typing
#     the password. The refresh row itself is NOT rotated on each
#     refresh - the same token id stays valid until expiry or revoke.
#   * Revoked on /logout (single token), password change (all rows
#     for the user), or user deletion (all rows for the user).
#   * Expired rows are reaped daily by a background thread spawned
#     from server/app.py's startup hook.

_REFRESH_TOKEN_TTL_SECONDS = 7 * 24 * 3600  # 7 days


def create_refresh_token(username: str) -> str:
    """
    Generate and persist a fresh refresh token for ``username``. Returns
    the opaque token id (43-char base64-url string) the caller should
    ship to the browser as the cookie value.

    The id is the only secret tied to this row. Server-side validation
    is a single-row SELECT; we don't bcrypt the id because it's already
    high-entropy random and never leaves the HttpOnly cookie + this
    table.
    """
    init_auth_db()
    uname = (username or "").strip()
    if not uname:
        raise ValueError("Username must not be empty.")
    token_id = secrets.token_urlsafe(32)  # 256 bits of entropy
    now = time.time()
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO refresh_tokens "
            "(id, username, issued_at, expires_at, revoked) "
            "VALUES (?, ?, ?, ?, 0)",
            (token_id, uname, now, now + _REFRESH_TOKEN_TTL_SECONDS),
        )
    finally:
        conn.close()
    return token_id


def validate_refresh_token(token_id: str) -> Optional[str]:
    """
    Look up a refresh token. Returns the owning username if the token
    exists, is not revoked, and is not past its expiry. Returns ``None``
    on every failure path so callers surface a generic 401 without an
    oracle for "no such row" vs "expired" vs "revoked".
    """
    init_auth_db()
    if not isinstance(token_id, str) or not token_id:
        return None
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT username, expires_at, revoked "
            "FROM refresh_tokens WHERE id = ?",
            (token_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    if row["revoked"]:
        return None
    if float(row["expires_at"]) <= time.time():
        return None
    return row["username"]


def revoke_refresh_token(token_id: str) -> None:
    """
    Mark a single refresh-token row as revoked. Idempotent; no error if
    the row is missing or already revoked. Called from /logout.
    """
    init_auth_db()
    if not isinstance(token_id, str) or not token_id:
        return
    conn = _connect()
    try:
        conn.execute(
            "UPDATE refresh_tokens SET revoked = 1 WHERE id = ?",
            (token_id,),
        )
    finally:
        conn.close()


def revoke_all_for_user(username: str) -> int:
    """
    Revoke every refresh-token row belonging to ``username``. Returns
    the count of rows touched (useful for log lines). Called on
    password change and on user delete so existing sessions on other
    devices can no longer silently mint new access tokens.
    """
    init_auth_db()
    uname = (username or "").strip()
    if not uname:
        return 0
    conn = _connect()
    try:
        cur = conn.execute(
            "UPDATE refresh_tokens SET revoked = 1 "
            "WHERE username = ? AND revoked = 0",
            (uname,),
        )
        return cur.rowcount
    finally:
        conn.close()


def cleanup_expired_tokens() -> int:
    """
    Delete every refresh-token row past its expiry. Returns the count
    deleted. Called once at startup and once a day from a background
    daemon thread in server/app.py.
    """
    init_auth_db()
    conn = _connect()
    try:
        cur = conn.execute(
            "DELETE FROM refresh_tokens WHERE expires_at < ?",
            (time.time(),),
        )
        return cur.rowcount
    finally:
        conn.close()
