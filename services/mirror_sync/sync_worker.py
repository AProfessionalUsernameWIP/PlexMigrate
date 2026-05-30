"""
Library-pair sync worker (2026-05-19, operator request).

Polls every enabled subscription on its own ``poll_interval_seconds``,
records observations to ``sync_observations``, and (when the
subscription isn't in ``dry_run`` mode) issues exact-target writes via
the adapter contract. Every decision the worker makes — write or
intent-to-write — lands as a row in ``sync_writes`` so the operator
can audit.

Architecture:

  * One daemon thread per worker process. The thread wakes every N
    seconds (default 30) and runs ``_tick``.
  * ``_tick`` iterates ``list_subscriptions(enabled_only=True)`` and
    processes any subscription whose
    ``last_polled_at + poll_interval_seconds <= now`` is due.
  * For each due subscription, the worker:
      1. Resolves the (source_server, dest_server, source_library,
         dest_library) tuples it should reconcile — expanding
         server-scope subscriptions into per-library pairs via the
         library_mappings table.
      2. Observes the current state on BOTH sides via the adapter
         (mirror DB readers when warm; live adapter calls otherwise).
      3. Resolves items across the pair via the resolver chain
         (GUIDs first; falls back to path / title).
      4. Computes a target per-item per-user under the subscription's
         conflict_policy.
      5. Issues writes (or logs intents in dry_run mode) via the
         already-shipped ``adapter.set_watched(view_count=N,
         current_view_count=C)`` exact-target contract.
      6. Records sync_writes rows + stamps last_polled_at / last_synced_at.

  * Bidirectional subscriptions run the reconciler TWICE per tick —
    once for source→dest and once for dest→source. The conflict
    policy resolves which side wins; bidirectional just means BOTH
    sides receive the resolved target (instead of only one).

  * Webhook-driven activation is deferred per operator's order-of-
    operations request. Polling is the universal fallback that
    works across every backend.

Status reporting:
  After each tick the worker writes a JSON status blob into
  ``subscription.last_status_json`` so the UI can render
  "12 items synced in last poll, 3 errors, last cycle 14:32:08".

Failure handling:
  Per-item failures never abort a tick — they record an ``error``
  string in sync_writes and the worker continues. A subscription-
  level failure (mirror DB unavailable, adapter connect refused)
  stamps the status JSON with an error reason + skips that
  subscription until the next tick.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set, Tuple


# 2026-05-19 (operator request): sync activity must NEVER bleed
# into a running job's runtime.log. The dedicated sync logger
# (propagate=False, writes to sync.log) is what every emission
# inside this module routes through. Manual / operator-initiated
# playlist copies are unaffected — they don't pass this logger
# into playlist_copy.copy_playlist and so keep using the module
# logger which propagates to runtime.log as before. See
# services.mirror_sync.log for the full rationale.
from services.mirror_sync.log import get_sync_logger
log = get_sync_logger()

# Cap on matched pairs reconciled per sync cycle so a single tick
# cannot monopolise the worker on a very large library.
_MAX_PAIRS_PER_CYCLE = 200


# Default top-level cadence. The thread wakes every N seconds; per-
# subscription poll intervals are honored within that.
#
# Lowered to 5s so a subscription
# configured with the minimum 5s interval actually fires that
# fast. Wake-up cost when no subscription is due is a single
# list_subscriptions() call (one SQLite query) plus a sleep, so a
# faster tick adds negligible overhead on idle systems.
_TICK_INTERVAL_SECONDS = 5


# Module-global thread state.
_worker_thread: Optional[threading.Thread] = None
_worker_stop = threading.Event()
_worker_lock = threading.Lock()


def start() -> None:
    """Start the sync worker thread. Idempotent — calling repeatedly
    is a no-op once the thread is running."""
    global _worker_thread
    with _worker_lock:
        if _worker_thread is not None and _worker_thread.is_alive():
            return
        _worker_stop.clear()
        _worker_thread = threading.Thread(
            target=_run, name="sync-worker", daemon=True,
        )
        _worker_thread.start()
        log.info("sync_worker: thread started")


def stop() -> None:
    """Signal the worker to exit. Called on app shutdown."""
    _worker_stop.set()


def _run() -> None:
    """Worker main loop. Wakes every _TICK_INTERVAL_SECONDS, runs
    _tick, sleeps. Exceptions inside _tick are logged + swallowed so
    the thread keeps running across transient failures."""
    log.info("sync_worker: main loop entering")
    while not _worker_stop.is_set():
        try:
            _tick()
        except Exception:
            log.exception("sync_worker: tick raised; continuing")
        # Sleep in small increments so stop() takes effect quickly.
        for _ in range(_TICK_INTERVAL_SECONDS):
            if _worker_stop.is_set():
                break
            time.sleep(1)
    log.info("sync_worker: main loop exited")


# ── Tick implementation ──────────────────────────────────────────────


def _tick() -> None:
    """Walk every enabled subscription. For each one whose poll
    interval has elapsed, run one reconcile pass."""
    try:
        from server import sync_db
    except Exception:
        return
    try:
        subs = sync_db.list_subscriptions(enabled_only=True)
    except Exception:
        log.exception("sync_worker: list_subscriptions failed")
        return
    now = time.time()
    for sub in subs:
        last = float(sub.get("last_polled_at") or 0.0)
        interval = float(sub.get("poll_interval_seconds") or 300)
        if last and (now - last) < interval:
            continue
        try:
            _reconcile_subscription(sub)
        except Exception as exc:
            log.exception(
                "sync_worker: subscription %s reconcile failed: %s",
                sub.get("id"), exc,
            )
            try:
                sync_db.stamp_polled(
                    sub_id=int(sub["id"]),
                    status={"error": f"{type(exc).__name__}: {exc}"},
                    issued_any_writes=False,
                )
            except Exception:
                pass


def _reconcile_subscription(sub: Dict[str, Any]) -> None:
    """Process one subscription. Routes by sync_type to the matching
    reconciler. Records the cycle's outcome in the status JSON +
    stamps last_polled_at (and last_synced_at when real writes
    fired)."""
    sync_type = sub.get("sync_type")
    cycle_id = uuid.uuid4().hex
    started = time.time()
    summary: Dict[str, Any] = {
        "cycle_id":  cycle_id,
        "started":   started,
        "sync_type": sync_type,
    }
    issued_any_writes = False

    if sync_type == "watch_counts":
        summary.update(
            _reconcile_watch_counts(sub, cycle_id=cycle_id)
        )
        issued_any_writes = bool(summary.get("writes_issued") or 0)
    elif sync_type == "playlists":
        summary.update(
            _reconcile_playlists(sub, cycle_id=cycle_id)
        )
        issued_any_writes = bool(summary.get("writes_issued") or 0)
    else:
        # ratings / favorites / last_watched have no reconciler yet.
        # Record this as an ERROR, not a soft "note": stamp_polled()
        # below runs unconditionally, so a soft note left the UI
        # showing the subscription as healthily "polling" while it
        # synced nothing at all. An error surfaces the gap honestly
        # and stops a silent no-op from looking like success.
        summary["error"] = (
            f"no reconciler implemented for sync_type {sync_type!r}; "
            "subscription is polling but syncing nothing"
        )
        log.warning(
            "sync_worker: subscription %s has sync_type %r with no "
            "reconciler - polling but not syncing",
            sub.get("id"), sync_type,
        )

    summary["elapsed_ms"] = int((time.time() - started) * 1000)
    from server import sync_db
    sync_db.stamp_polled(
        sub_id=int(sub["id"]), status=summary,
        issued_any_writes=issued_any_writes,
    )


def _reconcile_watch_counts(
    sub: Dict[str, Any], *, cycle_id: str,
) -> Dict[str, Any]:
    """Reconcile watch counts across the subscription's library
    pair(s). Returns a status dict the caller folds into the
    subscription's last_status_json.

    Steps:
      1. Resolve the list of (src_lib, dst_lib) pairs to walk. Library-
         scope subs walk one pair; server-scope subs expand to every
         library_mappings row between the two servers.
      2. For each pair, fetch current view_count + last_played_at on
         BOTH sides via the adapters.
      3. For each item matched across the pair (via GUIDs), compute
         the target per the subscription's conflict_policy.
      4. Issue exact-target writes (Plex: unscrobble + scrobble math;
         J/E: single UserData POST) for items below target. Skip
         writes in dry_run mode but still log intent.
    """
    pairs = _resolve_library_pairs(sub)
    if not pairs:
        return {"note": "no library pairs to reconcile", "writes_issued": 0}

    # Adapter connections — one per unique server_id.
    server_ids = {sub["source_server_id"], sub["dest_server_id"]}
    adapters = {}
    for sid in server_ids:
        try:
            from server import server_registry
            conn = server_registry.connect_registered_server(
                sid, log,
            )
            adapters[sid] = conn
        except Exception as exc:
            return {
                "error": f"connect_registered_server({sid}) failed: {exc}",
                "writes_issued": 0,
            }

    writes_issued = 0
    writes_logged = 0
    errors = 0
    dry_run = bool(sub.get("dry_run", True))
    bidirectional = bool(sub.get("bidirectional"))
    conflict = str(sub.get("conflict_policy") or "max")
    poll_cycle_id = cycle_id

    # Resolve user pairs ONCE per subscription (same across library
    # pairs). Notes are surfaced in the status JSON so the operator
    # sees skipped users in Sync Activity even though they didn't
    # produce sync_writes rows.
    user_pairs, user_notes = _resolve_users_for_sub(
        sub,
        source_server_id=sub["source_server_id"],
        dest_server_id=sub["dest_server_id"],
        adapters=adapters,
    )

    # Per-pair reconcile. The number of items per pair is bounded by
    # what each side's mirror knows; we read observations from the
    # mirror DB to keep the cost low. When the mirror is empty for a
    # pair, the engine logs a one-line "mirror empty; sync this server
    # first" status and moves on.
    if user_pairs:
        for (src_lib_id, src_lib_name, dst_lib_id, dst_lib_name) in pairs:
            try:
                n_issued, n_logged, n_errors = _reconcile_one_pair_watch(
                    sub=sub, cycle_id=poll_cycle_id,
                    source_server_id=sub["source_server_id"],
                    source_library_id=src_lib_id,
                    dest_server_id=sub["dest_server_id"],
                    dest_library_id=dst_lib_id,
                    adapters=adapters,
                    dry_run=dry_run,
                    bidirectional=bidirectional,
                    conflict_policy=conflict,
                    user_pairs=user_pairs,
                )
                writes_issued += n_issued
                writes_logged += n_logged
                errors += n_errors
            except Exception as exc:
                log.exception(
                    "sync_worker: pair %s -> %s reconcile failed: %s",
                    src_lib_id, dst_lib_id, exc,
                )
                errors += 1

    return {
        "pairs":          len(pairs),
        "user_pairs":     len(user_pairs),
        "user_notes":     user_notes,
        "user_scope":     (sub.get("user_scope") or "owner"),
        "writes_issued":  writes_issued,
        "writes_logged":  writes_logged,
        "errors":         errors,
        "dry_run":        dry_run,
    }


def _resolve_library_pairs(
    sub: Dict[str, Any],
) -> List[Tuple[str, str, str, str]]:
    """Expand a subscription's scope into concrete (src_lib_id,
    src_lib_name, dst_lib_id, dst_lib_name) tuples.

    Library-scoped subs return exactly one tuple. Server-scoped subs
    consult ``library_mappings`` for every saved row between the two
    servers and emit one tuple per row.
    """
    src_lib = (sub.get("source_library_id") or "").strip()
    dst_lib = (sub.get("dest_library_id") or "").strip()
    if src_lib and dst_lib:
        return [(
            src_lib, sub.get("source_library_name") or "",
            dst_lib, sub.get("dest_library_name") or "",
        )]
    try:
        from server import library_mapping_db
        rows = library_mapping_db.list_mappings_for_pair(
            sub["source_server_id"], sub["dest_server_id"],
        )
    except Exception:
        return []
    pairs: List[Tuple[str, str, str, str]] = []
    for r in rows:
        # Skip "operator skip" rows — those libraries are explicitly
        # excluded from sync.
        if not (r.get("dest_library_id") or ""):
            continue
        pairs.append((
            r["source_library_id"], r.get("source_library_name") or "",
            r["dest_library_id"], r.get("dest_library_name") or "",
        ))
    return pairs


# ── User-scope resolution ─────────────────────────────────────────────────
#
# Each subscription carries ``user_scope`` ∈ {owner, all, specific} and
# (when ``specific``) a ``user_filter`` list of source-side usernames.
# These helpers turn that declaration into a concrete list of
# (source_user, dest_user) pairs the per-pair reconciler iterates.
#
# Owner-only scope is the cheap default — one pair from each side's
# adapter.server_identity(). Multi-user scope walks managed_users on
# both servers, applies the allowlist, and resolves each source user
# to a destination user via identity_map first (authoritative) and
# case-insensitive username match second (lenient fallback; gated by
# the strict_identity_resolution tunable just like the
# services.identity.user_resolution chain).
#
# Plex per-user safety belt: the worker has no path to a managed
# user's per-user Plex token today. ``set_watched`` on a Plex target
# with only the admin token would misattribute the write to the
# owner. So when a write target is on a Plex backend AND the target
# user is not the owner, we emit an explicit "skip with reason" write
# row instead of corrupting owner state. The cycle status surfaces
# this so the operator sees the limitation in Sync Activity.


@dataclass(frozen=True)
class _SubUserPair:
    """One (source_user → destination_user) pair the worker iterates
    inside a library-pair reconcile call."""
    source_username: str
    source_backend_user_id: str
    source_is_owner: bool
    dest_username: str
    dest_backend_user_id: str
    dest_is_owner: bool


def _resolve_users_for_sub(
    sub: Dict[str, Any],
    *,
    source_server_id: str,
    dest_server_id: str,
    adapters: Dict[str, Any],
) -> Tuple[List[_SubUserPair], List[str]]:
    """Return ``(pairs, notes)`` for one subscription.

    ``notes`` carries human-readable strings describing source users
    that were skipped during enumeration (no destination match, empty
    filter, etc.) so the cycle status JSON can surface them in the
    Sync Activity panel.
    """
    scope = (sub.get("user_scope") or "owner").strip().lower()
    if scope not in ("owner", "all", "specific"):
        scope = "owner"

    src_owner_uid = ""
    dst_owner_uid = ""
    try:
        src_owner_uid = (
            adapters[source_server_id].adapter.server_identity().owner_user_id
            or ""
        )
        dst_owner_uid = (
            adapters[dest_server_id].adapter.server_identity().owner_user_id
            or ""
        )
    except Exception:
        pass

    owner_pair = _SubUserPair(
        source_username="owner",
        source_backend_user_id=src_owner_uid,
        source_is_owner=True,
        dest_username="owner",
        dest_backend_user_id=dst_owner_uid,
        dest_is_owner=True,
    )

    if scope == "owner":
        return ([owner_pair], [])

    # scope ∈ {all, specific} — enumerate active users on both sides
    # and resolve per-user pairs.
    #
    # Route through services.user_management.activity_filter so
    # tombstoned + auth-failing users are dropped consistently with
    # every other backend-touching path. Phase A: same behaviour as
    # media_db.list_managed_users(include_hidden=False) today; Phase
    # B's signal columns make the auth-health gate fire.
    try:
        from services.user_management.activity_filter import list_active_users
        src_users = list_active_users(source_server_id) or []
        dst_users = list_active_users(dest_server_id) or []
    except Exception as exc:
        return (
            [owner_pair],
            [f"managed-user enumeration failed: {exc}; "
             "degraded to owner-only for this cycle"],
        )

    dst_by_username_lc: Dict[str, Dict[str, Any]] = {}
    for u in dst_users:
        name = (u.get("username") or "").strip().lower()
        if name:
            dst_by_username_lc[name] = u

    filter_set: Optional[Set[str]] = None
    if scope == "specific":
        raw = sub.get("user_filter") or []
        filter_set = {
            str(x).strip().lower() for x in raw
            if x and str(x).strip()
        }

    pairs: List[_SubUserPair] = []
    notes: List[str] = []
    seen: Set[Tuple[str, str]] = set()

    for src in src_users:
        src_username = (src.get("username") or "").strip()
        if not src_username:
            continue
        src_username_lc = src_username.lower()
        # Filter gate. 'all' scope keeps everyone; 'specific' keeps
        # only listed users. The owner is treated like any other
        # source user — explicitly include "owner" in the filter to
        # sync owner, or use scope='owner' instead.
        if filter_set is not None and src_username_lc not in filter_set:
            continue
        src_is_owner = (src.get("kind") or "").lower() == "owner"
        src_backend_user_id = (src.get("backend_user_id") or "").strip()
        dest = _resolve_dest_user_for_sync(
            source_username=src_username,
            source_server_id=source_server_id,
            dest_server_id=dest_server_id,
            dest_by_username_lc=dst_by_username_lc,
        )
        if dest is None:
            notes.append(
                f"source user {src_username!r}: no destination match "
                "(no identity_map row + no username match); skipping"
            )
            continue
        dst_username = (dest.get("username") or "").strip()
        if not dst_username:
            continue
        dst_backend_user_id = (dest.get("backend_user_id") or "").strip()
        dst_is_owner = (dest.get("kind") or "").lower() == "owner"
        key = (src_username_lc, dst_username.lower())
        if key in seen:
            continue
        seen.add(key)
        pairs.append(_SubUserPair(
            source_username=src_username,
            source_backend_user_id=src_backend_user_id,
            source_is_owner=src_is_owner,
            dest_username=dst_username,
            dest_backend_user_id=dst_backend_user_id,
            dest_is_owner=dst_is_owner,
        ))

    if not pairs:
        # Filter list non-empty but matched zero users. Honour the
        # operator's intent (sync THESE users) rather than silently
        # falling back to owner-only.
        notes.append(
            "user_scope filter matched zero source users; "
            "cycle is a no-op"
        )
    return (pairs, notes)


def _resolve_dest_user_for_sync(
    *,
    source_username: str,
    source_server_id: str,
    dest_server_id: str,
    dest_by_username_lc: Dict[str, Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Lightweight 2-step destination user resolver for the sync
    worker. identity_map first (authoritative), case-insensitive
    username match second (skipped when ``strict_identity_resolution``
    is on). The full 5-step chain in services.identity.user_resolution is
    overkill here — the worker doesn't take per-job overrides and
    doesn't need the single-admin owner fallback the restorer uses."""
    try:
        from server.media_db import get_identity_maps_for_user
        for link in get_identity_maps_for_user(
            source_server_id, source_username,
        ) or []:
            if link.get("other_server_id") != dest_server_id:
                continue
            target_handle = (
                link.get("other_user_handle") or ""
            ).strip().lower()
            if target_handle and target_handle in dest_by_username_lc:
                return dest_by_username_lc[target_handle]
    except Exception:
        pass

    strict = False
    try:
        from services.tunables import strict_identity_resolution
        strict = bool(strict_identity_resolution())
    except Exception:
        strict = False
    if strict:
        return None
    norm = (source_username or "").strip().lower()
    if norm and norm in dest_by_username_lc:
        return dest_by_username_lc[norm]
    return None


