"""
Snapshot serializer. Reads a per-server snapshot ``.db`` file
(produced by :mod:`server.snapshot_capture`) and rebuilds a
``.plexexport.json``-shaped payload from it.

Two callers:

* The download endpoint when ``prebuilt_json_path`` is NULL on the
  registry row - i.e. the end user did not toggle ``prebuild_json``
  on the snapshot job. This module's :func:`build_payload_from_db`
  generates the JSON on demand and the endpoint streams it back.
* The sidecar materializer in :mod:`server.snapshot_registry` which
  caches the rendered JSON next to the .db file.

v0.15 library identity invariant
--------------------------------
A snapshot .db (schema_version >= 15) carries an authoritative
``library_sections`` table and a ``section_key`` column on every
per-server row (server_items, watch_events, ratings, playlists,
collections). The serializer partitions the rebuilt payload BY
library section - one entry per section_key - so the restore pipeline
can route work to the correct destination library without resorting
to fan-out / divide-by-N approximations.

The serializer refuses to read any snapshot file with
``snapshot_meta.schema_version < SNAPSHOT_SCHEMA_VERSION``. Older
snapshots are not migrated in place; the end user must re-capture.
The boot-time auto-archive in :mod:`server.app` retires pre-v15
``media.db`` files so the next snapshot run produces a v15-compliant
file.

Output shape (v0.15)
--------------------
::

    {
      "snapshot_meta": {
          "schema_version": 15,
          "server_id": "...",
          "server_name": "...",
          "backend": "plex",
          "reconstructed_from_db": true,
          "libraries": ["Movies", "TV Shows", ...],
          "captured_at": "...",
      },
      "captured_at": "<iso>",
      "libraries": [
          {
              "library":              "Movies",
              "library_section_id":   1,
              "library_section_type": "movie",
              "captured_at":          "<iso>",
              "users": {
                  "<owner-handle>":  {role: "owner",   ...},
                  "<managed-handle>": {role: "managed", ...},
              }
          },
          ...
      ]
    }

Each entry in the ``libraries`` array is shaped exactly like a live
per-library .plexexport.json the engine writes during a snapshot run -
so the restore loop can iterate over the array, treat each entry as
one library-scoped payload, and use the existing
:func:`services.restore.plex_native.restore_export_file` contract unchanged.
There is no ``"All Libraries (reconstructed from snapshot DB)"``
virtual library - that fallback was the source of the
``12,287-across-every-library`` totals bug and is gone in v0.15.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from server.snapshot_capture import SNAPSHOT_SCHEMA_VERSION


log = logging.getLogger("plexmigrate.server.snapshot_serializer")


class SnapshotSchemaMismatch(RuntimeError):
    """Raised when the snapshot .db's schema_version is lower than
    what this serializer requires. End user action: re-capture the
    snapshot with the current build, then retry the
    download / restore."""


def _safe_col(row: sqlite3.Row, col: str) -> Any:
    """sqlite3.Row raises IndexError for missing columns; return None
    instead so the caller can treat absent columns as NULLs. Used for
    additive columns the serializer wants to read even when reading
    a snapshot from a build that pre-dated the column."""
    try:
        return row[col]
    except (IndexError, KeyError):
        return None


def build_payload_from_db(
    snapshot_db_path: Path,
    *,
    server_name: str = "",
    server_id: str = "",
    libraries: Optional[List[str]] = None,
    captured_at_ts: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Read every row from the snapshot ``.db`` and assemble a v0.15
    multi-library payload as a Python dict.

    Refuses snapshots written by older builds (schema_version < 15) by
    raising :class:`SnapshotSchemaMismatch`. The end user must
    re-capture; there is no in-place migration of older .db files.

    ``libraries`` from the caller is treated as a hint for display
    only - the authoritative library list is read from the
    snapshot file's own ``library_sections`` table.
    """
    if not snapshot_db_path.is_file():
        raise FileNotFoundError(
            f"snapshot db not found: {snapshot_db_path}"
        )

    conn = sqlite3.connect(
        f"file:{snapshot_db_path}?mode=ro", uri=True, timeout=10.0,
    )
    conn.row_factory = sqlite3.Row

    try:
        # ── Schema-version gate ────────────────────────────────────────
        # Refuse pre-v15 files. The ``schema_version`` column was added
        # to ``snapshot_meta`` in v15; if the column is missing we treat
        # it as 0 and refuse. End user-facing error includes both the
        # observed and required versions.
        try:
            meta_row = conn.execute(
                "SELECT schema_version, captured_at FROM snapshot_meta LIMIT 1"
            ).fetchone()
            observed_version = int(meta_row["schema_version"] or 0) if meta_row else 0
        except sqlite3.OperationalError:
            observed_version = 0
            meta_row = None
        if observed_version < SNAPSHOT_SCHEMA_VERSION:
            raise SnapshotSchemaMismatch(
                f"Snapshot file {snapshot_db_path.name} has schema_version "
                f"{observed_version} but this build requires "
                f"{SNAPSHOT_SCHEMA_VERSION}. Re-capture the snapshot with "
                "the current build (older files are not migrated in place; "
                "the v0.15 library-identity refactor changed the on-disk "
                "shape in a way that requires re-running the engine)."
            )

        # ── Library sections (the integrity anchor) ────────────────────
        # Each (section_key) value here is one library the snapshot
        # covers. Without at least one row we have nothing to emit;
        # raise rather than fall through to a silent empty payload.
        section_rows = conn.execute(
            "SELECT server_id, section_key, section_title, section_type "
            "FROM library_sections "
            "WHERE server_id = ? OR ? = '' "
            "ORDER BY section_key",
            (server_id, server_id),
        ).fetchall()
        if not section_rows:
            raise RuntimeError(
                f"Snapshot file {snapshot_db_path.name} has no rows in "
                "library_sections. A v0.15-compliant snapshot must record "
                "at least one section; this file is malformed and must be "
                "re-captured."
            )

        # ── Items: id -> base payload ──────────────────────────────────
        # The items table is GUID-keyed and shared across sections. The
        # per-row section_key lives on server_items / watch_events /
        # ratings, so building items_by_id once and routing entries to
        # per-section buckets is correct.
        items_by_id: Dict[int, Dict[str, Any]] = {}
        for r in conn.execute("SELECT * FROM items").fetchall():
            items_by_id[int(r["id"])] = _item_to_payload(r)

        # ── server_items: item_id -> rating_key, section_key ───────────
        rk_by_item: Dict[int, int] = {}
        section_by_item: Dict[int, int] = {}
        try:
            for r in conn.execute(
                "SELECT item_id, rating_key, section_key FROM server_items"
            ).fetchall():
                iid = int(r["item_id"])
                rk_by_item[iid] = int(r["rating_key"])
                section_by_item[iid] = int(r["section_key"] or 0)
        except sqlite3.OperationalError as exc:
            raise SnapshotSchemaMismatch(
                f"Snapshot file {snapshot_db_path.name} is missing the "
                f"server_items.section_key column ({exc}). This is a v0.15 "
                "invariant; re-capture the snapshot."
            ) from exc

        # Decorate items with their per-server rating_key so members
        # arrays in playlists / collections can be cross-referenced
        # by ratingKey downstream.
        for iid, payload in items_by_id.items():
            rk = rk_by_item.get(iid)
            if rk is not None:
                payload["rating_key"] = rk

        # ── server_users metadata ──────────────────────────────────────
        users_meta_by_handle: Dict[str, Dict[str, Any]] = {}
        backend_in_db = "plex"
        try:
            for r in conn.execute("SELECT * FROM server_users").fetchall():
                h = r["user_handle"] or ""
                users_meta_by_handle[h] = {
                    "role": r["role"],
                    "display_name": r["display_name"],
                    "backend": r["backend"],
                    "backend_user_id": r["backend_user_id"],
                    # Surface the snapshot's canonical user identifier
                    # in the .plexexport.json sidecar. Older snapshots
                    # (v15 and earlier) carry NULL here; the JSON
                    # emitter writes null, matching the legacy shape.
                    "app_user_uuid": _safe_col(r, "app_user_uuid"),
                }
                if r["backend"]:
                    backend_in_db = r["backend"]
        except sqlite3.OperationalError:
            pass

        # ── Per-section row collectors ─────────────────────────────────
        # section_key -> handle -> bucket
        per_section: Dict[int, Dict[str, Dict[str, List[Dict[str, Any]]]]] = {
            int(s["section_key"]): {} for s in section_rows
        }

        def _bucket(section_key: int, handle: str) -> Dict[str, List[Dict[str, Any]]]:
            sec = per_section.setdefault(section_key, {})
            b = sec.get(handle)
            if b is None:
                b = {"watch_history": [], "ratings": [],
                     "playlists": [], "collections": []}
                sec[handle] = b
            return b

        # ── watch_events (filtered per section) ────────────────────────
        try:
            watch_rows = conn.execute(
                "SELECT * FROM watch_events"
            ).fetchall()
        except sqlite3.OperationalError as exc:
            raise SnapshotSchemaMismatch(
                f"Snapshot file {snapshot_db_path.name} is missing the "
                f"watch_events table ({exc})."
            ) from exc
        for r in watch_rows:
            iid = int(r["item_id"])
            base = items_by_id.get(iid)
            if base is None:
                continue
            section_key = int(r["section_key"] or 0)
            if section_key not in per_section:
                # Row references a section the integrity anchor doesn't
                # know about. With FK ON at write time this can't happen
                # for a freshly-captured file; surface loudly anyway so
                # a corrupted .db is visible rather than silently lossy.
                log.warning(
                    "watch_events row references unknown section_key=%d "
                    "(item_id=%d) in %s - skipping.",
                    section_key, iid, snapshot_db_path.name,
                )
                continue
            entry = dict(base)
            entry["view_count"] = int(r["view_count"] or 0)
            entry["view_offset"] = int(r["view_offset"] or 0)
            if r["last_viewed_at"] is not None:
                entry["last_viewed_at"] = r["last_viewed_at"]
            _bucket(section_key, (r["user_handle"] or "").strip())["watch_history"].append(entry)

        # ── ratings (filtered per section) ─────────────────────────────
        try:
            rating_rows = conn.execute("SELECT * FROM ratings").fetchall()
        except sqlite3.OperationalError as exc:
            raise SnapshotSchemaMismatch(
                f"Snapshot file {snapshot_db_path.name} is missing the "
                f"ratings table ({exc})."
            ) from exc
        for r in rating_rows:
            iid = int(r["item_id"])
            base = items_by_id.get(iid)
            if base is None:
                continue
            if r["rating"] is None:
                continue
            section_key = int(r["section_key"] or 0)
            if section_key not in per_section:
                log.warning(
                    "ratings row references unknown section_key=%d "
                    "(item_id=%d) in %s - skipping.",
                    section_key, iid, snapshot_db_path.name,
                )
                continue
            entry = dict(base)
            entry["rating"] = float(r["rating"])
            # Carry the backend-neutral favorite face. None for Plex
            # (no favorite concept); 0/1 for Jellyfin/Emby.
            _fav = r["is_favorite"] if "is_favorite" in r.keys() else None
            entry["is_favorite"] = None if _fav is None else bool(_fav)
            _bucket(section_key, (r["user_handle"] or "").strip())["ratings"].append(entry)

        # ── playlists (filtered per section) ───────────────────────────
        try:
            playlist_rows = conn.execute("SELECT * FROM playlists").fetchall()
        except sqlite3.OperationalError as exc:
            raise SnapshotSchemaMismatch(
                f"Snapshot file {snapshot_db_path.name} is missing the "
                f"playlists table ({exc})."
            ) from exc
        for r in playlist_rows:
            entry = _playlist_or_collection_row_to_payload(
                row=r, items_by_id=items_by_id, kind="playlist",
            )
            section_key = int(r["section_key"] or 0)
            if section_key not in per_section:
                log.warning(
                    "playlists row references unknown section_key=%d "
                    "(name=%r) in %s - skipping.",
                    section_key, r["name"], snapshot_db_path.name,
                )
                continue
            _bucket(section_key, (r["user_handle"] or "").strip())["playlists"].append(entry)

        # ── collections (filtered per section) ─────────────────────────
        try:
            collection_rows = conn.execute("SELECT * FROM collections").fetchall()
        except sqlite3.OperationalError as exc:
            raise SnapshotSchemaMismatch(
                f"Snapshot file {snapshot_db_path.name} is missing the "
                f"collections table ({exc})."
            ) from exc
        for r in collection_rows:
            entry = _playlist_or_collection_row_to_payload(
                row=r, items_by_id=items_by_id, kind="collection",
            )
            section_key = int(r["section_key"] or 0)
            if section_key not in per_section:
                log.warning(
                    "collections row references unknown section_key=%d "
                    "(name=%r) in %s - skipping.",
                    section_key, r["name"], snapshot_db_path.name,
                )
                continue
            _bucket(section_key, (r["user_handle"] or "").strip())["collections"].append(entry)
    finally:
        conn.close()

    # Prefer the caller-supplied timestamp; otherwise fall back to the
    # snapshot file's own ``snapshot_meta.captured_at`` so a
    # reconstructed payload reports when the snapshot was REALLY
    # taken, and only use "now" when neither is available.
    _file_captured_at: Optional[float] = None
    if meta_row is not None:
        try:
            _raw_cap = meta_row["captured_at"]
            _file_captured_at = float(_raw_cap) if _raw_cap is not None else None
        except (IndexError, KeyError, TypeError, ValueError):
            _file_captured_at = None
    _captured_ts = captured_at_ts if captured_at_ts is not None else _file_captured_at
    captured_iso = (
        datetime.fromtimestamp(float(_captured_ts), tz=timezone.utc).isoformat()
        if _captured_ts is not None
        else datetime.now(tz=timezone.utc).isoformat()
    )

    # ── Emit per-library entries ────────────────────────────────────────
    libraries_out: List[Dict[str, Any]] = []
    library_titles: List[str] = []
    for s in section_rows:
        section_key = int(s["section_key"])
        section_title = str(s["section_title"] or "")
        section_type = str(s["section_type"] or "")
        library_titles.append(section_title)

        users_block = _build_users_block(
            buckets=per_section.get(section_key, {}),
            users_meta_by_handle=users_meta_by_handle,
            backend_in_db=backend_in_db,
        )

        libraries_out.append({
            "library": section_title,
            "library_section_id": section_key,
            "library_section_type": section_type,
            "captured_at": captured_iso,
            "users": users_block,
        })

    # Audit trail: reconstructing a restore payload is a read across
    # every table of a snapshot .db. ``ingest_snapshot_payload`` already
    # logs the snapshot *write*; this is the matching *read* so the
    # db-access log shows the full snapshot -> restore round trip.
    try:
        from services.run_logs import db_access as db_access_log
        total_users = sum(
            len(per_section.get(int(s["section_key"]), {}))
            for s in section_rows
        )
        db_access_log.log_read(
            table=(
                "items,server_items,server_users,library_sections,"
                "watch_events,ratings,playlists,collections"
            ),
            where={"snapshot_db": snapshot_db_path.name, "server_id": server_id},
            intent=(
                f"reconstruct restore payload from snapshot DB "
                f"(libraries={len(libraries_out)}, "
                f"user-buckets={total_users}, schema_version={observed_version})"
            ),
        )
    except Exception:
        # Audit log writes are best-effort; never abort the render.
        log.debug("db_access_log write failed", exc_info=True)

    return {
        "captured_at": captured_iso,
        "snapshot_meta": {
            "schema_version": observed_version,
            "server_id": server_id,
            "server_name": server_name,
            "backend": backend_in_db,
            "libraries": library_titles,
            "captured_at": captured_iso,
            "reconstructed_from_db": True,
        },
        "libraries": libraries_out,
    }


