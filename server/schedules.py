"""
Background scheduler for recurring snapshots.

A schedule entry (defined in :mod:`server.persistence`) describes a
recurring snapshot run: which libraries, where to write output, and how
often the run should fire. This module owns a single daemon thread
that:

1. Wakes every 30 seconds.
2. Iterates over every enabled schedule.
3. Compares each schedule's *next firing time* to ``time.time()``.
4. If due, enqueues an snapshot job via :class:`server.jobs.JobQueue`
   and rolls ``next_run_at`` forward by the schedule's interval.
5. Persists the updated ``next_run_at`` back to disk so a restart
   doesn't re-fire something that was already handled.

Why not APScheduler? Adding a dependency for what amounts to "wake
up every 30 seconds and check a list" isn't worth the bytes. The
loop body is ~40 lines, all visible in this file.

Timezone policy
---------------
All comparisons are done in *local time* via the host clock (Docker's
``TZ`` env var if set). Schedules store wallclock fields (``hour``,
``minute``) and a UNIX timestamp ``next_run_at``. The UNIX timestamp
is the source of truth - the wallclock fields are only inputs to
:func:`_compute_next`.
"""

from __future__ import annotations

import datetime as dt
import logging
import threading
import time
from typing import Any, Dict, List, Optional

from server import persistence
from server.jobs import get_queue


log = logging.getLogger("plexmigrate.server.scheduler")


# ── Constants ────────────────────────────────────────────────────────────────

# How often the scheduler thread wakes. 30 seconds is fine: schedules
# fire at minute resolution, so half-minute polling guarantees no fire
# is missed by more than 30 s.
#
# Hot-reload (Phase 3): the loop reads ``tunables.scheduler_tick_seconds()``
# at the top of each iteration so a settings save takes effect within
# at most the previous tick's window. The constant below is the
# fallback used when the tunables module isn't importable (CLI-only
# checkouts) and matches the historical hardcoded value.
_TICK_SECONDS_FALLBACK = 30


# ── Next-fire computation ────────────────────────────────────────────────────

def _compute_next(schedule: Dict[str, Any], now: Optional[float] = None) -> float:
    """
    Return the UNIX timestamp of the next firing for ``schedule``.

    ``schedule`` carries:
      * ``frequency`` - "hourly", "daily", or "weekly"
      * ``hour``       - int 0..23  (used by daily / weekly)
      * ``minute``     - int 0..59  (used by all frequencies)
      * ``day_of_week``- int 0..6   (used by weekly; 0 = Monday)

    The returned timestamp is strictly *in the future* relative to
    ``now`` (or :func:`time.time`).
    """
    now_ts = now if now is not None else time.time()
    now_local = dt.datetime.fromtimestamp(now_ts)
    minute = int(schedule.get("minute", 0))
    hour = int(schedule.get("hour", 0))
    dow = int(schedule.get("day_of_week", 0))
    freq = schedule.get("frequency", "daily")

    if freq == "hourly":
        # Next occurrence of ":MM" in the current or following hour.
        candidate = now_local.replace(minute=minute, second=0, microsecond=0)
        if candidate <= now_local:
            candidate += dt.timedelta(hours=1)
        return candidate.timestamp()

    if freq == "daily":
        candidate = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= now_local:
            candidate += dt.timedelta(days=1)
        return candidate.timestamp()

    if freq == "weekly":
        # Python's weekday(): Monday=0 … Sunday=6 - matches our spec.
        target_dow = max(0, min(6, dow))
        candidate = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        delta_days = (target_dow - candidate.weekday()) % 7
        if delta_days == 0 and candidate <= now_local:
            delta_days = 7
        candidate += dt.timedelta(days=delta_days)
        return candidate.timestamp()

    # Unknown frequency - fall back to 24 hours from now so the
    # scheduler doesn't loop fire-immediately on a malformed entry.
    return now_ts + 86400


def ensure_next_run_at(schedule: Dict[str, Any]) -> Dict[str, Any]:
    """
    Ensure ``schedule`` carries a future ``next_run_at`` timestamp.

    Returns the schedule dict (possibly with ``next_run_at`` filled in
    or rolled forward). The caller is responsible for persisting the
    result if it was mutated.
    """
    now_ts = time.time()
    nxt = schedule.get("next_run_at")
    if not isinstance(nxt, (int, float)) or nxt <= now_ts:
        schedule["next_run_at"] = _compute_next(schedule, now=now_ts)
    return schedule