def _reconcile_one_pair_watch(
    *,
    sub: Dict[str, Any],
    cycle_id: str,
    source_server_id: str,
    source_library_id: str,
    dest_server_id: str,
    dest_library_id: str,
    adapters: Dict[str, Any],
    dry_run: bool,
    bidirectional: bool,
    conflict_policy: str,
    user_pairs: List[_SubUserPair],
) -> Tuple[int, int, int]:
    """Reconcile one library pair. Returns (writes_issued,
    writes_logged, errors).

    MVP scope: observation-driven reconcile using mirror DB GUID
    overlap. Future enhancement: live polling via the adapter when
    the mirror is empty. For now if the mirror has zero items for
    either side we record a "skip — mirror empty" status and move
    on, so the worker fails closed (no surprise writes) when data
    is missing.
    """
    from server import sync_db, server_mirror_db
    conn = server_mirror_db.get_connection()
    # Pull both sides' items in one query each. The sync engine
    # consults mirror_items + mirror_item_guids for cross-server
    # resolution. The SELECT also pulls the hierarchy columns so items with
    # no shared GUID can still be paired by hierarchy.
    _ITEM_COLS = (
        "SELECT i.rating_key AS rk, i.title AS title, "
        "       i.item_type AS item_type, i.artist AS artist, "
        "       i.album AS album, i.show_title AS show_title, "
        "       i.season_number AS season_number, "
        "       i.episode_number AS episode_number, "
        "       i.grandparent_guid AS grandparent_guid, "
        "       g.guid AS guid "
        "FROM mirror_items i "
        "LEFT JOIN mirror_item_guids g "
        "  ON g.server_id = i.server_id AND g.rating_key = i.rating_key "
        "WHERE i.server_id = ? AND i.section_id = ?"
    )
    src_rows = conn.execute(
        _ITEM_COLS, (source_server_id, source_library_id),
    ).fetchall()
    dst_rows = conn.execute(
        _ITEM_COLS, (dest_server_id, dest_library_id),
    ).fetchall()
    if not src_rows or not dst_rows:
        return (0, 0, 0)

    # Build GUID indexes: {guid: rating_key}.
    src_guid_to_rk: Dict[str, str] = {}
    for r in src_rows:
        if r["guid"]:
            src_guid_to_rk[str(r["guid"])] = str(r["rk"])
    dst_guid_to_rk: Dict[str, str] = {}
    for r in dst_rows:
        if r["guid"]:
            dst_guid_to_rk[str(r["guid"])] = str(r["rk"])
    shared_guids = set(src_guid_to_rk.keys()) & set(dst_guid_to_rk.keys())

    # ── Build the matched (src_rk, dst_rk) pair set ──────────────────
    # Tier 1: GUID overlap. Tier 2: hierarchy - pairs items that
    # share no GUID but DO
    # share a hierarchy key. Lets sync reconcile e.g. an episode that
    # the two servers' metadata agents tagged with different GUIDs.
    matched_pairs: List[Tuple[str, str]] = []
    seen_src: Set[str] = set()
    seen_dst: Set[str] = set()
    for guid in shared_guids:
        s_rk = src_guid_to_rk[guid]
        d_rk = dst_guid_to_rk[guid]
        if s_rk in seen_src or d_rk in seen_dst:
            continue
        matched_pairs.append((s_rk, d_rk))
        seen_src.add(s_rk)
        seen_dst.add(d_rk)

    def _hier_key(r: Any) -> Optional[Tuple]:
        """Hierarchy match key for one mirror row. Returns None when
        the row has no usable hierarchy. grandparent_guid keyed when
        present (strongest); title/index keyed otherwise."""
        it = (r["item_type"] or "").lower()
        gpg = (r["grandparent_guid"] or "").strip()
        if it == "episode":
            sn = r["season_number"]
            en = r["episode_number"]
            if sn is None or en is None:
                return None
            if gpg:
                return ("ep-guid", gpg, int(sn), int(en))
            show = (r["show_title"] or "").strip().lower()
            if show:
                return ("ep-title", show, int(sn), int(en))
        elif it == "track":
            ttl = (r["title"] or "").strip().lower()
            alb = (r["album"] or "").strip().lower()
            if not ttl:
                return None
            if gpg:
                return ("tr-guid", gpg, alb, ttl)
            art = (r["artist"] or "").strip().lower()
            if art:
                return ("tr-title", art, alb, ttl)
        return None

    # Hierarchy index per side — only keys that are UNIQUE within the
    # side are usable (a collision means we can't tell which item the
    # key refers to). Build counts first, then keep singletons.
    def _hier_index(rows: Any) -> Dict[Tuple, str]:
        counts: Dict[Tuple, int] = {}
        first: Dict[Tuple, str] = {}
        for r in rows:
            k = _hier_key(r)
            if k is None:
                continue
            counts[k] = counts.get(k, 0) + 1
            first.setdefault(k, str(r["rk"]))
        return {k: rk for k, rk in first.items() if counts[k] == 1}

    src_hier = _hier_index(src_rows)
    dst_hier = _hier_index(dst_rows)
    for k, s_rk in src_hier.items():
        d_rk = dst_hier.get(k)
        if d_rk is None:
            continue
        if s_rk in seen_src or d_rk in seen_dst:
            continue
        matched_pairs.append((s_rk, d_rk))
        seen_src.add(s_rk)
        seen_dst.add(d_rk)

    if not matched_pairs:
        return (0, 0, 0)

    writes_issued = 0
    writes_logged = 0
    errors = 0

    # Read backend tokens + service_type once. ``service_type`` gates
    # the Plex managed-user safety belt below.
    src_adapter = adapters[source_server_id].adapter
    dst_adapter = adapters[dest_server_id].adapter
    src_token = adapters[source_server_id].token
    dst_token = adapters[dest_server_id].token
    src_service_type = (adapters[source_server_id].service_type or "plex").lower()
    dst_service_type = (adapters[dest_server_id].service_type or "plex").lower()
    from services.adapters import ItemRef, UserContext

    def _plex_managed_skip(
        tgt_server_id: str, tgt_lib_id: str, tgt_rk: str,
        tgt_uid: str, tgt_username: str, direction: str,
    ) -> None:
        """Record a 'skip with reason' row when a managed-user write
        targets a Plex backend. The worker has no per-user Plex token
        path today; writing with only the admin token would
        misattribute to the owner, which is worse than skipping."""
        sync_db.record_write(
            subscription_id=int(sub["id"]), poll_cycle_id=cycle_id,
            target_server_id=tgt_server_id,
            target_library_id=tgt_lib_id,
            target_item_rating_key=tgt_rk,
            target_user_id=tgt_uid,
            sync_type="watch_counts",
            issued=False,
            error=(
                f"Plex managed-user sync requires per-user token "
                f"(not yet wired): {direction} write for "
                f"user={tgt_username!r} skipped"
            ),
        )

    # Per-matched-pair × per-user-pair: read view_count on both sides
    # via the adapter, compute target, write whichever side is short.
    # ``matched_pairs`` carries both GUID-overlap + hierarchy-matched
    # items.
    for src_rk, dst_rk in matched_pairs[:_MAX_PAIRS_PER_CYCLE]:
        for pair in user_pairs:
            # Build per-pair UserContext for reads on each side. Admin
            # token is used in both cases; backend routes per
            # ``backend_user_id``. ``is_admin`` is True when the user
            # IS the owner (so PlexAdapter doesn't accidentally
            # demand a per-user token for the owner case).
            src_ctx = UserContext(
                backend_user_id=pair.source_backend_user_id,
                username=pair.source_username,
                auth_token=src_token,
                is_admin=pair.source_is_owner,
            )
            dst_ctx = UserContext(
                backend_user_id=pair.dest_backend_user_id,
                username=pair.dest_username,
                auth_token=dst_token,
                is_admin=pair.dest_is_owner,
            )
            try:
                src_state = src_adapter.get_view_state(
                    ItemRef(backend_item_id=src_rk),
                    user_context=src_ctx,
                )
                dst_state = dst_adapter.get_view_state(
                    ItemRef(backend_item_id=dst_rk),
                    user_context=dst_ctx,
                )
            except Exception as exc:
                errors += 1
                sync_db.record_write(
                    subscription_id=int(sub["id"]), poll_cycle_id=cycle_id,
                    target_server_id=dest_server_id,
                    target_library_id=dest_library_id,
                    target_item_rating_key=dst_rk,
                    target_user_id=pair.dest_backend_user_id,
                    sync_type="watch_counts",
                    issued=False,
                    error=(
                        f"read failed (user="
                        f"{pair.source_username}→{pair.dest_username}): "
                        f"{exc}"
                    ),
                )
                continue
            # JOBS-03: get_view_state returns (count, last_viewed_ts)
            # or None; the timestamp feeds the latest_wins policy and
            # is None when the adapter cannot read it.
            src_n = int(src_state[0]) if src_state else 0
            src_ts = src_state[1] if src_state else None
            dst_n = int(dst_state[0]) if dst_state else 0
            dst_ts = dst_state[1] if dst_state else None
            target = _compute_target(
                src_n, dst_n, conflict_policy,
                src_ts=src_ts, dst_ts=dst_ts,
            )
            # Decide which sides need writes. In bidirectional mode
            # both sides reach the same target; in directional mode
            # only dest.
            side_targets: List[Tuple[
                str, str, str, str, str, UserContext, bool, str, int, int,
            ]] = []
            if dst_n != target:
                side_targets.append((
                    dest_server_id, dest_library_id, dst_rk,
                    pair.dest_backend_user_id, pair.dest_username,
                    dst_ctx, pair.dest_is_owner, dst_service_type,
                    dst_n, target,
                ))
            if bidirectional and src_n != target:
                side_targets.append((
                    source_server_id, source_library_id, src_rk,
                    pair.source_backend_user_id, pair.source_username,
                    src_ctx, pair.source_is_owner, src_service_type,
                    src_n, target,
                ))
            for (
                tgt_server_id, tgt_lib_id, tgt_rk, tgt_uid,
                tgt_username, tgt_ctx, tgt_is_owner, tgt_service_type,
                current, want,
            ) in side_targets:
                # Plex managed-user safety belt — see _plex_managed_skip
                # above. The write would otherwise hit the owner.
                if (not tgt_is_owner) and tgt_service_type == "plex":
                    errors += 1
                    direction = (
                        "destination" if tgt_server_id == dest_server_id
                        else "source (bidirectional)"
                    )
                    _plex_managed_skip(
                        tgt_server_id, tgt_lib_id, tgt_rk,
                        tgt_uid, tgt_username, direction,
                    )
                    continue
                if dry_run:
                    writes_logged += 1
                    sync_db.record_write(
                        subscription_id=int(sub["id"]), poll_cycle_id=cycle_id,
                        target_server_id=tgt_server_id,
                        target_library_id=tgt_lib_id,
                        target_item_rating_key=tgt_rk,
                        target_user_id=tgt_uid,
                        sync_type="watch_counts",
                        before_value=float(current),
                        after_value=float(want),
                        issued=False,
                        error=None,
                    )
                    continue
                tgt_adapter = adapters[tgt_server_id].adapter
                try:
                    r = tgt_adapter.set_watched(
                        ItemRef(backend_item_id=tgt_rk),
                        view_count=want, last_viewed_at=None,
                        user_context=tgt_ctx, current_view_count=current,
                    )
                    if getattr(r, "success", False):
                        writes_issued += 1
                        sync_db.record_write(
                            subscription_id=int(sub["id"]), poll_cycle_id=cycle_id,
                            target_server_id=tgt_server_id,
                            target_library_id=tgt_lib_id,
                            target_item_rating_key=tgt_rk,
                            target_user_id=tgt_uid,
                            sync_type="watch_counts",
                            before_value=float(current),
                            after_value=float(want),
                            issued=True,
                            error=None,
                        )
                    else:
                        errors += 1
                        sync_db.record_write(
                            subscription_id=int(sub["id"]), poll_cycle_id=cycle_id,
                            target_server_id=tgt_server_id,
                            target_library_id=tgt_lib_id,
                            target_item_rating_key=tgt_rk,
                            target_user_id=tgt_uid,
                            sync_type="watch_counts",
                            before_value=float(current),
                            after_value=float(want),
                            issued=False,
                            error=str(getattr(r, "detail", "write returned no success")),
                        )
                except Exception as exc:
                    errors += 1
                    sync_db.record_write(
                        subscription_id=int(sub["id"]), poll_cycle_id=cycle_id,
                        target_server_id=tgt_server_id,
                        target_library_id=tgt_lib_id,
                        target_item_rating_key=tgt_rk,
                        target_user_id=tgt_uid,
                        sync_type="watch_counts",
                        before_value=float(current),
                        after_value=float(want),
                        issued=False,
                        error=f"set_watched raised: {exc}",
                    )

    return (writes_issued, writes_logged, errors)


