"""
Persistence layer for the PlexMigrate server.

The server keeps two pieces of state on disk so they survive container
restarts and rebuilds:

* ``settings.json`` - Plex server URL + auth token + default output/log
  directories. There is one settings document; the file is rewritten
  in full on every change.
* ``schedules.json`` - list of saved schedule entries (which libraries
  to snapshot, how often, where to write the output, next-run timestamp).

Both files live in ``$PLEXMIGRATE_DATA_DIR`` (defaulting to
``./server_data``). In the Docker setup ``./server_data`` is a bind
mount, so the data outlives ``docker compose down``.

Why JSON files and not a database? Schedules and settings change rarely,
there is exactly one writer (the FastAPI process), and a JSON file is
trivially inspectable and editable from a host shell if the server is
ever wedged. SQLite would be overkill.

Concurrent access from the server's own threads is guarded by
``_FILE_LOCK`` - both the scheduler thread and the request handler may
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
# A single lock guarding both files is enough - writes are tiny and
# infrequent, and the simpler invariant ("only one thread is writing
# any persistence file at a time") is easier to reason about than two
# locks.
_FILE_LOCK = threading.Lock()


# ── Default documents ────────────────────────────────────────────────────────

# The schema for settings.json. Any key omitted from an on-disk
# document is filled in with these defaults on load - that keeps the
# server compatible with older settings files when new fields are added.
_DEFAULT_SETTINGS: Dict[str, Any] = {
    "plex_url": "http://host.docker.internal:32400",
    "plex_token": "",
    "output_dir": "./snapshots",
    "log_dir": "./plex_logs",
    "workers": 16,
    "scrobble_workers": 8,
    "verbose": False,
    "strict_match": True,
    # PR-13 - snapshot retention. Global ceiling + optional per-server
    # override map ({server_id: int}). Override applies only when
    # strictly lower than the global; never higher. See
    # ``snapshot_registry.effective_retention_for`` for the resolution.
    "snapshot_retention_global": 30,
    "snapshot_retention_per_server": {},
    # Global default for the snapshot JSON-sidecar toggle. None or
    # False means "off"; the operator-set per-server map below or the
    # per-job toggle override at job-fire time.
    "prebuild_json_sidecar_default": False,
    # Per-server snapshot-time defaults map (Servers ▸ Advanced Settings).
    # See SettingsIn.snapshot_defaults_per_server for the recognised fields.
    "snapshot_defaults_per_server": {},
    # Direct-transfer resolver-tier policy. Tier 2 (filepath suffix)
    # defaults ON; Tier 3 (fuzzy title) defaults OFF. The flip applies
    # only at the direct-transfer call boundary - snapshot/import paths
    # keep both fallbacks active regardless.
    "transfer_resolution": {
        "allow_filepath_fallback": True,
        "allow_fuzzy_fallback": False,
    },
    # media.db retention + cascade-delete policy. cascade_delete is
    # the greedy-restrictive default (auto-purges per-server rows on
    # server removal); operators who want to preserve data must
    # explicitly set prevent_cascade_delete = true before deleting.
    # prune_stale_*_days are placeholders - the sweep logic itself
    # lands in a later commit.
    "media_db_retention": {
        "cascade_delete_on_server_remove": True,
        "prevent_cascade_delete": False,
        "prune_stale_watch_events_days": 0,
        "prune_stale_server_data_days": 0,
    },
    # Rule 2: library-walk job cadence. The walk ticks
    # server_items.last_seen_at for every item present on each
    # registered server; the Prune Missing Items action reads those
    # timestamps. Defaults: enabled, every 24h. Interval is floored
    # at 1h to prevent thrashing large libraries.
    # ``stale_threshold_days`` is the default value the Prune UI's
    # slider opens at; operators override per-prune.
    "library_walk": {
        "enabled": True,
        "interval_seconds": 86400,
        "stale_threshold_days": 7,
    },
}


# ── Settings I/O ─────────────────────────────────────────────────────────────

def load_settings() -> Dict[str, Any]:
    """
    Return a complete settings dict. Missing file or missing keys are
    backfilled from ``_DEFAULT_SETTINGS``.

    v0.9.5: if the on-disk document carries ``"_encrypted": True``,
    the ``plex_token`` field is decrypted before being returned. If
    the marker is absent, the field is treated as plaintext (legacy /
    pre-migration shape) and returned as-is - the v0.8→v0.9 migration
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

    Keys not present in ``settings`` retain their previous values -
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
    the new file is fully there, or the old one is still there - never
    a half-written file. This matters because both files are read on
    every request.

    M14: the temp file is chmod'd to ``0o600`` before the replace so
    the destination (``settings.json`` / ``servers.json``) is never
    world-readable - both can carry Fernet-encrypted Plex tokens, and
    the keyfile lives in the same directory. M16: ``chmod`` only has
    real effect on POSIX hosts; on Windows the data-directory ACL is
    the actual security boundary and must be locked down separately.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, default=str)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        # Best-effort; never block a settings write on a chmod failure.
        pass
    os.replace(tmp, path)


