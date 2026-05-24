"""
Per-library / per-server sync subscriptions + observation log.

The sync engine ingests two kinds of data:

1. **Subscriptions** — operator-declared intent. "On this server pair
   (or this library pair within the pair), keep watch counts in sync
   using policy X." Driven by the Sync Subscriptions UI.

2. **Observations** — the historical record of what each server has
   reported about each item over time. Every poll cycle appends rows
   here (not a key/value cache; an event log). The reconciler reads
   the most-recent-per-item per-server row + the last-known-target
   it wrote to decide whether the live state has changed since last
   sync and which side wins under the subscription's conflict policy.

Both live in a dedicated ``sync.db`` so they can be wiped, audited,
or examined independently of media.db / playlist_cache.db.

Granularity:
  * A subscription with NON-NULL ``source_library_id`` + ``dest_library_id``
    is library-scoped — only that specific library-pair is reconciled.
  * A subscription with NULL ``source_library_id`` + NULL
    ``dest_library_id`` is server-scoped — the engine expands it to
    cover every library pair currently mapped in library_mappings.
  * A row with one ID set and the other NULL is INVALID and rejected
    at the API boundary (granularity must be symmetric per row).

The expansion happens at READ time in the engine, not on write, so the
operator's intent stays stable as new library mappings come and go.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from server.persistence import get_data_dir
from server._db_connect import open_db


log = logging.getLogger("plexmigrate.server.sync_db")


_DB_NAME = "sync.db"


def _db_path() -> Path:
    return get_data_dir() / _DB_NAME


_DB_LOCK = threading.Lock()
_conn: Optional[sqlite3.Connection] = None
_init_lock = threading.Lock()
_initialised = False


# Sync types are an open enum — start small, extend as features land.
# The engine routes each type to a different reconciler. Operator-
# visible labels live in the frontend; the DB stores the raw token.
SYNC_TYPES = (
    "watch_counts",   # PlayCount / viewCount across users
    "ratings",        # numeric user rating (0-10 / 0-5 scale)
    "favorites",      # IsFavorite boolean
    "last_watched",   # LastPlayedDate timestamp
    "playlists",      # auto-migrate playlist creates/edits
)

# Conflict-resolution policies. The reconciler picks an absolute target
# per item from the latest observation on each side using this policy.
CONFLICT_POLICIES = (
    "max",             # target = max(src, dst) — never lose a play
    "sum",             # target = src + dst — total combined listens
    "latest_wins",     # target = whichever side's last observed
                       #          change is more recent
    "source_of_truth",  # one side is authoritative; the other always
                        # mirrors it. Direction encoded by which side
                        # of the subscription is "source" + the
                        # bidirectional flag.
)


_SCHEMA = """
-- ── Subscriptions ────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS library_pair_sync_subscriptions (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    -- Server pair (always required).
    source_server_id     TEXT NOT NULL,
    dest_server_id       TEXT NOT NULL,
    -- Library scope (both NULL = server-wide, both set = library-pair,
    -- one of each set is rejected at the API).
    source_library_id    TEXT,
    source_library_name  TEXT NOT NULL DEFAULT '',
    dest_library_id      TEXT,
    dest_library_name    TEXT NOT NULL DEFAULT '',
    -- One of SYNC_TYPES.
    sync_type            TEXT NOT NULL,
    -- One of CONFLICT_POLICIES.
    conflict_policy      TEXT NOT NULL DEFAULT 'max',
    -- 0 = subscription declared but worker won't write yet (dormant);
    -- 1 = active. Operator toggles via the UI.
    enabled              INTEGER NOT NULL DEFAULT 0,
    -- 1 = worker logs intended writes without actually issuing them.
    -- Useful for first-deploy sanity checks against a large library.
    dry_run              INTEGER NOT NULL DEFAULT 1,
    -- 1 = sync reconciles both directions. Defaults 0 so a fresh
    -- subscription mirrors the directional intent of the
    -- library_mappings row it sits on top of.
    bidirectional        INTEGER NOT NULL DEFAULT 0,
    -- 'owner' = only the source/dest server's owner accounts are
    -- reconciled. 'all' = every managed user that the user_identity_map
    -- can resolve. 'specific' = consult user_filter (JSON list).
    user_scope           TEXT NOT NULL DEFAULT 'owner',
    user_filter          TEXT,  -- JSON list when user_scope='specific'
    -- Poll cadence. Clamped to [5, 86400] at write time. Different
    -- types may need different cadences (watch counts: minutes;
    -- playlists: longer is fine since they change rarely). The
    -- 5-second floor matches the sync_worker tick floor so an
    -- operator can have a near-realtime sub (e.g. a 5-sec poll for
    -- watch counts on a heavily-used server pair) without the tick
    -- swallowing the wake-up.
    poll_interval_seconds INTEGER NOT NULL DEFAULT 300,
    -- Time the worker last completed a poll for this subscription.
    -- Updated on EACH poll attempt regardless of dry_run.
    last_polled_at       REAL,
    -- Time the worker last issued at least one real write for this
    -- subscription. Updated only on real-write runs (not dry-run).
    last_synced_at       REAL,
    -- Last polled outcome — what the reconciler reported. JSON blob
    -- so the UI surface can render mixed-shape data without a per-
    -- type column explosion.
    last_status_json     TEXT,
    -- Playlist auto-migrate: when sync_type='playlists', this flag
    -- controls whether NEW playlists created on the source since the
    -- last poll get automatically added to playlist_sync_selections
    -- (1 = auto-add, 0 = only operator-selected playlists sync). No
    -- effect on non-playlists sync_types.
    auto_sync_new_playlists INTEGER NOT NULL DEFAULT 0,
    created_at           REAL NOT NULL,
    created_by           TEXT,
    UNIQUE (
        source_server_id, source_library_id,
        dest_server_id, dest_library_id, sync_type
    )
);

