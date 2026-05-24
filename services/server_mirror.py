"""
Server metadata mirror sync layer.

Sits between the backend adapters (Plex / Jellyfin / Emby) and
``server.server_mirror_db``. Responsibilities:

  1. Probe section freshness against live state (cheap call).
  2. Decide whether a delta-sync or full-sync is needed.
  3. Drive the section walk and apply rows transactionally.
  4. Record drift events when live disagrees with mirror.
  5. Coordinate single-flight syncs via ``threading.Event`` keyed
     by ``(server_id, section_id)`` so two jobs that simultaneously
     trigger a sync do not duplicate work (L11 pattern from the
     playlist-transfer optimization journey).

This module is adapter-agnostic. Backend-specific code (e.g.
PlexAdapter) supplies an ``item_provider`` callable that yields
:class:`ItemRow` instances for a given (section_id, since_ts).
Sync orchestration here calls that provider, then applies the
returned rows to the mirror DB.

Snapshot 100% accuracy invariant (operator non-negotiable, Plan
section 19.6): the mirror is a CACHE. It is fed by snapshot's live
walk; it is never consulted by snapshot to short-circuit reads.
Other jobs (restore / direct / fan-out / playlist transfer) may
consult the mirror after a probe, and fall through to live on miss
or in always-live mode.

Concurrency model
-----------------
Single-flight per (server_id, section_id) section sync. Two jobs
calling :func:`sync_for_job` for the same server + same section at
the same time: only one runs the walk; the second waits on the
build-event and reuses the result. Different sections sync in
parallel (the caller decides how many threads to use).

Section walks happen OUTSIDE the DB lock; the apply step takes the
lock for a short transaction. SQLite WAL allows concurrent readers
to see the prior coherent state during the apply.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Optional,
    Tuple,
)

from server import server_mirror_db

log = logging.getLogger("plexmigrate.services.server_mirror")


# ── Data shapes ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SectionInfo:
    """Live view of a library section as reported by the backend.

    The probe + sync layer treats sections as opaque identifiers
    plus a few freshness signals.
    """
    section_id: str
    name: str
    section_type: str
    live_total_size: Optional[int]
    live_updated_at: Optional[float]


@dataclass(frozen=True)
class ItemRow:
    """One mirror row's worth of payload from an item_provider.

    Mirrors :class:`mirror_items` columns; the apply step maps these
    fields straight onto the table.
    """
    rating_key: str
    title: str
    item_type: str
    file_path: Optional[str]
    guids: Tuple[str, ...]
    artist: Optional[str] = None
    album: Optional[str] = None
    show_title: Optional[str] = None
    season_number: Optional[int] = None
    episode_number: Optional[int] = None
    parent_rating_key: Optional[str] = None
    # parent item's
    # cross-server GUID (series GUID for episodes, artist GUID for
    # tracks). Drives the resolver's hierarchy tier.
    grandparent_guid: Optional[str] = None
    live_updated_at: Optional[float] = None


@dataclass(frozen=True)
class ProbeResult:
    """Output of :func:`probe_section_freshness`.

    ``is_fresh`` is the high-level verdict callers use to skip a
    sync. ``mirror_*`` fields are read from
    ``mirror_library_sections``; ``live_*`` fields come from the
    section provider; the remaining fields support drift logging.
    """
    server_id: str
    section_id: str
    section_name: str
    is_fresh: bool
    drift_detected: bool
    mirror_total_size: Optional[int]
    live_total_size: Optional[int]
    mirror_updated_at: Optional[float]
    live_updated_at: Optional[float]


@dataclass
class SyncResult:
    """Outcome of a single section's sync attempt."""
    server_id: str
    section_id: str
    section_name: str
    mode: str  # "probe-only" | "delta" | "full"
    added: int = 0
    updated: int = 0
    removed: int = 0
    elapsed_ms: int = 0
    error: Optional[str] = None
    drift_recorded: bool = False


@dataclass
class SyncSummary:
    """Aggregate of per-section :class:`SyncResult` for one job."""
    server_id: str
    job_id: Optional[str]
    started_at: float
    finished_at: float
    sections: List[SyncResult] = field(default_factory=list)

    @property
    def added(self) -> int:
        return sum(s.added for s in self.sections)

    @property
    def updated(self) -> int:
        return sum(s.updated for s in self.sections)

    @property
    def removed(self) -> int:
        return sum(s.removed for s in self.sections)

    @property
    def error_count(self) -> int:
        return sum(1 for s in self.sections if s.error)


# Item-provider protocol (informal): a callable
# (section_id: str, since_ts: Optional[float]) -> Iterable[ItemRow].
ItemProvider = Callable[[str, Optional[float]], Iterable[ItemRow]]


# ── Single-flight coordination ──────────────────────────────────────────────

_BUILD_EVENTS: Dict[Tuple[str, str], threading.Event] = {}
_BUILD_EVENTS_LOCK = threading.Lock()


def _claim_or_wait(server_id: str, section_id: str) -> Tuple[bool, threading.Event]:
    """Single-flight gate for (server_id, section_id) syncs.

    Returns ``(claimed, event)``. ``claimed=True`` means the caller
    owns the sync; they must call :func:`_release` when done.
    ``claimed=False`` means another caller is already syncing; the
    returned event will be set when they finish (caller can ``.wait``
    on it).
    """
    key = (server_id, section_id)
    with _BUILD_EVENTS_LOCK:
        existing = _BUILD_EVENTS.get(key)
        if existing is not None:
            return False, existing
        ev = threading.Event()
        _BUILD_EVENTS[key] = ev
        return True, ev


def _release(server_id: str, section_id: str) -> None:
    """Mark the in-flight sync complete; wake any waiters."""
    key = (server_id, section_id)
    with _BUILD_EVENTS_LOCK:
        ev = _BUILD_EVENTS.pop(key, None)
    if ev is not None:
        ev.set()