# ── Helpers ────────────────────────────────────────────────────────────────

_GUID_COLUMNS = (
    ("imdb", "imdb_id"),
    ("tmdb", "tmdb_id"),
    ("tvdb", "tvdb_id"),
    ("musicbrainz", "musicbrainz_id"),
    ("plex", "plex_guid"),
)


def _item_to_payload(row: sqlite3.Row) -> Dict[str, Any]:
    """Convert one ``items`` row to the JSON entry shape."""
    guids: List[str] = []
    for prefix, col in _GUID_COLUMNS:
        v = row[col]
        if v:
            guids.append(f"{prefix}://{v}")
    out: Dict[str, Any] = {
        "guids": guids,
        "title": row["title"],
        "type": row["media_type"],
    }
    year = row["year"]
    if year is not None:
        out["year"] = int(year)
    fp = row["filepath_suffix"]
    if fp:
        out["filepath"] = fp
    # Emit the hierarchy columns so a DB -> JSON sidecar round-trip
    # preserves them. Snapshot schema is v17+ by the time the
    # serializer runs (older files are refused at load), but
    # ``_row_keys`` stays defensive so a freshly-built DB missing a
    # column never raises. Empty / NULL values are omitted to keep
    # movie rows + GUID-less items clean.
    keys = set(row.keys())

    def _emit_str(json_key: str, col: str) -> None:
        if col in keys:
            v = row[col]
            if v not in (None, ""):
                out[json_key] = str(v)

    def _emit_int(json_key: str, col: str) -> None:
        if col in keys:
            v = row[col]
            if v is not None:
                try:
                    out[json_key] = int(v)
                except (TypeError, ValueError):
                    pass

    _emit_str("show_title", "show_title")
    _emit_str("artist", "artist")
    _emit_str("album", "album")
    _emit_str("grandparent_guid", "grandparent_guid")
    # ``parent_index`` is the JSON key the engine + snapshot_capture
    # use for items.season_index (kept for shape-compat with the
    # adapter path's engine dict).
    _emit_int("parent_index", "season_index")
    _emit_int("episode_index", "episode_index")
    return out