# ── Path validation (v0.9.5) ─────────────────────────────────────────────────

# Catches Windows-style host paths that won't work inside the Linux
# container: drive-letter roots (``C:\``, ``Y:/``, ``z:\foo``) and UNC
# paths (``\\server\share``). Linux paths and relative paths (``./`` or
# bare names like ``snapshots``) pass through.
_WINDOWS_PATH_RE = re.compile(r"^([A-Za-z]:[\\/]|\\\\)")


def validate_container_path(path: str, field_label: str) -> None:
    """
    Reject filesystem paths that won't work inside the Linux backend
    container.

    The backend runs in Docker; it only sees host paths that are
    explicitly bind-mounted via ``docker-compose.yml``. Operators
    occasionally enter a Windows host path like ``Y:\\plexexports`` in
    the Settings tab; the engine then ``mkdir``s a literal directory
    named ``Y:\\plexexports`` inside ``/app/`` (since backslash and
    colon are legal in Linux filenames) and writes the snapshot there,
    where the host can never see it.

    This guard rejects such paths at the API boundary with an actionable
    error message that names the field and tells the operator exactly
    how to wire up an external drive instead.

    Empty strings are not rejected - the engine has its own per-field
    default fallback (``./snapshots``, ``./plex_logs``) so leaving a
    field blank is a valid "use default" signal.

    M10: two additional containment checks beyond the Windows-path
    guard. Bind-mounting an export drive at an arbitrary container
    path (e.g. ``/app/nas_exports``) is a supported, documented
    workflow, so we do NOT confine to a single data root - but a path
    must not (1) contain a ``..`` component, or (2) resolve to the
    ``server_data/`` directory or anything inside it. ``server_data/``
    holds the keyfile, ``auth.db``, ``settings.json``, and
    ``media.db``; letting an authenticated caller aim a snapshot write
    or import read at it would expose or clobber credentials.

    Raises:
        ValueError - with a multi-line user-facing message. Callers in
        FastAPI handlers should catch this and translate to 400.
    """
    if not path:
        return

    # M10 (1): no parent-directory traversal. Normalise separators
    # first - ``_WINDOWS_PATH_RE`` only catches drive-letter / UNC
    # *prefixes*, so an embedded ``foo\..\bar`` would slip past it.
    if ".." in path.replace("\\", "/").split("/"):
        raise ValueError(
            f"{field_label} {path!r} contains a '..' path component. "
            f"Parent-directory traversal is not allowed - enter a "
            f"direct path to the target directory."
        )

    # M10 (2): must not resolve into the credentials directory.
    try:
        data_dir = get_data_dir().resolve()
        resolved = Path(path).resolve()
        resolved.relative_to(data_dir)
    except ValueError:
        # relative_to raised - the path is NOT inside server_data/.
        pass
    except OSError:
        # resolve() can raise on some platforms for pathological
        # input; fall through to the Windows-path check rather than
        # crashing the request.
        pass
    else:
        raise ValueError(
            f"{field_label} {path!r} resolves inside the server_data "
            f"directory, which holds credentials and databases. Pick a "
            f"different location - this directory is never a valid "
            f"export target or import source."
        )

    if not _WINDOWS_PATH_RE.match(path):
        return
    raise ValueError(
        f"{field_label} {path!r} looks like a Windows host path, but the "
        f"backend runs inside a Linux container and can only write to "
        f"paths that are bind-mounted in docker-compose.yml. "
        f"To use this location: "
        f"(1) open docker-compose.yml and add a volume entry such as "
        f"`Y:/plexexports:/app/nas_exports` under the backend's "
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
    is dropped from the response too - it's a storage detail.
    """
    redacted = dict(settings)
    token: Optional[str] = redacted.pop("plex_token", None)
    redacted.pop("_encrypted", None)
    redacted["has_token"] = bool(token)
    return redacted


# ── PR-13 startup migrations ────────────────────────────────────────────────

def migrate_output_dir_setting() -> None:
    """
    Rename the default ``plex_exports`` output directory to
    ``snapshots`` in ``settings.json``. Custom non-default paths are
    left untouched - we only flip the exact pre-rename literal so an
    operator who intentionally pointed at a different directory keeps
    their choice.

    Idempotent: running on an already-migrated install is a no-op
    silent return.
    """
    log = logging.getLogger("plexmigrate.server.persistence")
    try:
        settings = load_settings()
    except Exception:
        log.exception("migrate_output_dir_setting: load_settings failed; skipping")
        return
    current = settings.get("output_dir")
    # Match the most common default-value spellings: bare 'plex_exports',
    # leading './', and an optional trailing slash. Anything else is
    # treated as an operator-customised path and left untouched.
    legacy_defaults = {
        "plex_exports", "plex_exports/",
        "./plex_exports", "./plex_exports/",
    }
    if isinstance(current, str) and current in legacy_defaults:
        settings["output_dir"] = "./snapshots"
        save_settings(settings)
        log.info(
            "Migrated output_dir setting from %r to './snapshots'.",
            current,
        )


def relocate_legacy_exports() -> int:
    """
    Move any pre-rename ``.plexexport.json`` files out of the old
    ``plex_exports/`` directory (and out of the root of the new
    ``snapshots/`` directory if any leaked there) into
    ``<output_dir>/legacy/``.

    Returns the count moved. Idempotent; silently no-ops when there
    is nothing to relocate. Errors on individual files are logged
    and counted but don't stop the sweep.

    Registry-aware (post-bug-3): any ``.plexexport.json`` whose stem
    matches a ``snapshot_name`` in ``snapshots.db`` is a current
    sidecar produced by ``materialise_sidecar``, NOT a legacy archive.
    Pre-fix, every boot moved those fresh sidecars into ``legacy/``,
    making the registry's ``prebuilt_json_path`` go stale and the file
    show up in the wrong UI panel. We skip any file the registry
    claims, in either of two ways: a stem matching a row's
    ``snapshot_name``, OR a file_path matching a row's
    ``prebuilt_json_path`` exactly.
    """
    import shutil
    log = logging.getLogger("plexmigrate.server.persistence")
    settings = load_settings()
    output_dir = (settings.get("output_dir") or "snapshots").strip()
    new_dir = Path(output_dir)
    legacy_dir = new_dir / "legacy"

    # Build the don't-touch set from the snapshot registry. Best-effort:
    # if the registry isn't initialised yet (very early boot, fresh
    # install) this returns an empty set and we behave like the
    # pre-fix version - which is the right thing on a clean install
    # where there's nothing in the registry to protect anyway.
    protected_stems: set = set()
    protected_paths: set = set()
    try:
        from server import snapshot_registry
        for row in snapshot_registry.list_snapshots():
            name = (row.get("snapshot_name") or "").strip()
            if name:
                # Strip the " (recovered)" suffix - the sidecar on disk
                # uses the bare snapshot_name, not the registry's label.
                bare = name.removesuffix(" (recovered)").strip()
                protected_stems.add(bare)
            pre = (row.get("prebuilt_json_path") or "").strip()
            if pre:
                try:
                    protected_paths.add(str(Path(pre).resolve()))
                except OSError:
                    protected_paths.add(pre)
    except Exception:
        log.exception(
            "relocate_legacy_exports: snapshot_registry consult failed; "
            "proceeding without registry protection (treating every "
            "top-level .plexexport.json as legacy).",
        )

    moved = 0
    # Look in BOTH locations: the literal old directory (for installs
    # that ran a snapshot before this migration landed) and the new
    # directory's own root (for any leaked per-library files).
    for source_dir in (Path("plex_exports"), new_dir):
        try:
            if not source_dir.exists() or not source_dir.is_dir():
                continue
        except OSError:
            continue
        for f in list(source_dir.iterdir()):
            try:
                if not f.is_file():
                    continue
            except OSError:
                continue
            if not f.name.endswith((".plexexport.json", ".plexbackup.json")):
                continue
            # Skip protected files - the registry claims them as the
            # cached sidecar of a real snapshot, not legacy data.
            stem = f.name[:-len(".plexexport.json")]
            try:
                abs_path = str(f.resolve())
            except OSError:
                abs_path = str(f)
            if stem in protected_stems or abs_path in protected_paths:
                continue
            # Don't touch files that live inside the new snapshot
            # tree's subdirectories (iterdir at the root level only).
            legacy_dir.mkdir(parents=True, exist_ok=True)
            target = legacy_dir / f.name
            # Avoid clobber: if a same-named file already exists in
            # legacy/, suffix the move target with a numeric.
            n = 1
            while target.exists():
                target = legacy_dir / f"{f.stem}_{n}{f.suffix}"
                n += 1
            try:
                shutil.move(str(f), str(target))
                moved += 1
            except OSError:
                log.exception("Failed to relocate legacy export %s", f)
        # Best-effort rmdir of the empty old plex_exports/ directory.
        if source_dir.name == "plex_exports":
            try:
                source_dir.rmdir()
            except OSError:
                pass  # not empty (operator put non-JSON files there); leave it
    if moved:
        log.info("Relocated %d legacy .plexexport.json file(s) to %s", moved, legacy_dir)
    return moved