def _build_events_snapshot_for_tests() -> List[Tuple[str, str]]:
    """Test-only helper to inspect what syncs are currently in flight."""
    with _BUILD_EVENTS_LOCK:
        return sorted(_BUILD_EVENTS.keys())


def _clear_build_events_for_tests() -> None:
    """Test-only helper to drop all build-event state. Production code
    must not call this; it would orphan in-flight syncs."""
    with _BUILD_EVENTS_LOCK:
        _BUILD_EVENTS.clear()


# ── Public state helpers ────────────────────────────────────────────────────


def upsert_server_state(
    *,
    server_id: str,
    backend: str,
    url_fingerprint: Optional[str] = None,
    token_hash: Optional[str] = None,
    mode_override: Optional[str] = None,
) -> None:
    """Ensure a :class:`mirror_server_state` row exists for the
    given server. Idempotent; preserves first_sync_at on update."""
    conn = server_mirror_db.get_connection()
    lock = server_mirror_db.get_db_lock()
    now = time.time()
    with lock:
        existing = conn.execute(
            "SELECT first_sync_at FROM mirror_server_state WHERE server_id = ?",
            (server_id,),
        ).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO mirror_server_state("
                "server_id, backend, first_sync_at, "
                "url_fingerprint, token_hash, mode_override) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (server_id, backend, now, url_fingerprint,
                 token_hash, mode_override),
            )
        else:
            conn.execute(
                "UPDATE mirror_server_state SET "
                "backend = ?, "
                "url_fingerprint = COALESCE(?, url_fingerprint), "
                "token_hash = COALESCE(?, token_hash), "
                "mode_override = ? "
                "WHERE server_id = ?",
                (backend, url_fingerprint, token_hash,
                 mode_override, server_id),
            )


def get_server_state(server_id: str) -> Optional[Dict[str, Any]]:
    conn = server_mirror_db.get_connection()
    row = conn.execute(
        "SELECT * FROM mirror_server_state WHERE server_id = ?",
        (server_id,),
    ).fetchone()
    return dict(row) if row is not None else None


def list_server_states() -> List[Dict[str, Any]]:
    conn = server_mirror_db.get_connection()
    rows = conn.execute(
        "SELECT * FROM mirror_server_state ORDER BY server_id"
    ).fetchall()
    return [dict(r) for r in rows]


def get_section_state(
    server_id: str, section_id: str,
) -> Optional[Dict[str, Any]]:
    conn = server_mirror_db.get_connection()
    row = conn.execute(
        "SELECT * FROM mirror_library_sections "
        "WHERE server_id = ? AND section_id = ?",
        (server_id, section_id),
    ).fetchone()
    return dict(row) if row is not None else None


def list_section_states(server_id: str) -> List[Dict[str, Any]]:
    conn = server_mirror_db.get_connection()
    rows = conn.execute(
        "SELECT * FROM mirror_library_sections "
        "WHERE server_id = ? ORDER BY section_id",
        (server_id,),
    ).fetchall()
    return [dict(r) for r in rows]


# ── Probe ───────────────────────────────────────────────────────────────────


def probe_section_freshness(
    *, server_id: str, section: SectionInfo,
) -> ProbeResult:
    """Compare a live :class:`SectionInfo` against the mirror's
    recorded state. Returns a :class:`ProbeResult` describing whether
    a sync is needed.

    "Fresh" means: both live_total_size and live_updated_at match the
    mirror's recorded values. Any mismatch (or no mirror row yet) is
    considered drift.
    """
    state = get_section_state(server_id, section.section_id)
    mirror_total = state["live_total_size"] if state else None
    mirror_updated = state["live_updated_at"] if state else None
    drift = (
        state is None
        or mirror_total != section.live_total_size
        or _floats_differ(mirror_updated, section.live_updated_at)
    )
    return ProbeResult(
        server_id=server_id,
        section_id=section.section_id,
        section_name=section.name,
        is_fresh=not drift,
        drift_detected=drift,
        mirror_total_size=mirror_total,
        live_total_size=section.live_total_size,
        mirror_updated_at=mirror_updated,
        live_updated_at=section.live_updated_at,
    )


def _floats_differ(a: Optional[float], b: Optional[float],
                   tolerance: float = 1.0) -> bool:
    """Treat None vs not-None as different, and floats within 1s of
    each other as equal (Plex reports updatedAt as a Unix integer
    second, so sub-second drift is meaningless)."""
    if a is None and b is None:
        return False
    if a is None or b is None:
        return True
    return abs(a - b) > tolerance


# ── Apply ───────────────────────────────────────────────────────────────────


