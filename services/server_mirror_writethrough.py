"""
Mirror write-through helpers.

Reads recently-captured rows from ``media.db`` after a snapshot
completes and bulk-upserts them into ``server_mirror.db``. The
snapshot writer is not modified; this module hooks in AFTER the
snapshot artifact commits.

Snapshot 100% accuracy invariant (Plan section 19.6): the snapshot
artifact (.db + .plexexport.json) is bit-for-bit identical with or
without this write-through. The mirror upsert is a side-effect that
runs out-of-band; any failure here is logged + dropped, never
propagated back to the snapshot job.

L1 (narrow except): every DB read + write is wrapped in
``sqlite3.*Error`` / ``OSError``. A mirror write failure logs at
WARNING and returns; the snapshot caller never sees the exception.
L6 (shared walk): media.db is the SAME data the snapshot just
wrote. One walk (snapshot) feeds two artifacts (snapshot.db + mirror).
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from typing import Optional

from services import tunables


def after_snapshot(
    *,
    server_id: str,
    backend: str,
    logger: Optional[logging.Logger] = None,
) -> int:
    """Post-snapshot mirror write-through.

    Reads (server_items JOIN items) rows for ``server_id`` from
    media.db, builds :class:`services.server_mirror.ItemRow` instances,
    and bulk-applies them to mirror_items grouped by section. Returns
    the number of items written (added + updated).

    Tunable-gated by ``engine_mirror_snapshot_writethrough`` (D6,
    default true). When the operator flips this off (forensic
    snapshots that must not touch mirror), this is a no-op.

    Errors are caught + logged; the function never raises into the
    snapshot caller.
    """
    log = logger or logging.getLogger(
        "plexmigrate.services.server_mirror_writethrough"
    )

    if not tunables.engine_mirror_snapshot_writethrough():
        log.debug(
            "server_mirror writethrough disabled by tunable; "
            "skipping for server=%s", server_id,
        )
        return 0

    try:
        from server import media_db, server_mirror_db
        from services import server_mirror
    except ImportError as exc:
        log.warning(
            "server_mirror writethrough imports failed: %s; skipping",
            exc,
        )
        return 0

    try:
        media_conn = media_db._require_conn()
    except RuntimeError:
        log.debug(
            "media.db not initialised; skipping mirror writethrough "
            "for server=%s", server_id,
        )
        return 0

    try:
        rows = media_conn.execute(
            "SELECT si.rating_key, si.section_key, si.updated_at, "
            "i.title, i.media_type, i.imdb_id, i.tmdb_id, "
            "i.tvdb_id, i.musicbrainz_id, i.plex_guid, "
            "i.filepath_suffix, "
            "ls.section_title, ls.section_type, ls.library_guid "
            "FROM server_items si "
            "JOIN items i ON i.id = si.item_id "
            "LEFT JOIN library_sections ls "
            "  ON ls.server_id = si.server_id "
            "  AND ls.section_key = si.section_key "
            "WHERE si.server_id = ?",
            (server_id,),
        ).fetchall()
    except sqlite3.OperationalError as exc:
        log.warning(
            "server_mirror writethrough read failed for server=%s: %s",
            server_id, exc,
        )
        return 0

    if not rows:
        log.debug(
            "server_mirror writethrough: no media.db rows for "
            "server=%s", server_id,
        )
        return 0

    # Ensure the per-server mirror_server_state row exists so the
    # subsequent sync layer + UI badge surface make sense.
    try:
        server_mirror.upsert_server_state(
            server_id=server_id, backend=backend,
        )
    except sqlite3.Error as exc:
        log.warning(
            "server_mirror upsert_server_state failed: %s", exc,
        )

    mirror_conn = server_mirror_db.get_connection()
    mirror_lock = server_mirror_db.get_db_lock()
    now = time.time()

    written = 0
    with mirror_lock:
        mirror_conn.execute("BEGIN IMMEDIATE")
        try:
            # Track unique sections we touched so we can stamp
            # mirror_library_sections too.
            sections_seen: dict = {}
            for r in rows:
                rk = str(r[0])
                section_key = int(r[1] or 0)
                updated_at = float(r[2]) if r[2] is not None else None
                title = r[3] or ""
                media_type = r[4] or "unknown"
                imdb = r[5]
                tmdb = r[6]
                tvdb = r[7]
                mb = r[8]
                plex_guid = r[9]
                filepath_suffix = r[10]
                section_title = r[11] or f"section_{section_key}"
                section_type = r[12] or media_type
                library_guid = r[13]
                # CONSOLE-08: match the live mirror sync's section_id
                # scheme. Plex keys on the numeric section key;
                # Jellyfin/Emby key on the raw library GUID. Writing
                # str(section_key) for J/E (a CRC32 hash) split the
                # mirror into two section_id namespaces for one
                # library. Fall back to the hash when the GUID is
                # absent (Plex, or a library_sections row written
                # before migration v22).
                if (backend or "").lower() in ("jellyfin", "emby") and library_guid:
                    section_id = str(library_guid)
                else:
                    section_id = str(section_key)

                guids = []
                if plex_guid:
                    guids.append(plex_guid)
                if imdb:
                    guids.append(f"imdb://{imdb}")
                if tmdb:
                    guids.append(f"tmdb://{tmdb}")
                if tvdb:
                    guids.append(f"tvdb://{tvdb}")
                if mb:
                    guids.append(f"mbid://{mb}")

                mirror_conn.execute(
                    "INSERT OR REPLACE INTO mirror_items("
                    "server_id, section_id, rating_key, title, "
                    "item_type, file_path, guids_json, "
                    "live_updated_at, mirror_synced_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (server_id, section_id, rk, title, media_type,
                     filepath_suffix, json.dumps(guids), updated_at,
                     now),
                )
                # Replace GUID rows for this item.
                mirror_conn.execute(
                    "DELETE FROM mirror_item_guids "
                    "WHERE server_id = ? AND rating_key = ?",
                    (server_id, rk),
                )
                for g in guids:
                    if not g:
                        continue
                    mirror_conn.execute(
                        "INSERT OR IGNORE INTO mirror_item_guids("
                        "server_id, rating_key, guid) "
                        "VALUES (?, ?, ?)",
                        (server_id, rk, g),
                    )
                written += 1
                if section_id not in sections_seen:
                    sections_seen[section_id] = {
                        "name": section_title,
                        "type": section_type,
                        "count": 0,
                    }
                sections_seen[section_id]["count"] += 1

            # Stamp per-section state. Note we do NOT touch
            # live_total_size or live_updated_at here (those are
            # populated by the proper sync layer when a live probe
            # runs). We DO update mirror_synced_at so the freshness
            # check has a recent timestamp.
            for sid, meta in sections_seen.items():
                mirror_conn.execute(
                    "INSERT INTO mirror_library_sections("
                    "server_id, section_id, name, section_type, "
                    "mirror_synced_at) VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(server_id, section_id) DO UPDATE SET "
                    "name = excluded.name, "
                    "section_type = excluded.section_type, "
                    "mirror_synced_at = excluded.mirror_synced_at",
                    (server_id, sid, meta["name"], meta["type"], now),
                )
            mirror_conn.execute("COMMIT")
        except sqlite3.Error as exc:
            mirror_conn.execute("ROLLBACK")
            log.warning(
                "server_mirror writethrough write failed for "
                "server=%s: %s", server_id, exc,
            )
            return 0

    log.info(
        "server_mirror writethrough server=%s wrote %d item(s) "
        "across %d section(s)",
        server_id, written, len(sections_seen),
    )
    return written
