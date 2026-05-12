"""
Persistence layer for the PlexMigrate server.

The server keeps two pieces of state on disk so they survive container
restarts and rebuilds:

* ``settings.json`` — Plex server URL + auth token + default output/log
  directories. There is one settings document; the file is rewritten
  in full on every change.
* ``schedules.json`` — list of saved schedule entries (which libraries
  to export, how often, where to write the output, next-run timestamp).

Both files live in ``$PLEXMIGRATE_DATA_DIR`` (defaulting to
``./server_data``). In the Docker setup ``./server_data`` is a bind
mount, so the data outlives ``docker compose down``.

Why JSON files and not a database? Schedules and settings change rarely,
there is exactly one writer (the FastAPI process), and a JSON file is
trivially inspectable and editable from a host shell if the server is
ever wedged. SQLite would be overkill.

Concurrent access from the server's own threads is guarded by
``_FILE_LOCK`` — both the scheduler thread and the request handler may
write at the same time.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional


log = logging.getLogger("plexmigrate.server.persistence")


# ── Data directory resolution ────────────────────────────────────────────────

def get_data_dir() -> Path:
    """
    Returns the directory where settings.json and schedules.json live.

    Honours ``PLEXMIGRATE_DATA_DIR`` for the Docker setup; falls back
    to ``./server_data`` so a developer running ``uvicorn`` directly
    against a checkout gets a sensible default. The directory is
    created on first access.
    """
    raw = os.environ.get("PLEXMIGRATE_DATA_DIR", "./server_data")
    p = Path(raw)
    p.mkdir(parents=True, exist_ok=True)
    return p


# ── File paths ───────────────────────────────────────────────────────────────

def _settings_path() -> Path:
    return get_data_dir() / "settings.json"


def _schedules_path() -> Path:
    return get_data_dir() / "schedules.json"


# ── Cross-thread write lock ───────────────────────────────────────────────────
# A single lock guarding both files is enough — writes are tiny and
# infrequent, and the simpler invariant ("only one thread is writing
# any persistence file at a time") is easier to reason about than two
# locks.
_FILE_LOCK = threading.Lock()


# ── Default documents ────────────────────────────────────────────────────────

# The schema for settings.json. Any key omitted from an on-disk
# document is filled in with these defaults on load — that keeps the
# server compatible with older settings files when new fields are added.
_DEFAULT_SETTINGS: Dict[str, Any] = {
    "plex_url": "http://host.docker.internal:32400",
    "plex_token": "",
    "output_dir": "./plex_exports",
    "log_dir": "./plex_logs",
    "workers": 16,
    "scrobble_workers": 8,
    "verbose": False,
    "strict_match": True,
}


# ── Settings I/O ─────────────────────────────────────────────────────────────

def load_settings() -> Dict[str, Any]:
    """
    Return a complete settings dict. Missing file or missing keys are
    backfilled from ``_DEFAULT_SETTINGS``.

    v0.9.5: if the on-disk document carries ``"_encrypted": True``,
    the ``plex_token`` field is decrypted before being returned. If
    the marker is absent, the field is treated as plaintext (legacy /
    pre-migration shape) and returned as-is — the v0.8→v0.9 migration
    in ``server_registry.migrate_legacy_settings`` is the one path
    that depends on this; once it runs, ``_clear_legacy_fields`` writes
    an empty string back through ``save_settings``, which sets the
    marker for all subsequent loads.
    """
    path = _settings_path()
    if not path.exists():
        return dict(_DEFAULT_SETTINGS)
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except (json.JSONDecodeError, OSError):
        # A corrupt or unreadable file should not crash the server.
        # Treat it as missing and let the next save rewrite it.
        return dict(_DEFAULT_SETTINGS)
    merged = dict(_DEFAULT_SETTINGS)
    merged.update(raw or {})

    if merged.get("_encrypted") and merged.get("plex_token"):
        from server.secrets import decrypt_str
        from cryptography.fernet import InvalidToken
        try:
            merged["plex_token"] = decrypt_str(str(merged["plex_token"]))
        except InvalidToken:
            # Keyfile lost / regenerated. Treat as no-token rather
            # than crash; the legacy-migration path will then see an
            # empty token and not try to import a garbage value.
            log.warning(
                "settings.json: stored plex_token can't be decrypted "
                "(encryption key has changed). Treating as empty."
            )
            merged["plex_token"] = ""

    return merged


def save_settings(settings: Dict[str, Any]) -> Dict[str, Any]:
    """
    Atomically replace the on-disk settings document. Returns the full
    merged document so the caller can echo it back to the client.

    Keys not present in ``settings`` retain their previous values —
    this is a partial update.

    v0.9.5: the ``plex_token`` field is encrypted before write and
    the ``_encrypted`` marker is set. The returned dict still holds
    the plaintext token so callers that immediately consume the
    document don't need to know about encryption.
    """
    with _FILE_LOCK:
        existing = load_settings()
        existing.update(settings)

        # Build the on-disk payload with the token encrypted. Returned
        # dict keeps the plaintext for the caller.
        from server.secrets import encrypt_str
        on_disk = dict(existing)
        plain_token = on_disk.get("plex_token") or ""
        on_disk["plex_token"] = encrypt_str(plain_token) if plain_token else ""
        on_disk["_encrypted"] = True

        _atomic_write_json(_settings_path(), on_disk)
        return existing


# ── Schedule I/O ─────────────────────────────────────────────────────────────

def load_schedules() -> List[Dict[str, Any]]:
    """
    Return the list of schedule documents. Missing file = empty list.
    """
    path = _schedules_path()
    if not path.exists():
        return []
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except (json.JSONDecodeError, OSError):
        return []
    return list(raw) if isinstance(raw, list) else []


def save_schedules(schedules: List[Dict[str, Any]]) -> None:
    """
    Atomically replace the on-disk schedules list.
    """
    with _FILE_LOCK:
        _atomic_write_json(_schedules_path(), schedules)


def upsert_schedule(schedule: Dict[str, Any]) -> Dict[str, Any]:
    """
    Insert (if ``id`` missing) or replace (if ``id`` exists) a schedule
    entry. Returns the stored document including any auto-assigned id.
    """
    with _FILE_LOCK:
        items = load_schedules()
        if not schedule.get("id"):
            schedule["id"] = str(uuid.uuid4())
            items.append(schedule)
        else:
            target_id = schedule["id"]
            replaced = False
            for i, existing in enumerate(items):
                if existing.get("id") == target_id:
                    items[i] = schedule
                    replaced = True
                    break
            if not replaced:
                items.append(schedule)
        _atomic_write_json(_schedules_path(), items)
        return schedule


def delete_schedule(schedule_id: str) -> bool:
    """
    Remove the schedule with the given id. Returns True if a row was
    actually removed, False if no schedule had that id.
    """
    with _FILE_LOCK:
        items = load_schedules()
        kept = [s for s in items if s.get("id") != schedule_id]
        if len(kept) == len(items):
            return False
        _atomic_write_json(_schedules_path(), kept)
        return True


def delete_schedules_by_server_name(server_name: str) -> int:
    """
    Remove every schedule whose ``source_server_name`` equals
    ``server_name``. Returns the number of rows removed.

    Used by the server-removal cascade (v0.9.5): when a registered
    server is deleted, schedules that fire against it can never run
    again, so they're cleared in lockstep rather than left to log a
    "no source_server_name set" warning every 30 seconds.
    """
    with _FILE_LOCK:
        items = load_schedules()
        kept = [s for s in items if s.get("source_server_name") != server_name]
        removed = len(items) - len(kept)
        if removed:
            _atomic_write_json(_schedules_path(), kept)
        return removed


# ── Helpers ──────────────────────────────────────────────────────────────────

def _atomic_write_json(path: Path, payload: Any) -> None:
    """
    Write ``payload`` to ``path`` atomically.

    We write to a sibling temp file then ``os.replace()`` it on top of
    the destination. That gives us a crash-safety guarantee: either
    the new file is fully there, or the old one is still there — never
    a half-written file. This matters because both files are read on
    every request.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, default=str)
    os.replace(tmp, path)