def _apply_items_transaction(
    *, server_id: str, section_id: str, items: List[ItemRow],
    full_sync: bool,
) -> Tuple[int, int, int]:
    """Apply ``items`` to mirror_items + mirror_item_guids inside one
    transaction. Returns ``(added, updated, removed)``.

    When ``full_sync`` is True, items NOT in ``items`` but present in
    the mirror for this (server, section) are deleted (the section
    walk was exhaustive). When False, only the supplied items are
    upserted; existing rows not in the list are left alone (delta).

    Uses the mirror DB connection's autocommit mode (set in
    :func:`server.server_mirror_db.init_server_mirror_db` via
    ``isolation_level=None``) by issuing explicit BEGIN / COMMIT.
    """
    import json

    conn = server_mirror_db.get_connection()
    lock = server_mirror_db.get_db_lock()
    added = 0
    updated = 0
    removed = 0
    now = time.time()

    with lock:
        conn.execute("BEGIN IMMEDIATE")
        try:
            keys_seen: set = set()
            for it in items:
                guids_json = json.dumps(list(it.guids))
                cur = conn.execute(
                    "SELECT 1 FROM mirror_items "
                    "WHERE server_id = ? AND rating_key = ?",
                    (server_id, it.rating_key),
                )
                existed = cur.fetchone() is not None
                conn.execute(
                    "INSERT OR REPLACE INTO mirror_items("
                    "server_id, section_id, rating_key, title, item_type, "
                    "file_path, guids_json, artist, album, show_title, "
                    "season_number, episode_number, parent_rating_key, "
                    "grandparent_guid, "
                    "live_updated_at, mirror_synced_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (server_id, section_id, it.rating_key, it.title,
                     it.item_type, it.file_path, guids_json, it.artist,
                     it.album, it.show_title, it.season_number,
                     it.episode_number, it.parent_rating_key,
                     it.grandparent_guid,
                     it.live_updated_at, now),
                )
                # Replace GUID rows for this item: delete + reinsert.
                conn.execute(
                    "DELETE FROM mirror_item_guids "
                    "WHERE server_id = ? AND rating_key = ?",
                    (server_id, it.rating_key),
                )
                for g in it.guids:
                    if not g:
                        continue
                    conn.execute(
                        "INSERT OR IGNORE INTO mirror_item_guids("
                        "server_id, rating_key, guid) VALUES (?, ?, ?)",
                        (server_id, it.rating_key, g),
                    )
                keys_seen.add(it.rating_key)
                if existed:
                    updated += 1
                else:
                    added += 1

            if full_sync:
                # Delete rows not in keys_seen for this section. Done
                # in two passes to avoid mutating while iterating.
                cur = conn.execute(
                    "SELECT rating_key FROM mirror_items "
                    "WHERE server_id = ? AND section_id = ?",
                    (server_id, section_id),
                )
                stale_keys = [
                    r[0] for r in cur.fetchall()
                    if r[0] not in keys_seen
                ]
                for rk in stale_keys:
                    conn.execute(
                        "DELETE FROM mirror_items "
                        "WHERE server_id = ? AND rating_key = ?",
                        (server_id, rk),
                    )
                removed = len(stale_keys)

            conn.execute("COMMIT")
        except sqlite3.Error:
            conn.execute("ROLLBACK")
            raise

    return added, updated, removed


def _upsert_section_state(
    *, server_id: str, section: SectionInfo, mirror_synced_at: float,
) -> None:
    conn = server_mirror_db.get_connection()
    lock = server_mirror_db.get_db_lock()
    with lock:
        conn.execute(
            "INSERT OR REPLACE INTO mirror_library_sections("
            "server_id, section_id, name, section_type, "
            "live_total_size, live_updated_at, mirror_synced_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (server_id, section.section_id, section.name,
             section.section_type, section.live_total_size,
             section.live_updated_at, mirror_synced_at),
        )


def _record_drift_event(
    *, server_id: str, section: SectionInfo, probe: ProbeResult,
    job_id: Optional[str],
) -> None:
    """Insert a drift_events row capturing the probe verdict."""
    conn = server_mirror_db.get_connection()
    lock = server_mirror_db.get_db_lock()
    with lock:
        conn.execute(
            "INSERT INTO drift_events("
            "server_id, section_id, section_name, detected_at, "
            "mirror_total_size, live_total_size, mirror_updated_at, "
            "live_updated_at, detected_by_job_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (server_id, section.section_id, section.name, time.time(),
             probe.mirror_total_size, probe.live_total_size,
             probe.mirror_updated_at, probe.live_updated_at, job_id),
        )


# ── Sync flavors ────────────────────────────────────────────────────────────


def full_sync_section(
    *,
    server_id: str,
    section: SectionInfo,
    item_provider: ItemProvider,
    job_id: Optional[str] = None,
) -> SyncResult:
    """Walk the whole section (since_ts=None) and replace the mirror
    rows. Stale rows (present in mirror but not in the walk) are
    deleted.

    Single-flight: concurrent calls for the same (server, section)
    block on the build-event and reuse the result when it lands.
    """
    started = time.time()
    claimed, ev = _claim_or_wait(server_id, section.section_id)
    if not claimed:
        # Wait on the in-flight build; return a probe-only result so
        # the caller knows we didn't do the work.
        ev.wait(timeout=120.0)
        return SyncResult(
            server_id=server_id,
            section_id=section.section_id,
            section_name=section.name,
            mode="waited-on-inflight",
            elapsed_ms=int((time.time() - started) * 1000),
        )
    try:
        items: List[ItemRow] = list(item_provider(section.section_id, None))
        added, updated, removed = _apply_items_transaction(
            server_id=server_id, section_id=section.section_id,
            items=items, full_sync=True,
        )
        _upsert_section_state(
            server_id=server_id, section=section,
            mirror_synced_at=time.time(),
        )
        _update_last_full_sync_at(server_id)
        return SyncResult(
            server_id=server_id,
            section_id=section.section_id,
            section_name=section.name,
            mode="full",
            added=added, updated=updated, removed=removed,
            elapsed_ms=int((time.time() - started) * 1000),
        )
    except (sqlite3.Error, OSError) as exc:
        log.warning(
            "server_mirror full_sync_section server=%s section=%s failed: %s",
            server_id, section.section_id, exc,
        )
        return SyncResult(
            server_id=server_id,
            section_id=section.section_id,
            section_name=section.name,
            mode="full",
            elapsed_ms=int((time.time() - started) * 1000),
            error=str(exc),
        )
    finally:
        _release(server_id, section.section_id)