CREATE INDEX IF NOT EXISTS idx_subscriptions_by_pair
    ON library_pair_sync_subscriptions(source_server_id, dest_server_id);
CREATE INDEX IF NOT EXISTS idx_subscriptions_enabled
    ON library_pair_sync_subscriptions(enabled, last_polled_at);


-- ── Observations ─────────────────────────────────────────────────────
-- One row per (server, library, item, user, sync_type) per poll
-- cycle. The reconciler reads the MOST RECENT row per
-- (server, library, item, user) when computing targets. The history
-- is preserved for audit + future ETA modeling.
CREATE TABLE IF NOT EXISTS sync_observations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    server_id       TEXT NOT NULL,
    library_id      TEXT NOT NULL,
    item_rating_key TEXT NOT NULL,
    user_id         TEXT NOT NULL,
    -- Wall-clock when WE recorded this row (poll cycle timestamp).
    observed_at     REAL NOT NULL,
    -- Backend's idea of when the user last touched this item. Used
    -- by latest_wins policy as the tiebreaker. May be NULL when the
    -- backend doesn't expose it.
    last_played_at  REAL,
    view_count      INTEGER,    -- absolute play count at observation
    user_rating     REAL,       -- absolute user rating
    is_favorite     INTEGER,    -- 0/1
    -- Item identity for cross-server resolution. GUID list as JSON.
    -- Indexed lookup happens via item_guid_lookup below.
    guids_json      TEXT NOT NULL DEFAULT '[]',
    -- The poll cycle that produced this row. UUID-like; lets a
    -- reconcile run filter to "the latest cycle for this sub".
    poll_cycle_id   TEXT
);

CREATE INDEX IF NOT EXISTS idx_observations_by_item
    ON sync_observations(server_id, library_id, item_rating_key, user_id, observed_at);
CREATE INDEX IF NOT EXISTS idx_observations_by_cycle
    ON sync_observations(poll_cycle_id);


-- ── Sync log ─────────────────────────────────────────────────────────
-- Per-write audit row. Every target-change the reconciler decides on
-- (dry_run or real) appends here so the operator can review.
CREATE TABLE IF NOT EXISTS sync_writes (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    subscription_id      INTEGER NOT NULL,
    poll_cycle_id        TEXT NOT NULL,
    -- Which side received the write. The subscription's bidirectional
    -- flag enables writes on BOTH sides; this column records which one.
    target_server_id     TEXT NOT NULL,
    target_library_id    TEXT NOT NULL,
    target_item_rating_key TEXT NOT NULL,
    target_user_id       TEXT NOT NULL,
    sync_type            TEXT NOT NULL,
    -- Numerical before/after for watch counts + ratings; NULL for
    -- non-numerical types (favorites is 0/1 stored as 0/1; playlists
    -- write a different shape and stamp a 'success' marker only).
    before_value         REAL,
    after_value          REAL,
    -- 1 = real write issued; 0 = dry_run logged intent only.
    issued               INTEGER NOT NULL DEFAULT 0,
    -- Reason rows that DIDN'T write (resolver missed, item not on
    -- destination, write failed): plain text. NULL on success.
    error                TEXT,
    written_at           REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sync_writes_by_sub
    ON sync_writes(subscription_id, written_at DESC);
