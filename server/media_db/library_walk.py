"""media.db library-walk tracking, staleness queries, and prune.

The ``library_walks`` bookkeeping plus the last-seen / stale-item /
prune functions that keep the per-server item cache honest.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from ._core import _DB_LOCK, _require_conn, log


# ── Library walk + last_seen tracking (Rule 2) ──────────────────────────────
#
# The library-walk job runs out-of-band (separate from snapshot /
# import / direct-transfer) and exists for one purpose: tick
# ``last_seen_at`` on every (item, server) row whose item is still
# present on the live server. The Prune Missing Items action reads
# those timestamps to identify items the server hasn't reported in
# more than N days - candidates the end user may want to remove.
#
# Critical design notes:
#
# * Auto-deletion is **never** triggered by the walk itself. Absence
#   on a single scan is not proof of permanent removal (library scan
#   could have missed, file could be temporarily offline). The walk
#   *only* refreshes timestamps; pruning is a separate, explicit,
#   end user-initiated action.
# * The walk records its own provenance in ``library_walks`` so the
#   prune UI can show "no walk in last X days, results may be stale"
#   before the end user commits to a destructive sweep.


def start_library_walk(server_id: str) -> int:
    """
    Insert a ``library_walks`` row with status='running' and return
    its primary key. The library-walk job updates this row's
    ``items_seen`` / ``libraries_seen`` as it progresses and stamps
    ``finished_at`` + ``status`` at the end.
    """
    conn = _require_conn()
    now = time.time()
    with _DB_LOCK:
        cur = conn.execute(
            """
            INSERT INTO library_walks (server_id, started_at, status)
            VALUES (?, ?, 'running')
            """,
            (server_id, now),
        )
        return int(cur.lastrowid)


def record_item_sighting(
    *, server_id: str, rating_key: int, walk_at: Optional[float] = None,
) -> Optional[int]:
    """
    Tick ``last_seen_at`` on the (item, server) row for one item the
    library walk just confirmed present on the live server. Returns
    the ``items.id`` that was touched, or ``None`` if no row exists
    for this rating_key (the resolver hasn't cached it yet - the walk
    is purely a refresh pass, never a discovery pass).

    The walk job calls this once per rating_key as it iterates the
    server's library. ``rating_key`` is mandatory because the walk
    enumerates by Plex section, where ratingKey is what python-plexapi
    surfaces.
    """
    conn = _require_conn()
    ts = float(walk_at if walk_at is not None else time.time())
    with _DB_LOCK:
        row = conn.execute(
            "SELECT item_id FROM server_items "
            "WHERE server_id = ? AND rating_key = ? LIMIT 1",
            (server_id, int(rating_key)),
        ).fetchone()
        if row is None:
            return None
        item_id = int(row["item_id"])
        conn.execute(
            "UPDATE server_items SET last_seen_at = ? "
            "WHERE item_id = ? AND server_id = ?",
            (ts, item_id, server_id),
        )
        conn.execute(
            "UPDATE items SET last_seen_at = ? WHERE id = ? "
            # COALESCE keeps the column monotonic - never go backwards.
            "AND (last_seen_at IS NULL OR last_seen_at < ?)",
            (ts, item_id, ts),
        )
        return item_id


def finish_library_walk(
    walk_id: int,
    *,
    status: str,
    items_seen: int,
    libraries_seen: int,
    error_message: Optional[str] = None,
) -> None:
    """
    Mark a walk row finished. ``status`` must be one of
    ``completed`` / ``failed`` / ``cancelled``. ``error_message`` is
    only stored when status != completed.
    """
    if status not in ("completed", "failed", "cancelled"):
        raise ValueError(f"invalid walk status: {status!r}")
    conn = _require_conn()
    with _DB_LOCK:
        conn.execute(
            """
            UPDATE library_walks
            SET finished_at = ?, status = ?, items_seen = ?,
                libraries_seen = ?, error_message = ?
            WHERE id = ?
            """,
            (
                time.time(), status, int(items_seen),
                int(libraries_seen),
                error_message if status != "completed" else None,
                walk_id,
            ),
        )


def list_library_walks(
    server_id: Optional[str] = None,
    limit: int = 20,
) -> List[Dict[str, Any]]:
    """
    Return the most recent walk rows, newest first. Filtered to one
    server when ``server_id`` is given.
    """
    conn = _require_conn()
    if server_id:
        rows = conn.execute(
            """
            SELECT * FROM library_walks
            WHERE server_id = ?
            ORDER BY started_at DESC
            LIMIT ?
            """,
            (server_id, int(limit)),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM library_walks ORDER BY started_at DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
    return [dict(r) for r in rows]


def get_last_walk_summary(server_id: str) -> Optional[Dict[str, Any]]:
    """
    Latest completed walk row for one server, or ``None`` when no
    walk has ever finished. Powers the Servers panel's "Last walked"
    column and the Prune UI's freshness warning.
    """
    conn = _require_conn()
    row = conn.execute(
        """
        SELECT * FROM library_walks
        WHERE server_id = ? AND status = 'completed'
        ORDER BY started_at DESC LIMIT 1
        """,
        (server_id,),
    ).fetchone()
    return dict(row) if row is not None else None


def list_stale_items(
    *,
    server_id: str,
    older_than_seconds: float,
    limit: int = 1000,
) -> List[Dict[str, Any]]:
    """
    Return items on ``server_id`` whose per-server ``last_seen_at``
    is present AND older than ``now - older_than_seconds``.

    Items with ``last_seen_at IS NULL`` are deliberately EXCLUDED: a
    missing timestamp means the library-walk job has never confirmed
    the item one way or the other (the column is only ever set by a
    walk). Treating "never walked" as "stale" would let the very first
    prune - run before any walk has populated timestamps - qualify the
    entire library and destroy all its watch history and ratings.
    Absence of evidence is not evidence of staleness.

    Cap defaults at 1000 so a huge library doesn't dump everything
    into one response. The UI surfaces the truncation explicitly.

    NOTE: this function NEVER deletes. It's a query that powers the
    Prune Missing Items UI's preview list; deletion is a separate
    explicit call to :func:`prune_stale_items`.
    """
    conn = _require_conn()
    cutoff = time.time() - float(older_than_seconds)
    rows = conn.execute(
        """
        SELECT i.id AS item_id, i.title, i.media_type, i.year,
               i.filepath_suffix, si.rating_key, si.last_seen_at
        FROM server_items si
        JOIN items i ON i.id = si.item_id
        WHERE si.server_id = ?
          AND si.last_seen_at IS NOT NULL
          AND si.last_seen_at < ?
        ORDER BY si.last_seen_at ASC, i.title
        LIMIT ?
        """,
        (server_id, cutoff, int(limit)),
    ).fetchall()
    return [dict(r) for r in rows]


def count_stale_items(
    *, server_id: str, older_than_seconds: float,
) -> int:
    """
    Cheap COUNT(*) for the Prune Missing Items confirmation modal.

    Counts only rows with a non-NULL ``last_seen_at`` older than the
    cutoff - a never-walked item (NULL timestamp) is not stale, see
    :func:`list_stale_items` for why.
    """
    conn = _require_conn()
    cutoff = time.time() - float(older_than_seconds)
    row = conn.execute(
        """
        SELECT COUNT(*) AS n
        FROM server_items
        WHERE server_id = ?
          AND last_seen_at IS NOT NULL
          AND last_seen_at < ?
        """,
        (server_id, cutoff),
    ).fetchone()
    return int(row["n"]) if row else 0


def prune_stale_items(
    *,
    server_id: str,
    older_than_seconds: float,
    dry_run: bool = False,
) -> Dict[str, int]:
    """
    Delete every per-server row for items not seen within the cutoff.
    Returns ``{server_items, watch_events, ratings, playlists_touched,
    collections_touched, items_orphaned}``. ``dry_run=True`` returns
    the same shape with the counts that WOULD be deleted but writes
    nothing.

    What gets deleted (end user-confirmed, never automatic):

    * ``server_items`` rows for the stale items on this server. The
      resolver's Tier-0 cache loses these entries for this server
      only. Other servers' caches are untouched.
    * Dependent ``watch_events`` and ``ratings`` rows for these
      (item, server) pairs.
    * Playlist / collection ``item_ids_json`` lists have stale ids
      filtered out (the playlist row itself is kept; just trimmed).

    What is preserved:

    * The ``items`` row itself is preserved when ANY other server
      still references it. Items keyed only by GUID across servers
      stay intact for cross-server matching.
    * Snapshot ``.db`` files are NEVER touched - Rule 3 (snapshots
      are historical records).
    * ``server_users`` rows are deliberately NOT pruned. A roster
      entry records that the user exists on the server and anchors
      cross-server identity-map links; a user with no remaining item
      data is still a valid roster row. Item-staleness is not
      user-staleness, and deleting the row could orphan identity
      mappings, so user pruning is a separate, explicit operation.

    Two H2 data-loss guards (see :func:`list_stale_items`):

    * Never-walked rows (``last_seen_at IS NULL``) are NOT prunable -
      a missing timestamp is not evidence the item is gone.
    * The whole call is refused (non-dry-run) until at least one
      library walk has completed for the server, so a prune run before
      any walk can't silently no-op or, worse, act on partial data.
    """
    conn = _require_conn()
    cutoff = time.time() - float(older_than_seconds)

    # H2: a destructive prune is only meaningful once a library walk
    # has actually populated last_seen_at timestamps. Refuse outright
    # rather than silently pruning nothing, so the end user gets a
    # clear reason instead of a confusing "0 items pruned" result.
    if not dry_run and get_last_walk_summary(server_id) is None:
        raise ValueError(
            "Refusing to prune: no completed library walk exists for this "
            "server. Run a library walk first so 'last seen' timestamps "
            "are populated."
        )

    counters = {
        "server_items": 0, "watch_events": 0, "ratings": 0,
        "playlists_touched": 0, "collections_touched": 0,
        "items_orphaned": 0,
    }

    with _DB_LOCK:
        stale = conn.execute(
            "SELECT item_id FROM server_items "
            "WHERE server_id = ? "
            "  AND last_seen_at IS NOT NULL "
            "  AND last_seen_at < ?",
            (server_id, cutoff),
        ).fetchall()
        stale_ids = {int(r["item_id"]) for r in stale}
        counters["server_items"] = len(stale_ids)
        if not stale_ids:
            return counters

        if dry_run:
            # Counts only.
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM watch_events "
                "WHERE server_id = ? "
                f"  AND item_id IN ({','.join('?' * len(stale_ids))})",
                (server_id, *stale_ids),
            ).fetchone()
            counters["watch_events"] = int(row["n"]) if row else 0
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM ratings "
                "WHERE server_id = ? "
                f"  AND item_id IN ({','.join('?' * len(stale_ids))})",
                (server_id, *stale_ids),
            ).fetchone()
            counters["ratings"] = int(row["n"]) if row else 0
            return counters

        # M12: the prune is several destructive statements on an
        # autocommit connection (isolation_level=None). Without an
        # explicit transaction an exception midway (e.g. a malformed
        # item_ids_json) would leave a partially-applied prune with no
        # rollback. Wrap the whole block so it commits all-or-nothing.
        placeholders = ",".join("?" * len(stale_ids))
        conn.execute("BEGIN")
        try:
            we_cur = conn.execute(
                f"DELETE FROM watch_events WHERE server_id = ? "
                f"  AND item_id IN ({placeholders})",
                (server_id, *stale_ids),
            )
            counters["watch_events"] = we_cur.rowcount
            ra_cur = conn.execute(
                f"DELETE FROM ratings WHERE server_id = ? "
                f"  AND item_id IN ({placeholders})",
                (server_id, *stale_ids),
            )
            counters["ratings"] = ra_cur.rowcount

            # Playlist / collection trim: load each row, filter the JSON
            # array, write it back when changed. Cheap because the
            # WHERE narrows by server.
            import json as _json
            for tbl, counter_key in (
                ("playlists", "playlists_touched"),
                ("collections", "collections_touched"),
            ):
                rows = conn.execute(
                    f"SELECT id, item_ids_json FROM {tbl} WHERE server_id = ?",
                    (server_id,),
                ).fetchall()
                for row in rows:
                    try:
                        ids = _json.loads(row["item_ids_json"] or "[]")
                    except Exception:
                        continue
                    kept = [i for i in ids if int(i) not in stale_ids]
                    if len(kept) != len(ids):
                        conn.execute(
                            f"UPDATE {tbl} SET item_ids_json = ?, updated_at = ? "
                            f"WHERE id = ?",
                            (_json.dumps(kept), time.time(), row["id"]),
                        )
                        counters[counter_key] += 1

            si_cur = conn.execute(
                f"DELETE FROM server_items WHERE server_id = ? "
                f"  AND item_id IN ({placeholders})",
                (server_id, *stale_ids),
            )
            counters["server_items"] = si_cur.rowcount

            # Items orphaned by this purge (no remaining server_items
            # row anywhere). Delete them too - keeping orphan item rows
            # would bloat the GUID-keyed pool indefinitely. Snapshot
            # files copy the items row at capture time, so any
            # historical reference is preserved there.
            orphan = conn.execute(
                f"SELECT id FROM items WHERE id IN ({placeholders}) "
                f"  AND NOT EXISTS (SELECT 1 FROM server_items WHERE item_id = items.id)",
                tuple(stale_ids),
            ).fetchall()
            if orphan:
                orphan_ids = [int(r["id"]) for r in orphan]
                op_cur = conn.execute(
                    f"DELETE FROM items WHERE id IN "
                    f"({','.join('?' * len(orphan_ids))})",
                    tuple(orphan_ids),
                )
                counters["items_orphaned"] = op_cur.rowcount

            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    try:
        from services.run_logs import db_access as db_access_log
        db_access_log.log_write(
            table="items,server_items,watch_events,ratings,playlists,collections",
            where={"server_id": server_id, "older_than_seconds": older_than_seconds},
            affected_rows=sum(counters.values()),
            intent=f"prune stale items (counters={counters})",
        )
    except Exception:
        log.exception("db_access_log emit failed for prune_stale_items")
    return counters