def delta_sync_section(
    *,
    server_id: str,
    section: SectionInfo,
    since_ts: Optional[float],
    item_provider: ItemProvider,
    job_id: Optional[str] = None,
) -> SyncResult:
    """Fetch items where updatedAt > since_ts and upsert them. Does
    NOT delete stale rows (delta cannot know what is missing without
    a full walk; staleness is handled by drift detection + next full
    sync).
    """
    started = time.time()
    claimed, ev = _claim_or_wait(server_id, section.section_id)
    if not claimed:
        ev.wait(timeout=120.0)
        return SyncResult(
            server_id=server_id,
            section_id=section.section_id,
            section_name=section.name,
            mode="waited-on-inflight",
            elapsed_ms=int((time.time() - started) * 1000),
        )
    try:
        items: List[ItemRow] = list(
            item_provider(section.section_id, since_ts)
        )
        added, updated, _ = _apply_items_transaction(
            server_id=server_id, section_id=section.section_id,
            items=items, full_sync=False,
        )
        _upsert_section_state(
            server_id=server_id, section=section,
            mirror_synced_at=time.time(),
        )
        return SyncResult(
            server_id=server_id,
            section_id=section.section_id,
            section_name=section.name,
            mode="delta",
            added=added, updated=updated,
            elapsed_ms=int((time.time() - started) * 1000),
        )
    except (sqlite3.Error, OSError) as exc:
        log.warning(
            "server_mirror delta_sync_section server=%s section=%s failed: %s",
            server_id, section.section_id, exc,
        )
        return SyncResult(
            server_id=server_id,
            section_id=section.section_id,
            section_name=section.name,
            mode="delta",
            elapsed_ms=int((time.time() - started) * 1000),
            error=str(exc),
        )
    finally:
        _release(server_id, section.section_id)


def _update_last_full_sync_at(server_id: str) -> None:
    conn = server_mirror_db.get_connection()
    lock = server_mirror_db.get_db_lock()
    now = time.time()
    with lock:
        existing = conn.execute(
            "SELECT 1 FROM mirror_server_state WHERE server_id = ?",
            (server_id,),
        ).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO mirror_server_state("
                "server_id, backend, first_sync_at, last_full_sync_at) "
                "VALUES (?, ?, ?, ?)",
                (server_id, "unknown", now, now),
            )
        else:
            conn.execute(
                "UPDATE mirror_server_state "
                "SET last_full_sync_at = ?, "
                "first_sync_at = COALESCE(first_sync_at, ?) "
                "WHERE server_id = ?",
                (now, now, server_id),
            )
    # invalidate library-mapping auto rows that touch
    # this server. The mirror just got fresh item counts + GUIDs, so
    # any auto-computed mapping based on prior fingerprints is stale.
    # Operator-confirmed rows are preserved (operator already decided
    # — the matcher's input changing doesn't override that).
    # Library_mapping_db isn't always available (tests, ad-hoc CLI);
    # any failure here is non-blocking on the sync itself.
    try:
        from server import library_mapping_db
        wiped_source = library_mapping_db.invalidate_auto_mappings(
            source_server_id=server_id,
        )
        wiped_dest = library_mapping_db.invalidate_auto_mappings(
            dest_server_id=server_id,
        )
        if wiped_source or wiped_dest:
            log.info(
                "library-mapping invalidation: server=%s wiped "
                "%d auto-row(s) (as source) + %d (as dest) after "
                "mirror sync. Operator-confirmed rows preserved.",
                server_id, wiped_source, wiped_dest,
            )
    except Exception:
        log.debug(
            "library-mapping invalidation: db unavailable for "
            "server=%s; skipping.", server_id,
        )


# ── Top-level orchestration ────────────────────────────────────────────────


def sync_for_job(
    *,
    server_id: str,
    sections: List[SectionInfo],
    item_provider: ItemProvider,
    job_id: Optional[str] = None,
    force_full: bool = False,
    max_age_seconds: float = 86400.0,
) -> SyncSummary:
    """Probe each section; sync as needed.

    For each section:
      - probe live state vs mirror_library_sections row
      - if fresh AND mirror_synced_at < now-max_age, force a full
        sync anyway (time-based staleness gate)
      - else if drift detected, choose full vs delta:
          * no prior row, or section_type unknown to mirror → full
          * else → delta from mirror_synced_at
      - record drift events on every divergence

    Returns a :class:`SyncSummary` with per-section :class:`SyncResult`.
    """
    started = time.time()
    out_sections: List[SyncResult] = []
    drift_check_at = time.time()
    for sec in sections:
        section_state = get_section_state(server_id, sec.section_id)
        probe = probe_section_freshness(server_id=server_id, section=sec)
        drift_recorded = False
        if probe.drift_detected:
            try:
                _record_drift_event(
                    server_id=server_id, section=sec, probe=probe,
                    job_id=job_id,
                )
                drift_recorded = True
            except sqlite3.Error as exc:
                log.warning(
                    "server_mirror: drift event record failed for "
                    "server=%s section=%s: %s",
                    server_id, sec.section_id, exc,
                )

        prior_sync_at = (
            section_state.get("mirror_synced_at")
            if section_state else None
        )
        time_stale = (
            prior_sync_at is None
            or (time.time() - prior_sync_at) > max_age_seconds
        )
        # CONSOLE-02: the delta cursor must be the section's server-
        # reported content watermark (live_updated_at), not the local
        # mirror_synced_at wall-clock. mirror_synced_at is always later
        # than the server watermark, so using it as the delta "since"
        # silently drops items edited between the watermark and the
        # local sync instant until the next full sync. A small margin
        # absorbs > vs >= boundary and minor clock skew. mirror_synced_at
        # stays the input to the time-staleness gate above only.
        prior_watermark = (
            section_state.get("live_updated_at")
            if section_state else None
        )
        delta_since = (
            max(0.0, float(prior_watermark) - 5.0)
            if prior_watermark is not None else None
        )

        if force_full or time_stale or section_state is None:
            result = full_sync_section(
                server_id=server_id, section=sec,
                item_provider=item_provider, job_id=job_id,
            )
        elif probe.drift_detected and delta_since is not None:
            result = delta_sync_section(
                server_id=server_id, section=sec,
                since_ts=delta_since, item_provider=item_provider,
                job_id=job_id,
            )
        elif probe.drift_detected:
            # Drift detected but no server watermark recorded yet -
            # cannot compute a correct delta cursor, so fall back to a
            # full sync rather than risk a since_ts that drops edits.
            result = full_sync_section(
                server_id=server_id, section=sec,
                item_provider=item_provider, job_id=job_id,
            )
        else:
            result = SyncResult(
                server_id=server_id,
                section_id=sec.section_id,
                section_name=sec.name,
                mode="probe-only",
                elapsed_ms=int((time.time() - started) * 1000),
            )
        result.drift_recorded = drift_recorded
        out_sections.append(result)

    _update_last_drift_check_at(server_id, drift_check_at)
    return SyncSummary(
        server_id=server_id,
        job_id=job_id,
        started_at=started,
        finished_at=time.time(),
        sections=out_sections,
    )