def _reconcile_playlists(
    sub: Dict[str, Any], *, cycle_id: str,
) -> Dict[str, Any]:
    """Playlist auto-migrate reconciler.

    Behaviour:
      * Reads the subscription's ``playlist_sync_selections`` rows
        (operator-picked + auto-added).
      * Optionally discovers NEW playlists on source when
        ``auto_sync_new_playlists`` is 1, inserting them into
        playlist_sync_selections with added_by='auto'.
      * For each enabled selection, submits a playlist copy job via
        the existing ``services.playlist_copy`` orchestrator. In
        ``dry_run`` mode, records intent in sync_writes only.
      * For ``bidirectional`` subscriptions, this MVP runs the same
        loop once with source/dest swapped. The directional version
        only fires source→dest.

    User scoping for the MVP is owner-only; per-managed-user fan-out
    follows the same shape but needs the user identity map to map
    source usernames to dest usernames.
    """
    from server import sync_db
    pairs = _resolve_library_pairs(sub)
    if not pairs:
        return {"note": "no library pairs", "writes_issued": 0}

    dry_run = bool(sub.get("dry_run", True))
    bidirectional = bool(sub.get("bidirectional"))
    issued = 0
    logged = 0
    errors = 0

    # User-scope gate. Playlist sync is owner-only today (the
    # adapter contract has no per-managed-user playlist enumeration
    # path that doesn't also need a per-user Plex token). Honour
    # the operator's intent at the subscription level: when scope
    # is 'specific' AND 'owner' is not in the filter set, skip the
    # whole cycle — the operator explicitly excluded the only user
    # this reconciler can touch.
    scope = (sub.get("user_scope") or "owner").strip().lower()
    user_notes: List[str] = []
    if scope == "specific":
        raw_filter = sub.get("user_filter") or []
        filter_set = {
            str(x).strip().lower() for x in raw_filter
            if x and str(x).strip()
        }
        if "owner" not in filter_set:
            return {
                "pairs":         len(pairs),
                "writes_issued": 0,
                "writes_logged": 0,
                "errors":        0,
                "dry_run":       dry_run,
                "user_scope":    scope,
                "user_notes":    [
                    "playlist sync is owner-only today; scope='specific' "
                    "filter does not include 'owner' so cycle is a no-op"
                ],
            }
    elif scope == "all":
        user_notes.append(
            "playlist sync currently fires for owner only; managed-user "
            "playlist sync follows once per-user Plex token plumbing lands"
        )

    # Iterate each library pair within this subscription's scope.
    for (src_lib_id, src_lib_name, dst_lib_id, dst_lib_name) in pairs:
        try:
            n_i, n_l, n_e = _migrate_playlists_for_pair(
                sub=sub, cycle_id=cycle_id,
                source_server_id=sub["source_server_id"],
                source_library_id=src_lib_id,
                dest_server_id=sub["dest_server_id"],
                dest_library_id=dst_lib_id,
                dry_run=dry_run,
                direction="forward",
            )
            issued += n_i; logged += n_l; errors += n_e
            if bidirectional:
                n_i, n_l, n_e = _migrate_playlists_for_pair(
                    sub=sub, cycle_id=cycle_id,
                    source_server_id=sub["dest_server_id"],
                    source_library_id=dst_lib_id,
                    dest_server_id=sub["source_server_id"],
                    dest_library_id=src_lib_id,
                    dry_run=dry_run,
                    direction="reverse",
                )
                issued += n_i; logged += n_l; errors += n_e
        except Exception as exc:
            log.exception(
                "playlist sync: pair %s -> %s failed: %s",
                src_lib_id, dst_lib_id, exc,
            )
            errors += 1
    return {
        "pairs":         len(pairs),
        "writes_issued": issued,
        "writes_logged": logged,
        "errors":        errors,
        "dry_run":       dry_run,
        "user_scope":    scope,
        "user_notes":    user_notes,
    }


