"""media.db identity layer.

``app_user_uuid`` generation, the cross-server user identity map and
its CRUD, backend_user_id auto-linking, and the server-id rewriters
that run on a server rename / boot migration.
"""

from __future__ import annotations

import sqlite3
import time
from typing import Any, Dict, List, Optional, Tuple

from services.identity.user_uuid import (
    build_app_user_uuid,
    generate_user_key,
    server_uid_from_app_user_uuid,
    slugify_host_name,
)

from . import _core
from ._core import _DB_LOCK, _require_conn, log


# ── Test / diagnostic helpers ────────────────────────────────────────────────

# ── app_user_uuid helpers (USER-MGMT-IDENTITY-AUDIT follow-up) ──────────────
#
# The app-generated stable user identifier (``app_user_uuid``) is the
# cross-server identity anchor populated on every managed_users /
# server_users row and used as the primary key in user_identity_map
# pairs. Format and design rationale live in :mod:`services.identity.user_uuid`.

def _server_host_name(server_id: str) -> str:
    """Look up the friendly server name for the HostNameSlug portion of
    an app_user_uuid. Best-effort: returns the empty string (which the
    slugifier collapses to ``"Unnamed"``) when the registry can't be
    read or the server id is unknown. Avoids hard-coupling media_db
    init order to server_registry being fully available."""
    try:
        # Late import to avoid the legacy media_db <- server_registry
        # cycle: server_registry already imports media_db for
        # ``rewrite_server_ids_in_identity_map`` and friends, and
        # importing it at module load here would invert the dependency
        # under some test orders.
        from server import server_registry
        row = server_registry.get_server_by_id(server_id, include_token=False)
        if not row:
            return ""
        return str(row.get("name") or "")
    except Exception:
        return ""


def generate_unique_app_user_uuid(
    *,
    service_type: str,
    server_id: str,
    server_uid: str,
    max_attempts: int = 8,
) -> str:
    """Generate an ``app_user_uuid`` that is not already present in
    ``managed_users.app_user_uuid`` OR ``server_users.app_user_uuid``.

    Retries up to ``max_attempts`` times on UNIQUE collision (the
    8-hex userkey has ~4.29B distinct values per server so a single-
    retry case is essentially never hit in practice; the loop is a
    correctness guarantee, not a hot path).

    Raises ``RuntimeError`` if every attempt collides - this only
    happens when the database is corrupt enough that the partial
    unique indexes are broken, in which case the caller wants the
    loud failure rather than silently inserting a duplicate.

    ``server_id`` is the registered server's id used to look up the
    friendly name; ``server_uid`` is the same string when developer's
    prefixed UID scheme is in play (the v9 boot migration rewrites
    bare UUIDs to ``<service>_<uuid>`` form), so both args usually
    carry the same value. They are kept distinct so a future caller
    that wants to mint a UUID for a row before the server is fully
    registered (e.g. a test fixture) can pass an explicit server_uid.
    """
    host_name = _server_host_name(server_id)
    conn = _require_conn()
    for _ in range(max(1, int(max_attempts))):
        user_key = generate_user_key()
        candidate = build_app_user_uuid(
            service_type=service_type,
            host_name=host_name,
            server_uid=server_uid,
            user_key=user_key,
        )
        # Probe both tables; either hit means we need a fresh key.
        # Cheap O(1) lookups via the partial unique indexes.
        hit_mu = conn.execute(
            "SELECT 1 FROM managed_users WHERE app_user_uuid = ? LIMIT 1",
            (candidate,),
        ).fetchone()
        hit_su = conn.execute(
            "SELECT 1 FROM server_users WHERE app_user_uuid = ? LIMIT 1",
            (candidate,),
        ).fetchone()
        if hit_mu is None and hit_su is None:
            return candidate
    raise RuntimeError(
        "generate_unique_app_user_uuid: exhausted retries; "
        "the app_user_uuid space appears exhausted for this server. "
        "Inspect managed_users.app_user_uuid for duplicates."
    )