def _update_last_drift_check_at(server_id: str, ts: float) -> None:
    conn = server_mirror_db.get_connection()
    lock = server_mirror_db.get_db_lock()
    with lock:
        existing = conn.execute(
            "SELECT 1 FROM mirror_server_state WHERE server_id = ?",
            (server_id,),
        ).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO mirror_server_state("
                "server_id, backend, last_drift_check_at) "
                "VALUES (?, ?, ?)",
                (server_id, "unknown", ts),
            )
        else:
            conn.execute(
                "UPDATE mirror_server_state "
                "SET last_drift_check_at = ? WHERE server_id = ?",
                (ts, server_id),
            )


# ── Invalidation ────────────────────────────────────────────────────────────


def invalidate_mirror(
    *,
    server_id: Optional[str] = None,
    section_id: Optional[str] = None,
) -> int:
    """Drop mirror rows. Returns total rows deleted across all tables.

      * Both args None: drop every row in every mirror table (operator
        clicked "Nuke everything").
      * server_id set, section_id None: drop everything for that
        server (server_state, sections, items, guids, drift events).
      * Both set: drop items + guids for that (server, section) and
        the matching mirror_library_sections row. Server-state row is
        kept (the server is still registered).
    """
    conn = server_mirror_db.get_connection()
    lock = server_mirror_db.get_db_lock()
    total = 0
    with lock:
        conn.execute("BEGIN IMMEDIATE")
        try:
            if server_id is None and section_id is None:
                for tbl in (
                    "mirror_item_guids", "mirror_items",
                    "mirror_library_sections", "mirror_server_state",
                    "drift_events",
                ):
                    cur = conn.execute(f"DELETE FROM {tbl}")
                    total += cur.rowcount or 0
            elif server_id is not None and section_id is None:
                for tbl in (
                    "mirror_item_guids", "mirror_items",
                    "mirror_library_sections", "drift_events",
                ):
                    cur = conn.execute(
                        f"DELETE FROM {tbl} WHERE server_id = ?",
                        (server_id,),
                    )
                    total += cur.rowcount or 0
                cur = conn.execute(
                    "DELETE FROM mirror_server_state WHERE server_id = ?",
                    (server_id,),
                )
                total += cur.rowcount or 0
            elif server_id is not None and section_id is not None:
                cur = conn.execute(
                    "DELETE FROM mirror_item_guids "
                    "WHERE server_id = ? AND rating_key IN ("
                    "  SELECT rating_key FROM mirror_items "
                    "  WHERE server_id = ? AND section_id = ?"
                    ")", (server_id, server_id, section_id),
                )
                total += cur.rowcount or 0
                cur = conn.execute(
                    "DELETE FROM mirror_items "
                    "WHERE server_id = ? AND section_id = ?",
                    (server_id, section_id),
                )
                total += cur.rowcount or 0
                cur = conn.execute(
                    "DELETE FROM mirror_library_sections "
                    "WHERE server_id = ? AND section_id = ?",
                    (server_id, section_id),
                )
                total += cur.rowcount or 0
            else:
                raise ValueError(
                    "invalidate_mirror: section_id without server_id "
                    "is not supported"
                )
            conn.execute("COMMIT")
        except sqlite3.Error:
            conn.execute("ROLLBACK")
            raise
    return total


# ── Drift event read API ────────────────────────────────────────────────────


