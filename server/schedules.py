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
                # Build the snapshot params from the schedule. The job
                # queue inherits any unset fields from saved settings
                # automatically.
                params: Dict[str, Any] = {
                    "libraries": list(row.get("libraries") or []),
                }
                if row.get("output_dir"):
                    params["output_dir"] = row["output_dir"]
                # Tag the run so the resulting .plexexport.json carries
                # "Scheduled: <schedule name>" in its metadata - the
                # Snapshots tab uses this to badge each row by origin.
                params["_trigger"] = "schedule"
                params["_schedule_name"] = str(row.get("name") or "")
                if row.get("source_server_name"):
                    params["source_server_name"] = row["source_server_name"]
                else:
                    log.warning(
                        "Schedule %r has no source_server_name set; rolling forward without firing. "
                        "Edit it under the Schedules tab and pick a registered server.",
                        row.get("name"),
                    )
                    row["next_run_at"] = _compute_next(row, now=now_ts)
                    mutated = True
                    continue
                # P2-1: forward per-schedule strict_match override when set.
                # None on the schedule row means "inherit settings.json".
                if row.get("strict_match") is not None:
                    params["strict_match"] = bool(row["strict_match"])
                # PR-3 / Phase D - forward the four-flag data-type filter
                # from the schedule row. Schedules saved before Phase D
                # default to all-true at load time via the Pydantic
                # model so unset fields preserve pre-Phase-D behaviour.
                for _flag in (
                    "include_watch_history", "include_ratings",
                    "include_playlists", "include_collections",
                    "prebuild_json_sidecar",
                ):
                    if _flag in row:
                        params[_flag] = bool(row[_flag])
                # v0.14 Per-Run Settings on schedules. Each non-None
                # field overrides the global / per-server value the
                # snapshot job would otherwise inherit at fire time.
                # Empty / blank strings on watch_ratings_filter_strategy
                # are skipped so the chain falls through to per-server
                # or global default.
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
                rec = queue.submit_snapshot(params)
                row["last_job_id"] = rec.job_id
                row["last_fired_at"] = now_ts
                # Roll forward to the next future occurrence.
                row["next_run_at"] = _compute_next(row, now=now_ts)
                mutated = True
                log.info(
                    "Scheduler fired schedule %r (server=%s, job_id=%s, next=%s)",
                    row.get("name"),
                    row.get("source_server_name"),
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
    """
    rows = persistence.load_schedules()
    for row in rows:
        ensure_next_run_at(row)
    return rows
