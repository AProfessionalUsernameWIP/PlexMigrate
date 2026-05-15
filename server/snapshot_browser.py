"""
Read-only browser over the ``snapshots/`` directory.

Lists every ``*.plexexport.json`` produced by the snapshot pipeline so
the frontend can show them in a table and let the user download or
re-import them.

The contents of each file are NOT read on listing - we only parse the
``library`` and ``captured_at`` keys for the index, and that requires
reading the first few KB. ``.plexexport.json`` files can be hundreds
of MB on large libraries; reading them all on every list request
would make the page slow.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from server.persistence import load_settings


# How many bytes to read from the start of a export file when building
# the index. The two metadata keys we care about appear within the
# first few KB; reading more is wasteful.
_HEAD_PEEK_BYTES = 8 * 1024


def _resolve_within(base: Path, relative: str) -> Path:
    """Path containment guard - same idea as in :mod:`server.log_browser`."""
    candidate = (base / relative).resolve()
    base_resolved = base.resolve()
    try:
        candidate.relative_to(base_resolved)
    except ValueError as e:
        raise ValueError(f"Path {relative!r} escapes snapshot directory {base!s}") from e
    return candidate


def _archive_dir() -> Path:
    """
    Resolve the JSON-archive directory: ``<output_dir>/legacy/``.

    Standalone JSON archives (operator-kept after deleting a
    snapshot, or pre-PR-13 archives relocated by
    ``relocate_legacy_exports``) live here. The directory is named
    ``legacy/`` for historical reasons; the UI labels it "JSON
    Archives" and the JobForm exposes it as "From JSON archive".

    Distinct from ``<output_dir>`` top-level which holds *active*
    cached sidecars next to their .db files - those belong to the
    snapshot registry and are listed via ``/api/snapshots``.
    """
    settings = load_settings()
    return Path(settings.get("output_dir") or "./snapshots") / "legacy"


def list_snapshots() -> List[Dict[str, Any]]:
    """
    Return one entry per .plexexport.json in the JSON-archive
    directory (``<output_dir>/legacy/``). Newest first.

    Each entry: ``name`` (filename), ``size`` (bytes), ``mtime``
    (UNIX timestamp), ``library`` (parsed from file metadata), and
    ``captured_at`` (parsed from file metadata, ISO 8601 string).

    Pre-fix this read the top of ``output_dir`` which mixed active
    cached sidecars (registered to live .db files) with genuine
    archives. The Exports panel now distinguishes the two: registered
    snapshots come from ``/api/snapshots``; archived JSON files come
    from here.
    """
    base = _archive_dir()
    if not base.exists():
        return []

    items: List[Dict[str, Any]] = []
    for f in base.iterdir():
        if not f.is_file() or not f.name.endswith((".plexexport.json", ".plexbackup.json")):
            continue
        st = f.stat()
        meta = _peek_metadata(f)
        items.append({
            "name": f.name,
            "size": st.st_size,
            "mtime": st.st_mtime,
            "library": meta.get("library"),
            "captured_at": meta.get("captured_at"),
            "source_server": meta.get("source_server_name"),
            "source_server_url": meta.get("source_server_url"),
            "trigger": meta.get("trigger"),
            "schedule_name": meta.get("schedule_name"),
        })
    items.sort(key=lambda e: e["mtime"], reverse=True)
    return items


def snapshot_path(file_name: str) -> Path:
    """
    Return the absolute path of one archived snapshot file, after the
    containment check. Used by the archive-download endpoint.
    """
    base = _archive_dir()
    p = _resolve_within(base, file_name)
    if not p.is_file():
        raise FileNotFoundError(f"No such snapshot: {file_name}")
    return p


def delete_snapshot(file_name: str) -> None:
    """
    Delete one archived ``.plexexport.json`` file from
    ``<output_dir>/legacy/``.

    Goes through :func:`_resolve_within` so a crafted ``file_name``
    (``../etc/passwd`` etc.) can't escape the archive root. Raises
    :class:`FileNotFoundError` if the target doesn't exist and
    :class:`ValueError` on containment failure - the route layer
    maps those to 404 / 400 respectively. The ``.plexexport.json``
    suffix is enforced here so this endpoint can't be repurposed
    to wipe arbitrary files.
    """
    if not file_name.endswith((".plexexport.json", ".plexbackup.json")):
        raise ValueError(f"Refusing to delete non-export file: {file_name!r}")
    base = _archive_dir()
    p = _resolve_within(base, file_name)
    if not p.is_file():
        raise FileNotFoundError(f"No such snapshot: {file_name}")
    p.unlink()


def delete_all_archives() -> Dict[str, Any]:
    """
    Best-effort wipe of every ``.plexexport.json`` archive in
    ``<output_dir>/legacy/``. Returns ``{deleted, errors}``.

    Non-archive files in the same directory are left alone so an
    operator who has dropped something else in there doesn't lose it
    to a blanket clear-all. The route-level db_admin gate keeps this
    behind explicit consent.
    """
    base = _archive_dir()
    deleted = 0
    errors: List[str] = []
    if not base.exists():
        return {"deleted": 0, "errors": []}
    for f in list(base.iterdir()):
        try:
            if not f.is_file() or not f.name.endswith((".plexexport.json", ".plexbackup.json")):
                continue
        except OSError:
            continue
        try:
            f.unlink()
            deleted += 1
        except OSError as e:
            errors.append(f"{f.name}: {type(e).__name__}: {e}")
    return {"deleted": deleted, "errors": errors}


# ── Helpers ──────────────────────────────────────────────────────────────────

def _peek_metadata(path: Path) -> Dict[str, Optional[str]]:
    """
    Read only the first ``_HEAD_PEEK_BYTES`` of a export file and
    parse the small handful of top-level string fields the UI needs.

    Returns a dict with keys ``library``, ``captured_at``,
    ``source_server_name``, ``source_server_url``. Any field that
    can't be extracted (older export pre-dating the field, or
    unparseable file) maps to ``None``.

    Two-strategy approach: first try to parse the whole prefix as
    valid JSON (works for small files), then fall back to a tiny
    string-search scanner that picks each field out of the indented
    pretty-printed prefix.
    """
    out: Dict[str, Optional[str]] = {
        "library": None,
        "captured_at": None,
        "source_server_name": None,
        "source_server_url": None,
        "trigger": None,
        "schedule_name": None,
    }
    try:
        with open(path, "rb") as fh:
            head = fh.read(_HEAD_PEEK_BYTES)
    except OSError:
        return out

    # Strategy 1: the whole file fits in the peek window.
    try:
        data = json.loads(head.decode("utf-8", errors="replace"))
        if isinstance(data, dict):
            out["library"] = data.get("library")
            # PR-13 compat: legacy files used ``exported_at``; new files
            # use ``captured_at``. Accept either when reading so
            # pre-rename .plexexport.json files remain browseable.
            out["captured_at"] = data.get("captured_at") or data.get("exported_at")
            out["source_server_name"] = data.get("source_server_name")
            out["source_server_url"] = data.get("source_server_url")
            out["trigger"] = data.get("trigger")
            out["schedule_name"] = data.get("schedule_name")
            return out
    except json.JSONDecodeError:
        pass

    # Strategy 2: scan the prefix for the top-level fields. The
    # snapshotter writes JSON with 2-space indent (services/snapshotter.py),
    # so the top-level keys appear at the start of a line and the
    # values are easy to extract with a tiny string search.
    text = head.decode("utf-8", errors="replace")
    out["library"] = _extract_top_level_string(text, "library")
    # PR-13 compat: try the new key first, fall back to legacy.
    out["captured_at"] = (
        _extract_top_level_string(text, "captured_at")
        or _extract_top_level_string(text, "exported_at")
    )
    out["source_server_name"] = _extract_top_level_string(text, "source_server_name")
    out["source_server_url"] = _extract_top_level_string(text, "source_server_url")
    out["trigger"] = _extract_top_level_string(text, "trigger")
    out["schedule_name"] = _extract_top_level_string(text, "schedule_name")
    return out


# ── Slug-scoped cascade helpers (v0.9.5) ─────────────────────────────────────

def _slug_pattern(slug: str) -> "re.Pattern[str]":
    """
    Compile the exact-position regex that matches snapshot filenames
    produced by the server whose ``safe_server_name`` slug is ``slug``.

    Snapshotter filename shape (services/snapshotter.py):

        ``<library>_<slug>_<YYYYMMDD>_<HHMMSS>.plexexport.json``

    The slug always appears between an underscore and the run
    timestamp, so the pattern anchors on ``_<slug>_<8 digits>_<6 digits>``
    immediately preceding ``.plexexport.json``. This avoids false
    positives if the slug substring happens to appear inside a
    library name (e.g. a library literally named "My-Server" with a
    different server slug).

    Pre-v0.9.0 exports carry no slug at all (``<library>_<ts>``);
    those never match this pattern by design - they aren't
    attributable to any specific registered server.
    """
    return re.compile(
        r"_" + re.escape(slug) + r"_\d{8}_\d{6}\.plexexport\.json$"
    )


def _iter_exports_for_slug(slug: str) -> List[Path]:
    """Return every snapshot file path attributable to ``slug``."""
    settings = load_settings()
    base = Path(settings.get("output_dir") or "./snapshots")
    if not base.exists():
        return []
    pat = _slug_pattern(slug)
    out: List[Path] = []
    for f in base.iterdir():
        if not f.is_file() or not f.name.endswith((".plexexport.json", ".plexbackup.json")):
            continue
        if pat.search(f.name):
            out.append(f)
    return out


def count_exports_by_slug(slug: str) -> int:
    """How many snapshot files would a cascade delete of ``slug`` remove?"""
    return len(_iter_exports_for_slug(slug))


def delete_exports_by_slug(slug: str) -> Tuple[int, List[str]]:
    """
    Delete every snapshot file matching ``slug``. Returns
    ``(deleted_count, errors)`` where ``errors`` is a list of
    human-readable strings - one per file we tried to delete and
    couldn't. Best-effort: a failure on one file does not stop the
    sweep.
    """
    deleted = 0
    errors: List[str] = []
    for p in _iter_exports_for_slug(slug):
        try:
            p.unlink()
            deleted += 1
        except OSError as e:
            errors.append(f"{p.name}: {type(e).__name__}: {e}")
    return deleted, errors


def _extract_top_level_string(text: str, key: str) -> Optional[str]:
    """
    Pull a top-level string value out of indented pretty-printed JSON.

    Looks for ``"key": "<value>"`` and returns the unescaped value.
    Tolerant of indentation and trailing commas. Not a full JSON parser
    - only handles the simple case the snapshotter actually writes.
    """
    needle = f'"{key}"'
    idx = text.find(needle)
    if idx < 0:
        return None
    # Walk past ': "' to the opening quote of the value.
    colon = text.find(":", idx + len(needle))
    if colon < 0:
        return None
    open_quote = text.find('"', colon + 1)
    if open_quote < 0:
        return None
    # L4: find the *unescaped* closing quote. A bare
    # ``text.find('"', ...)`` stops at the first quote even when it is
    # a ``\"`` escape inside the value, truncating any value that
    # contains a quote (e.g. a library name with a double-quote in it).
    # Walk the string honouring backslash escapes instead.
    i = open_quote + 1
    n = len(text)
    close_quote = -1
    while i < n:
        c = text[i]
        if c == "\\":
            i += 2  # skip the escaped char (\\ or \")
            continue
        if c == '"':
            close_quote = i
            break
        i += 1
    if close_quote < 0:
        return None
    # Basic unescape: only handle \" and \\, which are the only
    # escapes json.dump produces by default on ASCII strings.
    return text[open_quote + 1:close_quote].replace('\\"', '"').replace("\\\\", "\\")