def _backfill_app_user_uuids() -> None:
    """Walk ``managed_users`` and ``server_users`` for rows with NULL
    ``app_user_uuid`` and fill them with freshly-generated UUIDs.

    Idempotent: a second call after every row is filled sees an empty
    work queue and exits immediately. Safe to call from
    :func:`init_media_db` on every boot.

    Best-effort: per-row insert failures are logged and the loop
    continues. A row left NULL stays NULL and will be retried on the
    next call; resolution paths fall back to handle matching until the
    row is filled.
    """
    if _core._conn is None:
        return  # init still in flight; caller will retry
    rows_mu = _core._conn.execute(
        "SELECT server_id, service_type, username "
        "FROM managed_users WHERE app_user_uuid IS NULL"
    ).fetchall()
    rows_su = _core._conn.execute(
        "SELECT server_id, backend, user_handle "
        "FROM server_users WHERE app_user_uuid IS NULL"
    ).fetchall()
    if not rows_mu and not rows_su:
        return
    log.info(
        "Backfilling app_user_uuid: %d managed_users row(s) + "
        "%d server_users row(s).",
        len(rows_mu), len(rows_su),
    )
    with _DB_LOCK:
        for r in rows_mu:
            try:
                uuid = generate_unique_app_user_uuid(
                    service_type=r["service_type"] or "plex",
                    server_id=r["server_id"],
                    server_uid=r["server_id"],
                )
                _core._conn.execute(
                    "UPDATE managed_users SET app_user_uuid = ? "
                    "WHERE server_id = ? AND username = ? "
                    "AND app_user_uuid IS NULL",
                    (uuid, r["server_id"], r["username"]),
                )
            except Exception:
                log.exception(
                    "_backfill_app_user_uuids: failed for managed_users "
                    "(server=%r, username=%r); will retry next boot",
                    r["server_id"], r["username"],
                )
        for r in rows_su:
            try:
                uuid = generate_unique_app_user_uuid(
                    service_type=r["backend"] or "plex",
                    server_id=r["server_id"],
                    server_uid=r["server_id"],
                )
                _core._conn.execute(
                    "UPDATE server_users SET app_user_uuid = ? "
                    "WHERE server_id = ? AND user_handle = ? "
                    "AND app_user_uuid IS NULL",
                    (uuid, r["server_id"], r["user_handle"]),
                )
            except Exception:
                log.exception(
                    "_backfill_app_user_uuids: failed for server_users "
                    "(server=%r, handle=%r); will retry next boot",
                    r["server_id"], r["user_handle"],
                )


def get_managed_user_app_uuid(
    server_id: str, username: str,
) -> Optional[str]:
    """Return the ``app_user_uuid`` for the ``managed_users`` row
    matching ``(server_id, username)``, or ``None`` if the row does not
    exist (or has not yet been backfilled - which should not happen on
    a fully-initialised install but is defensive)."""
    if not (server_id or "").strip() or not (username or "").strip():
        return None
    conn = _require_conn()
    row = conn.execute(
        "SELECT app_user_uuid FROM managed_users "
        "WHERE server_id = ? AND username = ?",
        (str(server_id), str(username)),
    ).fetchone()
    if row is None:
        return None
    return row["app_user_uuid"]


def get_server_user_app_uuid(
    server_id: str, user_handle: str,
) -> Optional[str]:
    """``managed_users``-sibling lookup against ``server_users``."""
    if not (server_id or "").strip():
        return None
    conn = _require_conn()
    row = conn.execute(
        "SELECT app_user_uuid FROM server_users "
        "WHERE server_id = ? AND user_handle = ?",
        (str(server_id), str(user_handle or "")),
    ).fetchone()
    if row is None:
        return None
    return row["app_user_uuid"]


def get_row_by_app_user_uuid(app_user_uuid: str) -> Optional[Dict[str, Any]]:
    """Resolve an ``app_user_uuid`` back to its (server, user) coordinates.

    Returns a dict ``{table, server_id, handle, display_name, service_type,
    role, backend_user_id, app_user_uuid}`` or ``None`` if the UUID does
    not exist on either table. The ``table`` field is ``"managed_users"``
    or ``"server_users"`` so the caller knows which side to address.

    Used by the resolution helper and the UI identity-links panel to
    walk identity_map edges back to the row that holds the credentials
    and display fields.
    """
    if not (app_user_uuid or "").strip():
        return None
    conn = _require_conn()
    row = conn.execute(
        "SELECT server_id, username AS handle, display_name, service_type, "
        "kind AS role, app_user_uuid "
        "FROM managed_users WHERE app_user_uuid = ?",
        (app_user_uuid,),
    ).fetchone()
    if row is not None:
        # Pull backend_user_id from the same row.
        bk = conn.execute(
            "SELECT backend_user_id FROM managed_users "
            "WHERE app_user_uuid = ?",
            (app_user_uuid,),
        ).fetchone()
        return {
            "table":            "managed_users",
            "server_id":        row["server_id"],
            "handle":           row["handle"],
            "display_name":     row["display_name"],
            "service_type":     row["service_type"],
            "role":             row["role"],
            "backend_user_id":  bk["backend_user_id"] if bk else None,
            "app_user_uuid":    row["app_user_uuid"],
        }
    row = conn.execute(
        "SELECT server_id, user_handle AS handle, display_name, backend AS service_type, "
        "role, backend_user_id, app_user_uuid "
        "FROM server_users WHERE app_user_uuid = ?",
        (app_user_uuid,),
    ).fetchone()
    if row is not None:
        return {
            "table":            "server_users",
            "server_id":        row["server_id"],
            "handle":           row["handle"],
            "display_name":     row["display_name"],
            "service_type":     row["service_type"],
            "role":             row["role"],
            "backend_user_id":  row["backend_user_id"],
            "app_user_uuid":    row["app_user_uuid"],
        }
    return None


# ── user_identity_map (v12 shape: app_user_uuid pairs) ──────────────────────
#
# Storage shape after migration v12: ``(user_a_uuid, user_b_uuid,
# source, created_at)``. Lookups walk both directions
# (a -> b and b -> a) so the caller doesn't need to know which side of
# the pair the user lives on.

