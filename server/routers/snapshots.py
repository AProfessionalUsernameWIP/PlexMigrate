"""Snapshot registry routes.

Every ``/api/snapshots/*`` handler, including the legacy-archive
endpoints and the per-snapshot library-counts helper that powers
the restore-from-snapshot mapping panel. Behaviour preserved
verbatim from the prior in-app.py definitions.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, JSONResponse

from server import (
    auth_router as _auth_router_module,
    snapshot_browser,
)


log = logging.getLogger("plexmigrate.server")

router = APIRouter(tags=["snapshots"])


# ── Snapshot library-counts (used by restore-mapping panel) ──────


@router.get("/api/snapshots/{snapshot_id}/library-counts")
def get_snapshot_library_counts(
    snapshot_id: str,
    _user: Dict[str, Any] = Depends(
        _auth_router_module.require_role("viewer")
    ),
) -> Dict[str, Any]:
    """For restore-from-snapshot mode, the per-run mapping panel
    needs per-library counts on the source side. The snapshot.db
    has every per-server row tagged with ``section_key`` so we
    can aggregate:

      - server_items grouped by section_key → top-level item
        counts per library (movies, shows, artists, episodes,
        tracks — depending on what the snapshot captured).
      - watch_events / ratings / playlists / collections
        grouped by section_key → per-library coverage.

    Returns:
      {
        "libraries": [
          {
            "section_key": int,
            "section_title": str,
            "section_type": str,
            "item_count": int,
            "watch_events": int,
            "ratings": int,
            "playlists": int,
            "collections": int,
          },
          ...
        ]
      }

    Read-only; opens the snapshot.db read-only and queries it
    per request. The snapshot.db is small relative to media.db
    (one server's data, no triggers) so the per-request open is
    cheap. Files that no longer exist on disk return an empty
    libraries array (the registry's ``available=false`` flag is
    the operator-facing signal for that case)."""
    import sqlite3 as _sq3
    from server import snapshot_registry
    rec = snapshot_registry.get(snapshot_id)
    if rec is None:
        raise HTTPException(
            status_code=404,
            detail=f"No snapshot with id {snapshot_id!r}.",
        )
    file_path = str(rec.get("file_path") or "")
    if not file_path:
        return {"libraries": []}
    try:
        db_path = Path(file_path)
    except Exception:
        return {"libraries": []}
    if not db_path.exists():
        return {"libraries": []}
    out_libs: List[Dict[str, Any]] = []
    try:
        uri = f"file:{db_path.as_posix()}?mode=ro"
        conn = _sq3.connect(uri, uri=True, timeout=10.0)
        conn.row_factory = _sq3.Row
    except Exception as exc:
        log.warning(
            "snapshot library-counts: open %s failed: %s",
            db_path, exc,
        )
        return {"libraries": []}
    try:
        try:
            section_rows = conn.execute(
                "SELECT section_key, section_title, section_type "
                "FROM library_sections ORDER BY section_key"
            ).fetchall()
        except _sq3.OperationalError:
            section_rows = []
        # Split server_items rows by ``items.media_type`` so we
        # don't conflate parent rows (artists / shows / movies)
        # with their leaves (tracks / episodes). A naive union
        # count reports "12,005 artists" for a library that has
        # 1,100 artists + 7,400 tracks, which is the wrong answer.
        #
        # We join ``items`` (which carries ``media_type``) to
        # ``server_items`` (which carries ``section_key``) and
        # aggregate into a ``{section_key: {media_type: count}}``
        # nested dict. The caller renders the right line per
        # libtype (top-level + leaves).
        try:
            rows = conn.execute(
                "SELECT si.section_key AS sk, "
                "       i.media_type    AS mt, "
                "       COUNT(*)        AS n "
                "FROM server_items si "
                "JOIN items i ON i.id = si.item_id "
                "GROUP BY si.section_key, i.media_type"
            ).fetchall()
        except _sq3.OperationalError:
            rows = []
        media_type_counts_idx: Dict[int, Dict[str, int]] = {}
        for r in rows:
            sk = int(r["sk"])
            mt = str(r["mt"] or "")
            n = int(r["n"] or 0)
            if not mt or n <= 0:
                continue
            media_type_counts_idx.setdefault(sk, {})[mt] = n
        # Per-table per-section counts for the other metric tables
        # (watch_events / ratings / playlists / collections). These
        # tables are not media-type-faceted; row count per
        # section_key is the right number.
        def _counts(table: str) -> Dict[int, int]:
            try:
                rows2 = conn.execute(
                    f"SELECT section_key, COUNT(*) AS n "
                    f"FROM {table} GROUP BY section_key"
                ).fetchall()
            except _sq3.OperationalError:
                return {}
            return {int(r["section_key"]): int(r["n"] or 0) for r in rows2}
        we_idx = _counts("watch_events")
        ratings_idx = _counts("ratings")
        pl_idx = _counts("playlists")
        coll_idx = _counts("collections")
        # Distinct hierarchy counts per section. Because the
        # snapshot's items table carries show_title / season_index
        # / artist, the "artist count" / "series count" is the
        # number of distinct non-null hierarchy values across the
        # items referenced by this section's metrics. COALESCE
        # keys so an item missing one hierarchy field never
        # collapses two real values into one.
        hierarchy_counts_idx: Dict[int, Dict[str, int]] = {}
        try:
            hrows = conn.execute(
                "SELECT si.section_key AS sk, "
                "  COUNT(DISTINCT i.artist) AS artists, "
                "  COUNT(DISTINCT i.show_title) AS series, "
                "  COUNT(DISTINCT i.show_title || '/' || "
                "        COALESCE(i.season_index, -1)) AS seasons, "
                "  COUNT(DISTINCT i.album) AS albums "
                "FROM server_items si "
                "JOIN items i ON i.id = si.item_id "
                "GROUP BY si.section_key"
            ).fetchall()
            for hr in hrows:
                sk = int(hr["sk"])
                d: Dict[str, int] = {}
                if int(hr["artists"] or 0) > 0:
                    d["artists"] = int(hr["artists"])
                if int(hr["series"] or 0) > 0:
                    d["series"] = int(hr["series"])
                if int(hr["seasons"] or 0) > 0:
                    d["seasons"] = int(hr["seasons"])
                if int(hr["albums"] or 0) > 0:
                    d["albums"] = int(hr["albums"])
                if d:
                    hierarchy_counts_idx[sk] = d
        except _sq3.OperationalError:
            # Pre-v17 snapshot without the hierarchy columns.
            # media_type_counts still carries the per-type
            # numbers; distinct counts are simply absent.
            hierarchy_counts_idx = {}
        for r in section_rows:
            sk = int(r["section_key"])
            mt_counts = media_type_counts_idx.get(sk, {})
            # ``item_count`` is the TOP-LEVEL count for the
            # library's primary media type. For a movie library
            # that's media_type='movie'; for a show library
            # 'show' (not 'episode'); for an artist library
            # 'artist' (not 'track'). Falls back to the sum
            # across all media_types when the parent type isn't
            # in the index (e.g. a snapshot that captured only
            # leaves like episodes/tracks via watch_history;
            # better to surface that as "N total items" than to
            # report 0).
            sec_type = (r["section_type"] or "").lower()
            top_level_key = {
                "movie":  "movie",
                "show":   "show",
                "artist": "artist",
            }.get(sec_type, "")
            if top_level_key and top_level_key in mt_counts:
                item_count = mt_counts[top_level_key]
            else:
                item_count = sum(mt_counts.values())
            out_libs.append({
                "section_key":         sk,
                "section_title":       r["section_title"],
                "section_type":        r["section_type"],
                "item_count":          item_count,
                "media_type_counts":   mt_counts,
                # Distinct hierarchy counts. Empty for pre-v17
                # snapshots whose items lack the hierarchy
                # columns.
                "hierarchy_counts":    hierarchy_counts_idx.get(sk, {}),
                "watch_events":        we_idx.get(sk, 0),
                "ratings":             ratings_idx.get(sk, 0),
                "playlists":           pl_idx.get(sk, 0),
                "collections":         coll_idx.get(sk, 0),
            })
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return {"libraries": out_libs}


# ── Snapshot registry ────────────────────────────────────────────
#
# GET  /api/snapshots                       - list registry rows
# GET  /api/snapshots/{id}/download         - stream JSON for one
# DELETE /api/snapshots/{id}                - delete one (db_admin)
# DELETE /api/snapshots/server/{server_id}  - clear all on server (db_admin)
# GET  /api/snapshots/legacy                - list pre-rename .plexbackup.json files
# GET  /api/snapshots/legacy/{name}/download - stream a legacy JSON as-is
# DELETE /api/snapshots/legacy/{name}       - delete one legacy file (db_admin)


@router.get("/api/snapshots")
def list_snapshot_registry() -> Dict[str, Any]:
    """
    Return every registered snapshot, newest first. Rows are
    registry metadata only - no payload data.
    """
    from server import snapshot_registry
    return {"snapshots": snapshot_registry.list_snapshots()}


@router.get("/api/snapshots/legacy")
def list_legacy_snapshots() -> List[Dict[str, Any]]:
    """
    List pre-rename ``.plexbackup.json`` files left over from the
    old ``plex_exports/`` era and now under ``snapshots/legacy/``.
    These are read-only artefacts; downloads stream the file as-is.
    """
    return snapshot_browser.list_snapshots()


@router.get("/api/snapshots/legacy/{file_name}/download")
def download_legacy_snapshot(file_name: str) -> FileResponse:
    """Stream a legacy ``.plexbackup.json`` as-is."""
    try:
        path = snapshot_browser.snapshot_path(file_name)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return FileResponse(
        path=str(path),
        media_type="application/json",
        filename=path.name,
    )


@router.delete("/api/snapshots/legacy")
def delete_all_legacy_archives(
    body: Dict[str, Any],
    _admin: Dict[str, Any] = Depends(_auth_router_module.require_role("admin")),
) -> Dict[str, Any]:
    """
    Wipe every ``.plexbackup.json`` archive in
    ``<output_dir>/legacy/``. Two-factor gated (admin JWT +
    db_admin body credential). Returns ``{deleted, errors}``.

    Non-archive files in the same directory (anything not ending
    in ``.plexbackup.json``) are left alone - the end user may
    have dropped unrelated content there.
    """
    from server import auth_db
    _auth_router_module.verify_db_admin_from_body(body)
    return snapshot_browser.delete_all_archives()


@router.delete("/api/snapshots/legacy/{file_name}")
def delete_legacy_snapshot(
    file_name: str,
    body: Dict[str, Any],
    _admin: Dict[str, Any] = Depends(_auth_router_module.require_role("admin")),
) -> Dict[str, str]:
    """
    Delete one legacy ``.plexbackup.json`` file. Two-factor gated:
    admin/root_admin JWT plus ``{db_admin_username,
    db_admin_password}`` in the body.
    """
    from server import auth_db
    _auth_router_module.verify_db_admin_from_body(body)
    try:
        snapshot_browser.delete_snapshot(file_name)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"deleted": file_name}


@router.get("/api/snapshots/{snapshot_id}/users")
def list_snapshot_users(
    snapshot_id: str,
    _user: Dict[str, Any] = Depends(_auth_router_module.require_role("viewer")),
) -> Dict[str, Any]:
    """
    Return the user list captured inside a snapshot ``.db`` file.

    Reads the ``snapshot_users`` table from the .db at the registry
    row's ``file_path``. Each row is shaped to mirror the
    ``ServerUser`` payload the destination's ``/api/servers/{id}/users``
    endpoint returns so the frontend can intersect both lists without
    a translation step:

        { "users": [ { "kind", "plex_id", "raw_name", "display_name" }, ... ] }

    ``kind`` is derived from the table's ``is_owner`` flag.
    ``plex_id`` mirrors ``user_handle`` (the canonical join key -
    owner email for owner rows, managed-user username for the
    rest). The Restore form uses this to render the intersection
    picker (snapshot ∩ destination) with a "no destination user"
    badge for rows that only exist on one side.

    Returns 404 / 410 for missing row / missing .db file (same
    contract as the download endpoints below) and 500 with a
    sanitised detail on any other failure.
    """
    import logging as _logging
    try:
        from server import snapshot_registry
        row = snapshot_registry.get(snapshot_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"No snapshot with id {snapshot_id!r}")
        db_path = Path(row.get("file_path") or "")
        if not db_path.is_file():
            raise HTTPException(
                status_code=410,
                detail=(
                    "Snapshot .db is missing on disk. The registry row "
                    "still exists but the file behind it cannot be found."
                ),
            )
        import sqlite3
        uri = f"file:{db_path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            try:
                rows = conn.execute(
                    "SELECT user_handle, display_name, is_owner "
                    "FROM snapshot_users ORDER BY is_owner DESC, user_handle ASC"
                ).fetchall()
            except sqlite3.OperationalError:
                # Older snapshot files (pre-snapshot_users) - no
                # table to read. Return an empty list rather than
                # raise; the UI handles the empty case as
                # "user filter unavailable for this snapshot."
                rows = []
        finally:
            conn.close()
        users: List[Dict[str, Any]] = []
        for r in rows:
            handle = str(r["user_handle"] or "")
            display = str(r["display_name"] or "")
            is_owner = bool(r["is_owner"])
            users.append({
                "kind": "owner" if is_owner else "managed",
                "plex_id": handle,
                "raw_name": handle,
                "display_name": display or handle,
            })
        return {"users": users, "error": None}
    except HTTPException:
        raise
    except Exception as exc:
        _logging.getLogger("plexmigrate.server.jobs").exception(
            "list_snapshot_users failed for %r", snapshot_id,
        )
        raise HTTPException(
            status_code=500,
            detail="Could not read snapshot users - see the run log for details.",
        )


@router.get("/api/snapshots/{snapshot_id}/download-db")
def download_snapshot_db(
    snapshot_id: str,
    _user: Dict[str, Any] = Depends(_auth_router_module.require_role("viewer")),
) -> FileResponse:
    """
    Stream the per-snapshot ``.db`` file directly. No rendering,
    no caching - the .db lives on disk as the canonical artifact
    and the download is a flat FileResponse over the binary.

    Companion to ``/download`` (which renders + streams JSON).
    End users get both: ``.db`` for restore-into-another-install,
    ``.plexbackup.json`` for portable archive / inspection.

    Returns 404 if the snapshot id is unknown; 410 if the row
    exists but the .db file is missing on disk (orphan-row case,
    also surfaced via the ``available`` field on list rows);
    500 with the actual exception class in the detail on any
    other failure (so the UI can render something useful instead
    of a bare "Internal Server Error").
    """
    import logging as _logging
    try:
        from server import snapshot_registry
        row = snapshot_registry.get(snapshot_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"No snapshot with id {snapshot_id!r}")
        db_path = Path(row.get("file_path") or "")
        if not db_path.is_file():
            raise HTTPException(
                status_code=410,
                detail=(
                    "Snapshot .db is missing on disk. The registry row "
                    "still exists but the file behind it cannot be "
                    "found - use Remove entry to clean it up."
                ),
            )
        # Compose a friendly filename from the registry fields
        # rather than relying on snapshot_name. This works
        # uniformly for old rows (which carry the pre-rename
        # snapshot_name like "My-Server_20260513_022609") and new
        # rows (which already have the friendly name baked in).
        friendly = snapshot_registry.format_snapshot_filename(
            server_name=row.get("server_name") or "",
            libraries=row.get("libraries") or [],
            captured_at=row.get("captured_at") or 0,
        )
        return FileResponse(
            path=str(db_path),
            # SQLite has no standard IANA type; vnd.sqlite3 is the de
            # facto convention. Browsers will treat it as a download.
            media_type="application/vnd.sqlite3",
            filename=f"{friendly}.db",
        )
    except HTTPException:
        raise
    except Exception as exc:
        # Log via plexmigrate.server.jobs so the failure shows up in
        # the active run's runtime.log (Fix 2(b) whitelist). The
        # detail string ends up in the UI banner.
        _logging.getLogger("plexmigrate.server.jobs").exception(
            "download_snapshot_db failed for %r", snapshot_id,
        )
        # Generic detail only: the exception text can carry
        # filesystem paths / Plex URLs. The full traceback is in
        # the run log above for the end user to inspect.
        raise HTTPException(
            status_code=500,
            detail=".db download failed - see the run log for details.",
        )


@router.get("/api/snapshots/{snapshot_id}/download")
def download_snapshot(snapshot_id: str):
    """
    Download a snapshot as ``.plexbackup.json``. First call
    materialises a sidecar next to the ``.db`` and stamps the
    registry row's ``prebuilt_json_path``; subsequent calls stream
    the cached file. Failure to write the sidecar (read-only FS,
    out of space) is non-fatal - the endpoint falls back to a
    live in-memory render so the end user still gets their file.

    Any other render failure (corrupt .db, schema mismatch, etc.)
    surfaces a 500 with the exception class+message in the detail
    so the UI banner is debuggable instead of a bare
    "Internal Server Error".
    """
    import logging as _logging
    try:
        from server import snapshot_registry
        row = snapshot_registry.get(snapshot_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"No snapshot with id {snapshot_id!r}")

        # First try the cached sidecar. Either pre-existing or
        # just-materialised by this request. ``materialise_sidecar``
        # is idempotent and returns the existing path when one is
        # already on disk.
        friendly = snapshot_registry.format_snapshot_filename(
            server_name=row.get("server_name") or "",
            libraries=row.get("libraries") or [],
            captured_at=row.get("captured_at") or 0,
        )
        sidecar = snapshot_registry.materialise_sidecar(snapshot_id)
        if sidecar:
            p = Path(sidecar)
            if p.is_file():
                return FileResponse(
                    path=str(p),
                    media_type="application/json",
                    filename=f"{friendly}.plexexport.json",
                )

        # Sidecar materialisation failed but the end user still wants
        # their JSON. Render in memory and stream it without caching.
        db_path = Path(row.get("file_path") or "")
        if not db_path.is_file():
            raise HTTPException(
                status_code=410,
                detail=(
                    "Snapshot file is missing on disk. The registry row "
                    "still exists but the .db file behind it cannot be "
                    "found - it may have been deleted out-of-band."
                ),
            )
        from server import snapshot_serializer
        payload = snapshot_serializer.build_payload_from_db(
            db_path,
            server_name=row.get("server_name") or "",
            server_id=row.get("server_id") or "",
            libraries=row.get("libraries") or [],
            captured_at_ts=row.get("captured_at"),
        )
        return JSONResponse(
            content=payload,
            headers={
                "Content-Disposition": (
                    f'attachment; filename="{friendly}.plexexport.json"'
                ),
            },
        )
    except HTTPException:
        raise
    except Exception as exc:
        _logging.getLogger("plexmigrate.server.jobs").exception(
            "download_snapshot (JSON) failed for %r", snapshot_id,
        )
        # Generic detail only - exception text can leak paths /
        # URLs. Full traceback is in the run log above.
        raise HTTPException(
            status_code=500,
            detail="JSON render failed - see the run log for details.",
        )


@router.delete("/api/snapshots/{snapshot_id}")
def delete_snapshot_row(
    snapshot_id: str,
    body: Dict[str, Any],
    _admin: Dict[str, Any] = Depends(_auth_router_module.require_role("admin")),
) -> Dict[str, Any]:
    """
    Delete one snapshot: registry row + the per-snapshot ``.db``
    file. By default the cached JSON sidecar is removed too;
    when the body carries ``keep_json: true`` the sidecar is
    materialised (if needed) and moved into the JSON-archive
    directory instead, so it survives the snapshot deletion and
    appears in the Backups panel's "JSON Archives" section.

    Two-factor gated: admin/root_admin JWT plus
    ``{db_admin_username, db_admin_password}`` in the body, plus
    the optional ``keep_json`` flag.
    """
    from server import auth_db, snapshot_registry
    _auth_router_module.verify_db_admin_from_body(body)
    keep_json = bool(body.get("keep_json") or False)
    try:
        return snapshot_registry.delete(snapshot_id, keep_json=keep_json)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.delete("/api/snapshots/server/{server_id}")
def delete_all_snapshots_for_server(
    server_id: str,
    body: Dict[str, Any],
    _admin: Dict[str, Any] = Depends(_auth_router_module.require_role("admin")),
) -> Dict[str, Any]:
    """
    Wipe every snapshot for one server: each registry row + each
    ``.db`` file + each pre-built JSON sidecar. Two-factor gated:
    admin/root_admin JWT + db_admin body credential.
    """
    from server import auth_db, snapshot_registry
    _auth_router_module.verify_db_admin_from_body(body)
    return snapshot_registry.delete_all_for_server(server_id)


@router.post("/api/snapshots/server/{server_id}/merge-orphans")
def merge_orphan_snapshots(
    server_id: str,
    _admin: Dict[str, Any] = Depends(_auth_router_module.require_role("admin")),
) -> Dict[str, Any]:
    """
    Reassign orphan snapshot rows (rows whose stored ``server_name``
    matches this server but whose ``server_id`` points at a
    no-longer-registered server) into this server. The Exports
    panel already merges them at display time; this endpoint
    rewrites the underlying rows so the migration is permanent.

    Admin-gated (no db_admin step) because the operation is
    non-destructive: no files are deleted, only the foreign-key
    column is rewritten. The Exports panel state ends up the same
    either way.
    """
    from server import server_registry, snapshot_registry
    row = server_registry.get_server_by_id(server_id, include_token=False)
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=f"No server with id {server_id!r}.",
        )
    return snapshot_registry.reassign_orphan_snapshots(
        target_server_id=server_id,
        target_server_name=row.get("name") or "",
    )
