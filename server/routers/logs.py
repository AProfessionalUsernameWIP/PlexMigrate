"""Log browser + application-log routes.

Every ``/api/logs/*`` handler. Behaviour preserved verbatim.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from server import (
    auth_router as _auth_router_module,
    log_browser,
)


log = logging.getLogger("plexmigrate.server")

router = APIRouter(tags=["logs"])


# ── Log browser ──────────────────────────────────────────────────


@router.get("/api/logs")
def list_log_runs() -> List[Dict[str, Any]]:
    return log_browser.list_runs()


# ── Application-level log surfaces (Settings > Application Logs) ───
#
# The per-run job logs surfaced by /api/logs above are now grouped
# per-server under Servers > Logs. Settings > Logs keeps the
# application-level audit trail: db access, future auth events,
# future network events. Today only db_access.log writes outside
# of a run; the other categories are scaffolded so the UI is in
# place when their log writers ship.


@router.get("/api/logs/paths")
def list_log_paths(
    _admin: Dict[str, Any] = Depends(_auth_router_module.require_role("admin")),
) -> Dict[str, Any]:
    """Return the resolved on-disk path for every application-level
    log this build writes. Surfaced in Settings > Logging so the
    operator can find the file from a shell (e.g. ``tail -f
    <path>``) without having to navigate to each category in the
    Application Logs viewer.

    Keys mirror the categories on ``/api/logs/app/{category}``.
    Paths are resolved relative to ``server_data/`` (or whatever
    ``PLEXMIGRATE_DATA_DIR`` is set to at boot)."""
    from server.persistence import get_data_dir
    data_dir = get_data_dir()
    return {
        "data_dir": str(data_dir),
        "paths": {
            "app":            str(data_dir / "app.log"),
            "db-access":      str(data_dir / "db_access.log"),
            "playlist-cache": str(data_dir / "playlist_cache.log"),
            "sync":           str(data_dir / "sync.log"),
            "smart-playlist": str(data_dir / "smart_playlist.log"),
        },
    }


@router.get("/api/logs/app/{category}")
def read_app_log(
    category: str,
    tail_bytes: int = 0,
    since: int = 0,
    backup: str = "",
    _admin: Dict[str, Any] = Depends(_auth_router_module.require_role("admin")),
) -> Dict[str, Any]:
    """
    Read the application-level log file for ``category``. Two
    modes, selected by the presence of ``since``:

    * ``since=0`` (default): return the last ``tail_bytes`` of the
      file. Used by the panel's initial render and by Reload.
    * ``since=<offset>``: return bytes from ``offset`` to the
      file's current end. Used by the panel's 2-second poll for
      incremental live tail.

    Optional ``backup`` query parameter switches the read target
    from the active file to a specific rotated backup
    (``db_access.log.1`` etc.). When set, polling is meaningless
    (the file does not grow); the endpoint always reads the tail
    and ignores ``since``.

    Either mode returns:

    * ``content``        - the bytes read (UTF-8 decoded)
    * ``size_bytes``     - total size of the file on disk
    * ``next_offset``    - cursor for the next poll
    * ``head_omitted``   - True when the initial-tail path
                           returned only the last ``tail_bytes``
                           of a larger file. Older bytes are
                           still in the same file; bump
                           ``tail_bytes`` to fetch more. Distinct
                           from ``rotated_during_poll``: this is
                           normal-for-large-file, not a discontinuity.
    * ``rotated_during_poll`` - True when the polling cursor
                           (``since > 0``) landed past the file's
                           current end. Means logrotate fired
                           between polls; older bytes are GONE
                           from this file (look in the
                           ``backups`` list).
    * ``backups``        - list of rotated backup files for this
                           category, newest first. Each entry
                           carries ``filename`` and ``size_bytes``.
                           Empty when no rotation has fired yet.

    Categories:

    * ``db-access``: ``server_data/db_access.log`` (when no run is
      active; otherwise that file may be empty and the audit lines
      live inside the active run's log dir).
    * ``auth`` / ``network`` / ``debug``: scaffolded but no
      backing log writer ships today.

    Admin-gated. ``tail_bytes=0`` (default) resolves to the
    end user-tuned ``log_read_max_bytes`` setting (default 16 MB),
    so small log files load fully without the UI flashing a
    misleading "tail-only" banner. Explicit non-zero values
    clamp to [1024, log_read_max_bytes].
    """
    from pathlib import Path
    from server.persistence import get_data_dir, load_settings

    settings = load_settings() or {}
    # End user-tuned read cap (also used by the per-run log
    # viewer; default 16 MB). Same knob, same behaviour across
    # log surfaces.
    try:
        max_cap = max(1024, int(settings.get("log_read_max_bytes") or 16 * 1024 * 1024))
    except (TypeError, ValueError):
        max_cap = 16 * 1024 * 1024
    try:
        requested = int(tail_bytes) if tail_bytes else max_cap
        clamped = max(1024, min(requested, max_cap))
    except (TypeError, ValueError):
        clamped = max_cap
    try:
        since_offset = max(0, int(since))
    except (TypeError, ValueError):
        since_offset = 0

    category_to_filename: Dict[str, str] = {
        "db-access": "db_access.log",
        # Playlist-cache refresh audit trail. Written by
        # services.playlist_cache_log on every refresh attempt
        # (per-user + bulk-server).
        "playlist-cache": "playlist_cache.log",
        # Sync engine activity. Written by services.sync_log on
        # every sync_worker poll cycle + every sync-initiated
        # playlist copy. Lives in a separate file so it never
        # bleeds into a running job's runtime.log.
        "sync": "sync.log",
        # Every plexmigrate.* record at INFO+ level. Written by
        # the persistent RotatingFileHandler attached to the
        # "plexmigrate" root logger at app startup (see
        # _install_app_log_handler). Catches the user-capture
        # diagnostics, owner-mirror decisions, refresh-users
        # traces, and every other server-level event the per-run
        # logs don't cover.
        "app": "app.log",
        # User-activity sweeper audit. One file across all
        # servers; the LogsPanel's per-server sub-tab filters
        # by the server tag in each line client-side.
        "user-activity": "user_activity.log",
        # Smart Playlist Migration job activity - filter decode,
        # id->name translation, preflight warnings, re-create
        # outcomes. Written by services.smart_playlist_log; the
        # migration panel's in-panel live log reads this category.
        "smart-playlist": "smart_playlist.log",
    }
    placeholder_categories = ("auth", "network", "debug")

    if category in placeholder_categories:
        return {
            "category": category,
            "path": "",
            "size_bytes": 0,
            "next_offset": 0,
            "head_omitted": False,
            "rotated_during_poll": False,
            "content": "",
            "backups": [],
            "note": (
                f"The '{category}' application-log category is scaffolded "
                "but no writer ships in this build. Operators who need "
                "this surface filed should request the matching log "
                "writer in a follow-up scope."
            ),
        }

    filename = category_to_filename.get(category)
    if filename is None:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown application-log category {category!r}.",
        )

    # Build the rotated-backup list. RotatingFileHandler names them
    # ``<base>.1``, ``<base>.2``, ... with .1 being the most recent
    # rotation. Sort by the numeric suffix so the picker shows
    # newest first.
    data_dir = get_data_dir()
    backups_list: List[Dict[str, Any]] = []
    try:
        for child in data_dir.iterdir():
            if not child.is_file():
                continue
            name = child.name
            if not name.startswith(filename + "."):
                continue
            suffix = name[len(filename) + 1:]
            if not suffix.isdigit():
                continue
            try:
                sz = child.stat().st_size
            except OSError:
                sz = 0
            backups_list.append({
                "filename": name,
                "size_bytes": int(sz),
                "suffix": int(suffix),
            })
    except OSError:
        pass
    backups_list.sort(key=lambda b: b["suffix"])
    # Strip the internal sort key before returning.
    backups_response = [
        {"filename": b["filename"], "size_bytes": b["size_bytes"]}
        for b in backups_list
    ]

    # Resolve the read target. If ``backup`` is set, point at that
    # specific rotated file; otherwise the active log. Bounds-check
    # the backup name against the allowlist we just built so the
    # end user can't slip a path traversal through the parameter.
    if backup:
        allowed = {b["filename"] for b in backups_list}
        if backup not in allowed:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"Backup {backup!r} not found for category "
                    f"{category!r}."
                ),
            )
        path = data_dir / backup
        # Polling is meaningless on an inactive backup; reset
        # since_offset so the read always returns the tail.
        since_offset = 0
    else:
        path = data_dir / filename

    if not path.is_file():
        return {
            "category": category,
            "path": str(path),
            "size_bytes": 0,
            "next_offset": 0,
            "head_omitted": False,
            "rotated_during_poll": False,
            "content": "",
            "backups": backups_response,
            "note": "Log file has not been written yet on this host.",
        }
    try:
        size = path.stat().st_size
    except OSError:
        size = 0

    head_omitted = False
    rotated_during_poll = False
    try:
        with path.open("rb") as fh:
            if since_offset > 0:
                if since_offset > size:
                    # File shrank between polls = rotation fired.
                    # Restart from the tail and flag the
                    # discontinuity so the UI shows the right
                    # banner.
                    rotated_during_poll = True
                    start = max(0, size - clamped)
                    fh.seek(start)
                    if start > 0:
                        fh.readline()
                else:
                    fh.seek(since_offset)
            else:
                # Initial / Reload path. Show only the tail when
                # the file is bigger than the read cap; flag
                # head_omitted so the UI can label what the
                # end user is seeing without conflating it with
                # an actual rotation.
                if size > clamped:
                    fh.seek(size - clamped)
                    fh.readline()
                    head_omitted = True
            content = fh.read().decode("utf-8", errors="replace")
    except OSError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Could not read application log: {exc}",
        )
    return {
        "category": category,
        "path": str(path),
        "size_bytes": int(size),
        "next_offset": int(size),
        "head_omitted": head_omitted,
        "rotated_during_poll": rotated_during_poll,
        "content": content,
        "backups": backups_response,
        "note": "",
    }


@router.get("/api/logs/{run_name}")
def list_log_files(
    run_name: str,
    _user: Dict[str, Any] = Depends(_auth_router_module.require_role("viewer")),
) -> List[Dict[str, Any]]:
    try:
        return log_browser.list_files(run_name)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/api/logs/{run_name}/{file_name}/download")
def download_log_file(run_name: str, file_name: str) -> FileResponse:
    """
    Stream a log file as a regular HTTP attachment. Bypasses the
    in-browser viewer's 16 MB live-tail cap so an end user can
    grab the full content of a huge log on demand. Path traversal
    is rejected by ``log_browser.resolve_file_path``.
    """
    try:
        path = log_browser.resolve_file_path(run_name, file_name)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return FileResponse(
        path=str(path),
        media_type="text/plain",
        filename=path.name,
    )


# NOTE: this route is intentionally declared BEFORE the
# ``/api/logs/{run_name}/{file_name}`` reader below so FastAPI's
# in-order matching catches the literal ``zip`` suffix first. A
# log file literally named ``zip`` would otherwise be unreachable
# via the zip endpoint; in practice the engine never produces
# one, but order-of-declaration is the cheap insurance.
@router.get("/api/logs/{run_name}/zip")
def download_log_run_zip(run_name: str) -> FileResponse:
    """
    Bundle every file in one run directory into a ZIP and stream
    it back as an attachment. The temp zip on disk is removed via
    a BackgroundTask once the response finishes (success or not).
    """
    try:
        zip_path, filename = log_browser.build_run_zip(run_name)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    def _cleanup(p: str) -> None:
        try:
            os.unlink(p)
        except OSError:
            pass

    return FileResponse(
        path=str(zip_path),
        media_type="application/zip",
        filename=filename,
        background=BackgroundTask(_cleanup, str(zip_path)),
    )


@router.get("/api/logs/{run_name}/{file_name}")
def read_log_file(
    run_name: str, file_name: str, since: int = 0,
    _user: Dict[str, Any] = Depends(_auth_router_module.require_role("viewer")),
) -> Dict[str, Any]:
    """
    Read a log file's contents. Pass ``?since=<byte-offset>`` to
    fetch only the bytes appended since the previous read - used
    by the frontend's live-tail poll. Omit (or ``since=0``) for
    a full read.
    """
    try:
        return log_browser.read_file(run_name, file_name, since=since)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/api/logs/{run_name}")
def delete_log_run(
    run_name: str,
    _admin: Dict[str, Any] = Depends(
        _auth_router_module.require_role("admin")
    ),
) -> Dict[str, Any]:
    """
    Permanently remove one run directory. Returns the number of
    files removed and a list of best-effort errors (e.g. a file
    locked by a live engine job).
    """
    try:
        file_count, errors = log_browser.delete_run(run_name)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"deleted": run_name, "file_count": file_count, "errors": errors}


@router.delete("/api/logs")
def delete_all_log_runs(
    _admin: Dict[str, Any] = Depends(
        _auth_router_module.require_role("admin")
    ),
) -> Dict[str, Any]:
    """
    Wipe every run directory under the configured log dir. Returns
    ``{deleted, errors}``. Per-directory errors do not abort the
    sweep; the end user can retry to mop up anything that was
    locked at the time.
    """
    deleted, errors = log_browser.delete_all_runs()
    return {"deleted": deleted, "errors": errors}
