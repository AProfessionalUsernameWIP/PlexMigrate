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
import os
import threading
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional


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
    return merged


def save_settings(settings: Dict[str, Any]) -> Dict[str, Any]:
    """
    Atomically replace the on-disk settings document. Returns the full
    merged document so the caller can echo it back to the client.

    Keys not present in ``settings`` retain their previous values —
    this is a partial update.
    """
    with _FILE_LOCK:
        existing = load_settings()
        existing.update(settings)
        _atomic_write_json(_settings_path(), existing)
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


def redact_settings(settings: Dict[str, Any]) -> Dict[str, Any]:
    """
    Return a copy of ``settings`` suitable for sending to the browser.

    The Plex token is replaced with a boolean ``has_token``. The raw
    token never leaves the server.
    """
    redacted = dict(settings)
    token: Optional[str] = redacted.pop("plex_token", None)
    redacted["has_token"] = bool(token)
    return redacted
