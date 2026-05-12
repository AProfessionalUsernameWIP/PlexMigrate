"""
Multi-server registry for PlexMigrate v0.9.0.

Replaces the single ``plex_url`` + ``plex_token`` fields that lived in
``settings.json`` with a *list* of registered Plex servers. Each entry
carries a user-given friendly name, the URL, the token, and cached
status fields (last connection result, last contact time, last
library catalogue) so the frontend can paint a server-list page
without forcing every navigation to hit Plex.

Persistence
-----------
The registry lives in ``$PLEXMIGRATE_DATA_DIR/servers.json`` (defaults
to ``./server_data/servers.json`` — same volume mount as the rest of
the persistence layer). Writes go through the same atomic-write
helper that ``server/persistence.py`` uses, so a crash mid-save
leaves either the old document intact or the new one fully written.

Migration from v0.8.0
---------------------
v0.8.0 stored ``plex_url`` / ``plex_token`` in ``settings.json``.
On first boot in v0.9.0 :func:`migrate_legacy_settings` checks for
those keys and, if present, registers them as a server called
``"Default"`` then clears them from ``settings.json``. The legacy
fields are gone from the persistence default schema, so re-running
migration on an already-migrated install is a no-op.

Engine contract
---------------
This module **does not** modify the engine. It exposes
:func:`connect_registered_server` which returns the same
``(PlexServer, url, token, owner_name)`` tuple the engine has
always accepted. The engine is unaware of the registry; it sees a
single connection and a single token per call, exactly as in
single-server mode.

Threading note
--------------
Several PlexMigrate threads can read and write this file:
- The request handler when the user adds / renames / deletes a server.
- The "test connection" handler when the user clicks the refresh icon.
- The job worker reading the registry to resolve a name at run start.

A single module-level lock (:data:`_REG_LOCK`) serialises every
read-modify-write cycle. Reads that don't mutate (e.g. ``list_servers``)
also take the lock to ensure they see a consistent snapshot rather
than a mid-write file.
"""

from __future__ import annotations

import logging
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from server.persistence import (
    _atomic_write_json,
    delete_schedules_by_server_name,
    get_data_dir,
    load_settings,
    save_settings,
)
from server.secrets import decrypt_str, encrypt_str


log = logging.getLogger("plexmigrate.server.registry")


class ServerCredentialError(Exception):
    """
    Raised when a server's stored token can't be decrypted.

    The message is user-actionable — it names the server and tells
    the operator exactly what to do — because this exception flows
    all the way out to HTTP error responses and dashboard activity
    feed entries.
    """


# ── File location ────────────────────────────────────────────────────────────

def _registry_path() -> Path:
    """Absolute path to ``servers.json`` inside the configured data dir."""
    return get_data_dir() / "servers.json"


# ── Cross-thread lock ────────────────────────────────────────────────────────

# All public functions in this module that touch ``servers.json``
# acquire this lock. Granularity is whole-file because the file is
# small and access is infrequent — a finer lock would buy us nothing
# and complicate the invariants.
_REG_LOCK = threading.Lock()


# ── Defaults ─────────────────────────────────────────────────────────────────