def list_recent_drift_events(
    *,
    server_id: Optional[str] = None,
    since_ts: Optional[float] = None,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    """Return drift_events rows, newest first.

    ``since_ts`` filters to events whose detected_at >= since_ts.
    ``server_id`` filters to a single server. ``limit`` clamps to
    [1, 10000].
    """
    limit = max(1, min(limit, 10000))
    conn = server_mirror_db.get_connection()
    where = []
    params: List[Any] = []
    if server_id is not None:
        where.append("server_id = ?")
        params.append(server_id)
    if since_ts is not None:
        where.append("detected_at >= ?")
        params.append(since_ts)
    sql = "SELECT * FROM drift_events"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY detected_at DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


# ── Mode dispatcher ─────────────────────────────────────────────────────────
# Plan section 6 + D2 + D7. Three-layer precedence:
#   1. per_submission_override (passed in job submit body)
#   2. mirror_server_state.mode_override for this server
#   3. tunable engine_mirror_mode (global default)
#
# Returns one of {"auto", "always-live"}. Unknown values fall back
# to the global default. Callers cache the resolved mode at adapter
# construction time; a tunable / per-server change requires a
# server_registry.invalidate_adapter_cache() to take effect.


def effective_mode_for(
    server_id: str,
    *,
    per_submission_override: Optional[str] = None,
) -> str:
    """Resolve mirror mode for ``server_id``. Always returns one of
    "auto" or "always-live". Per-submission > per-server > global.
    """
    from services import tunables

    valid = {"auto", "always-live"}
    if per_submission_override and per_submission_override in valid:
        return per_submission_override
    state = get_server_state(server_id)
    if state is not None:
        override = state.get("mode_override")
        if override and override in valid:
            return override
    return tunables.engine_mirror_mode()


def set_server_mode_override(
    *, server_id: str, mode: Optional[str],
) -> None:
    """Set or clear the per-server mode override.

    Passing ``mode=None`` clears the override (falls back to global
    default). Passing ``"auto"`` or ``"always-live"`` pins the
    server. Invalid values raise ValueError so the REST layer can
    return 400.
    """
    valid = {"auto", "always-live"}
    if mode is not None and mode not in valid:
        raise ValueError(
            f"engine mirror mode must be one of {sorted(valid)} or None"
        )
    conn = server_mirror_db.get_connection()
    lock = server_mirror_db.get_db_lock()
    with lock:
        existing = conn.execute(
            "SELECT 1 FROM mirror_server_state WHERE server_id = ?",
            (server_id,),
        ).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO mirror_server_state("
                "server_id, backend, mode_override) "
                "VALUES (?, ?, ?)",
                (server_id, "unknown", mode),
            )
        else:
            conn.execute(
                "UPDATE mirror_server_state SET mode_override = ? "
                "WHERE server_id = ?",
                (mode, server_id),
            )


# ── Resolver query helpers ──────────────────────────────────────────────────
# Step 4: SQL-backed implementations of the 4 resolution methods that
# PlexAdapter exposes. Each returns the first matching rating_key (or
# a small ranked list for fuzzy). Per-tier behavior preserved from the
# in-memory caches that the playlist-transfer optimization built.

def lookup_by_guids(
    *, server_id: str, guids: Iterable[str],
    section_id: Optional[str] = None,
) -> Optional[str]:
    """Tier 1 (GUID) lookup. Returns the rating_key whose GUID set
    intersects ``guids``. If multiple matches exist, returns the
    first by (server_id, rating_key) tuple ordering.

    ``section_id`` optional narrowing: when supplied, only items in
    that section are considered (library-of-truth pattern).
    """
    guid_list = [g for g in guids if g]
    if not guid_list:
        return None
    placeholders = ",".join("?" for _ in guid_list)
    params: List[Any] = [server_id, *guid_list]
    sql = (
        "SELECT g.rating_key FROM mirror_item_guids g "
        "JOIN mirror_items m ON m.server_id = g.server_id "
        "AND m.rating_key = g.rating_key "
        "WHERE g.server_id = ? AND g.guid IN (" + placeholders + ")"
    )
    if section_id:
        sql += " AND m.section_id = ?"
        params.append(section_id)
    sql += " LIMIT 1"
    conn = server_mirror_db.get_connection()
    row = conn.execute(sql, params).fetchone()
    return row[0] if row else None


def lookup_by_full_path(
    *, server_id: str, file_path: str,
    item_type_hint: Optional[str] = None,
) -> Optional[str]:
    """Tier 2 (full-path) lookup. Exact match on ``mirror_items.file_path``.

    A case-insensitive fallback handles Windows-style path drift; the
    partial index ``idx_mirror_items_file_path`` keeps the case-sensitive
    lookup O(log n).
    """
    if not file_path:
        return None
    conn = server_mirror_db.get_connection()
    params: List[Any] = [server_id, file_path]
    sql = (
        "SELECT rating_key FROM mirror_items "
        "WHERE server_id = ? AND file_path = ?"
    )
    if item_type_hint:
        sql += " AND item_type = ?"
        params.append(item_type_hint)
    sql += " LIMIT 1"
    row = conn.execute(sql, params).fetchone()
    if row is not None:
        return row[0]
    # Case-insensitive fallback (no partial index; full-table scan
    # but only fires when the case-sensitive miss).
    params_ci: List[Any] = [server_id, file_path]
    sql_ci = (
        "SELECT rating_key FROM mirror_items "
        "WHERE server_id = ? "
        "AND lower(file_path) = lower(?)"
    )
    if item_type_hint:
        sql_ci += " AND item_type = ?"
        params_ci.append(item_type_hint)
    sql_ci += " LIMIT 1"
    row = conn.execute(sql_ci, params_ci).fetchone()
    return row[0] if row else None


def lookup_by_path_tail(
    *, server_id: str, file_path: str, tail_components: int = 3,
    item_type_hint: Optional[str] = None,
) -> Optional[str]:
    """Tier 2.5 (path-tail) lookup. Matches the last N path components
    of ``file_path`` against mirror_items.file_path. ``tail_components``
    defaults to 3 (Artist/Album/Track for music; Show/Season/Episode for TV).
    """
    if not file_path:
        return None
    # Normalize separators and split.
    parts = file_path.replace("\\", "/").split("/")
    # Strip empty leading slash component.
    parts = [p for p in parts if p]
    if len(parts) < tail_components:
        return None
    tail = "/".join(parts[-tail_components:])
    if not tail:
        return None
    # We need SUFFIX matching on file_path. SQLite LIKE with a leading
    # wildcard cannot use the file_path index, but the partial index
    # at least narrows to rows with non-empty file_path. Acceptable
    # because path-tail is Tier 2.5 (only fires when full-path missed).
    conn = server_mirror_db.get_connection()
    params: List[Any] = [server_id, f"%/{tail}"]
    sql = (
        "SELECT rating_key FROM mirror_items "
        "WHERE server_id = ? "
        "AND file_path IS NOT NULL AND file_path != '' "
        "AND replace(file_path, '\\', '/') LIKE ?"
    )
    if item_type_hint:
        sql += " AND item_type = ?"
        params.append(item_type_hint)
    sql += " LIMIT 1"
    row = conn.execute(sql, params).fetchone()
    return row[0] if row else None


def lookup_by_fuzzy_title(
    *, server_id: str, title: str, item_type: str,
    artist: Optional[str] = None,
    show_title: Optional[str] = None,
    season_number: Optional[int] = None,
    episode_number: Optional[int] = None,
    album: Optional[str] = None,
    ambiguous_behavior: str = "strict",
    limit: int = 32,
) -> List[Dict[str, Any]]:
    """Tier 3 (fuzzy title) lookup.

    Returns a list of candidate rows (dicts with rating_key, title,
    artist, album, show_title, season_number, episode_number,
    file_path). Caller applies ``ambiguous_behavior`` policy in Python:
      * ``"strict"`` → callers reject any non-unique candidate set
      * ``"first"`` → callers take rows[0]
      * ``"all"`` → callers take all candidates

    Filters applied at SQL level:
      * item_type exact match (NOCASE)
      * title exact match (NOCASE)
      * artist filter for tracks (when supplied)
      * show_title + season + episode for episodes (when supplied)
    """
    if not title:
        return []
    conn = server_mirror_db.get_connection()
    where = ["server_id = ?", "item_type = ?", "title = ? COLLATE NOCASE"]
    params: List[Any] = [server_id, item_type, title]
    if item_type == "track" and artist:
        where.append("artist = ? COLLATE NOCASE")
        params.append(artist)
        if album:
            where.append("album = ? COLLATE NOCASE")
            params.append(album)
    if item_type == "episode":
        if show_title:
            where.append("show_title = ? COLLATE NOCASE")
            params.append(show_title)
        if season_number is not None:
            where.append("season_number = ?")
            params.append(int(season_number))
        if episode_number is not None:
            where.append("episode_number = ?")
            params.append(int(episode_number))
    sql = (
        "SELECT rating_key, title, item_type, artist, album, "
        "show_title, season_number, episode_number, file_path, "
        "section_id "
        "FROM mirror_items "
        "WHERE " + " AND ".join(where) +
        " LIMIT ?"
    )
    params.append(max(1, min(limit, 256)))
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def lookup_by_hierarchy(
    *, server_id: str, item_type: str, title: str,
    grandparent_guid: Optional[str] = None,
    show_title: Optional[str] = None,
    season_number: Optional[int] = None,
    episode_number: Optional[int] = None,
    artist: Optional[str] = None,
    album: Optional[str] = None,
) -> Optional[str]:
    """Hierarchy resolver tier. Returns a single matched rating_key, or None.

    The resolver consults this tier AFTER GUID + file-path tiers
    miss and BEFORE the fuzzy-title tier (operator decision D2): a
    hierarchy match is a stronger signal than a bare title match.

    Match strategy, strongest first:

      1. Parent-GUID exact + leaf coordinates. For an episode:
         grandparent_guid (the series GUID) + season_number +
         episode_number. For a track: grandparent_guid (the artist
         GUID) + album + title. A parent-GUID match is immune to
         localized titles + metadata-agent drift, so when the
         single resulting row is unique it is treated as certain.
      2. Parent-TITLE + leaf coordinates. Same shape but keyed on
         show_title / artist strings instead of the parent GUID —
         used when the source side has no parent GUID (Jellyfin /
         Emby, which don't expose one cheaply).

    Only returns a rating_key when the match is UNIQUE. An ambiguous
    result (two rows satisfy the same hierarchy coordinates) returns
    None so the caller falls through to fuzzy-title rather than
    guessing.
    """
    if item_type not in ("episode", "track"):
        return None
    conn = server_mirror_db.get_connection()

    def _unique(where: List[str], params: List[Any]) -> Optional[str]:
        sql = (
            "SELECT rating_key FROM mirror_items "
            "WHERE " + " AND ".join(where) + " LIMIT 2"
        )
        rows = conn.execute(sql, params).fetchall()
        if len(rows) == 1:
            return str(rows[0][0])
        return None

    # ── Strategy 1: parent-GUID keyed ──────────────────────────────
    if grandparent_guid:
        if item_type == "episode" and season_number is not None \
           and episode_number is not None:
            rk = _unique(
                ["server_id = ?", "item_type = 'episode'",
                 "grandparent_guid = ?", "season_number = ?",
                 "episode_number = ?"],
                [server_id, grandparent_guid,
                 int(season_number), int(episode_number)],
            )
            if rk:
                return rk
        if item_type == "track" and title:
            where = ["server_id = ?", "item_type = 'track'",
                     "grandparent_guid = ?", "title = ? COLLATE NOCASE"]
            params: List[Any] = [server_id, grandparent_guid, title]
            if album:
                where.append("album = ? COLLATE NOCASE")
                params.append(album)
            rk = _unique(where, params)
            if rk:
                return rk

    # ── Strategy 2: parent-TITLE keyed ─────────────────────────────
    if item_type == "episode" and show_title \
       and season_number is not None and episode_number is not None:
        rk = _unique(
            ["server_id = ?", "item_type = 'episode'",
             "show_title = ? COLLATE NOCASE", "season_number = ?",
             "episode_number = ?"],
            [server_id, show_title,
             int(season_number), int(episode_number)],
        )
        if rk:
            return rk
    if item_type == "track" and artist and title:
        where = ["server_id = ?", "item_type = 'track'",
                 "artist = ? COLLATE NOCASE", "title = ? COLLATE NOCASE"]
        params = [server_id, artist, title]
        if album:
            where.append("album = ? COLLATE NOCASE")
            params.append(album)
        rk = _unique(where, params)
        if rk:
            return rk
    return None


def get_item_row(
    *, server_id: str, rating_key: str,
) -> Optional[Dict[str, Any]]:
    """Read a single mirror_items row by (server_id, rating_key).
    Used by callers that need the full row after a lookup helper
    returned just the rating_key (e.g. to inspect section_id for
    library-of-truth caching upstream)."""
    conn = server_mirror_db.get_connection()
    row = conn.execute(
        "SELECT * FROM mirror_items WHERE server_id = ? AND rating_key = ?",
        (server_id, rating_key),
    ).fetchone()
    return dict(row) if row else None


def count_items(
    *, server_id: str, section_id: Optional[str] = None,
    item_type: Optional[str] = None,
) -> int:
    """Row-count helper for diagnostics + the mode badge."""
    conn = server_mirror_db.get_connection()
    where = ["server_id = ?"]
    params: List[Any] = [server_id]
    if section_id is not None:
        where.append("section_id = ?")
        params.append(section_id)
    if item_type is not None:
        where.append("item_type = ?")
        params.append(item_type)
    sql = "SELECT COUNT(*) FROM mirror_items WHERE " + " AND ".join(where)
    row = conn.execute(sql, params).fetchone()
    return int(row[0]) if row else 0


# ── Cross-feed: Direction 1 ─────────────────────────────────────────────────
# Plan section 19.2 (XF). Playlist cache rows are already on disk with
# rating_key + title + type + GUIDs + file_path for every playlist
# member. Mirror reads them on sync and upserts any rating_keys it
# does not yet have. Zero API calls; popular items (those in
# operator-curated playlists) become the fastest to resolve because
# they are guaranteed to be in mirror after the bootstrap.

def bootstrap_from_playlist_cache(
    *,
    server_id: str,
    section_id_hint: Optional[str] = None,
) -> int:
    """Read every playlist_cache_items row for ``server_id`` and upsert
    any rating_keys not already present in mirror_items.

    Returns the number of rows ADDED to mirror_items (existing rows
    are left untouched; the live sync layer is canonical for those).

    ``section_id_hint`` is used as a placeholder section_id when the
    playlist_cache row does not have section info (it never does,
    since the cache predates the mirror schema). Callers can pass a
    backend-specific placeholder; the real section_id is filled in
    on the next live sync of the section containing the item.

    Wrapped in narrow ``sqlite3.OperationalError`` except (L1). A
    playlist_cache read failure during bootstrap is logged + skipped;
    it MUST NOT block the surrounding mirror sync.
    """
    import json
    from server import playlist_cache_db

    placeholder_section = section_id_hint or "_bootstrap"
    added = 0
    try:
        src_conn = playlist_cache_db._require_conn()
    except RuntimeError:
        # playlist_cache_db never initialised in this process; nothing
        # to bootstrap from.
        log.debug(
            "bootstrap_from_playlist_cache: playlist_cache_db not "
            "initialised; skipping bootstrap for server=%s", server_id,
        )
        return 0

    try:
        rows = src_conn.execute(
            "SELECT rating_key, title, type, guids_json, "
            "playlist_id, position FROM playlist_cache_items "
            "WHERE server_id = ? AND rating_key IS NOT NULL "
            "AND rating_key != ''",
            (server_id,),
        ).fetchall()
    except sqlite3.OperationalError as exc:
        log.warning(
            "bootstrap_from_playlist_cache: read failed for server=%s: %s",
            server_id, exc,
        )
        return 0

    if not rows:
        return 0

    conn = server_mirror_db.get_connection()
    lock = server_mirror_db.get_db_lock()
    now = time.time()
    with lock:
        conn.execute("BEGIN IMMEDIATE")
        try:
            seen: set = set()
            for r in rows:
                rk = r["rating_key"] if isinstance(r, sqlite3.Row) else r[0]
                if rk in seen:
                    continue
                seen.add(rk)
                existing = conn.execute(
                    "SELECT 1 FROM mirror_items "
                    "WHERE server_id = ? AND rating_key = ?",
                    (server_id, rk),
                ).fetchone()
                if existing is not None:
                    continue
                title = (
                    r["title"] if isinstance(r, sqlite3.Row) else r[1]
                ) or ""
                item_type = (
                    r["type"] if isinstance(r, sqlite3.Row) else r[2]
                ) or "unknown"
                guids_json_raw = (
                    r["guids_json"] if isinstance(r, sqlite3.Row) else r[3]
                ) or "[]"
                try:
                    guids_list = json.loads(guids_json_raw)
                    if not isinstance(guids_list, list):
                        guids_list = []
                except (ValueError, TypeError):
                    guids_list = []
                conn.execute(
                    "INSERT OR IGNORE INTO mirror_items("
                    "server_id, section_id, rating_key, title, "
                    "item_type, guids_json, mirror_synced_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (server_id, placeholder_section, str(rk), title,
                     item_type, json.dumps(guids_list), now),
                )
                added += 1
                for g in guids_list:
                    if not g:
                        continue
                    conn.execute(
                        "INSERT OR IGNORE INTO mirror_item_guids("
                        "server_id, rating_key, guid) VALUES (?, ?, ?)",
                        (server_id, str(rk), g),
                    )
            conn.execute("COMMIT")
        except sqlite3.Error as exc:
            conn.execute("ROLLBACK")
            log.warning(
                "bootstrap_from_playlist_cache: write failed for "
                "server=%s: %s", server_id, exc,
            )
            return 0

    if added:
        log.info(
            "server_mirror bootstrap: server=%s added %d item(s) "
            "from playlist_cache", server_id, added,
        )
    return added


def prune_old_drift_events(*, retention_days: int) -> int:
    """Delete drift_events older than ``retention_days`` days from now.

    Returns number of rows deleted. Called by the background
    refresher each tick.
    """
    if retention_days <= 0:
        return 0
    cutoff = time.time() - (retention_days * 86400)
    conn = server_mirror_db.get_connection()
    lock = server_mirror_db.get_db_lock()
    with lock:
        cur = conn.execute(
            "DELETE FROM drift_events WHERE detected_at < ?", (cutoff,),
        )
        return cur.rowcount or 0
