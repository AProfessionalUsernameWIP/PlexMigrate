"""
Read-only browser over the engine's log directories.

The engine writes per-run logs into ``$log_dir/<timestamp>[_PASS|_FAIL]/``.
Each directory contains:

  * ``run_*.log``                 - the full run transcript
  * ``<Library>_success_*.log``   - per-library success log (if any)
  * ``<Library>_fail_*.log``      - per-library failure log (if any)
  * ``troubleshoot_*.log``        - categorised failures + fix steps
  * ``unresolved_*.log``          - items that failed all tiers
  * ``errors.log``                - emitted when something fatal happened

This module exposes the directory tree to the frontend without
duplicating any engine logic: it lists, it reads, it never writes.

All path inputs are validated against the configured log directory so
a client cannot ``../../etc/passwd`` out of the sandbox.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
import zipfile
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


def _resolve_run_dir(base: Path, run_name: str) -> Path:
    """
    Resolve a run directory by name, tolerating the ``_PASS`` / ``_FAIL``
    suffix that :func:`_finalise_run_dir` appends at the end of a job.

    The frontend's live log tail opens a stream at the run's *original*
    name (``run_<ts>``); when the job finishes the engine renames the
    directory to ``run_<ts>_PASS`` / ``_FAIL`` and the next poll would
    404. We try the literal name first, then each suffix variant, so
    the tail keeps resolving across the boundary.

    Returns the resolved directory path. Caller still applies the
    containment guard via :func:`_resolve_within`. Raises
    :class:`FileNotFoundError` when none of the three candidates exist.
    """
    for candidate_name in (run_name, f"{run_name}_PASS", f"{run_name}_FAIL"):
        # Belt-and-suspenders: even though we synthesise the suffixes
        # ourselves, run every candidate through the containment guard
        # so a crafted run_name that includes ``..`` can't sneak past.
        candidate = _resolve_within(base, candidate_name)
        if candidate.is_dir():
            return candidate
    raise FileNotFoundError(f"No such run: {run_name}")


# ── Listing ──────────────────────────────────────────────────────────────────

_RUN_DIR_RE = re.compile(
    # Engine writes run dirs as ``run_<slug>_<YYYYMMDD>_<HHMMSS>`` and
    # appends ``_PASS`` / ``_FAIL`` on finalisation. <slug> may contain
    # underscores (combined fan-out slugs include hyphens but no
    # underscores between the slug and the timestamp), so we anchor on
    # the trailing date / time tail to extract the slug greedily.
    r"^run_(?P<slug>.+?)_(?P<date>\d{8})_(?P<time>\d{6})(?:_PASS|_FAIL)?$"
)


def _extract_server_slug(run_dir_name: str) -> "str | None":
    """
    Pull the server slug out of a per-run directory name. Returns
    None when the name does not match the engine's run-dir format
    (e.g. an unrelated directory dropped into ``log_dir/`` by hand).

    Slugs may legitimately encode multi-server combined transfers as
    ``<src>-to-<dst>`` (see jobs._run_direct's combined_slug); those
    are returned verbatim so the per-server UI can decide whether to
    surface them under both source and dest.
    """
    m = _RUN_DIR_RE.match(run_dir_name)
    return m.group("slug") if m else None


def list_runs() -> List[Dict[str, Any]]:
    """
    Return the per-run subdirectories under the configured log dir,
    newest first. Each entry has:

      * ``name``         - directory name (e.g. ``run_Plex1_20260510_135425_PASS``)
      * ``mtime``        - modification time UNIX timestamp
      * ``size``         - total bytes across all files in the run
      * ``passed``       - True/False/None for the *_PASS / *_FAIL / no-suffix cases
      * ``file_count``   - number of regular files in the run dir
      * ``server_slug``  - extracted from the directory name, or None
      * ``server_id``    - reverse-mapped to a registered server by
                           comparing the slug to every server's
                           safe_server_name. None when no live server
                           matches (server removed since the run wrote).
      * ``server_name``  - friendly name of that server when matched.
    """
    settings = load_settings()
    base = Path(settings.get("log_dir") or "./plex_logs")
    if not base.exists():
        return []

    # Build the slug -> (server_id, server_name) reverse map once.
    # Done inside list_runs (not at module level) so the map refreshes
    # on every call; the end user may add or remove servers between
    # log-list refreshes.
    slug_to_server: Dict[str, Dict[str, str]] = {}
    try:
        from server.server_registry import list_servers, backend_aware_slug
        for sv in list_servers():
            service_type = (sv.get("service_type") or "plex").lower()
            slug = backend_aware_slug(sv.get("name") or "", service_type)
            if slug:
                slug_to_server[slug] = {
                    "server_id": sv.get("id") or "",
                    "server_name": sv.get("name") or "",
                    # TODO: ship service_type on the log-list row so the
                    # Logs panel's backend-tier filter can join via
                    # the response shape instead of an extra
                    # registry round-trip per row.
                    "service_type": service_type,
                }
    except Exception:
        # Registry unavailable; degrade gracefully. The per-run
        # entries still carry server_slug; the frontend can fall back
        # to "(unknown server)" grouping if needed.
        pass

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
        slug = _extract_server_slug(name)
        match = slug_to_server.get(slug or "") if slug else None
        entries.append({
            "name": name,
            "mtime": child.stat().st_mtime,
            "size": total,
            "passed": passed,
            "file_count": len(files),
            "server_slug": slug,
            "server_id": (match or {}).get("server_id") or None,
            "server_name": (match or {}).get("server_name") or None,
            # TODO: backend discriminator so the Logs panel
            # can filter without joining through the registry per row.
            # ``None`` when the slug doesn't resolve (server removed
            # or run from a since-deleted registration); the frontend
            # treats null as the legacy "Plex" bucket.
            "service_type": (match or {}).get("service_type") or None,
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
    # Tolerate the post-finalize rename so the live-tail keeps
    # resolving when the engine appends ``_PASS`` / ``_FAIL``.
    run_dir = _resolve_run_dir(base, run_name)

    files: List[Dict[str, Any]] = []
    for f in run_dir.iterdir():
        if not f.is_file():
            continue
        st = f.stat()
        files.append({"name": f.name, "size": st.st_size, "mtime": st.st_mtime})
    files.sort(key=lambda e: e["name"])
    return files


# ── Slug-scoped cascade helpers ──────────────────────────────────────────────

def _slug_dir_pattern(slug: str) -> "re.Pattern[str]":
    """
    Compile the exact regex that matches per-run log directory names
    produced by the server whose ``safe_server_name`` slug is ``slug``.

    Engine writes run dirs as ``run_<slug>_<YYYYMMDD>_<HHMMSS>``, then
    finalises them with a ``_PASS`` or ``_FAIL`` suffix (see
    services/snapshotter.py and services/importer.py). The pattern
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
    ``(deleted_count, errors)``. Best-effort - a single failed
    rmtree does not stop the sweep.

    ``shutil.rmtree`` is recursive; the engine writes file handles
    into the run dir during a job, so a cascade attempted while a
    job is in flight may legitimately fail on the live run dir.
    That's fine - the error string surfaces in the cascade summary
    and the end user can retry once the job finishes.
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


# ── Delete ───────────────────────────────────────────────────────────────────

def delete_run(run_name: str) -> Tuple[int, List[str]]:
    """
    Permanently remove one run directory. Returns
    ``(file_count_removed, errors)``. Best-effort: a single failed
    ``rmtree`` (e.g. a live job is still writing to the directory)
    surfaces in ``errors`` rather than raising.

    Raises ``FileNotFoundError`` if the run does not exist, and
    ``ValueError`` if the supplied name escapes the configured log
    directory (path-traversal guard, same as the read helpers).
    """
    settings = load_settings()
    base = Path(settings.get("log_dir") or "./plex_logs")
    # Tolerate the post-finalize ``_PASS`` / ``_FAIL`` rename.
    run_dir = _resolve_run_dir(base, run_name)
    # os.walk(followlinks=False) so a symlink planted inside the run
    # dir doesn't make the count reach outside it. shutil.rmtree below
    # already unlinks symlinks rather than recursing through them.
    file_count = sum(
        len(files) for _root, _dirs, files in os.walk(run_dir, followlinks=False)
    )
    errors: List[str] = []
    try:
        shutil.rmtree(run_dir)
    except OSError as e:
        errors.append(f"{type(e).__name__}: {e}")
    return file_count, errors


def build_run_zip(run_name: str) -> Tuple[Path, str]:
    """
    Bundle an entire run directory into a temporary ZIP and return
    ``(temp_zip_path, suggested_download_filename)``. Caller is
    responsible for deleting the temp file once the response has been
    streamed (the route handler attaches a BackgroundTask).

    Compression is ``ZIP_DEFLATED`` so a typical text-log run dir
    shrinks ~5-10x on the wire. Path-traversal guard reuses the
    standard :func:`_resolve_within` helper.

    Raises ``FileNotFoundError`` if the run does not exist and
    ``ValueError`` if the name escapes the configured log directory.
    """
    settings = load_settings()
    base = Path(settings.get("log_dir") or "./plex_logs")
    # Tolerate the post-finalize ``_PASS`` / ``_FAIL`` rename.
    run_dir = _resolve_run_dir(base, run_name)

    # mkstemp gives us a real path on disk we can hand to FileResponse;
    # NamedTemporaryFile would close-delete it under Windows the moment
    # the handle goes out of scope.
    fd, tmp_path = tempfile.mkstemp(prefix=f"{run_name}_", suffix=".zip")
    os.close(fd)
    tmp = Path(tmp_path)
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
            # os.walk(followlinks=False) so a symlinked subdir planted
            # inside the run dir can't pull external files into the ZIP.
            for _root, _dirs, _files in os.walk(run_dir, followlinks=False):
                for _name in _files:
                    f = Path(_root) / _name
                    if not f.is_file():
                        continue
                    # Put files under a top-level folder named after the
                    # run so unzipping into a downloads folder doesn't
                    # splatter loose log files everywhere.
                    arcname = Path(run_name) / f.relative_to(run_dir)
                    zf.write(f, arcname.as_posix())
    except Exception:
        # Best-effort cleanup so a half-written zip doesn't leak into
        # the temp dir when something goes wrong mid-build.
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    return tmp, f"{run_name}.zip"


def resolve_file_path(run_name: str, file_name: str) -> Path:
    """
    Path-traversal-checked resolution of one log file. Returns the
    absolute path on success; raises ``FileNotFoundError`` if missing
    or ``ValueError`` if the supplied name escapes the configured log
    directory. Used by the download endpoint to hand the raw file to
    a ``FileResponse`` (which streams the full content unchunked,
    bypassing the in-browser viewer's read cap).
    """
    settings = load_settings()
    base = Path(settings.get("log_dir") or "./plex_logs")
    # Tolerate the post-finalize ``_PASS`` / ``_FAIL`` rename.
    run_dir = _resolve_run_dir(base, run_name)
    file_path = _resolve_within(run_dir, file_name)
    if not file_path.is_file():
        raise FileNotFoundError(f"No such file: {run_name}/{file_name}")
    return file_path


def delete_all_runs() -> Tuple[int, List[str]]:
    """
    Permanently remove every run directory under the configured log
    dir. Returns ``(deleted_count, errors)``. Best-effort: failures
    on individual directories are collected but do not stop the
    sweep. Non-directory children are left alone.
    """
    settings = load_settings()
    base = Path(settings.get("log_dir") or "./plex_logs")
    if not base.exists():
        return 0, []
    deleted = 0
    errors: List[str] = []
    for child in base.iterdir():
        if not child.is_dir():
            continue
        try:
            shutil.rmtree(child)
            deleted += 1
        except OSError as e:
            errors.append(f"{child.name}: {type(e).__name__}: {e}")
    return deleted, errors


# ── Read ─────────────────────────────────────────────────────────────────────

# Cap *live-tail* reads at 16 MB so a runaway client request can't
# try to hydrate a multi-gigabyte file into the browser in one shot.
# Files larger than this fall back to a tail-of-last-16-MB view in
# the in-browser viewer; the end user can still fetch the complete
# file via :func:`open_for_download` (which the
# ``GET /api/logs/{run}/{file}/download`` endpoint serves as a
# regular HTTP attachment, bypassing this cap).
#
# 16 MB covers the vast majority of runs in the viewer without
# forcing the end user to download; a smaller cap such as 4 MB is too
# aggressive given typical snapshot-run log sizes.
#
# The cap is read from ``services.tunables.log_read_max_bytes`` at
# each read so an end user bumping it via Settings ▸ Tunables takes
# effect on the next request. The constant below is the fallback.
_MAX_READ_BYTES_FALLBACK = 16 * 1024 * 1024


def _max_read_bytes() -> int:
    try:
        from services.tunables import log_read_max_bytes
        return int(log_read_max_bytes())
    except Exception:
        return _MAX_READ_BYTES_FALLBACK


def read_file(run_name: str, file_name: str, *, since: int = 0) -> Dict[str, Any]:
    """
    Return one log file's contents.

    Two modes:
      * ``since == 0`` - full read (subject to the 4 MB tail cap for huge
        files). Used for the initial paint of a file viewer.
      * ``since > 0`` - incremental tail read from byte offset ``since``
        to end of file (also capped). Used by the live-tail poll on the
        frontend: each tick passes the previous response's ``next_offset``
        so the client only receives newly-appended bytes.

    Returns ``{content, truncated, size, next_offset}``:
      * ``content``     - decoded text bytes from the requested window.
      * ``truncated``   - true if the file was larger than the cap and
                          older bytes were dropped (only meaningful when
                          ``since == 0``).
      * ``size``        - current total size of the file in bytes.
      * ``next_offset`` - byte offset to pass back as ``since`` on the
                          next poll. Always equal to ``size`` after a
                          successful read.
    """
    settings = load_settings()
    base = Path(settings.get("log_dir") or "./plex_logs")
    # Tolerate the post-finalize ``_PASS`` / ``_FAIL`` rename so a
    # live-tail poll that started mid-run keeps resolving once the
    # engine finishes and the directory gets the suffix.
    run_dir = _resolve_run_dir(base, run_name)
    file_path = _resolve_within(run_dir, file_name)
    if not file_path.is_file():
        raise FileNotFoundError(f"No such file: {run_name}/{file_name}")

    size = file_path.stat().st_size
    truncated = False

    # Sanitise since: a stale offset bigger than the current file means
    # the file was rotated or truncated underneath us. Treat as 0.
    if since < 0 or since > size:
        since = 0

    max_bytes = _max_read_bytes()
    if since > 0:
        # Incremental read - from offset to end, capped.
        readable = size - since
        with open(file_path, "rb") as fh:
            fh.seek(since)
            if readable > max_bytes:
                blob = fh.read(max_bytes)
                truncated = True
            else:
                blob = fh.read()
    elif size > max_bytes:
        truncated = True
        with open(file_path, "rb") as fh:
            # Read the *tail* of the file rather than the head: when
            # something goes wrong the bottom of the log is what the
            # user wants to see first.
            fh.seek(-max_bytes, 2)
            blob = fh.read()
    else:
        with open(file_path, "rb") as fh:
            blob = fh.read()

    # Decode permissively - log files are normally UTF-8 but the engine
    # logs media filenames straight through, and those can carry any
    # encoding the user's filesystem uses.
    content = blob.decode("utf-8", errors="replace")
    return {
        "content": content,
        "truncated": truncated,
        "size": size,
        "next_offset": since + len(blob) if since > 0 else size,
    }
