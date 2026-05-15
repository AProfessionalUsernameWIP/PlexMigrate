"""
PR-13 - Snapshot serializer. Reads a per-server snapshot ``.db`` file
(produced by :mod:`server.snapshot_capture`) and rebuilds a
``.plexbackup.json``-shaped payload from it.

Two callers:

* The download endpoint when ``prebuilt_json_path`` is NULL on the
  registry row - i.e. the operator did not toggle ``prebuild_json``
  on the snapshot job. This module's :func:`build_payload_from_db`
  generates the JSON on demand and the endpoint streams it back.
* A future "re-export legacy snapshot" or "import from snapshot DB"
  flow that needs the JSON shape rather than reading the DB
  directly. (Not used today; the import path still consumes
  per-library JSON files.)

Shape parity with the engine's per-library JSON
-----------------------------------------------
The engine's :mod:`services.snapshotter` writes one JSON file per
library with fields::

    {
      "library": "<library name>",
      "captured_at": "<ISO timestamp>",
      "snapshot_meta": {...},
      "items":   {"watch_history": [...], "playlists": [...], ...},
      "users":   {"<user_handle>": {...}, ...}
    }

The snapshot ``.db`` covers ALL libraries on one server but the DB
schema doesn't carry a per-library column - items are keyed by GUID
and side tables join on item_id. So this serializer collapses
everything into a single virtual library entry. The result is
ingestible by :func:`server.media_db.ingest_snapshot_payload` and
adequate for the operator's archive / portability use case.

The shape carries an explicit ``"reconstructed_from_db": True``
marker on the snapshot_meta block so a future import path can tell
when it's looking at a regenerated payload vs. an engine-original
one (and apply any lossy-shape compensation if needed).
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


log = logging.getLogger("plexmigrate.server.snapshot_serializer")


def build_payload_from_db(
    snapshot_db_path: Path,
    *,
    server_name: str = "",
    server_id: str = "",
    libraries: Optional[List[str]] = None,
    captured_at_ts: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Read every row from the snapshot ``.db`` and assemble a
    ``.plexbackup.json``-shaped payload as a Python dict.

    Caller wraps the return value with ``json.dumps`` (the endpoint
    layer does that). Empty tables produce empty lists; an empty
    .db (no items, no users) returns a minimal-shape skeleton.
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
        # ── Items: id -> {guids, title, type, year, filepath} ──
        items_by_id: Dict[int, Dict[str, Any]] = {}
        for r in conn.execute("SELECT * FROM items").fetchall():
            items_by_id[int(r["id"])] = _item_to_payload(r)

        # ── server_items: item_id -> rating_key ──
        rk_by_item: Dict[int, int] = {}
        try:
            for r in conn.execute(
                "SELECT item_id, rating_key FROM server_items"
            ).fetchall():
                rk_by_item[int(r["item_id"])] = int(r["rating_key"])
        except sqlite3.OperationalError:
            # Older snapshot DB without server_items - leave map empty.
            pass

        # Decorate items with their per-server rating_key when known.
        for iid, payload in items_by_id.items():
            rk = rk_by_item.get(iid)
            if rk is not None:
                payload["rating_key"] = rk

        # ── watch_events ──
        owner_watch: List[Dict[str, Any]] = []
        per_user_watch: Dict[str, List[Dict[str, Any]]] = {}
        for r in conn.execute("SELECT * FROM watch_events").fetchall():
            iid = int(r["item_id"])
            base = items_by_id.get(iid)
            if base is None:
                continue
            entry = dict(base)
            entry["view_count"] = int(r["view_count"] or 0)
            entry["view_offset"] = int(r["view_offset"] or 0)
            if r["last_viewed_at"] is not None:
                entry["last_viewed_at"] = r["last_viewed_at"]
            uh = (r["user_handle"] or "").strip()
            if not uh:
                owner_watch.append(entry)
            else:
                per_user_watch.setdefault(uh, []).append(entry)

        # ── ratings ──
        owner_ratings: List[Dict[str, Any]] = []
        per_user_ratings: Dict[str, List[Dict[str, Any]]] = {}
        for r in conn.execute("SELECT * FROM ratings").fetchall():
            iid = int(r["item_id"])
            base = items_by_id.get(iid)
            if base is None:
                continue
            # L3: skip rows with a NULL rating rather than letting
            # ``float(None)`` raise and abort reconstruction of the
            # entire server payload. A rating row with no value carries
            # nothing to restore. Matches the defensive ``or 0`` style
            # the watch_events block above already uses.
            if r["rating"] is None:
                continue
            entry = dict(base)
            entry["rating"] = float(r["rating"])
            uh = (r["user_handle"] or "").strip()
            if not uh:
                owner_ratings.append(entry)
            else:
                per_user_ratings.setdefault(uh, []).append(entry)

        # ── playlists ──
        owner_playlists: List[Dict[str, Any]] = []
        per_user_playlists: Dict[str, List[Dict[str, Any]]] = {}
        for r in conn.execute("SELECT * FROM playlists").fetchall():
            entry = _playlist_or_collection_row_to_payload(
                row=r, items_by_id=items_by_id, kind="playlist",
            )
            uh = (r["user_handle"] or "").strip()
            if not uh:
                owner_playlists.append(entry)
            else:
                per_user_playlists.setdefault(uh, []).append(entry)

        # ── collections ──
        owner_collections: List[Dict[str, Any]] = []
        per_user_collections: Dict[str, List[Dict[str, Any]]] = {}
        for r in conn.execute("SELECT * FROM collections").fetchall():
            entry = _playlist_or_collection_row_to_payload(
                row=r, items_by_id=items_by_id, kind="collection",
            )
            uh = (r["user_handle"] or "").strip()
            if not uh:
                owner_collections.append(entry)
            else:
                per_user_collections.setdefault(uh, []).append(entry)
    finally:
        conn.close()

    # Compose user blocks. A given user may appear in only one of the
    # four tables, so the union of keys is the source of truth.
    user_handles = (
        set(per_user_watch)
        | set(per_user_ratings)
        | set(per_user_playlists)
        | set(per_user_collections)
    )
    users_block: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
    for uh in sorted(user_handles):
        users_block[uh] = {
            "watch_history": per_user_watch.get(uh, []),
            "ratings": per_user_ratings.get(uh, []),
            "playlists": per_user_playlists.get(uh, []),
            "collections": per_user_collections.get(uh, []),
        }

    captured_iso = (
        datetime.fromtimestamp(float(captured_at_ts), tz=timezone.utc).isoformat()
        if captured_at_ts is not None
        else datetime.now(tz=timezone.utc).isoformat()
    )

    # Audit trail: reconstructing a restore payload is a read across
    # every table of a snapshot .db. ``ingest_snapshot_payload`` already
    # logs the snapshot *write*; this is the matching *read* so the
    # db-access log shows the full snapshot -> restore round trip.
    try:
        from services import db_access_log
        db_access_log.log_read(
            table="items,server_items,watch_events,ratings,playlists,collections",
            where={"snapshot_db": snapshot_db_path.name, "server_id": server_id},
            intent=(
                f"reconstruct restore payload from snapshot DB "
                f"(owner: {len(owner_watch)} watched / {len(owner_ratings)} rated / "
                f"{len(owner_playlists)} playlist(s) / {len(owner_collections)} "
                f"collection(s); {len(users_block)} home user(s))"
            ),
        )
    except Exception:
        pass

    return {
        # Single virtual library; the snapshot .db doesn't preserve
        # the per-library split the engine writes from. Documented in
        # the module docstring + the meta marker below.
        "library": "All Libraries (reconstructed from snapshot DB)",
        "captured_at": captured_iso,
        "snapshot_meta": {
            "server_id": server_id,
            "server_name": server_name,
            "libraries": list(libraries or []),
            "reconstructed_from_db": True,
        },
        "items": {
            "watch_history": owner_watch,
            "ratings": owner_ratings,
            "playlists": owner_playlists,
            "collections": owner_collections,
        },
        "users": users_block,
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