CREATE INDEX IF NOT EXISTS idx_sync_writes_by_cycle
    ON sync_writes(poll_cycle_id);


-- ── Playlist sync selections ───────────────────────────────────────
-- For sync_type='playlists' subscriptions, this table holds the
-- specific source playlists the operator has opted into syncing. When
-- the subscription's auto_sync_new_playlists flag is 1, the worker
-- inserts rows here on each poll for newly-created source playlists
-- (so the operator can review what got auto-added). The destination
-- playlist is identified at sync time by name match (then created via
-- playlist_copy when missing); we don't denormalise the dest id here
-- because dest playlists can be renamed/deleted independently.
CREATE TABLE IF NOT EXISTS playlist_sync_selections (
    subscription_id      INTEGER NOT NULL,
    source_playlist_id   TEXT NOT NULL,
    source_playlist_name TEXT NOT NULL DEFAULT '',
    -- 'operator' for explicit Add-via-UI rows;
    -- 'auto' for rows added by the auto-sync-new-playlists path.
    added_by             TEXT NOT NULL DEFAULT 'operator',
    -- 1 = sync this playlist on every poll; 0 = paused.
    enabled              INTEGER NOT NULL DEFAULT 1,
    added_at             REAL NOT NULL,
    PRIMARY KEY (subscription_id, source_playlist_id)
);

CREATE INDEX IF NOT EXISTS idx_playlist_selections_by_sub
    ON playlist_sync_selections(subscription_id, enabled);