def _equivalence_class_for_uuid(app_user_uuid: str) -> List[str]:
    """Return every ``app_user_uuid`` reachable from the supplied one
    via ``user_identity_map`` edges (BFS).

    Includes the starting UUID itself. Each row in identity_map is a
    bidirectional edge, so a single row connects both endpoints'
    classes. Empty list when ``app_user_uuid`` is missing or itself
    has no edges.

    Used by :func:`add_identity_map` to fan out a new manual edge
    across the bipartite product of the two classes it joins so
    every transitively-equivalent pair lands as a row in identity_map.
    """
    if not (app_user_uuid or "").strip():
        return []
    conn = _require_conn()
    seen: set = {app_user_uuid}
    frontier: List[str] = [app_user_uuid]
    while frontier:
        next_frontier: List[str] = []
        for uuid in frontier:
            rows = conn.execute(
                """
                SELECT user_a_uuid, user_b_uuid FROM user_identity_map
                WHERE user_a_uuid = ? OR user_b_uuid = ?
                """,
                (uuid, uuid),
            ).fetchall()
            for r in rows:
                other = r["user_b_uuid"] if r["user_a_uuid"] == uuid else r["user_a_uuid"]
                if other not in seen:
                    seen.add(other)
                    next_frontier.append(other)
        frontier = next_frontier
    return sorted(seen)