def _migrate_playlists_for_pair(
    *,
    sub: Dict[str, Any],
    cycle_id: str,
    source_server_id: str,
    source_library_id: str,
    dest_server_id: str,
    dest_library_id: str,
    dry_run: bool,
    direction: str,
) -> Tuple[int, int, int]:
    """One library pair, one direction (forward or reverse for
    bidirectional subs). Returns (writes_issued, writes_logged,
    errors)."""
    from server import sync_db
    try:
        from services import playlist_copy
    except Exception as exc:
        log.warning("playlist_copy import failed: %s", exc)
        return (0, 0, 1)

    # Discover new playlists on the source side if auto_sync_new
    # is enabled. The discovery uses the same list_user_playlists
    # the Playlist Mgmt UI consumes so we get cache-aware reads.
    auto_sync_new = bool(sub.get("auto_sync_new_playlists"))
    sub_id = int(sub["id"])

    # Owner-only scope for MVP. user_id resolves to the source's
    # owner via the server registry below.
    try:
        from server import server_registry
        src_conn = server_registry.connect_registered_server(
            source_server_id, log,
        )
        src_owner_uid = src_conn.adapter.server_identity().owner_user_id or ""
    except Exception as exc:
        return (0, 0, 1)

    try:
        listing = playlist_copy.list_user_playlists(
            server_id=source_server_id, user_id=src_owner_uid,
        )
    except Exception as exc:
        log.warning(
            "playlist sync: list_user_playlists failed for %s/%s: %s",
            source_server_id, src_owner_uid, exc,
        )
        return (0, 0, 1)
    source_playlists = list(listing.playlists or [])

    # Index existing selections so we don't duplicate-insert.
    selections = sync_db.list_playlist_selections(subscription_id=sub_id)
    selected_ids = {s["source_playlist_id"] for s in selections}

    # Auto-add new playlists when requested. Only fires in the forward
    # direction — bidirectional reverse pulls from a different source
    # but writes to the same subscription's selections (operator might
    # be confused if reverse-direction syncs add to selections). MVP:
    # only forward direction populates selections.
    if auto_sync_new and direction == "forward":
        for pl in source_playlists:
            pid = str(pl.get("playlist_id") or "")
            if not pid or pid in selected_ids:
                continue
            # Filter by library scope: include only playlists whose
            # primary_library_id matches the pair's source library.
            # Playlists without a primary_library_id are conservatively
            # included so we don't silently drop them.
            primary = str(pl.get("primary_library_id") or "")
            if primary and source_library_id and primary != source_library_id:
                continue
            sync_db.add_playlist_selection(
                subscription_id=sub_id,
                source_playlist_id=pid,
                source_playlist_name=str(pl.get("name") or ""),
                added_by="auto",
                enabled=True,
            )

    # Re-read selections after the auto-add so the loop below picks
    # them up.
    selections = sync_db.list_playlist_selections(
        subscription_id=sub_id, enabled_only=True,
    )

    issued = 0
    logged = 0
    errors = 0
    source_by_id = {
        str(p.get("playlist_id") or ""): p for p in source_playlists
    }
    # The selection list is stored per-subscription keyed by the
    # FORWARD source's playlist ids. On the reverse leg of a
    # bidirectional sub the source is the original destination, whose
    # ids differ - so the reverse leg matches each selection to a
    # reverse-source playlist by NAME (the same identity copy_playlist
    # itself uses to create-or-merge).
    source_by_name = {
        str(p.get("name") or "").strip().lower(): p
        for p in source_playlists
        if str(p.get("name") or "").strip()
    }
    for sel in selections:
        sel_pid = sel["source_playlist_id"]
        if direction == "reverse":
            sel_name = str(sel.get("source_playlist_name") or "").strip().lower()
            pl = source_by_name.get(sel_name) if sel_name else None
        else:
            pl = source_by_id.get(sel_pid)
        if pl is None:
            errors += 1
            sync_db.record_write(
                subscription_id=sub_id, poll_cycle_id=cycle_id,
                target_server_id=dest_server_id,
                target_library_id=dest_library_id,
                target_item_rating_key=sel_pid,
                target_user_id="owner",
                sync_type="playlists",
                issued=False,
                error="source playlist no longer present",
            )
            continue
        # Copy FROM the id the resolved playlist carries on the current
        # source side - the selection's stored id is the forward
        # source's and is wrong for the reverse leg.
        pid = str(pl.get("playlist_id") or sel_pid)
        if dry_run:
            logged += 1
            sync_db.record_write(
                subscription_id=sub_id, poll_cycle_id=cycle_id,
                target_server_id=dest_server_id,
                target_library_id=dest_library_id,
                target_item_rating_key=pid,
                target_user_id="owner",
                sync_type="playlists",
                issued=False,
                error=None,
            )
            continue
        try:
            # Submit the copy via playlist_copy. The orchestrator
            # already handles GUID-based item resolution + create-or-
            # append on the destination.
            from server import server_registry
            dst_conn = server_registry.connect_registered_server(
                dest_server_id, log,
            )
            dst_owner_uid = dst_conn.adapter.server_identity().owner_user_id or ""
            result = playlist_copy.copy_playlist(
                source_server_id=source_server_id,
                source_user_id=src_owner_uid,
                source_playlist_id=pid,
                dest_server_id=dest_server_id,
                dest_user_id=dst_owner_uid,
                dest_playlist_name=None,  # preserve source name
                # sync subscriptions are an *ongoing*
                # reconcile — every cycle re-runs against the same
                # source playlist. The default "create" policy would
                # spawn a duplicate-named playlist on every cycle.
                # "merge" looks up the existing same-name playlist
                # on the destination, dedups items by backend_item_id,
                # and appends only the missing ones. First-cycle
                # behaviour (no existing playlist) is unchanged: it
                # falls through to create.
                on_existing="merge",
                # Route playlist_copy's log records to sync.log
                # instead of letting them propagate to the running
                # job's runtime.log. Operator-initiated copies
                # (Playlist Management, restore-mode playlists) leave
                # this None so their records still land in runtime.log.
                logger=log,
            )
            # copy_playlist returns a dict matching PlaylistCopyResult.
            # ``success=True`` means it landed; otherwise ``errors``
            # carries one or more reason strings.
            if isinstance(result, dict) and result.get("success"):
                issued += 1
                sync_db.record_write(
                    subscription_id=sub_id, poll_cycle_id=cycle_id,
                    target_server_id=dest_server_id,
                    target_library_id=dest_library_id,
                    target_item_rating_key=pid,
                    target_user_id=dst_owner_uid,
                    sync_type="playlists",
                    issued=True,
                    error=None,
                )
            else:
                errors += 1
                err_msg = "copy returned no success"
                if isinstance(result, dict):
                    errs = result.get("errors") or []
                    if errs:
                        err_msg = "; ".join(str(e) for e in errs)
                sync_db.record_write(
                    subscription_id=sub_id, poll_cycle_id=cycle_id,
                    target_server_id=dest_server_id,
                    target_library_id=dest_library_id,
                    target_item_rating_key=pid,
                    target_user_id=dst_owner_uid,
                    sync_type="playlists",
                    issued=False,
                    error=err_msg,
                )
        except Exception as exc:
            errors += 1
            sync_db.record_write(
                subscription_id=sub_id, poll_cycle_id=cycle_id,
                target_server_id=dest_server_id,
                target_library_id=dest_library_id,
                target_item_rating_key=pid,
                target_user_id="owner",
                sync_type="playlists",
                issued=False,
                error=f"copy raised: {exc}",
            )

    return (issued, logged, errors)