# ── Scheduler thread ─────────────────────────────────────────────────────────

class Scheduler:
    """
    The scheduler singleton. :func:`get_scheduler` lazily creates one;
    :func:`start` starts the daemon thread on first call.
    """

    def __init__(self) -> None:
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._started = False

    def start(self) -> None:
        """
        Idempotently start the scheduler. Must be called from the
        FastAPI startup hook so jobs persist across server restarts.

        On startup we re-validate every schedule's ``next_run_at``
        - if the server was down through a fire time, the schedule
        is simply rolled forward to the next future fire. We do not
        try to retroactively run missed exports; the user's intent
        with a scheduled export is "do it again soon", not "catch up
        on the ones I missed", and silently spawning queued jobs
        after a downtime would be surprising.
        """
        if self._started:
            return
        self._started = True
        # Refresh next_run_at for everything on disk so the first
        # tick has a consistent view.
        rows = persistence.load_schedules()
        changed = False
        for row in rows:
            before = row.get("next_run_at")
            ensure_next_run_at(row)
            if row.get("next_run_at") != before:
                changed = True
        if changed:
            persistence.save_schedules(rows)

        self._thread = threading.Thread(
            target=self._loop, name="plexmigrate-scheduler", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """Signal the scheduler thread to exit at its next tick."""
        self._stop.set()

    def _loop(self) -> None:
        """
        Main scheduler loop. See module docstring for the contract.
        """
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as exc:  # pragma: no cover (defensive)
                log.exception("Scheduler tick failed: %s", exc)
            # ``Event.wait`` returns early if .stop() is called while
            # we're sleeping - that gives us a clean shutdown.
            # Hot-reload: read the cadence each iteration so a save
            # to ``tunables.scheduler_tick_seconds`` takes effect on
            # the next wake without a process restart.
            try:
                from services.tunables import scheduler_tick_seconds
                tick = scheduler_tick_seconds()
            except Exception:
                tick = _TICK_SECONDS_FALLBACK
            self._stop.wait(tick)

    def _tick(self) -> None:
        """
        One scheduler iteration. Reads schedules from disk, fires any
        that are due (and not skipped because a job is already running
        - we let the job queue serialise them naturally), and writes
        the updated next_run_at values back.

        Multi-server (v0.9.0): each schedule carries a
        ``source_server_name`` that resolves against the registry at
        fire time. A schedule missing this field (carried over from a
        pre-v0.9.0 install) is skipped with a warning and its
        ``next_run_at`` is still rolled forward - we don't keep
        firing the same broken schedule once per minute, but we also
        don't auto-pick a server because that risks running against
        the wrong server.
        """
        rows = persistence.load_schedules()
        if not rows:
            return
        now_ts = time.time()
        mutated = False
        queue = get_queue()
        for row in rows:
            if not row.get("enabled", True):
                continue
            # Fix for a v0.8.0 bug: we used to call ensure_next_run_at()
            # here, but that helper rolls a stale next_run_at forward,
            # which meant any schedule that became due since the last
            # tick got rolled past the fire-check below and never
            # actually fired. The startup pass in :func:`Scheduler.start`
            # already rolls forward any schedules that were due during a
            # downtime, so the tick only needs to handle the corner case
            # of a malformed row missing next_run_at altogether.
            nxt = row.get("next_run_at")
            if not isinstance(nxt, (int, float)):
                row["next_run_at"] = _compute_next(row, now=now_ts)
                mutated = True
                continue
            if row["next_run_at"] <= now_ts:
                # Task 2 (admin-management plan follow-up, 2026-05-15):
                # schedules can fire snapshot OR restore OR direct.
                # Pre-Task-2 schedules have no ``mode`` field; treat
                # them as snapshot so behaviour on upgrade is identical.
                mode = str(row.get("mode") or "snapshot").strip()
                if mode not in ("snapshot", "restore", "direct"):
                    log.warning(
                        "Schedule %r has unknown mode %r; rolling forward without firing.",
                        row.get("name"), row.get("mode"),
                    )
                    row["next_run_at"] = _compute_next(row, now=now_ts)
                    mutated = True
                    continue

                # Common params shared by every mode.
                params: Dict[str, Any] = {
                    "libraries": list(row.get("libraries") or []),
                }
                if row.get("output_dir"):
                    params["output_dir"] = row["output_dir"]
                params["_trigger"] = "schedule"
                params["_schedule_name"] = str(row.get("name") or "")

                # ── Mode-specific requirement checks. Same shape as
                # the ScheduleIn validator but reading from the on-
                # disk row to handle schedules that were saved by an
                # older API version (defensive). ──
                if mode in ("snapshot", "direct") and not row.get("source_server_name"):
                    log.warning(
                        "Schedule %r (mode=%s) has no source_server_name; rolling forward.",
                        row.get("name"), mode,
                    )
                    row["next_run_at"] = _compute_next(row, now=now_ts)
                    mutated = True
                    continue
                if mode in ("restore", "direct"):
                    dests = row.get("dest_server_names") or []
                    if not dests:
                        log.warning(
                            "Schedule %r (mode=%s) has no dest_server_names; rolling forward.",
                            row.get("name"), mode,
                        )
                        row["next_run_at"] = _compute_next(row, now=now_ts)
                        mutated = True
                        continue
                if mode == "restore":
                    files = row.get("input_files") or []
                    if not files:
                        log.warning(
                            "Schedule %r (mode='restore') has no input_files; rolling forward.",
                            row.get("name"),
                        )
                        row["next_run_at"] = _compute_next(row, now=now_ts)
                        mutated = True
                        continue

                # ── Forward source / destination(s). ──
                if row.get("source_server_name"):
                    params["source_server_name"] = row["source_server_name"]
                if mode in ("restore", "direct"):
                    params["dest_server_names"] = list(row.get("dest_server_names") or [])
                if mode == "restore":
                    params["input_files"] = list(row.get("input_files") or [])

                # ── Per-mode-specific knobs. ──
                if mode in ("restore", "direct"):
                    if row.get("restore_mode") in ("merge", "replace"):
                        params["mode"] = row["restore_mode"]
                    if row.get("auto_capture_before_replace") is not None:
                        params["auto_capture_before_replace"] = bool(row["auto_capture_before_replace"])
                    if row.get("merge_watch_strategy") in ("higher", "sum"):
                        # 'sum' is allowed on a schedule when the
                        # end user set confirm_additive_merge=true at
                        # save time. The ScheduleIn validator gates
                        # this; the check below is belt-and-suspenders
                        # against a hand-edited row that drops the
                        # confirmation flag.
                        if (
                            row["merge_watch_strategy"] == "sum"
                            and not row.get("confirm_additive_merge")
                        ):
                            log.warning(
                                "Schedule %r has merge_watch_strategy='sum' but no "
                                "confirm_additive_merge flag; coercing to 'higher' for this fire.",
                                row.get("name"),
                            )
                            params["merge_watch_strategy"] = "higher"
                        else:
                            params["merge_watch_strategy"] = row["merge_watch_strategy"]
                    if row.get("restore_mode") == "replace":
                        # The validator gates this on confirm_replace at
                        # save time; double-check at fire time so a
                        # legacy row never silently fires Replace.
                        if not row.get("confirm_replace"):
                            log.warning(
                                "Schedule %r requested restore_mode='replace' without confirm_replace; "
                                "rolling forward without firing.",
                                row.get("name"),
                            )
                            row["next_run_at"] = _compute_next(row, now=now_ts)
                            mutated = True
                            continue
                        params["confirm_replace"] = True
                    if row.get("remap_old"):
                        params["remap_old"] = str(row["remap_old"])
                    if row.get("remap_new"):
                        params["remap_new"] = str(row["remap_new"])
                    if row.get("strict_match") is not None:
                        params["strict_match"] = bool(row["strict_match"])
                else:  # snapshot
                    if row.get("strict_match") is not None:
                        params["strict_match"] = bool(row["strict_match"])

                # ── Data-type filter (all four modes accept the same flags). ──
                for _flag in (
                    "include_watch_history", "include_ratings",
                    "include_playlists", "include_collections",
                ):
                    if _flag in row:
                        params[_flag] = bool(row[_flag])
                if mode == "snapshot" and "prebuild_json_sidecar" in row:
                    params["prebuild_json_sidecar"] = bool(row["prebuild_json_sidecar"])

                # ── Phase C (admin-management follow-up, 2026-05-15):
                # per-library metric map. The schedule's ScheduleIn
                # validator already expanded any global include_*
                # flags into library_metrics at save time, so the row
                # on disk should have a populated library_metrics
                # entry. Pass it straight through to the fired job.
                if row.get("library_metrics"):
                    params["library_metrics"] = row["library_metrics"]

                # ── Per-run settings (worker counts, verbose, log dir, etc.). ──
                for _num_field in ("workers", "scrobble_workers"):
                    _v = row.get(_num_field)
                    if isinstance(_v, (int, float)) and _v >= 1:
                        params[_num_field] = int(_v)
                for _bool_field in (
                    "verbose", "skip_playlist_prebuild", "fast_collection_detection",
                ):
                    if row.get(_bool_field) is not None:
                        params[_bool_field] = bool(row[_bool_field])
                _log_dir = row.get("log_dir")
                if isinstance(_log_dir, str) and _log_dir.strip():
                    params["log_dir"] = _log_dir.strip()
                _wr = row.get("watch_ratings_filter_strategy")
                if isinstance(_wr, str) and _wr.strip() in ("smart", "force_bulk", "force_server_side"):
                    params["watch_ratings_filter_strategy"] = _wr.strip()
                _uf = row.get("user_filter")
                if isinstance(_uf, list):
                    params["user_filter"] = [str(s) for s in _uf if isinstance(s, str)]

                # ── Schedules-alignment additions (2026-05-16) ──────
                # Forward the new per-job knobs that bring the schedule
                # row to Run Job submit parity. Each field is only
                # forwarded when present and meaningful on the disk row;
                # pre-alignment schedules omit them and the engine falls
                # back to the same defaults Run Job uses on a fresh form.
                # D-OWNER (a): per-user fan-out toggle. Default True
                # matches the adapter engines' include_managed_users
                # kwarg. We forward unconditionally so a hand-edited row
                # that flipped it to False is honored.
                if "include_managed_users" in row:
                    params["include_managed_users"] = bool(row["include_managed_users"])
                # D-RATE: per-job rating-mapping policy. Cross-backend
                # routes (source.service_type != dest.service_type)
                # consult these; same-backend routes ignore them. The
                # engine reads rate_threshold only when rate_mode ==
                # 'tunable' (validator gates this combination too).
                _rm = row.get("rate_mode")
                if _rm in ("default", "tunable", "numeric_only"):
                    params["rate_mode"] = _rm
                _rt = row.get("rate_threshold")
                if isinstance(_rt, (int, float)):
                    params["rate_threshold"] = float(_rt)
                # D-OWNER (b): pre-confirmed user-create specs. The
                # adapter engine reads this list and idempotently
                # creates missing destination users before the data
                # write. List of dicts shaped like ProposedUser /
                # UserCreateSpec.
                _ucs = row.get("user_create_specs")
                if isinstance(_ucs, list) and _ucs:
                    params["user_create_specs"] = _ucs
                # Per-Run Settings parity: overwrite_playlists. Run Job
                # exposes this in Per-Run Settings > General; the engine
                # treats it as a back-compat no-op today but Run Job
                # forwards it on every submit so we mirror that on
                # scheduled fires too.
                if row.get("overwrite_playlists") is not None:
                    params["overwrite_playlists"] = bool(row["overwrite_playlists"])
                # Plan[MIXED-MEDIA-PLAYLISTS]-2026-05-16: per-run mixed-
                # media playlist overrides. None on the row means
                # "inherit global tunable", so we forward only when the
                # end user set an explicit value. Same field set lives
                # on SnapshotJobIn / RestoreJobIn / DirectTransferIn.
                for _mm_field in (
                    "mixed_media_behavior",
                    "mixed_media_dominance_threshold",
                    "mixed_media_video_routing",
                    "mixed_media_logging",
                    "mixed_media_collision_handling",
                ):
                    _mm_val = row.get(_mm_field)
                    if _mm_val is not None:
                        params[_mm_field] = _mm_val
                # Note: pin_preflight_ack is a save-time UX gate ONLY.
                # It is intentionally NOT forwarded to the engine; the
                # field tracks end user acknowledgement of cross-server
                # PIN risk in the editor and clears when the schedule's
                # source server changes.

                # ── Dispatch to the right submit_* call. ──
                if mode == "snapshot":
                    rec = queue.submit_snapshot(params)
                elif mode == "restore":
                    rec = queue.submit_restore(params)
                else:  # direct
                    rec = queue.submit_direct(params)
                row["last_job_id"] = rec.job_id
                row["last_fired_at"] = now_ts
                # Roll forward to the next future occurrence.
                row["next_run_at"] = _compute_next(row, now=now_ts)
                mutated = True
                log.info(
                    "Scheduler fired schedule %r (mode=%s, server=%s, dests=%s, job_id=%s, next=%s)",
                    row.get("name"),
                    mode,
                    row.get("source_server_name"),
                    row.get("dest_server_names") or "-",
                    rec.job_id,
                    dt.datetime.fromtimestamp(row["next_run_at"]).isoformat(timespec="seconds"),
                )
        if mutated:
            persistence.save_schedules(rows)


_singleton: Optional[Scheduler] = None


def get_scheduler() -> Scheduler:
    global _singleton
    if _singleton is None:
        _singleton = Scheduler()
    return _singleton


# ── List view helper ─────────────────────────────────────────────────────────

def list_schedules() -> List[Dict[str, Any]]:
    """
    Return all schedules with their ``next_run_at`` freshly computed.

    Used by ``GET /api/schedules`` so the frontend always sees an
    up-to-date "next run" column even right after a fire.

    Also augments each row with ``resolutions_status`` per
    Plan[CROSS-PLATFORM-PREFLIGHT] step 5: a status badge for the
    schedule list UI based on whether the row's stored
    cross_platform_resolutions still point at users that exist on
    the current destination roster.
    """
    rows = persistence.load_schedules()
    for row in rows:
        ensure_next_run_at(row)
        row["resolutions_status"] = _compute_resolutions_status(row)
    return rows


def _compute_resolutions_status(schedule_row: Dict[str, Any]) -> str:
    """Return 'ok' | 'auto_fallback' | 'needs_review' for one schedule.

    Conservative classification:
      * No stored resolutions OR no cross-platform involvement -> 'ok'.
      * Any stored decision points at a destination that's no longer
        registered, OR a 'map' decision targets a backend_user_id that
        no longer exists on the destination roster -> 'needs_review'.
      * Any decision is 'accept_proposed' -> 'auto_fallback' (the
        schedule fires with the original implicit pick; consider
        locking with an identity map).
      * Otherwise -> 'ok'.

    Read-only. Failures during the live roster check default the
    affected destination to 'ok' so the schedules list always
    renders; the engine still enforces correctness at fire time.
    """
    stored = schedule_row.get("cross_platform_resolutions") or {}
    if not isinstance(stored, dict) or not stored:
        return "ok"
    worst = "ok"
    rank = {"ok": 0, "auto_fallback": 1, "needs_review": 2}
    for dest_server_id, ack in stored.items():
        if not isinstance(ack, dict):
            continue
        # Destination still registered?
        try:
            from server import server_registry
            row = server_registry.get_server_by_id(
                dest_server_id, include_token=False,
            )
        except Exception:
            row = None
        if row is None:
            worst = "needs_review"
            continue
        decisions = ack.get("resolutions") or []
        if not isinstance(decisions, list):
            continue
        live_user_ids = _live_dest_user_ids(dest_server_id)
        for dec in decisions:
            if not isinstance(dec, dict):
                continue
            action = (dec.get("action") or "").strip().lower()
            if action == "map":
                target = (dec.get("dest_user_id") or "").strip()
                if (target and live_user_ids is not None
                        and target not in live_user_ids):
                    if rank["needs_review"] > rank[worst]:
                        worst = "needs_review"
            elif action == "accept_proposed":
                if rank["auto_fallback"] > rank[worst]:
                    worst = "auto_fallback"
    return worst


def _live_dest_user_ids(dest_server_id: str):
    """Return the current backend_user_id set for the destination,
    or None on any lookup failure (caller treats None as
    'can't verify; skip drift check' rather than 'every id is stale')."""
    try:
        from server import server_registry
        connection = server_registry.connect_registered_server(
            dest_server_id, log,
        )
        users = connection.adapter.list_users() or []
        return {u.backend_user_id for u in users if u.backend_user_id}
    except Exception:
        return None
