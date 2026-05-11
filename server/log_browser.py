"""
Read-only browser over the engine's log directories.

The engine writes per-run logs into ``$log_dir/<timestamp>[_PASS|_FAIL]/``.
Each directory contains:

  * ``run_*.log``                 — the full run transcript
  * ``<Library>_success_*.log``   — per-library success log (if any)
  * ``<Library>_fail_*.log``      — per-library failure log (if any)
  * ``troubleshoot_*.log``        — categorised failures + fix steps
  * ``unresolved_*.log``          — items that failed all tiers
  * ``errors.log``                — emitted when something fatal happened

This module exposes the directory tree to the frontend without
duplicating any engine logic: it lists, it reads, it never writes.

All path inputs are validated against the configured log directory so
a client cannot ``../../etc/passwd`` out of the sandbox.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any, Dict, List, Tuple

from server.persistence import load_settings


# ── Path containment guard ───────────────────────────────────────────────────

def _resolve_within(base: Path, relative: str) -> Path:
    """
    Resolve ``relative`` against ``base`` and assert the result stays
    inside ``base``. Raises ``ValueError`` on traversal attempts or on
    absolute paths that don't share the base prefix.
    """
    candidate = (base / relative).resolve()
    base_resolved = base.resolve()
    try:
        candidate.relative_to(base_resolved)
    except ValueError as e:
        raise ValueError(f"Path {relative!r} escapes log directory {base!s}") from e
    return candidate


# ── Listing ──────────────────────────────────────────────────────────────────

def list_runs() -> List[Dict[str, Any]]:
    """
    Return the per-run subdirectories under the configured log dir,
    newest first. Each entry has:

      * ``name``       — directory name (e.g. ``20260510_135425_PASS``)
      * ``mtime``      — modification time UNIX timestamp
      * ``size``       — total bytes across all files in the run
      * ``passed``     — True/False/None for the *_PASS / *_FAIL / no-suffix cases
      * ``file_count`` — number of regular files in the run dir
    """
    settings = load_settings()
    base = Path(settings.get("log_dir") or "./plex_logs")
    if not base.exists():
        return []

    entries: List[Dict[str, Any]] = []
    for child in base.iterdir():
        if not child.is_dir():
            continue
        files = [f for f in child.rglob("*") if f.is_file()]
        total = sum(f.stat().st_size for f in files)
        name = child.name
        if name.endswith("_PASS"):
            passed: "bool | None" = True
        elif name.endswith("_FAIL"):
            passed = False
        else:
            passed = None
        entries.append({
            "name": name,
            "mtime": child.stat().st_mtime,
            "size": total,
            "passed": passed,
            "file_count": len(files),
        })
    entries.sort(key=lambda e: e["mtime"], reverse=True)
    return entries


def list_files(run_name: str) -> List[Dict[str, Any]]:
    """
    Return the log files inside one run directory. Each entry has
    ``name``, ``size``, ``mtime``.
    """
    settings = load_settings()
    base = Path(settings.get("log_dir") or "./plex_logs")
    run_dir = _resolve_within(base, run_name)
    if not run_dir.is_dir():
        raise FileNotFoundError(f"No such run: {run_name}")

    files: List[Dict[str, Any]] = []
    for f in run_dir.iterdir():
        if not f.is_file():
            continue
        st = f.stat()
        files.append({"name": f.name, "size": st.st_size, "mtime": st.st_mtime})
    files.sort(key=lambda e: e["name"])
    return files


# ── Slug-scoped cascade helpers (v0.9.5) ─────────────────────────────────────

def _slug_dir_pattern(slug: str) -> "re.Pattern[str]":
    """
    Compile the exact regex that matches per-run log directory names
    produced by the server whose ``safe_server_name`` slug is ``slug``.

    Engine writes run dirs as ``run_<slug>_<YYYYMMDD>_<HHMMSS>``, then
    finalises them with a ``_PASS`` or ``_FAIL`` suffix (see
    services/exporter.py and services/importer.py). The pattern
    anchors on the leading ``run_<slug>_`` and accepts an optional
    finalisation suffix.
    """
    return re.compile(
        r"^run_" + re.escape(slug) + r"_\d{8}_\d{6}(?:_PASS|_FAIL)?$"
    )


def _iter_log_dirs_for_slug(slug: str) -> List[Path]:
    """Return every run directory attributable to ``slug``."""
    settings = load_settings()
    base = Path(settings.get("log_dir") or "./plex_logs")
    if not base.exists():
        return []
    pat = _slug_dir_pattern(slug)
    out: List[Path] = []
    for child in base.iterdir():
        if not child.is_dir():
            continue
        if pat.match(child.name):
            out.append(child)
    return out


def count_log_dirs_by_slug(slug: str) -> int:
    """How many run directories would a cascade delete of ``slug`` remove?"""
    return len(_iter_log_dirs_for_slug(slug))


def delete_log_dirs_by_slug(slug: str) -> Tuple[int, List[str]]:
    """
    Delete every run directory matching ``slug``. Returns
    ``(deleted_count, errors)``. Best-effort — a single failed
    rmtree does not stop the sweep.

    ``shutil.rmtree`` is recursive; the engine writes file handles
    into the run dir during a job, so a cascade attempted while a
    job is in flight may legitimately fail on the live run dir.
    That's fine — the error string surfaces in the cascade summary
    and the operator can retry once the job finishes.
    """
    deleted = 0
    errors: List[str] = []
    for d in _iter_log_dirs_for_slug(slug):
        try:
            shutil.rmtree(d)
            deleted += 1
        except OSError as e:
            errors.append(f"{d.name}: {type(e).__name__}: {e}")
    return deleted, errors


# ── Read ─────────────────────────────────────────────────────────────────────

# Cap reads at 4 MB so a runaway client request can't try to download
# a multi-gigabyte file in one shot. Files larger than this are
# truncated and the response carries ``truncated: true``.
_MAX_READ_BYTES = 4 * 1024 * 1024


def read_file(run_name: str, file_name: str, *, since: int = 0) -> Dict[str, Any]:
    """
    Return one log file's contents.

    Two modes:
      * ``since == 0`` — full read (subject to the 4 MB tail cap for huge
        files). Used for the initial paint of a file viewer.
      * ``since > 0`` — incremental tail read from byte offset ``since``
        to end of file (also capped). Used by the live-tail poll on the
        frontend: each tick passes the previous response's ``next_offset``
        so the client only receives newly-appended bytes.

    Returns ``{content, truncated, size, next_offset}``:
      * ``content``     — decoded text bytes from the requested window.
      * ``truncated``   — true if the file was larger than the cap and
                          older bytes were dropped (only meaningful when
                          ``since == 0``).
      * ``size``        — current total size of the file in bytes.
      * ``next_offset`` — byte offset to pass back as ``since`` on the
                          next poll. Always equal to ``size`` after a
                          successful read.
    """
    settings = load_settings()
    base = Path(settings.get("log_dir") or "./plex_logs")
    run_dir = _resolve_within(base, run_name)
    file_path = _resolve_within(run_dir, file_name)
    if not file_path.is_file():
        raise FileNotFoundError(f"No such file: {run_name}/{file_name}")

    size = file_path.stat().st_size
    truncated = False

    # Sanitise since: a stale offset bigger than the current file means
    # the file was rotated or truncated underneath us. Treat as 0.
    if since < 0 or since > size:
        since = 0

    if since > 0:
        # Incremental read — from offset to end, capped.
        readable = size - since
        with open(file_path, "rb") as fh:
            fh.seek(since)
            if readable > _MAX_READ_BYTES:
                blob = fh.read(_MAX_READ_BYTES)
                truncated = True
            else:
                blob = fh.read()
    elif size > _MAX_READ_BYTES:
        truncated = True
        with open(file_path, "rb") as fh:
            # Read the *tail* of the file rather than the head: when
            # something goes wrong the bottom of the log is what the
            # user wants to see first.
            fh.seek(-_MAX_READ_BYTES, 2)
            blob = fh.read()
    else:
        with open(file_path, "rb") as fh:
            blob = fh.read()

    # Decode permissively — log files are normally UTF-8 but the engine
    # logs media filenames straight through, and those can carry any
    # encoding the user's filesystem uses.
    content = blob.decode("utf-8", errors="replace")
    return {
        "content": content,
        "truncated": truncated,
        "size": size,
        "next_offset": since + len(blob) if since > 0 else size,
    }