def _compute_target(
    src: int,
    dst: int,
    policy: str,
    *,
    src_ts: Optional[float] = None,
    dst_ts: Optional[float] = None,
) -> int:
    """Pure function: pick the target count for an item under a
    conflict policy.

    ``src_ts`` / ``dst_ts`` are each side's last-viewed timestamp
    (unix epoch) when the adapter could read it; the ``latest_wins``
    policy needs them. ``source_of_truth`` encodes its direction via
    which server the subscription names as source."""
    if policy == "sum":
        return int(src) + int(dst)
    if policy == "source_of_truth":
        # Source wins. Direction is encoded by which server the
        # subscription names as source.
        return int(src)
    if policy == "latest_wins":
        # JOBS-03: whichever side's count changed most recently wins -
        # this is the only policy that can propagate a count DECREASE.
        # last-viewed time is the available proxy for "last changed".
        # Decide by timestamp only when BOTH sides reported one; with
        # an incomplete pair we cannot tell which is newer, so fall
        # back to max (never lose a play). Before JOBS-03 this policy
        # silently fell through to the max default.
        if src_ts is not None and dst_ts is not None:
            return int(src) if src_ts >= dst_ts else int(dst)
        return max(int(src), int(dst))
    # Default: max — never lose a play.
    return max(int(src), int(dst))