# ── Path validation (v0.9.5) ─────────────────────────────────────────────────

# Catches Windows-style host paths that won't work inside the Linux
# container: drive-letter roots (``C:\``, ``Y:/``, ``z:\foo``) and UNC
# paths (``\\server\share``). Linux paths and relative paths (``./`` or
# bare names like ``plex_exports``) pass through.
_WINDOWS_PATH_RE = re.compile(r"^([A-Za-z]:[\\/]|\\\\)")


def validate_container_path(path: str, field_label: str) -> None:
    """
    Reject filesystem paths that won't work inside the Linux backend
    container.

    The backend runs in Docker; it only sees host paths that are
    explicitly bind-mounted via ``docker-compose.yml``. Operators
    occasionally enter a Windows host path like ``Y:\\plexbackups`` in
    the Settings tab; the engine then ``mkdir``s a literal directory
    named ``Y:\\plexbackups`` inside ``/app/`` (since backslash and
    colon are legal in Linux filenames) and writes the export there,
    where the host can never see it.

    This guard rejects such paths at the API boundary with an actionable
    error message that names the field and tells the operator exactly
    how to wire up an external drive instead.

    Empty strings are not rejected — the engine has its own per-field
    default fallback (``./plex_exports``, ``./plex_logs``) so leaving a
    field blank is a valid "use default" signal.

    Raises:
        ValueError — with a multi-line user-facing message. Callers in
        FastAPI handlers should catch this and translate to 400.
    """
    if not path:
        return
    if not _WINDOWS_PATH_RE.match(path):
        return
    raise ValueError(
        f"{field_label} {path!r} looks like a Windows host path, but the "
        f"backend runs inside a Linux container and can only write to "
        f"paths that are bind-mounted in docker-compose.yml. "
        f"To use this location: "
        f"(1) open docker-compose.yml and add a volume entry such as "
        f"`Y:/plexbackups:/app/nas_exports` under the backend's "
        f"`volumes:` block (use forward slashes on the host side); "
        f"(2) run `docker compose up -d` to recreate the container with "
        f"the new mount; "
        f"(3) come back to this field and enter the container-side path "
        f"(e.g. `/app/nas_exports`)."
    )


def redact_settings(settings: Dict[str, Any]) -> Dict[str, Any]:
    """
    Return a copy of ``settings`` suitable for sending to the browser.

    The Plex token is replaced with a boolean ``has_token``. The raw
    token never leaves the server. The internal ``_encrypted`` marker
    is dropped from the response too — it's a storage detail.
    """
    redacted = dict(settings)
    token: Optional[str] = redacted.pop("plex_token", None)
    redacted.pop("_encrypted", None)
    redacted["has_token"] = bool(token)
    return redacted