def add_identity_map(
    *,
    user_a_uuid: Optional[str] = None,
    user_b_uuid: Optional[str] = None,
    server_a_id: Optional[str] = None,
    user_a_handle: Optional[str] = None,
    server_b_id: Optional[str] = None,
    user_b_handle: Optional[str] = None,
    source: str = "manual",
) -> Optional[int]:
    """Insert one (A, B) identity-link pair plus its transitive closure.

    Two call shapes are supported so existing callers in
    developer's CRUD endpoints (which build the pair from
    ``(server_id, user_handle)`` tuples) keep working unchanged:

      * UUID-direct:  ``add_identity_map(user_a_uuid=..., user_b_uuid=...)``
      * Tuple-resolve: ``add_identity_map(server_a_id=..., user_a_handle=...,
                       server_b_id=..., user_b_handle=...)``

    When the tuple-resolve form is used, the helper looks up each side's
    ``app_user_uuid`` from ``managed_users`` first, then falls back to
    ``server_users``. Raises ``ValueError`` if either side cannot be
    resolved to a UUID (end user must register / sync the server first).

    Transitive fanout (v13+): after the primary row is written, the
    helper walks the equivalence class of each endpoint and writes one
    auto_copy row per missing bipartite pair. Each fanned-out row
    carries ``derived_from_id`` pointing at the primary row so
    :func:`delete_identity_map` can cascade-delete them cleanly when
    the parent is removed.

    Returns the new row id of the PRIMARY pair on insert, ``None``
    when the primary pair already exists (UNIQUE conflict; the
    transitive fanout is still attempted defensively in case the
    equivalence class grew since the last write).
    """
    a_uuid = (user_a_uuid or "").strip() or None
    b_uuid = (user_b_uuid or "").strip() or None
    if a_uuid is None:
        if not (server_a_id or "").strip() or not (user_a_handle or "").strip():
            raise ValueError(
                "add_identity_map: provide either user_a_uuid OR both "
                "server_a_id and user_a_handle."
            )
        a_uuid = (
            get_managed_user_app_uuid(server_a_id, user_a_handle)
            or get_server_user_app_uuid(server_a_id, user_a_handle)
        )
        if not a_uuid:
            raise ValueError(
                f"add_identity_map: no app_user_uuid for "
                f"(server={server_a_id!r}, handle={user_a_handle!r}); "
                f"sync managed_users for that server first."
            )
    if b_uuid is None:
        if not (server_b_id or "").strip() or not (user_b_handle or "").strip():
            raise ValueError(
                "add_identity_map: provide either user_b_uuid OR both "
                "server_b_id and user_b_handle."
            )
        b_uuid = (
            get_managed_user_app_uuid(server_b_id, user_b_handle)
            or get_server_user_app_uuid(server_b_id, user_b_handle)
        )
        if not b_uuid:
            raise ValueError(
                f"add_identity_map: no app_user_uuid for "
                f"(server={server_b_id!r}, handle={user_b_handle!r}); "
                f"sync managed_users for that server first."
            )
    if a_uuid == b_uuid:
        raise ValueError(
            "add_identity_map: refuse to map a UUID to itself."
        )
    src = (source or "manual").strip().lower()
    if src not in ("manual", "auto_copy"):
        src = "manual"
    # Canonicalise the primary insert by sorting (a_uuid, b_uuid)
    # lexicographically before writing. The UNIQUE constraint on the
    # table is on the ORDERED (user_a_uuid, user_b_uuid) tuple, so
    # SQLite treats (A,B) and (B,A) as distinct rows. Without
    # canonicalisation, a caller like the auto-link helper (which
    # iterates pairs sorted by server_id, not UUID) could write
    # (K, M) as the primary row while the transitive fanout writes
    # the canonical (M, K) as a derived row, and the read helper's
    # bidirectional walk would return BOTH rows as separate links
    # for the same logical pair. Canonicalising here makes (A,B) and
    # (B,A) inputs from any caller collapse to the same stored row.
    primary_a, primary_b = (a_uuid, b_uuid) if a_uuid < b_uuid else (b_uuid, a_uuid)
    conn = _require_conn()
    # Compute equivalence classes BEFORE the primary insert. After the
    # insert the two classes are merged (the new edge bridges them),
    # so capturing each side's class beforehand is the only way to
    # know which cross-pairs need to be fanned out.
    class_a = _equivalence_class_for_uuid(a_uuid)
    class_b = _equivalence_class_for_uuid(b_uuid)
    # Primary insert (canonical order).
    primary_id: Optional[int] = None
    try:
        with _DB_LOCK:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO user_identity_map (
                    user_a_uuid, user_b_uuid, source, created_at, derived_from_id
                ) VALUES (?, ?, ?, ?, NULL)
                """,
                (primary_a, primary_b, src, time.time()),
            )
        if (cur.rowcount or 0) > 0 and cur.lastrowid:
            primary_id = int(cur.lastrowid)
    except Exception:
        log.exception("add_identity_map: primary insert failed")
        raise
    # Transitive fanout. Even when the primary insert was a duplicate
    # no-op (primary_id is None), we still fan out so a class that
    # has grown since the last fan attempt catches up.
    #
    # The fanout target is the BIPARTITE PRODUCT of class_a and
    # class_b minus the pair we just inserted. Each derived row
    # carries derived_from_id pointing at the primary row when one
    # exists; otherwise NULL (the cascade has nothing to track).
    if class_a and class_b:
        now = time.time()
        # Compute the parent id used for derived_from on every
        # fanned-out row. When the primary was a no-op, look up the
        # existing row id for the (a, b) ordered pair so the cascade
        # still works.
        parent_id = primary_id
        if parent_id is None:
            # Look up the existing primary row by its CANONICAL
            # ordered pair (primary_a, primary_b), not (a_uuid,
            # b_uuid). A lookup on (a_uuid, b_uuid) would miss the
            # canonical row when the caller passed the pair in the
            # non-canonical order.
            existing = conn.execute(
                "SELECT id FROM user_identity_map "
                "WHERE user_a_uuid = ? AND user_b_uuid = ?",
                (primary_a, primary_b),
            ).fetchone()
            if existing:
                parent_id = int(existing["id"])
        # Walk every cross-pair. Skip the primary's canonical pair so
        # we don't write a derived duplicate of the parent itself.
        # Pre-fix, the skip-check compared against (a_uuid, b_uuid) in
        # the order the caller passed them; combined with the
        # non-canonicalised primary insert, that allowed the fanout
        # to write the canonical (M, K) row when the primary was the
        # non-canonical (K, M). Both rows then coexisted as separate
        # links for the same logical pair.
        for x in class_a:
            for y in class_b:
                if x == y:
                    continue
                # Order the pair lexicographically so (X, Y) and (Y, X)
                # collapse to one canonical row. The UNIQUE constraint
                # on (user_a_uuid, user_b_uuid) is on the ordered pair;
                # without canonicalisation we would write both directions.
                lo, hi = (x, y) if x < y else (y, x)
                if lo == primary_a and hi == primary_b:
                    continue  # the primary row (canonical match)
                try:
                    with _DB_LOCK:
                        conn.execute(
                            """
                            INSERT OR IGNORE INTO user_identity_map (
                                user_a_uuid, user_b_uuid, source, created_at,
                                derived_from_id
                            ) VALUES (?, ?, 'auto_copy', ?, ?)
                            """,
                            (lo, hi, now, parent_id),
                        )
                except Exception:
                    log.exception(
                        "add_identity_map: transitive fanout row "
                        "(%r, %r) insert failed; continuing.",
                        lo, hi,
                    )
    # After the edge is wired and the transitive fanout has merged
    # equivalence classes, copy any populated PIN onto rows in the
    # class whose PIN columns are empty. Additive only; gated by the
    # ``auto_backfill_pin_from_identity_links`` tunable. Best-effort:
    # a failure here doesn't unwind the edge insert.
    try:
        from .managed_users import backfill_pin_across_links
        backfill_pin_across_links(a_uuid)
    except Exception:
        log.exception(
            "add_identity_map: PIN backfill across links failed for "
            "edge (%r, %r); the identity_map edge IS persisted.",
            a_uuid, b_uuid,
        )
    return primary_id


def delete_identity_map(map_id: int) -> bool:
    """Remove one identity-map row by id and cascade to its derived
    children (rows with ``derived_from_id`` equal to this row's id).

    The cascade only touches rows the helper itself wrote during a
    transitive fanout; auto_link-by-backend_user_id rows and manual
    rows the end user typed separately are never collateral damage.

    Returns True when at least the named row was deleted, False when
    ``map_id`` did not exist.
    """
    try:
        map_id = int(map_id)
    except (TypeError, ValueError):
        return False
    conn = _require_conn()
    # Cascade first so the parent row's id is still valid for the
    # children's WHERE clause. SQLite doesn't enforce ON DELETE CASCADE
    # on FKs added via ALTER TABLE ADD COLUMN; we do it explicitly.
    # Both deletes run under _DB_LOCK so they serialise with every
    # other media.db writer.
    with _DB_LOCK:
        try:
            children = conn.execute(
                "DELETE FROM user_identity_map WHERE derived_from_id = ?",
                (map_id,),
            )
            cascaded = children.rowcount or 0
        except Exception:
            log.exception(
                "delete_identity_map: cascade-delete for parent id=%r "
                "failed; continuing with parent delete.",
                map_id,
            )
            cascaded = 0
        cur = conn.execute(
            "DELETE FROM user_identity_map WHERE id = ?", (map_id,),
        )
    if (cur.rowcount or 0) > 0 and cascaded:
        log.info(
            "delete_identity_map: removed parent id=%r and %d cascaded "
            "child row(s).", map_id, cascaded,
        )
    return (cur.rowcount or 0) > 0


def list_identity_maps() -> List[Dict[str, Any]]:
    """Return every identity-map row in insertion order.

    Each row carries both the v12 UUID pair AND the resolved
    ``(server_id, user_handle)`` tuples on each side, so the v11-shaped
    UI surfaces (developer's UserMappingPanel) keep rendering without a
    payload migration. UUIDs that no longer resolve to a row
    (server removed, user deleted) surface ``server_id=None`` /
    ``user_handle=None`` on that side; the panel can show a dimmed
    "unresolved" badge in that case.
    """
    conn = _require_conn()
    rows = conn.execute(
        """
        SELECT id, user_a_uuid, user_b_uuid, source, created_at
        FROM user_identity_map
        ORDER BY created_at ASC, id ASC
        """,
    ).fetchall()
    out: List[Dict[str, Any]] = []
    for r in rows:
        a_row = get_row_by_app_user_uuid(r["user_a_uuid"]) or {}
        b_row = get_row_by_app_user_uuid(r["user_b_uuid"]) or {}
        out.append({
            "id":             int(r["id"]),
            "user_a_uuid":    r["user_a_uuid"],
            "user_b_uuid":    r["user_b_uuid"],
            "server_a_id":    a_row.get("server_id"),
            "user_a_handle":  a_row.get("handle"),
            "server_b_id":    b_row.get("server_id"),
            "user_b_handle":  b_row.get("handle"),
            "source":         r["source"],
            "created_at":     float(r["created_at"]),
        })
    return out


def get_identity_maps_for_user(
    server_id: str, user_handle: str,
) -> List[Dict[str, Any]]:
    """Return every (other_server, other_handle, other_uuid) row linked
    to the supplied (server_id, user_handle).

    Resolves (server_id, user_handle) -> app_user_uuid first, then
    walks both A->B and B->A so the caller doesn't need to know which
    side of the pair the user lives on. Empty list when the source
    has no app_user_uuid yet (pre-backfill row) or no map entries.
    """
    if not (server_id or "").strip() or not (user_handle or "").strip():
        return []
    my_uuid = (
        get_managed_user_app_uuid(server_id, user_handle)
        or get_server_user_app_uuid(server_id, user_handle)
    )
    if not my_uuid:
        return []
    return get_identity_maps_for_uuid(my_uuid)


def get_identity_maps_for_uuid(
    app_user_uuid: str,
) -> List[Dict[str, Any]]:
    """Same as :func:`get_identity_maps_for_user` but takes the
    app_user_uuid directly. Preferred entry point for the resolver:
    one lookup instead of two."""
    if not (app_user_uuid or "").strip():
        return []
    conn = _require_conn()
    rows = conn.execute(
        """
        SELECT id, user_a_uuid, user_b_uuid, source, created_at
        FROM user_identity_map
        WHERE user_a_uuid = ? OR user_b_uuid = ?
        ORDER BY created_at ASC, id ASC
        """,
        (app_user_uuid, app_user_uuid),
    ).fetchall()
    out: List[Dict[str, Any]] = []
    for r in rows:
        if r["user_a_uuid"] == app_user_uuid:
            other_uuid = r["user_b_uuid"]
        else:
            other_uuid = r["user_a_uuid"]
        other_row = get_row_by_app_user_uuid(other_uuid) or {}
        out.append({
            "id":                 int(r["id"]),
            "other_user_uuid":    other_uuid,
            "other_server_id":    other_row.get("server_id"),
            "other_user_handle":  other_row.get("handle"),
            "other_display_name": other_row.get("display_name"),
            "other_service_type": other_row.get("service_type"),
            "other_role":         other_row.get("role"),
            "source":             r["source"],
            "created_at":         float(r["created_at"]),
        })
    return out


# ── Auto-link by backend_user_id (R-2) ──────────────────────────────────────
#
# Plex.tv issues stable numeric userIDs per human. Two managed_users
# rows with the same (service_type, backend_user_id) across distinct
# server_ids ARE the same human by the backend's own definition. This
# helper walks for those pairs and writes auto_copy identity_map rows.
# Idempotent (INSERT OR IGNORE) so it can run after every managed-users
# sync without producing duplicates. Scoped by service_type so a
# coincidental ID collision across backend ID spaces never creates a
# false link.

def auto_link_identity_map_by_backend_user_id(
    *, scope_to_server_id: Optional[str] = None,
) -> Dict[str, int]:
    """Derive ``identity_map`` entries from same-(service_type, backend_user_id)
    rows across distinct server_ids.

    Returns ``{"pairs_written": int, "pairs_skipped_duplicate": int,
    "groups_seen": int}``. Best-effort: per-pair failures are caught and
    logged so a single corrupt row never blocks the rest.

    Safe to call repeatedly; subsequent runs no-op on already-mapped
    pairs via INSERT OR IGNORE.

    ``scope_to_server_id`` is a verbose-log filter only: when set, the
    per-group / per-pair INFO lines are suppressed for groups that do
    NOT include that server. The pairing math is unchanged - the sweep
    still writes new identity_map rows for every cross-server pair
    everywhere - but the operator only sees log noise for groups
    relevant to the server they just synced. Pass None (the default)
    to log every group, which is the right shape for the future
    global "Sync all servers" button."""
    conn = _require_conn()
    # Pull every (service_type, backend_user_id, server_id, app_user_uuid,
    # username) tuple where backend_user_id is populated and the row has
    # its UUID backfilled. Group in Python (SQLite GROUP_CONCAT is awkward
    # to parse safely). The username is purely for the verbose log lines
    # below; the pairing itself keys on the app_user_uuid.
    rows = conn.execute(
        """
        SELECT service_type, backend_user_id, server_id, app_user_uuid,
               username
        FROM managed_users
        WHERE backend_user_id IS NOT NULL
          AND backend_user_id <> ''
          AND app_user_uuid IS NOT NULL
        """,
    ).fetchall()
    # Group by (service_type, backend_user_id).
    groups: Dict[Tuple[str, str], List[Tuple[str, str, str]]] = {}
    for r in rows:
        key = (str(r["service_type"]), str(r["backend_user_id"]))
        groups.setdefault(key, []).append(
            (str(r["server_id"]), str(r["app_user_uuid"]),
             str(r["username"] or "")),
        )
    pairs_written = 0
    pairs_skipped = 0
    groups_with_dupes = 0
    if scope_to_server_id:
        log.info(
            "auto_link_identity_map_by_backend_user_id: starting sweep "
            "(%d eligible managed_users rows, %d distinct groups; "
            "verbose log scoped to groups involving %r)",
            len(rows), len(groups), scope_to_server_id,
        )
    else:
        log.info(
            "auto_link_identity_map_by_backend_user_id: starting sweep "
            "(%d eligible managed_users rows, %d distinct "
            "(service_type, backend_user_id) groups)",
            len(rows), len(groups),
        )
    for (_svc, _bk_id), members in groups.items():
        # De-duplicate by server_id: an installation should only have
        # one managed_users row per (server_id, username), but defensively
        # we collapse here too.
        by_server: Dict[str, Tuple[str, str]] = {}
        for server_id, uuid, username in members:
            by_server.setdefault(server_id, (uuid, username))
        if len(by_server) < 2:
            continue
        groups_with_dupes += 1
        # When a per-server sync calls this helper, suppress verbose
        # output for groups that don't include the just-synced
        # server. Pairing math still runs for
        # every group (cross-server identity_map writes happen across
        # the whole table); only the log lines are scoped so the
        # operator doesn't see other servers' users when they only
        # clicked Sync on one.
        verbose_for_this_group = (
            scope_to_server_id is None
            or scope_to_server_id in by_server
        )
        if verbose_for_this_group:
            # Friendly group header so the operator can see who's being
            # paired across which servers. backend_user_id printed
            # because it's the join key; service_type so cross-backend
            # groups stand out from intra-backend ones.
            members_repr = ", ".join(
                f"{username!r} on {server_id}"
                for server_id, (_uuid, username) in sorted(by_server.items())
            )
            log.info(
                "  Group (service=%s, backend_user_id=%s, members=%d): %s",
                _svc, _bk_id, len(by_server), members_repr,
            )
        # For every (A, B) ordered pair with distinct server_ids, write
        # one identity_map row. We don't write both (A,B) and (B,A) -
        # the bidirectional read helper handles either direction.
        server_uuids = sorted(by_server.items())  # deterministic order
        for i, (srv_a, (uuid_a, name_a)) in enumerate(server_uuids):
            for (srv_b, (uuid_b, name_b)) in server_uuids[i + 1:]:
                if uuid_a == uuid_b:
                    continue
                try:
                    new_id = add_identity_map(
                        user_a_uuid=uuid_a,
                        user_b_uuid=uuid_b,
                        source="auto_copy",
                    )
                    if new_id is None:
                        pairs_skipped += 1
                        if verbose_for_this_group:
                            log.info(
                                "    - %r@%s <-> %r@%s: EXISTS (skipped)",
                                name_a, srv_a, name_b, srv_b,
                            )
                    else:
                        pairs_written += 1
                        if verbose_for_this_group:
                            log.info(
                                "    - %r@%s <-> %r@%s: NEW (identity_map_id=%d)",
                                name_a, srv_a, name_b, srv_b, new_id,
                            )
                except Exception:
                    log.exception(
                        "    - %r@%s <-> %r@%s: insert FAILED (continuing)",
                        name_a, srv_a, name_b, srv_b,
                    )
    log.info(
        "auto_link_identity_map_by_backend_user_id: sweep done — "
        "%d new pair(s), %d duplicate(s) skipped, %d group(s) with "
        "cross-server duplicates.",
        pairs_written, pairs_skipped, groups_with_dupes,
    )
    return {
        "pairs_written":           pairs_written,
        "pairs_skipped_duplicate": pairs_skipped,
        "groups_seen":             groups_with_dupes,
    }


# ── Slug rewriter on server rename (R-2 follow-on) ──────────────────────────

def rewrite_app_user_uuid_host_slug_for_server(
    server_uid: str, new_host_name: str,
) -> Dict[str, int]:
    """Refresh the HostNameSlug portion of every stored ``app_user_uuid``
    whose ``server_uid`` portion matches ``server_uid``.

    The server_uid + userkey segments are immutable: only the cosmetic
    slug shifts so identity_map links stay valid. Walks
    ``managed_users.app_user_uuid``, ``server_users.app_user_uuid``,
    AND both columns of ``user_identity_map`` so every stored UUID for
    the renamed server moves in lockstep.

    Returns ``{"managed_users": int, "server_users": int,
    "identity_map_a": int, "identity_map_b": int}`` row counts.
    """
    if not (server_uid or "").strip():
        return {
            "managed_users":   0,
            "server_users":    0,
            "identity_map_a":  0,
            "identity_map_b":  0,
        }
    new_slug = slugify_host_name(new_host_name)
    conn = _require_conn()
    counts = {
        "managed_users":   0,
        "server_users":    0,
        "identity_map_a":  0,
        "identity_map_b":  0,
    }
    with _DB_LOCK:
        for table, count_key in (
            ("managed_users", "managed_users"),
            ("server_users",  "server_users"),
        ):
            rows = conn.execute(
                f"SELECT rowid, app_user_uuid FROM {table} "
                f"WHERE app_user_uuid IS NOT NULL"
            ).fetchall()
            for r in rows:
                rowid_val = r[0]
                stored = r[1]
                if server_uid_from_app_user_uuid(stored) != server_uid:
                    continue
                try:
                    from services.identity.user_uuid import rewrite_host_slug
                    new_uuid = rewrite_host_slug(stored, new_host_name)
                except Exception:
                    continue
                if new_uuid == stored:
                    continue
                conn.execute(
                    f"UPDATE {table} SET app_user_uuid = ? WHERE rowid = ?",
                    (new_uuid, rowid_val),
                )
                counts[count_key] += 1
        # identity_map carries the UUID twice (one per side).
        rows = conn.execute(
            "SELECT id, user_a_uuid, user_b_uuid "
            "FROM user_identity_map"
        ).fetchall()
        from services.identity.user_uuid import rewrite_host_slug
        for r in rows:
            mutated_a = mutated_b = False
            new_a = r["user_a_uuid"]
            new_b = r["user_b_uuid"]
            if server_uid_from_app_user_uuid(new_a) == server_uid:
                try:
                    candidate = rewrite_host_slug(new_a, new_host_name)
                    if candidate != new_a:
                        new_a = candidate
                        mutated_a = True
                except Exception:
                    pass
            if server_uid_from_app_user_uuid(new_b) == server_uid:
                try:
                    candidate = rewrite_host_slug(new_b, new_host_name)
                    if candidate != new_b:
                        new_b = candidate
                        mutated_b = True
                except Exception:
                    pass
            if not (mutated_a or mutated_b):
                continue
            try:
                conn.execute(
                    "UPDATE user_identity_map SET user_a_uuid = ?, "
                    "user_b_uuid = ? WHERE id = ?",
                    (new_a, new_b, r["id"]),
                )
                if mutated_a:
                    counts["identity_map_a"] += 1
                if mutated_b:
                    counts["identity_map_b"] += 1
            except sqlite3.IntegrityError:
                # UNIQUE(user_a_uuid, user_b_uuid) collision means the
                # post-rewrite pair already exists (e.g. end user
                # renamed the server to a name that yields the same
                # slug as an old auto_copy row). Drop this duplicate.
                conn.execute(
                    "DELETE FROM user_identity_map WHERE id = ?",
                    (r["id"],),
                )
    if any(counts.values()):
        log.info(
            "rewrite_app_user_uuid_host_slug_for_server(%r): "
            "managed_users=%d, server_users=%d, identity_map_a=%d, "
            "identity_map_b=%d (new slug=%r)",
            server_uid, counts["managed_users"], counts["server_users"],
            counts["identity_map_a"], counts["identity_map_b"], new_slug,
        )
    return counts


# ── Server-UID boot migration helpers ────────────────────────────────────────
#
# When the boot-time `migrate_server_ids_add_backend_prefix` upgrade
# in `server/server_registry.py` rewrites bare-UUID server rows to the
# new prefixed form (`<service_type>_<uuid>`), every other surface
# that references those ids by string also needs to be rewritten.
# These two helpers do the bulk SQL update for the media.db side:
# user_identity_map (v11) and managed_users (v3+). Best-effort:
# failures are caught + logged + non-fatal so a bad rewrite doesn't
# crash the engine boot.

def _rewrite_server_uid_inside_app_user_uuid(
    stored: str, old_to_new: Dict[str, str],
) -> Optional[str]:
    """If ``stored``'s server_uid portion appears in ``old_to_new``,
    return a new UUID with the server_uid replaced. Returns ``None``
    when the UUID is malformed or its server_uid is not in the map."""
    server_uid = server_uid_from_app_user_uuid(stored)
    if not server_uid or server_uid not in old_to_new:
        return None
    new_uid = old_to_new[server_uid]
    # Rebuild using the existing (Service, HostNameSlug, userkey)
    # segments; only swap the server_uid portion.
    try:
        from services.identity.user_uuid import parse_app_user_uuid, build_app_user_uuid
        parts = parse_app_user_uuid(stored)
    except Exception:
        return None
    return build_app_user_uuid(
        service_type=parts["service"],
        host_name=parts["host_slug"],
        server_uid=new_uid,
        user_key=parts["user_key"],
    )


def rewrite_server_ids_in_identity_map(
    old_to_new: Dict[str, str],
) -> int:
    """Bulk-update ``user_identity_map`` rows whose A or B
    ``app_user_uuid`` carries a server_uid that appears in
    ``old_to_new``. Returns the total number of column-updates.

    v12 changed the storage shape from ``(server_a_id, user_a_handle,
    server_b_id, user_b_handle)`` to ``(user_a_uuid, user_b_uuid)``;
    this helper now rewrites the server_uid embedded inside each
    UUID rather than a top-level column. The server-rename helper
    :func:`rewrite_app_user_uuid_host_slug_for_server` handles the
    parallel slug-only refresh; this helper handles the full
    server_uid swap performed by developer's boot migration.

    Idempotent: rerunning with an empty / no-match map is a no-op.
    Defensive: catches per-row update failure and continues."""
    if not old_to_new:
        return 0
    conn = _require_conn()
    n = 0
    with _DB_LOCK:
        rows = conn.execute(
            "SELECT id, user_a_uuid, user_b_uuid FROM user_identity_map"
        ).fetchall()
        for r in rows:
            new_a = _rewrite_server_uid_inside_app_user_uuid(
                r["user_a_uuid"], old_to_new,
            )
            new_b = _rewrite_server_uid_inside_app_user_uuid(
                r["user_b_uuid"], old_to_new,
            )
            if new_a is None and new_b is None:
                continue
            try:
                conn.execute(
                    "UPDATE user_identity_map SET user_a_uuid = ?, "
                    "user_b_uuid = ? WHERE id = ?",
                    (new_a or r["user_a_uuid"],
                     new_b or r["user_b_uuid"],
                     r["id"]),
                )
                if new_a is not None:
                    n += 1
                if new_b is not None:
                    n += 1
            except sqlite3.IntegrityError:
                # UNIQUE collision: a post-rewrite pair already exists.
                # Drop the duplicate row.
                conn.execute(
                    "DELETE FROM user_identity_map WHERE id = ?",
                    (r["id"],),
                )
            except Exception:
                log.exception(
                    "rewrite_server_ids_in_identity_map: row %r update "
                    "failed; continuing.",
                    r["id"],
                )
    return n


def rewrite_server_ids_in_managed_users(
    old_to_new: Dict[str, str],
) -> int:
    """Bulk-update ``managed_users.server_id`` AND
    ``managed_users.app_user_uuid`` rows whose server_id (or whose
    UUID's embedded server_uid) appears in ``old_to_new``. Returns
    the count of column-updates.

    Also walks ``server_users`` for the same rewrites so both per-user
    tables stay in lockstep. The (server_id, username) /
    (server_id, user_handle) UNIQUE constraints are preserved because
    the new prefixed id is unique by construction.
    """
    if not old_to_new:
        return 0
    conn = _require_conn()
    n = 0
    with _DB_LOCK:
        # 1. Top-level server_id columns (unchanged from v11 semantics).
        for old_id, new_id in old_to_new.items():
            try:
                cur = conn.execute(
                    "UPDATE managed_users SET server_id = ? "
                    "WHERE server_id = ?", (new_id, old_id),
                )
                n += cur.rowcount or 0
                cur = conn.execute(
                    "UPDATE server_users SET server_id = ? "
                    "WHERE server_id = ?", (new_id, old_id),
                )
                n += cur.rowcount or 0
            except Exception:
                log.exception(
                    "rewrite_server_ids_in_managed_users: pair "
                    "(%r -> %r) server_id update failed; continuing.",
                    old_id, new_id,
                )
        # 2. Embedded server_uid inside app_user_uuid (v12 addition).
        for table in ("managed_users", "server_users"):
            try:
                rows = conn.execute(
                    f"SELECT rowid, app_user_uuid FROM {table} "
                    f"WHERE app_user_uuid IS NOT NULL"
                ).fetchall()
            except sqlite3.OperationalError:
                # Pre-v12 row; column doesn't exist yet.
                continue
            for r in rows:
                rowid_val = r[0]
                old_uuid_val = r[1]
                new_uuid = _rewrite_server_uid_inside_app_user_uuid(
                    old_uuid_val, old_to_new,
                )
                if new_uuid is None:
                    continue
                try:
                    conn.execute(
                        f"UPDATE {table} SET app_user_uuid = ? "
                        f"WHERE rowid = ?",
                        (new_uuid, rowid_val),
                    )
                    n += 1
                except Exception:
                    log.exception(
                        "rewrite_server_ids_in_managed_users: "
                        "app_user_uuid rewrite for %s.rowid=%r failed; "
                        "continuing.",
                        table, rowid_val,
                    )
    return n