def _playlist_or_collection_row_to_payload(
    *,
    row: sqlite3.Row,
    items_by_id: Dict[int, Dict[str, Any]],
    kind: str,
) -> Dict[str, Any]:
    """
    Build a playlist/collection entry. ``item_ids_json`` is a
    JSON-encoded list of internal items.id values; we translate each
    to its rich item payload via ``items_by_id`` so the result is
    self-contained.

    Members may live in a different section than the playlist itself
    (cross-section playlists). The primary-section-only routing
    decision means the playlist appears under its declared section
    once; its members carry their own item ids and resolve through
    the shared ``items_by_id`` map regardless of which section they
    live in.
    """
    try:
        ids = json.loads(row["item_ids_json"] or "[]")
    except Exception:
        ids = []
    members: List[Dict[str, Any]] = []
    for iid in ids:
        try:
            iid_int = int(iid)
        except (TypeError, ValueError):
            continue
        m = items_by_id.get(iid_int)
        if m is not None:
            members.append(dict(m))

    entry: Dict[str, Any] = {
        "name": row["name"],
        "items": members,
    }
    if kind == "playlist":
        entry["smart"] = bool(row["is_smart"])
        if row["smart_filter_json"]:
            entry["smart_content"] = row["smart_filter_json"]
        if row["description"]:
            entry["description"] = row["description"]
    return entry


