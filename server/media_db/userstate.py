"""media.db per-user media state.

Watch events, ratings, playlists, and collections - the side tables
keyed by ``(item_id, server_id, user_handle)``, plus the
``server_users`` roster row creation they all funnel through.
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

from ._core import _DB_LOCK, _require_conn
from .identity import generate_unique_app_user_uuid


def get_or_create_server_user(
    *,
    server_id: str,
    user_handle: str,
    role: Optional[str] = None,
    display_name: Optional[str] = None,
    backend: str = "plex",
    backend_user_id: Optional[str] = None,
) -> int:
    """
    Return the ``server_users.id`` for ``(server_id, user_handle)``,
    creating the row on first encounter.

    Role defaulting: when ``role`` is None, an empty ``user_handle``
    infers ``'owner'`` (matches the legacy sentinel) and any other
    handle infers ``'managed'``. Callers that already know the role
    (the snapshotter, the live-API sync helpers) should pass it
    explicitly so a future engine that decides to give the owner a
    real handle still labels it correctly.

    ``display_name`` / ``backend_user_id`` are LWW on conflict (a
    non-NULL incoming value overwrites; a NULL leaves the prior value
    alone). ``role`` is fixed at INSERT time and never updated on
    conflict - changing a user's role is a separate, explicit op,
    not a side effect of upserting their watch history. ``backend``
    similarly stays at its first-write value.

    The CHECK constraints on the table reject unknown roles and
    backends, so a typo here surfaces as an IntegrityError rather
    than a silently malformed row.
    """
    handle = user_handle or ""
    inferred_role = role or ("owner" if handle == "" else "managed")
    conn = _require_conn()
    now = time.time()
    # Generate the app_user_uuid OUTSIDE the writer lock: the generator
    # uses SELECT probes (WAL handles read isolation lock-free). The
    # COALESCE inside ON CONFLICT preserves an existing UUID on update,
    # so this freshly-generated value is only used when the conflict
    # path picks NULL (new insert OR pre-backfill row).
    candidate_uuid = generate_unique_app_user_uuid(
        service_type=backend or "plex",
        server_id=server_id,
        server_uid=server_id,
    )
    with _DB_LOCK:
        conn.execute(
            """
            INSERT INTO server_users (
                server_id, user_handle, display_name, role, backend,
                backend_user_id, app_user_uuid, created_at, last_seen_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(server_id, user_handle) DO UPDATE SET
                display_name    = COALESCE(excluded.display_name,
                                           server_users.display_name),
                backend_user_id = COALESCE(excluded.backend_user_id,
                                           server_users.backend_user_id),
                app_user_uuid   = COALESCE(server_users.app_user_uuid,
                                           excluded.app_user_uuid),
                last_seen_at    = excluded.last_seen_at
            """,
            (server_id, handle, display_name, inferred_role, backend,
             backend_user_id, candidate_uuid, now, now),
        )
        row = conn.execute(
            "SELECT id FROM server_users "
            "WHERE server_id = ? AND user_handle = ?",
            (server_id, handle),
        ).fetchone()
        return int(row["id"])


def _userstate_prologue(
    *,
    func_name: str,
    server_id: str,
    user_handle: str,
    section_key: int,
    role: Optional[str] = None,
    display_name: Optional[str] = None,
    backend_user_id: Optional[str] = None,
):
    """Shared prologue for the four per-user-state upserters:
    validate section_key (v0.15 schema-anchor invariant), upsert the
    server_users row, and return (server_user_id, now, conn).

    Centralising means a future schema-anchor rule change or a
    server_user write contract change applies to all four upserters
    in lockstep instead of needing 4 isolated edits.
    """
    if not isinstance(section_key, int) or section_key <= 0:
        raise ValueError(
            f"{func_name}: section_key must be a positive int "
            f"(got {section_key!r}). See v0.15 schema-anchor invariant."
        )
    server_user_id = get_or_create_server_user(
        server_id=server_id,
        user_handle=user_handle,
        role=role,
        display_name=display_name,
        backend_user_id=backend_user_id,
    )
    return server_user_id, time.time(), _require_conn()


def record_watch_event(
    *,
    item_id: int,
    server_id: str,
    user_handle: str,
    view_count: int,
    section_key: int,
    view_offset: int = 0,
    last_viewed_at: Optional[float] = None,
    role: Optional[str] = None,
    display_name: Optional[str] = None,
    backend_user_id: Optional[str] = None,
) -> None:
    """
    Upsert one ``(item, server, user)`` watch-state row.

    The role / display_name / backend_user_id keyword args are
    forwarded to :func:`get_or_create_server_user`; callers that know
    the user's identity (the snapshotter, direct transfer) pass them
    so the ``server_users`` row gets populated with real data on
    first sight. Callers that only have a handle (legacy code paths
    during the transition) can omit them - the row is created with
    NULL display_name and inferred role.

    ``section_key`` is required. Watch events without library
    identity break restore (items can't be matched to the correct
    destination library); the function refuses to write rather than
    silently produce data that restore will mishandle.
    """
    server_user_id, now, conn = _userstate_prologue(
        func_name="record_watch_event",
        server_id=server_id, user_handle=user_handle, section_key=section_key,
        role=role, display_name=display_name, backend_user_id=backend_user_id,
    )
    with _DB_LOCK:
        conn.execute(
            """
            INSERT INTO watch_events (
                item_id, server_id, user_handle, server_user_id, section_key,
                view_count, view_offset, last_viewed_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(item_id, server_id, user_handle) DO UPDATE SET
                server_user_id = excluded.server_user_id,
                section_key    = excluded.section_key,
                view_count     = excluded.view_count,
                view_offset    = excluded.view_offset,
                last_viewed_at = COALESCE(excluded.last_viewed_at, watch_events.last_viewed_at),
                updated_at     = excluded.updated_at
            """,
            (item_id, server_id, user_handle or "", server_user_id, int(section_key),
             int(view_count), int(view_offset), last_viewed_at, now),
        )


def upsert_rating(
    *,
    item_id: int,
    server_id: str,
    user_handle: str,
    rating: float,
    section_key: int,
    is_favorite: Optional[bool] = None,
    role: Optional[str] = None,
    display_name: Optional[str] = None,
    backend_user_id: Optional[str] = None,
) -> None:
    """Upsert one per-user item-affinity row. See
    :func:`record_watch_event` for the role / display_name /
    backend_user_id forwarding semantics.

    ``is_favorite`` is the backend-neutral favorite face of the
    affinity record. None means the capturing backend has no
    favorite concept (Plex); Jellyfin / Emby captures pass 0 or 1.
    A favorite-only row (the backend exposes a favorite but no
    numeric rating) is written with ``rating=0.0`` + ``is_favorite=1``.

    ``section_key`` is required - see record_watch_event for
    the integrity-anchor rationale.
    """
    server_user_id, now, conn = _userstate_prologue(
        func_name="upsert_rating",
        server_id=server_id, user_handle=user_handle, section_key=section_key,
        role=role, display_name=display_name, backend_user_id=backend_user_id,
    )
    with _DB_LOCK:
        conn.execute(
            """
            INSERT INTO ratings (
                item_id, server_id, user_handle, server_user_id, section_key,
                rating, is_favorite, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(item_id, server_id, user_handle) DO UPDATE SET
                server_user_id = excluded.server_user_id,
                section_key    = excluded.section_key,
                rating         = excluded.rating,
                is_favorite    = excluded.is_favorite,
                updated_at     = excluded.updated_at
            """,
            (item_id, server_id, user_handle or "", server_user_id, int(section_key),
             float(rating),
             (None if is_favorite is None else (1 if is_favorite else 0)),
             now),
        )


def get_watch_event(
    *,
    item_id: int,
    server_id: str,
    user_handle: str,
) -> Optional[Dict[str, Any]]:
    """
    Read the stored watch-state row for one ``(item, server, user)``
    triple, or ``None`` when nothing has been captured yet.

    Read side of :func:`record_watch_event`, keyed on the same
    ``(item_id, server_id, user_handle)`` unique tuple. The play-count
    pipeline reads the last-known stored ``view_count`` here before
    computing a "sum" / "higher" merge target, so a reconciliation
    still has a baseline when the live server is unreachable.

    Reads run lock-free: WAL gives a consistent snapshot without
    contending for :data:`_DB_LOCK`.
    """
    conn = _require_conn()
    row = conn.execute(
        "SELECT * FROM watch_events "
        "WHERE item_id = ? AND server_id = ? AND user_handle = ?",
        (item_id, server_id, user_handle or ""),
    ).fetchone()
    return dict(row) if row is not None else None


def get_rating(
    *,
    item_id: int,
    server_id: str,
    user_handle: str,
) -> Optional[Dict[str, Any]]:
    """
    Read the stored affinity row for one ``(item, server, user)``
    triple, or ``None`` when nothing has been captured yet.

    Read side of :func:`upsert_rating`, keyed on the same
    ``(item_id, server_id, user_handle)`` unique tuple. ``rating`` is
    the 0.0-10.0 numeric face; ``is_favorite`` is the backend-neutral
    favorite face (NULL when the capturing backend has no favorite
    concept). The ratings pipeline reads both so cross-backend
    translation can pick whichever field the destination backend
    actually supports.

    Reads run lock-free - see :func:`get_watch_event`.
    """
    conn = _require_conn()
    row = conn.execute(
        "SELECT * FROM ratings "
        "WHERE item_id = ? AND server_id = ? AND user_handle = ?",
        (item_id, server_id, user_handle or ""),
    ).fetchone()
    return dict(row) if row is not None else None


def upsert_playlist(
    *,
    server_id: str,
    user_handle: str,
    name: str,
    is_smart: bool,
    smart_filter: Optional[str],
    item_ids: List[int],
    section_key: int,
    description: Optional[str] = None,
) -> int:
    """
    Upsert one playlist row. ``item_ids`` is stored as a JSON array
    in ``item_ids_json`` - denormalised on purpose because playlist
    membership is read as a whole list (never queried by individual
    item-id) and storing ordering matters.

    **Dedup discipline - server-wide vs user-private.**
    The UNIQUE constraint is ``(server_id, user_handle, name)``, so
    the SAME name CAN appear in multiple rows when scoped to different
    users. To prevent the bug where a server-wide playlist gets
    written N times (once per home user), callers MUST split inputs
    before calling this function:

    * Server-wide playlists are those visible to every user - call
      with ``user_handle=""`` ONCE per playlist. Do not call again
      inside a per-user loop for that same playlist.

    * User-private playlists must be deduplicated against the
      server-wide set **by rating_key** (NOT by name), then written
      with ``user_handle=<username>``. Name-based dedup loses a
      legitimately personal playlist that happens to share a name
      with a server-wide one. The rating_key dedup pattern in
      :func:`services.direct_transfer.engine._gather_users_data` is the
      reference implementation.

    :func:`ingest_snapshot_payload` below applies this discipline
    automatically when callers feed it a full snapshot payload.

    ``section_key`` (required): the primary library section
    this playlist belongs to. Plex audio playlists can technically
    span multiple sections, but we anchor each playlist row to its
    primary section for restore matching. Cross-section membership
    is still preserved through the items list - each member item's
    own section_key in server_items remains accurate.
    """
    server_user_id, now, conn = _userstate_prologue(
        func_name="upsert_playlist",
        server_id=server_id, user_handle=user_handle, section_key=section_key,
    )
    with _DB_LOCK:
        conn.execute(
            """
            INSERT INTO playlists (
                server_id, user_handle, server_user_id, section_key, name, description,
                is_smart, smart_filter_json, item_ids_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(server_id, user_handle, name) DO UPDATE SET
                server_user_id    = excluded.server_user_id,
                section_key       = excluded.section_key,
                description       = excluded.description,
                is_smart          = excluded.is_smart,
                smart_filter_json = excluded.smart_filter_json,
                item_ids_json     = excluded.item_ids_json,
                updated_at        = excluded.updated_at
            """,
            (
                server_id, user_handle or "", server_user_id, int(section_key),
                name, description,
                1 if is_smart else 0,
                smart_filter,
                json.dumps(list(item_ids or [])),
                now,
            ),
        )
        row = conn.execute(
            "SELECT id FROM playlists WHERE server_id = ? AND user_handle = ? AND name = ?",
            (server_id, user_handle or "", name),
        ).fetchone()
        return int(row["id"])


def upsert_collection(
    *,
    server_id: str,
    user_handle: str,
    name: str,
    item_ids: List[int],
    section_key: int,
) -> int:
    """
    Upsert one collection row. Same shape as :func:`upsert_playlist`.

    **Dedup discipline - server-wide vs user-private.**
    Same rule applies: a server-wide collection is one row with
    ``user_handle=""``; user-private collections (Plex Pass) belong
    under ``user_handle=<username>``. Callers MUST NOT iterate every
    home user and call this with the same library-level collection -
    that produces N duplicate rows (one per user) and corrupts
    cross-user reasoning.

    The correct integration pattern is the one already used by
    :func:`services.direct_transfer.engine._gather_users_data`: capture the
    owner-side collection rating_keys into a set, then for each
    user's collection list, filter out anything whose rating_key
    matches before writing user-scoped rows. Dedup by rating_key
    rather than name - a user can legitimately have a personal
    collection that happens to share a name with a server-wide one.

    :func:`ingest_snapshot_payload` below applies this discipline
    automatically when callers feed it a full snapshot payload.

    ``section_key`` (required): the library section this
    collection belongs to. Plex collections are typically scoped to
    one library (a Movies collection lives in Movies); see
    upsert_playlist for the rationale on per-row anchoring.
    """
    server_user_id, now, conn = _userstate_prologue(
        func_name="upsert_collection",
        server_id=server_id, user_handle=user_handle, section_key=section_key,
    )
    with _DB_LOCK:
        conn.execute(
            """
            INSERT INTO collections (
                server_id, user_handle, server_user_id, section_key, name,
                item_ids_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(server_id, user_handle, name) DO UPDATE SET
                server_user_id = excluded.server_user_id,
                section_key    = excluded.section_key,
                item_ids_json  = excluded.item_ids_json,
                updated_at     = excluded.updated_at
            """,
            (server_id, user_handle or "", server_user_id, int(section_key), name,
             json.dumps(list(item_ids or [])), now),
        )
        row = conn.execute(
            "SELECT id FROM collections WHERE server_id = ? AND user_handle = ? AND name = ?",
            (server_id, user_handle or "", name),
        ).fetchone()
        return int(row["id"])