# The shape of one server record. Anything missing from an on-disk
# document is filled in from this dict on load so adding a new field
# in a future release doesn't break an existing registry.
_DEFAULT_SERVER: Dict[str, Any] = {
    "id": "",
    "name": "",
    "url": "",
    "token": "",
    "last_status": "unknown",         # "ok" | "unreachable" | "auth_error" | "unknown"
    "last_status_detail": "",         # Human-readable description of the last result.
    "last_checked_at": 0.0,           # UNIX timestamp of last connection attempt.
    "last_libraries": [],             # Cached library list ({name,type,key,count}).
    "owner_name": "",                 # myPlexUsername at last successful connect.
    # v0.9.1: response time in milliseconds for the last lightweight
    # ping. ``None`` if no ping has succeeded yet. Used by the Servers
    # tab live indicator and the JobForm server selector chips.
    "last_response_ms": None,

    # v0.9.5: marker indicating ``token`` is a Fernet ciphertext string.
    # Rows missing this marker (or with it set to False) are treated as
    # legacy plaintext on the next ``_load_raw`` and migrated in place.
    "_encrypted": False,
    # v0.9.6 Feature 3: operator-chosen friendly names for users on
    # this server. Keys: raw Plex identifier — owner's email for the
    # owner, managed user's username for managed users. Values: a
    # free-form display string. Empty dict on rows that have never
    # had a custom display name assigned. The frontend reads this map
    # to substitute friendly names in the dashboard header, activity
    # feed, and direct-transfer user selector. Purely a rendering aid
    # — engine logic, logs, and backup files always use the raw
    # identifier.
    "user_display_names": {},

# ── Name-safety helper ───────────────────────────────────────────────────────

# Some server names (e.g. "Living Room / Plex") contain characters
# that are not legal in filenames on Windows or that would break
# log-directory globs. ``safe_server_name`` produces a filename-safe
# slug while preserving readability — used to prefix log dirs and
# export filenames so outputs from different servers never collide.
def safe_server_name(name: str) -> str:
    """
    Turn a free-form friendly name into a filename-safe slug.

    Examples:
        "My Home Plex"     -> "My-Home-Plex"
        "Plex (NAS) #1"    -> "Plex-NAS-1"
        "café / régal"     -> "cafe-regal"
    """
    if not name:
        return "server"
    # ASCII-fold so non-ASCII names don't trip filesystem encoding
    # surprises across host platforms. ``encode('ascii', 'ignore')``
    # silently drops accents; that's fine for filename use.
    folded = name.encode("ascii", "ignore").decode("ascii")
    # Replace any run of non-alphanumeric chars with a single hyphen.
    slug = re.sub(r"[^A-Za-z0-9]+", "-", folded).strip("-")
    return slug or "server"


# ── Load / Save ──────────────────────────────────────────────────────────────

def _load_raw() -> List[Dict[str, Any]]:
    """
    Read the on-disk registry. Missing or malformed file = empty list.
    Returned list is *not* a deep copy — callers that mutate must
    re-save the result through :func:`_save_raw`.

    Migrates legacy plaintext tokens on first read (v0.9.5): any row
    without ``_encrypted: True`` is treated as plaintext, its ``token``
    is encrypted in place, the marker is set, and the file is
    rewritten atomically. Caller must already hold :data:`_REG_LOCK`
    so the rewrite is safe to issue here.
    """
    import json
    path = _registry_path()
    if not path.exists():
        return []
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        log.exception("servers.json unreadable; treating as empty")
        return []
    if not isinstance(data, list):
        return []
    # Backfill missing fields from the default schema so old documents
    # don't break newer code that expects keys added in later versions.
    out: List[Dict[str, Any]] = []
    for raw in data:
        if not isinstance(raw, dict):
            continue
        merged = dict(_DEFAULT_SERVER)
        merged.update(raw)
        out.append(merged)

    # Migrate any rows still carrying a plaintext token. Done under
    # the caller's existing _REG_LOCK acquisition so concurrent reads
    # serialise naturally; the worst-case race writes identical
    # (semantically equivalent — Fernet uses a fresh IV each time)
    # ciphertexts back. ``os.replace`` keeps the write atomic.
    if _migrate_unencrypted_tokens_in_place(out):
        _save_raw(out)

    return out


def _migrate_unencrypted_tokens_in_place(rows: List[Dict[str, Any]]) -> bool:
    """
    For every row missing ``_encrypted: True``, treat the ``token``
    field as plaintext and replace it with a Fernet ciphertext. Sets
    the marker. Returns True if at least one row was migrated so the
    caller knows to persist the rewrite.

    Empty tokens are migrated too (marker flipped to True, ciphertext
    stays empty) so the file converges on a uniform shape and we don't
    re-attempt migration on every load.
    """
    migrated = 0
    for row in rows:
        if row.get("_encrypted") is True:
            continue
        plain = row.get("token") or ""
        row["token"] = encrypt_str(plain) if plain else ""
        row["_encrypted"] = True
        migrated += 1
    if migrated:
        log.info(
            "servers.json: migrated %d plaintext token(s) to encrypted storage.",
            migrated,
        )
    return migrated > 0


def _save_raw(rows: List[Dict[str, Any]]) -> None:
    """Atomically replace ``servers.json``."""
    _atomic_write_json(_registry_path(), rows)


def decrypt_server_token(row: Dict[str, Any]) -> str:
    """
    Return the plaintext token for a registry row.

    The token is kept ciphertext on disk and inside :func:`_load_raw`
    output; callers that need to hand a plaintext token to plexapi or
    to a direct HTTP write call use this helper inline at the point
    of use. The decrypted string lives only in the local variable
    returned here — it is never stored on the row or cached.

    Raises:
        ServerCredentialError — when the row's ciphertext can't be
        decrypted. The message names the server and tells the operator
        to re-enter credentials in the Servers tab. Callers should
        let this exception propagate to the request handler / job
        worker, which will translate it to a clean 400/500 or
        dashboard error.
    """
    # Lazy import so a regression test that monkey-patches Fernet
    # doesn't have to load this module's import chain.
    from cryptography.fernet import InvalidToken

    cipher = row.get("token") or ""
    if not cipher:
        return ""
    # Legacy rows (pre-v0.9.5) that somehow reached a decrypt site
    # without going through _load_raw's migration: treat token as
    # plaintext. Reach this branch only if a row was hand-edited or
    # came from a code path that bypasses _load_raw — defensive.
    if not row.get("_encrypted"):
        return cipher
    try:
        return decrypt_str(cipher)
    except InvalidToken as exc:
        name = row.get("name") or row.get("id") or "?"
        raise ServerCredentialError(
            f"Server token for {name!r} is unreadable — the encryption "
            f"key has changed. Please re-enter this server's credentials "
            f"in the Servers tab."
        ) from exc


# ── Public read API ─────────────────────────────────────────────────────────

def list_servers(*, include_tokens: bool = False) -> List[Dict[str, Any]]:
    """
    Return every registered server.

    ``include_tokens`` defaults to False so callers that send the
    list over the network can do so without exposing tokens. When the
    job runner needs the actual token to connect, it passes True.
    """
    with _REG_LOCK:
        rows = _load_raw()
    if include_tokens:
        return rows
    redacted: List[Dict[str, Any]] = []
    for row in rows:
        copy = dict(row)
        copy["has_token"] = bool(copy.get("token"))
        copy.pop("token", None)
        # The ``_encrypted`` marker is an internal storage detail; the
        # frontend has no need for it.
        copy.pop("_encrypted", None)
        redacted.append(copy)
    return redacted


def get_server_by_name(name: str, *, include_token: bool = True) -> Optional[Dict[str, Any]]:
    """
    Look up one server by its friendly name (case-sensitive, exact match).

    Returns ``None`` if no server is registered with that name. The
    case-sensitive contract matches how schedules and CLI flags pass
    server names around — "Plex1" and "plex1" are different servers.
    """
    with _REG_LOCK:
        rows = _load_raw()
    for row in rows:
        if row.get("name") == name:
            if not include_token:
                row = {k: v for k, v in row.items() if k not in ("token", "_encrypted")}
            return row
    return None


def get_server_by_id(server_id: str, *, include_token: bool = True) -> Optional[Dict[str, Any]]:
    """
    Look up one server by its server-generated UUID id.

    ID-based lookup is what the frontend uses (because the friendly
    name can change). The CLI uses name-based lookup because typing a
    UUID at a shell prompt is hostile to the user.
    """
    with _REG_LOCK:
        rows = _load_raw()
    for row in rows:
        if row.get("id") == server_id:
            if not include_token:
                row = {k: v for k, v in row.items() if k not in ("token", "_encrypted")}
            return row
    return None


# ── Public write API ────────────────────────────────────────────────────────

def add_server(name: str, url: str, token: str) -> Dict[str, Any]:
    """
    Register a new server. Friendly name must be unique (case-sensitive).
    Raises ValueError if the name is already taken or any required
    field is empty.
    """
    if not name or not name.strip():
        raise ValueError("Server name must not be empty.")
    if not url or not url.strip():
        raise ValueError("Server URL must not be empty.")
    if not token or not token.strip():
        raise ValueError("Server token must not be empty.")
    with _REG_LOCK:
        rows = _load_raw()
        if any(r.get("name") == name for r in rows):
            raise ValueError(f"A server named {name!r} is already registered.")
        new_row = dict(_DEFAULT_SERVER)
        new_row.update({
            "id": str(uuid.uuid4()),
            "name": name.strip(),
            "url": url.strip().rstrip("/"),
            # Encrypt before storing — plaintext never reaches disk.
            "token": encrypt_str(token.strip()),
            "_encrypted": True,
            "last_status": "unknown",
            "last_checked_at": 0.0,
        })
        rows.append(new_row)
        _save_raw(rows)
        return new_row


def update_server(server_id: str, *, name: Optional[str] = None, url: Optional[str] = None,
                  token: Optional[str] = None) -> Dict[str, Any]:
    """
    Rename or re-credential an existing server. Any field set to
    ``None`` is left unchanged. The friendly name remains unique across
    the registry — a rename that collides with another entry raises
    ``ValueError``. Tokens passed empty-string mean "keep current" so
    the frontend can submit a form without re-entering the token.
    """
    with _REG_LOCK:
        rows = _load_raw()
        target: Optional[Dict[str, Any]] = None
        for row in rows:
            if row.get("id") == server_id:
                target = row
                break
        if target is None:
            raise ValueError(f"No server with id {server_id!r}")
        if name is not None and name.strip() and name != target.get("name"):
            if any(r.get("name") == name and r is not target for r in rows):
                raise ValueError(f"A server named {name!r} already exists.")
            target["name"] = name.strip()
        if url is not None and url.strip():
            target["url"] = url.strip().rstrip("/")
        if token is not None and token != "":
            # New token from the UI / API — encrypt before storing.
            # Empty string means "keep the existing (already-encrypted)
            # value", which we honour by leaving target['token']
            # untouched.
            target["token"] = encrypt_str(token)
            target["_encrypted"] = True
        _save_raw(rows)
        return target


def cascade_preview(server_id: str) -> Optional[Dict[str, Any]]:
    """
    Count what a cascading remove of ``server_id`` would delete,
    without modifying anything. Returns ``None`` if no server with
    that id is registered.

    Used by the Servers tab to populate the confirmation dialog
    before the operator clicks Delete — so the prompt can read
    *"Jade.TV has 2 schedule(s), 47 export file(s), 12 log directory/ies"*
    instead of the previous blanket warning.
    """
    # Local imports to keep the dependency direction registry → log/export
    # one-way; these modules already import from persistence/secrets.
    from server import export_browser, log_browser
    from server.persistence import load_schedules

    row = get_server_by_id(server_id, include_token=False)
    if row is None:
        return None

    name = row.get("name") or ""
    slug = safe_server_name(name)
    schedules = load_schedules()
    schedule_count = sum(
        1 for s in schedules if s.get("source_server_name") == name
    )
    return {
        "id": server_id,
        "name": name,
        "slug": slug,
        "schedules": schedule_count,
        "exports": export_browser.count_exports_by_slug(slug),
        "log_dirs": log_browser.count_log_dirs_by_slug(slug),
    }


def remove_server(server_id: str) -> Optional[Dict[str, Any]]:
    """
    Cascading delete (v0.9.5): drops the registry row and every other
    artefact attributable to the named server — schedules referencing
    it, ``.plexbackup.json`` files produced by it, and per-run log
    directories under its slug.

    Order is deliberate:

      1. Schedules first — atomic write to ``schedules.json``.
      2. Registry row — atomic write to ``servers.json``.
      3. Files last (exports + log dirs) — best-effort; a permission
         error on one file is recorded in the summary but doesn't
         block the rest of the sweep.

    Rationale: if the backend crashes between (2) and (3) the data
    stores are still consistent — the row is gone, and orphan files
    on disk can be cleaned up later. Doing it in reverse would risk
    a live registry row pointing at deleted exports.

    Returns ``None`` if no server with ``server_id`` is registered,
    otherwise a summary dict ``{deleted: bool, schedules, exports,
    exports_failed, log_dirs, log_dirs_failed, errors}``. The bool
    ``deleted`` is True when the registry row itself was removed.
    """
    from server import export_browser, log_browser

    # We need the server's NAME (for schedule match) and SLUG (for
    # file match) before deleting the row. Pull them under the lock
    # together with the row-removal so a concurrent rename can't
    # land between the lookup and the delete.
    with _REG_LOCK:
        rows = _load_raw()
        target: Optional[Dict[str, Any]] = None
        for row in rows:
            if row.get("id") == server_id:
                target = row
                break
        if target is None:
            return None
        name = target.get("name") or ""
        slug = safe_server_name(name)

        # (1) Schedules — separate lock inside delete_schedules_by_server_name
        #     so this is safe to call while holding _REG_LOCK.
        schedules_removed = delete_schedules_by_server_name(name)

        # (2) Registry row.
        kept = [r for r in rows if r.get("id") != server_id]
        _save_raw(kept)

    # (3) Filesystem sweep — outside the registry lock to avoid holding
    #     it across slow disk I/O. By this point the registry and
    #     schedules are already consistent, so a concurrent reader
    #     sees the server as gone even while the file deletion runs.
    exports_deleted, export_errors = export_browser.delete_exports_by_slug(slug)
    logs_deleted, log_errors = log_browser.delete_log_dirs_by_slug(slug)

    errors = export_errors + log_errors
    return {
        "deleted": True,
        "id": server_id,
        "name": name,
        "slug": slug,
        "schedules": schedules_removed,
        "exports": exports_deleted,
        "exports_failed": len(export_errors),
        "log_dirs": logs_deleted,
        "log_dirs_failed": len(log_errors),
        "errors": errors,
    }


def remove_server_by_name(name: str) -> Optional[Dict[str, Any]]:
    """
    CLI convenience wrapper around :func:`remove_server` that takes a
    friendly name instead of a UUID. Returns the same cascade summary.
    """
    row = get_server_by_name(name, include_token=False)
    if row is None:
        return None
    return remove_server(row["id"])


# ── Startup helpers (v0.9.5) ────────────────────────────────────────────────

def ensure_encrypted_at_rest() -> int:
    """
    Force any plaintext rows in ``servers.json`` to be encrypted now,
    rather than waiting for the next lazy ``_load_raw`` call from a
    request handler. Called from the FastAPI startup hook so by the
    time uvicorn binds the port every row on disk is ciphertext.

    Returns the row count after migration. The migration itself runs
    as a side effect of ``_load_raw``; this wrapper exists so the
    intent is explicit at the call site.
    """
    with _REG_LOCK:
        rows = _load_raw()
    return len(rows)


# ── Users (v0.9.6 Feature 3) ────────────────────────────────────────────────

def get_server_users(server_id: str, logger: logging.Logger) -> Dict[str, Any]:
    """
    Return the list of users associated with one registered server.

    Owner row uses the Plex.tv email as the identifier — the same
    string the dashboard's ``current_user`` resolves through
    ``user_display_names``. Managed users come from
    ``server.systemAccounts()`` and use the local server username.

    Best-effort:
      * If ``systemAccounts()`` fails (local-admin token, permission
        error), the response still includes the owner row plus an
        empty managed list and a non-null ``error`` string so the
        frontend can render "No managed users found" with a tooltip
        rather than an error state.
      * Connection failures bubble up as ``ConnectionError`` so the
        route layer can return a clean 502.

    Returns:
        ``{"users": [...], "error": <str|null>}``. Each user dict
        carries ``{kind: "owner"|"managed", plex_id, raw_name,
        display_name}``. ``display_name`` is the operator's choice
        from the row's ``user_display_names`` map if one exists;
        otherwise an empty string and the UI falls back to ``raw_name``.

    Raises:
        ValueError if no server with ``server_id`` is registered.
        ConnectionError if Plex is unreachable.
    """
    from services.auth import connect_to_server

    row = get_server_by_id(server_id)
    if row is None:
        raise ValueError(f"No server with id {server_id!r}")

    token = row.get("token") or ""
    if not token:
        raise ConnectionError(
            f"Server {row.get('name') or server_id!r} has no stored token. "
            f"Re-enter credentials under the Servers tab."
        )

    server = connect_to_server(row["url"], token, logger)
    if server is None:
        raise ConnectionError(
            f"Could not connect to {row.get('name') or server_id!r}. "
            f"Check the URL and token under the Servers tab."
        )

    display_names: Dict[str, str] = dict(row.get("user_display_names") or {})
    users: List[Dict[str, Any]] = []

    # Owner: email from ``myPlexAccount``. Captured separately because
    # the SystemAccount entry for the owner uses the *username*, not
    # the email — and the dashboard's ``current_user`` keys on email
    # for the owner role.
    owner_email = ""
    owner_username = ""
    try:
        account = server.myPlexAccount()
        owner_email = (getattr(account, "email", "") or "").strip()
        owner_username = (getattr(account, "username", "") or "").strip()
    except Exception as e:
        logger.debug(f"myPlexAccount unavailable for {row.get('name')!r}: {e}")

    if owner_email:
        users.append({
            "kind": "owner",
            "plex_id": owner_email,
            "raw_name": owner_email,
            "display_name": display_names.get(owner_email, ""),
        })

    # Managed users — read systemAccounts(); skip the entry whose
    # ``name`` matches the owner's username (that's the owner himself
    # appearing in the local accounts list, already represented above).
    sys_error: Optional[str] = None
    try:
        sys_accts = server.systemAccounts() or []
    except Exception as e:
        sys_accts = []
        sys_error = f"systemAccounts() unavailable: {e}"
        logger.debug(sys_error)

    for acct in sys_accts:
        name = (getattr(acct, "name", "") or "").strip()
        if not name:
            continue
        # v0.9.7 Item 7: harden owner detection. The original check
        # matched SystemAccount.name against the Plex.tv username
        # from myPlexAccount, but those identifiers can legitimately
        # differ (server stores "Plex Owner" or a handle while
        # Plex.tv stores an email). Without the id==1 fallback the
        # owner can show up twice — once as kind="owner" (from
        # myPlexAccount.email) and once as kind="managed" (from
        # the SystemAccounts row that didn't match). SystemAccount
        # id 1 is Plex's conventional server-owner local id.
        try:
            local_id = int(getattr(acct, "id", 0) or 0)
        except (TypeError, ValueError):
            local_id = 0
        if local_id == 1:
            continue
        if owner_username and name == owner_username:
            continue
        users.append({
            "kind": "managed",
            "plex_id": name,
            "raw_name": name,
            "display_name": display_names.get(name, ""),
        })

    return {"users": users, "error": sys_error}


def set_user_display_name(
    server_id: str, plex_id: str, display_name: str,
) -> Optional[Dict[str, Any]]:
    """
    Set / clear one entry in a server's ``user_display_names`` map.

    An empty / whitespace-only ``display_name`` removes the entry
    instead of storing an empty value — that way the frontend's
    fallback ("show raw_name when no display name set") works without
    extra logic.

    Returns the redacted server row after the write, or ``None`` if
    no server with ``server_id`` exists. Raises ``ValueError`` if
    ``plex_id`` is empty (a client bug or malformed request).
    """
    plex_id = (plex_id or "").strip()
    if not plex_id:
        raise ValueError("plex_id must not be empty.")
    cleaned = (display_name or "").strip()

    with _REG_LOCK:
        rows = _load_raw()
        target = next((r for r in rows if r.get("id") == server_id), None)
        if target is None:
            return None
        names = dict(target.get("user_display_names") or {})
        if cleaned:
            names[plex_id] = cleaned
        else:
            names.pop(plex_id, None)
        target["user_display_names"] = names
        _save_raw(rows)

    return get_server_by_id(server_id, include_token=False)


# ── Engine integration ──────────────────────────────────────────────────────

def connect_registered_server(name_or_id: str, logger: logging.Logger
                              ) -> Tuple[Any, Dict[str, Any]]:
    """
    Resolve a server identifier and connect to Plex.

    The identifier is tried first as a friendly name, then as a UUID
    id, so the same function works for both CLI input ("Plex1") and
    REST handlers that pass the id from the URL.

    Returns ``(PlexServer, server_row)``. The caller is responsible
    for passing ``server_row['url']`` and ``server_row['token']`` to
    any engine call that needs them. Updates the server row's
    ``last_status`` / ``last_checked_at`` / ``owner_name`` fields and
    persists them before returning, so the registry's status column
    is always up to date after a connection attempt.

    Raises:
        ValueError if no server with that name/id is registered.
        ConnectionError if Plex is unreachable or the token is bad.
    """
    # Import locally so this module stays usable in environments
    # where plexapi/requests aren't installed (e.g. unit tests).
    from services.auth import connect_to_server

    row = get_server_by_name(name_or_id) or get_server_by_id(name_or_id)
    if row is None:
        raise ValueError(f"No registered server named or ided {name_or_id!r}")

    # Decrypt the token only at the moment we hand it to plexapi.
    # The plaintext lives in a local variable for the duration of
    # this call; nothing on ``row`` is mutated.
    try:
        plain_token = decrypt_server_token(row)
    except ServerCredentialError as exc:
        _record_status(row["id"], status="auth_error",
                       detail=str(exc), checked_at=time.time())
        raise ConnectionError(str(exc)) from exc

    server = connect_to_server(row["url"], plain_token, logger)
    now = time.time()
    if server is None:
        _record_status(row["id"], status="unreachable",
                       detail=f"connect_to_server returned None for {row['url']}",
                       checked_at=now)
        raise ConnectionError(
            f"Cannot connect to registered server {row['name']!r} at {row['url']}. "
            f"Check the URL and token under the Servers tab."
        )

    owner = getattr(server, "myPlexUsername", None) or "Plex Owner"
    _record_status(row["id"], status="ok", detail="", checked_at=now, owner=owner)
    # Refresh our local copy so the caller sees the new fields.
    row["last_status"] = "ok"
    row["last_checked_at"] = now
    row["owner_name"] = owner
    return server, row


def test_connection(server_id: str, logger: logging.Logger) -> Dict[str, Any]:
    """
    Probe a registered server's connection without running any engine
    logic. Used by the "Test" button in the Servers tab and at
    startup to populate the status indicators.

    Always returns the (now-updated) server row. Never raises — a
    failure is recorded into the row instead so the frontend can
    render a useful tooltip.
    """
    from services.auth import connect_to_server

    row = get_server_by_id(server_id)
    if row is None:
        raise ValueError(f"No server with id {server_id!r}")
    now = time.time()
    try:
        plain_token = decrypt_server_token(row)
    except ServerCredentialError as exc:
        _record_status(server_id, status="auth_error",
                       detail=str(exc), checked_at=now)
        return get_server_by_id(server_id, include_token=False) or row
    try:
        server = connect_to_server(row["url"], plain_token, logger)
    except Exception as exc:
        _record_status(server_id, status="unreachable",
                       detail=f"{type(exc).__name__}: {exc}", checked_at=now)
        return get_server_by_id(server_id, include_token=False) or row
    if server is None:
        _record_status(server_id, status="unreachable",
                       detail="connect_to_server returned None", checked_at=now)
        return get_server_by_id(server_id, include_token=False) or row
    # Try a tiny live query so an "ok" status really means the token works.
    try:
        owner = getattr(server, "myPlexUsername", None) or "Plex Owner"
        libs_raw = list(server.library.sections())
        libs: List[Dict[str, Any]] = []
        for sec in libs_raw:
            try:
                count = sec.totalSize
            except Exception:
                count = 0
            libs.append({"name": sec.title, "type": sec.type, "key": sec.key, "count": count})
        _record_status(server_id, status="ok", detail="", checked_at=now,
                       owner=owner, libraries=libs)
    except Exception as exc:
        _record_status(server_id, status="auth_error",
                       detail=f"{type(exc).__name__}: {exc}", checked_at=now)
    return get_server_by_id(server_id, include_token=False) or row


def refresh_libraries(server_id: str, logger: logging.Logger) -> List[Dict[str, Any]]:
    """
    Reload the cached library list for a server. Returns the new list.
    Errors propagate so the frontend can show them.
    """
    row = test_connection(server_id, logger)
    return row.get("last_libraries", []) or []


# ── Lightweight ping (v0.9.1) ────────────────────────────────────────────────

def ping_server(server_id: str, *, timeout: float = 3.0) -> Dict[str, Any]:
    """
    Probe a registered server's reachability without enumerating its
    libraries — much cheaper than :func:`test_connection`. Used by the
    UI's 30-second status refresh poll.

    Issues one ``GET /identity?X-Plex-Token=...`` against the registered
    URL. Plex's ``/identity`` endpoint returns a small XML document
    with server metadata; if the server is reachable and the token is
    accepted, we get a 200 response in single-digit milliseconds on a
    LAN. Any non-200, any connection error, or any timeout means the
    server is not reachable for our purposes.

    The result is also written back into the row's status fields so
    the cached value visible elsewhere stays current. Returns a dict
    ``{"ok": bool, "response_ms": float, "status": str, "detail": str}``.

    Never raises — a failure to reach Plex is the *expected* outcome
    for at least some pings and the caller just wants the result.
    """
    # Import here so the module stays importable without ``requests``
    # in environments that only use the CRUD bits.
    import requests

    row = get_server_by_id(server_id)
    if row is None:
        return {"ok": False, "response_ms": 0.0, "status": "unknown",
                "detail": f"No server with id {server_id!r}"}

    url = (row.get("url") or "").rstrip("/")
    if not url or not (row.get("token") or ""):
        _record_status(server_id, status="unreachable",
                       detail="URL or token missing on registry row",
                       checked_at=time.time(), response_ms=0.0)
        return {"ok": False, "response_ms": 0.0, "status": "unreachable",
                "detail": "URL or token missing"}
    try:
        token = decrypt_server_token(row)
    except ServerCredentialError as exc:
        _record_status(server_id, status="auth_error",
                       detail=str(exc), checked_at=time.time(), response_ms=0.0)
        return {"ok": False, "response_ms": 0.0, "status": "auth_error",
                "detail": str(exc)}

    started = time.perf_counter()
    detail = ""
    status = "unreachable"
    ok = False
    try:
        resp = requests.get(
            f"{url}/identity",
            headers={"X-Plex-Token": token, "Accept": "application/xml"},
            timeout=timeout,
        )
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        if resp.status_code == 200:
            ok = True
            status = "ok"
        elif resp.status_code in (401, 403):
            status = "auth_error"
            detail = f"HTTP {resp.status_code}: token rejected"
        else:
            detail = f"HTTP {resp.status_code}"
    except requests.exceptions.Timeout:
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        detail = f"Timeout after {timeout:.1f}s"
    except requests.exceptions.ConnectionError as exc:
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        detail = f"Connection error: {exc.__class__.__name__}"
    except Exception as exc:  # pragma: no cover (defensive)
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        detail = f"{type(exc).__name__}: {exc}"

    _record_status(
        server_id,
        status=status,
        detail=detail,
        checked_at=time.time(),
        response_ms=elapsed_ms,
    )
    return {
        "ok": ok,
        "response_ms": round(elapsed_ms, 1),
        "status": status,
        "detail": detail,
    }


def _record_status(server_id: str, *, status: str, detail: str, checked_at: float,
                   owner: Optional[str] = None, libraries: Optional[List[Dict[str, Any]]] = None,
                   response_ms: Optional[float] = None) -> None:
    """
    Internal: update status fields on one row and persist.

    Called from :func:`connect_registered_server` (job run),
    :func:`test_connection` (Test button), and :func:`ping_server`
    (live status poll). Keeping the write path in one place ensures
    the same lock discipline is applied no matter how the status was
    learned.

    ``response_ms`` is recorded on every call so the UI can show
    "current latency" without needing a separate write path; callers
    that don't measure latency (e.g. :func:`test_connection` which
    measures library enumeration time, not raw ping) pass ``None``
    to leave the previous value in place.
    """
    with _REG_LOCK:
        rows = _load_raw()
        for row in rows:
            if row.get("id") == server_id:
                row["last_status"] = status
                row["last_status_detail"] = detail
                row["last_checked_at"] = checked_at
                if owner is not None:
                    row["owner_name"] = owner
                if libraries is not None:
                    row["last_libraries"] = libraries
                if response_ms is not None:
                    row["last_response_ms"] = response_ms
                break
        _save_raw(rows)


# ── Auto-migration from v0.8.0 ──────────────────────────────────────────────

def migrate_legacy_settings(logger: logging.Logger) -> Optional[Dict[str, Any]]:
    """
    Move legacy ``plex_url`` / ``plex_token`` from ``settings.json``
    into the registry as a server named "Default".

    Idempotent: a fully-migrated install (no legacy fields, or legacy
    fields empty) returns ``None`` and writes nothing. A partially
    migrated install (registry already has entries but legacy fields
    are still set) clears the legacy fields and returns ``None`` — we
    don't double-register.

    Returns the newly-created server row, or ``None`` if no migration
    was needed.
    """
    settings = load_settings()
    legacy_url = (settings.get("plex_url") or "").strip()
    legacy_tok = (settings.get("plex_token") or "").strip()
    # Idempotence: a fully-migrated install has both legacy fields empty
    # (load_settings backfills missing keys as ""). We early-out here so
    # the function performs zero work and writes zero bytes on every
    # subsequent startup. If a future schema change adds new legacy
    # fields, that path needs its own empty-check guard at this point.
    if not legacy_url and not legacy_tok:
        return None

    # If the registry already has the legacy URL/token under any name,
    # treat this as already-migrated and just clear the legacy fields.
    # Tokens in the registry are encrypted; decrypt for the compare.
    # A row whose token is unreadable can't match plaintext anyway, so
    # we just skip it.
    existing = list_servers(include_tokens=True)
    for row in existing:
        if row.get("url") != legacy_url.rstrip("/"):
            continue
        try:
            row_plain = decrypt_server_token(row)
        except ServerCredentialError:
            continue
        if row_plain == legacy_tok:
            _clear_legacy_fields(settings)
            return None

    # If there's no registry at all, register the legacy as "Default".
    # If there IS a registry already (user manually added servers) but
    # neither URL nor token match, the legacy fields probably refer to
    # a server the user no longer wants — clear them silently.
    if existing:
        _clear_legacy_fields(settings)
        logger.info(
            "Legacy plex_url/plex_token in settings.json ignored — "
            "registry already contains %d server(s).", len(existing),
        )
        return None

    # Heuristic default name: "Default" unless already taken (it
    # can't be — registry is empty here).
    new_row = add_server(name="Default", url=legacy_url, token=legacy_tok)
    _clear_legacy_fields(settings)
    logger.info("Migrated legacy plex_url/plex_token into registry as server 'Default'.")
    return new_row


def _clear_legacy_fields(settings: Dict[str, Any]) -> None:
    """Strip the now-unused single-server fields out of ``settings.json``."""
    patch = {"plex_url": "", "plex_token": ""}
    # ``save_settings`` is a merge — passing the patch overwrites just
    # those two keys without touching workers / output_dir / etc.
    save_settings(patch)
