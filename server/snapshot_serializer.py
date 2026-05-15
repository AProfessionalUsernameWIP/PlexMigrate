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

Shape parity with the engine's per-library JSON (v0.13.0 unified-users)
-----------------------------------------------------------------------
The engine's :mod:`services.snapshotter` writes one JSON file per
library with fields::

    {
      "library": "<library name>",
      "captured_at": "<ISO timestamp>",
      "snapshot_meta": {server_id, server_name, backend, libraries, ...},
      "users": {
          "<owner-handle>":  {role: "owner",   display_name, backend_user_id,
                              watch_history, ratings, playlists, collections},
          "<managed-handle>": {role: "managed", display_name, backend_user_id,
                              watch_history, ratings, playlists, collections},
          ...
      }
    }

The owner used to sit alone at a top-level ``items`` block; v0.13.0
collapsed it into ``users`` with an explicit ``role`` field so the
restorer can drive both paths through one loop and the schema stays
multi-backend (Jellyfin/Emby admin users will use the same shape with
``backend != "plex"``).

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

Pre-v0.13.0 snapshot ``.db`` files lack the ``server_users`` table.
The serializer falls back to the legacy ``user_handle`` grouping in
that case and synthesizes the role from the empty-string sentinel
(``""`` -> owner, anything else -> managed). display_name is left
NULL on those rows; downstream consumers should treat that as "use
the handle as the friendly name."
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

        # ── server_users metadata, keyed by user_handle ─────────────
        # v0.13.0: identity layer. Maps the legacy ``user_handle``
        # (which is still the SQL key on the wide tables during the
        # transition) to role + display_name + backend identity. Pre-
        # v0.13.0 snapshot .db files don't have this table - the
        # except-pass keeps the map empty and downstream code falls
        # back to handle-only grouping with synthesized roles.
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
                }
                # All rows on one snapshot share the same backend; use
                # whichever value we see (usually 'plex' today).
                if r["backend"]:
                    backend_in_db = r["backend"]
        except sqlite3.OperationalError:
            pass

        def _bucket_key(row_handle: Optional[str]) -> str:
            """Group key for accumulating per-user blocks. Empty handle
            is the owner sentinel; non-empty is the managed user's
            handle. This is the SQL-level grouping; the JSON-level key
            is decided after grouping (display_name for owner)."""
            return (row_handle or "").strip()

        # Buckets keyed by SQL user_handle. Each value is a per-user
        # block of the four lists.
        buckets: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}

        def _bucket(handle: str) -> Dict[str, List[Dict[str, Any]]]:
            b = buckets.get(handle)
            if b is None:
                b = {"watch_history": [], "ratings": [],
                     "playlists": [], "collections": []}
                buckets[handle] = b
            return b

        # ── watch_events ──
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
            _bucket(_bucket_key(r["user_handle"]))["watch_history"].append(entry)

        # ── ratings ──
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
            _bucket(_bucket_key(r["user_handle"]))["ratings"].append(entry)

        # ── playlists ──
        for r in conn.execute("SELECT * FROM playlists").fetchall():
            entry = _playlist_or_collection_row_to_payload(
                row=r, items_by_id=items_by_id, kind="playlist",
            )
            _bucket(_bucket_key(r["user_handle"]))["playlists"].append(entry)

        # ── collections ──
        for r in conn.execute("SELECT * FROM collections").fetchall():
            entry = _playlist_or_collection_row_to_payload(
                row=r, items_by_id=items_by_id, kind="collection",
            )
            _bucket(_bucket_key(r["user_handle"]))["collections"].append(entry)
    finally:
        conn.close()

    # ── Emit the unified users map ───────────────────────────────────
    # JSON key for each user:
    #   - owner (handle = ""): display_name from server_users if set,
    #     else the literal "Plex Owner" so the JSON dump is readable.
    #   - managed: the SQL handle directly.
    # Each bucket carries its role / display_name / backend_user_id so
    # downstream consumers don't need to re-query the identity table.
    users_block: Dict[str, Dict[str, Any]] = {}
    used_keys: set = set()

    def _meta_for(handle: str) -> Dict[str, Any]:
        m = users_meta_by_handle.get(handle)
        if m:
            return m
        # No metadata row (pre-v0.13.0 snapshot .db). Synthesize.
        return {
            "role": "owner" if handle == "" else "managed",
            "display_name": None,
            "backend": backend_in_db,
            "backend_user_id": None,
        }

    def _json_key_for(handle: str, meta: Dict[str, Any]) -> str:
        if meta["role"] == "owner":
            base = (meta.get("display_name") or "").strip() or "Plex Owner"
        else:
            base = handle
        # Collision-safe: a managed user named "Plex Owner" would
        # otherwise overwrite the owner block. Suffix until unique.
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
            **buckets[handle],
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
        owner_bucket = buckets.get("", {})
        managed_count = sum(1 for h in buckets if h != "")
        db_access_log.log_read(
            table="items,server_items,server_users,watch_events,ratings,playlists,collections",
            where={"snapshot_db": snapshot_db_path.name, "server_id": server_id},
            intent=(
                f"reconstruct restore payload from snapshot DB "
                f"(owner: {len(owner_bucket.get('watch_history', []))} watched / "
                f"{len(owner_bucket.get('ratings', []))} rated / "
                f"{len(owner_bucket.get('playlists', []))} playlist(s) / "
                f"{len(owner_bucket.get('collections', []))} collection(s); "
                f"{managed_count} managed user(s))"
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
            "backend": backend_in_db,
            "libraries": list(libraries or []),
            "reconstructed_from_db": True,
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