def _build_users_block(
    *,
    buckets: Dict[str, Dict[str, List[Dict[str, Any]]]],
    users_meta_by_handle: Dict[str, Dict[str, Any]],
    backend_in_db: str,
) -> Dict[str, Dict[str, Any]]:
    """
    Compose the per-section users map from row-level buckets +
    identity metadata. The JSON-level key is the display name for the
    owner (or ``"Plex Owner"`` if unknown) and the handle for managed
    users; collisions are suffixed with ``(2)``, ``(3)``, ... so a
    managed user named "Plex Owner" can never overwrite the owner
    block.
    """
    users_block: Dict[str, Dict[str, Any]] = {}
    used_keys: set = set()

    def _meta_for(handle: str) -> Dict[str, Any]:
        m = users_meta_by_handle.get(handle)
        if m:
            return m
        return {
            "role": "owner" if handle == "" else "managed",
            "display_name": None,
            "backend": backend_in_db,
            "backend_user_id": None,
            "app_user_uuid": None,
        }

    def _json_key_for(handle: str, meta: Dict[str, Any]) -> str:
        if meta["role"] == "owner":
            base = (meta.get("display_name") or "").strip() or "Plex Owner"
        else:
            base = handle
        key = base
        n = 2
        while key in used_keys:
            key = f"{base} ({n})"
            n += 1
        used_keys.add(key)
        return key

    for handle in sorted(buckets.keys()):
        meta = _meta_for(handle)
        json_key = _json_key_for(handle, meta)
        users_block[json_key] = {
            "role": meta["role"],
            "display_name": meta.get("display_name"),
            "backend_user_id": meta.get("backend_user_id"),
            # Emit the canonical app-generated user identifier. Old
            # snapshots (v15-) write null here; v16+ snapshots write
            # the value populated by
            # snapshot_capture._write_snapshot_users +
            # _write_server_user_row. End users who share a .plexexport
            # sidecar across installs surface the source install's
            # canonical identity natively.
            "app_user_uuid": meta.get("app_user_uuid"),
            **buckets[handle],
        }

    return users_block