"""


def init_sync_db() -> None:
    """Open the shared connection + create schema. Idempotent."""
    global _conn, _initialised
    with _init_lock:
        if _initialised:
            return
        path = _db_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = open_db(
            path,
            label="sync.db",
            check_same_thread=False,
            foreign_keys=True,
            synchronous_normal=True,
            chmod_sidecars=False,
        )
        conn.executescript(_SCHEMA)
        _conn = conn
        _initialised = True
        log.info("sync.db initialised at %s", path)


def _require_conn() -> sqlite3.Connection:
    if _conn is None:
        raise RuntimeError(
            "sync_db.init_sync_db() must be called before any other "
            "public API in this module."
        )
    return _conn


def _close_for_tests() -> None:
    global _conn, _initialised
    with _init_lock:
        if _conn is not None:
            try:
                _conn.close()
            except sqlite3.Error:
                pass
        _conn = None
        _initialised = False


# ── Subscriptions CRUD ──────────────────────────────────────────────────────


def _validate_granularity(
    source_library_id: Optional[str],
    dest_library_id: Optional[str],
) -> None:
    """Library-scope must be symmetric. Both NULL = server-scope; both
    non-empty = library-scope; one of each is rejected."""
    src_set = bool((source_library_id or "").strip())
    dst_set = bool((dest_library_id or "").strip())
    if src_set != dst_set:
        raise ValueError(
            "source_library_id and dest_library_id must BOTH be set "
            "(library scope) or BOTH be empty/null (server scope). "
            f"Got source={source_library_id!r}, dest={dest_library_id!r}."
        )


def upsert_subscription(
    *,
    source_server_id: str,
    dest_server_id: str,
    sync_type: str,
    source_library_id: Optional[str] = None,
    source_library_name: str = "",
    dest_library_id: Optional[str] = None,
    dest_library_name: str = "",
    conflict_policy: str = "max",
    enabled: bool = False,
    dry_run: bool = True,
    bidirectional: bool = False,
    user_scope: str = "owner",
    user_filter: Optional[List[str]] = None,
    poll_interval_seconds: int = 300,
    auto_sync_new_playlists: bool = False,
    created_by: Optional[str] = None,
) -> int:
    """Insert or update a subscription. Returns the row id. Raises
    :class:`ValueError` on invalid input (granularity mismatch,
    unknown sync_type / conflict_policy)."""
    if not source_server_id or not dest_server_id:
        raise ValueError("source_server_id and dest_server_id are required")
    if sync_type not in SYNC_TYPES:
        raise ValueError(
            f"sync_type must be one of {SYNC_TYPES}; got {sync_type!r}",
        )
    if conflict_policy not in CONFLICT_POLICIES:
        raise ValueError(
            f"conflict_policy must be one of {CONFLICT_POLICIES}; "
            f"got {conflict_policy!r}",
        )
    if user_scope not in ("owner", "all", "specific"):
        raise ValueError(
            f"user_scope must be 'owner' / 'all' / 'specific'; "
            f"got {user_scope!r}",
        )
    _validate_granularity(source_library_id, dest_library_id)
    # The poll-interval floor is 5s. The tick interval in sync_worker
    # matches it so a 5s sub actually fires every 5s instead of being
    # bounded by the worker wake cadence.
    interval = max(5, min(int(poll_interval_seconds or 300), 86400))
    user_filter_json: Optional[str] = None
    if user_scope == "specific":
        user_filter_json = json.dumps(list(user_filter or []))
    src_lib = (source_library_id or "").strip() or None
    dst_lib = (dest_library_id or "").strip() or None
    now = time.time()
    conn = _require_conn()
    with _DB_LOCK:
        # The unique key is (src_server, src_lib, dst_server, dst_lib,
        # sync_type) so we manage update-vs-insert manually since
        # SQLite's UNIQUE-on-nullable-columns treats NULLs as distinct.
        existing = conn.execute(
            "SELECT id FROM library_pair_sync_subscriptions "
            "WHERE source_server_id = ? "
            "  AND IFNULL(source_library_id, '') = IFNULL(?, '') "
            "  AND dest_server_id = ? "
            "  AND IFNULL(dest_library_id, '') = IFNULL(?, '') "
            "  AND sync_type = ?",
            (source_server_id, src_lib, dest_server_id, dst_lib, sync_type),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE library_pair_sync_subscriptions SET "
                "source_library_name = ?, dest_library_name = ?, "
                "conflict_policy = ?, enabled = ?, dry_run = ?, "
                "bidirectional = ?, user_scope = ?, user_filter = ?, "
                "poll_interval_seconds = ?, "
                "auto_sync_new_playlists = ? "
                "WHERE id = ?",
                (
                    source_library_name, dest_library_name,
                    conflict_policy, 1 if enabled else 0,
                    1 if dry_run else 0, 1 if bidirectional else 0,
                    user_scope, user_filter_json, interval,
                    1 if auto_sync_new_playlists else 0,
                    existing["id"],
                ),
            )
            return int(existing["id"])
        cur = conn.execute(
            "INSERT INTO library_pair_sync_subscriptions ("
            "source_server_id, dest_server_id, source_library_id, "
            "source_library_name, dest_library_id, dest_library_name, "
            "sync_type, conflict_policy, enabled, dry_run, bidirectional, "
            "user_scope, user_filter, poll_interval_seconds, "
            "auto_sync_new_playlists, created_at, created_by"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                source_server_id, dest_server_id, src_lib,
                source_library_name, dst_lib, dest_library_name,
                sync_type, conflict_policy, 1 if enabled else 0,
                1 if dry_run else 0, 1 if bidirectional else 0,
                user_scope, user_filter_json, interval,
                1 if auto_sync_new_playlists else 0,
                now, created_by,
            ),
        )
        return int(cur.lastrowid or 0)


def get_subscription(sub_id: int) -> Optional[Dict[str, Any]]:
    conn = _require_conn()
    row = conn.execute(
        "SELECT * FROM library_pair_sync_subscriptions WHERE id = ?",
        (int(sub_id),),
    ).fetchone()
    return _sub_row_to_dict(row) if row else None


def list_subscriptions(
    *,
    source_server_id: Optional[str] = None,
    dest_server_id: Optional[str] = None,
    enabled_only: bool = False,
) -> List[Dict[str, Any]]:
    conn = _require_conn()
    where = ["1=1"]
    params: List[Any] = []
    if source_server_id:
        where.append("source_server_id = ?")
        params.append(source_server_id)
    if dest_server_id:
        where.append("dest_server_id = ?")
        params.append(dest_server_id)
    if enabled_only:
        where.append("enabled = 1")
    rows = conn.execute(
        "SELECT * FROM library_pair_sync_subscriptions "
        f"WHERE {' AND '.join(where)} ORDER BY id",
        params,
    ).fetchall()
    return [_sub_row_to_dict(r) for r in rows]


def delete_subscription(sub_id: int) -> int:
    conn = _require_conn()
    with _DB_LOCK:
        cur = conn.execute(
            "DELETE FROM library_pair_sync_subscriptions WHERE id = ?",
            (int(sub_id),),
        )
        return int(cur.rowcount or 0)


def stamp_polled(
    sub_id: int, *, status: Optional[Dict[str, Any]] = None,
    issued_any_writes: bool = False,
) -> None:
    """Worker hook: stamp last_polled_at (always) + last_synced_at
    (only when real writes were issued) + persist status JSON."""
    now = time.time()
    conn = _require_conn()
    with _DB_LOCK:
        if issued_any_writes:
            conn.execute(
                "UPDATE library_pair_sync_subscriptions SET "
                "last_polled_at = ?, last_synced_at = ?, "
                "last_status_json = ? WHERE id = ?",
                (now, now, json.dumps(status or {}), int(sub_id)),
            )
        else:
            # On a failed cycle, preserve the last GOOD status under a
            # ``last_good`` key so the UI can still show the prior
            # successful sync next to the error - overwriting
            # last_status_json outright would discard it.
            payload = dict(status or {})
            if payload.get("error"):
                prior_row = conn.execute(
                    "SELECT last_status_json FROM "
                    "library_pair_sync_subscriptions WHERE id = ?",
                    (int(sub_id),),
                ).fetchone()
                if prior_row and prior_row["last_status_json"]:
                    try:
                        prior = json.loads(prior_row["last_status_json"])
                    except (TypeError, ValueError):
                        prior = None
                    if isinstance(prior, dict) and not prior.get("error"):
                        payload["last_good"] = prior
            conn.execute(
                "UPDATE library_pair_sync_subscriptions SET "
                "last_polled_at = ?, last_status_json = ? WHERE id = ?",
                (now, json.dumps(payload), int(sub_id)),
            )


def _sub_row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    user_filter_raw = row["user_filter"]
    user_filter: Optional[List[str]] = None
    if user_filter_raw:
        try:
            decoded = json.loads(user_filter_raw)
            if isinstance(decoded, list):
                user_filter = [str(x) for x in decoded]
        except (TypeError, ValueError):
            user_filter = None
    status_raw = row["last_status_json"]
    status: Optional[Dict[str, Any]] = None
    if status_raw:
        try:
            decoded = json.loads(status_raw)
            if isinstance(decoded, dict):
                status = decoded
        except (TypeError, ValueError):
            status = None
    return {
        "id": int(row["id"]),
        "source_server_id":     row["source_server_id"],
        "source_library_id":    row["source_library_id"] or "",
        "source_library_name":  row["source_library_name"] or "",
        "dest_server_id":       row["dest_server_id"],
        "dest_library_id":      row["dest_library_id"] or "",
        "dest_library_name":    row["dest_library_name"] or "",
        "sync_type":            row["sync_type"],
        "conflict_policy":      row["conflict_policy"],
        "enabled":              bool(row["enabled"]),
        "dry_run":              bool(row["dry_run"]),
        "bidirectional":        bool(row["bidirectional"]),
        "user_scope":           row["user_scope"],
        "user_filter":          user_filter,
        "auto_sync_new_playlists": bool(row["auto_sync_new_playlists"]),
        "poll_interval_seconds": int(row["poll_interval_seconds"] or 300),
        "last_polled_at":       float(row["last_polled_at"] or 0.0) or None,
        "last_synced_at":       float(row["last_synced_at"] or 0.0) or None,
        "last_status":          status,
        "created_at":           float(row["created_at"] or 0.0),
        "created_by":           row["created_by"],
        "scope":                "library" if (row["source_library_id"] or "") else "server",
    }


# ── Observations ────────────────────────────────────────────────────────────


def record_observation(
    *,
    server_id: str,
    library_id: str,
    item_rating_key: str,
    user_id: str,
    observed_at: float,
    last_played_at: Optional[float] = None,
    view_count: Optional[int] = None,
    user_rating: Optional[float] = None,
    is_favorite: Optional[bool] = None,
    guids: Optional[List[str]] = None,
    poll_cycle_id: Optional[str] = None,
) -> int:
    """Append one observation row. Returns the new id."""
    conn = _require_conn()
    with _DB_LOCK:
        cur = conn.execute(
            "INSERT INTO sync_observations ("
            "server_id, library_id, item_rating_key, user_id, "
            "observed_at, last_played_at, view_count, user_rating, "
            "is_favorite, guids_json, poll_cycle_id"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                server_id, library_id, item_rating_key, user_id,
                float(observed_at),
                None if last_played_at is None else float(last_played_at),
                None if view_count is None else int(view_count),
                None if user_rating is None else float(user_rating),
                None if is_favorite is None else (1 if is_favorite else 0),
                json.dumps(list(guids or [])),
                poll_cycle_id,
            ),
        )
        return int(cur.lastrowid or 0)


def latest_observations_for_pair(
    *,
    source_server_id: str,
    dest_server_id: str,
    source_library_id: Optional[str] = None,
    dest_library_id: Optional[str] = None,
) -> Dict[str, List[Dict[str, Any]]]:
    """Return the most-recent observation per (server, item, user) for
    the given pair. Keys: ``"source"`` and ``"dest"`` each carry a list
    of latest-per-(item,user) rows. The reconciler uses this to compute
    targets per item across the pair.

    Implementation: SQL window function picking ``ROW_NUMBER()=1`` per
    partition. SQLite 3.25+ supports this; the production
    sqlite3 module in Python 3.12 satisfies that requirement."""
    conn = _require_conn()
    rows = conn.execute(
        "SELECT * FROM ("
        "  SELECT *, "
        "    ROW_NUMBER() OVER ("
        "      PARTITION BY server_id, library_id, item_rating_key, user_id "
        "      ORDER BY observed_at DESC, id DESC"
        "    ) AS rn "
        "  FROM sync_observations "
        "  WHERE "
        "    (server_id = ? AND (? IS NULL OR library_id = ?)) "
        "    OR "
        "    (server_id = ? AND (? IS NULL OR library_id = ?))"
        ") WHERE rn = 1",
        (
            source_server_id, source_library_id, source_library_id,
            dest_server_id, dest_library_id, dest_library_id,
        ),
    ).fetchall()
    out: Dict[str, List[Dict[str, Any]]] = {"source": [], "dest": []}
    for r in rows:
        d = _observation_row_to_dict(r)
        if d["server_id"] == source_server_id:
            out["source"].append(d)
        elif d["server_id"] == dest_server_id:
            out["dest"].append(d)
    return out


def _observation_row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    guids: List[str] = []
    try:
        decoded = json.loads(row["guids_json"] or "[]")
        if isinstance(decoded, list):
            guids = [str(x) for x in decoded]
    except (TypeError, ValueError):
        guids = []
    return {
        "id":               int(row["id"]),
        "server_id":        row["server_id"],
        "library_id":       row["library_id"],
        "item_rating_key":  row["item_rating_key"],
        "user_id":          row["user_id"],
        "observed_at":      float(row["observed_at"] or 0.0),
        "last_played_at":   (
            float(row["last_played_at"])
            if row["last_played_at"] is not None else None
        ),
        "view_count":       (
            int(row["view_count"])
            if row["view_count"] is not None else None
        ),
        "user_rating":      (
            float(row["user_rating"])
            if row["user_rating"] is not None else None
        ),
        "is_favorite":      (
            bool(row["is_favorite"])
            if row["is_favorite"] is not None else None
        ),
        "guids":            guids,
        "poll_cycle_id":    row["poll_cycle_id"],
    }


# ── Sync write log ──────────────────────────────────────────────────────────


def record_write(
    *,
    subscription_id: int,
    poll_cycle_id: str,
    target_server_id: str,
    target_library_id: str,
    target_item_rating_key: str,
    target_user_id: str,
    sync_type: str,
    before_value: Optional[float] = None,
    after_value: Optional[float] = None,
    issued: bool = False,
    error: Optional[str] = None,
) -> int:
    conn = _require_conn()
    with _DB_LOCK:
        cur = conn.execute(
            "INSERT INTO sync_writes ("
            "subscription_id, poll_cycle_id, target_server_id, "
            "target_library_id, target_item_rating_key, target_user_id, "
            "sync_type, before_value, after_value, issued, error, "
            "written_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                int(subscription_id), poll_cycle_id, target_server_id,
                target_library_id, target_item_rating_key, target_user_id,
                sync_type, before_value, after_value,
                1 if issued else 0, error, time.time(),
            ),
        )
        return int(cur.lastrowid or 0)


def list_recent_writes(
    *, subscription_id: int, limit: int = 100,
) -> List[Dict[str, Any]]:
    """Most recent N writes for one subscription. UI surfaces these
    so the operator can review what the worker decided."""
    conn = _require_conn()
    rows = conn.execute(
        "SELECT * FROM sync_writes WHERE subscription_id = ? "
        "ORDER BY id DESC LIMIT ?",
        (int(subscription_id), max(1, min(int(limit or 100), 1000))),
    ).fetchall()
    return [
        {
            "id":                       int(r["id"]),
            "subscription_id":          int(r["subscription_id"]),
            "poll_cycle_id":            r["poll_cycle_id"],
            "target_server_id":         r["target_server_id"],
            "target_library_id":        r["target_library_id"],
            "target_item_rating_key":   r["target_item_rating_key"],
            "target_user_id":           r["target_user_id"],
            "sync_type":                r["sync_type"],
            "before_value":             (
                float(r["before_value"])
                if r["before_value"] is not None else None
            ),
            "after_value":              (
                float(r["after_value"])
                if r["after_value"] is not None else None
            ),
            "issued":                   bool(r["issued"]),
            "error":                    r["error"],
            "written_at":               float(r["written_at"] or 0.0),
        }
        for r in rows
    ]


def stats() -> Dict[str, int]:
    conn = _require_conn()
    try:
        n_sub = conn.execute(
            "SELECT COUNT(*) FROM library_pair_sync_subscriptions"
        ).fetchone()[0]
        n_obs = conn.execute(
            "SELECT COUNT(*) FROM sync_observations"
        ).fetchone()[0]
        n_writes = conn.execute(
            "SELECT COUNT(*) FROM sync_writes"
        ).fetchone()[0]
        return {
            "subscriptions": int(n_sub),
            "observations":  int(n_obs),
            "writes":        int(n_writes),
        }
    except sqlite3.OperationalError:
        return {"subscriptions": 0, "observations": 0, "writes": 0}


# ── Playlist sync selections ────────────────────────────────────────────────


def add_playlist_selection(
    *,
    subscription_id: int,
    source_playlist_id: str,
    source_playlist_name: str = "",
    added_by: str = "operator",
    enabled: bool = True,
) -> bool:
    """Insert or update one playlist in a subscription's selection
    list. ``added_by`` is 'operator' for explicit-add or 'auto' for
    auto_sync_new_playlists-discovered rows."""
    if not source_playlist_id:
        return False
    if added_by not in ("operator", "auto"):
        raise ValueError(
            f"added_by must be 'operator' or 'auto'; got {added_by!r}"
        )
    conn = _require_conn()
    now = time.time()
    with _DB_LOCK:
        try:
            conn.execute(
                "INSERT OR REPLACE INTO playlist_sync_selections ("
                "subscription_id, source_playlist_id, "
                "source_playlist_name, added_by, enabled, added_at"
                ") VALUES (?, ?, ?, ?, ?, ?)",
                (
                    int(subscription_id), source_playlist_id,
                    source_playlist_name, added_by,
                    1 if enabled else 0, now,
                ),
            )
            return True
        except sqlite3.OperationalError as exc:
            log.warning(
                "add_playlist_selection failed: %s", exc,
            )
            return False


def remove_playlist_selection(
    *, subscription_id: int, source_playlist_id: str,
) -> int:
    conn = _require_conn()
    with _DB_LOCK:
        cur = conn.execute(
            "DELETE FROM playlist_sync_selections "
            "WHERE subscription_id = ? AND source_playlist_id = ?",
            (int(subscription_id), source_playlist_id),
        )
        return int(cur.rowcount or 0)


def list_playlist_selections(
    *, subscription_id: int, enabled_only: bool = False,
) -> List[Dict[str, Any]]:
    conn = _require_conn()
    where = "subscription_id = ?"
    params: List[Any] = [int(subscription_id)]
    if enabled_only:
        where += " AND enabled = 1"
    rows = conn.execute(
        "SELECT * FROM playlist_sync_selections "
        f"WHERE {where} ORDER BY source_playlist_name COLLATE NOCASE",
        params,
    ).fetchall()
    return [
        {
            "subscription_id":      int(r["subscription_id"]),
            "source_playlist_id":   r["source_playlist_id"],
            "source_playlist_name": r["source_playlist_name"] or "",
            "added_by":             r["added_by"],
            "enabled":              bool(r["enabled"]),
            "added_at":             float(r["added_at"] or 0.0),
        }
        for r in rows
    ]
