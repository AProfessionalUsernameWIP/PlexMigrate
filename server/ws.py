"""
WebSocket broadcaster for live dashboard state.

A single async task polls :func:`services.state.get_dashboard().to_dashboard_frame`
at 4 Hz (matching the CLI dashboard's refresh rate) and pushes the
result to every connected WebSocket client. New clients receive an
immediate snapshot on connect so the dashboard never shows a blank
screen while waiting for the first tick.

Why poll instead of pushing on every state change? Because the engine
is heavily multi-threaded and every worker increments counters
several times per second - pushing on each mutation would saturate
the socket and the browser would be unable to render fast enough.
A 4 Hz tick is fine for a status dashboard and matches the CLI Rich
Live cadence the user is already familiar with.

The broadcaster is started by the FastAPI startup hook and stopped by
the shutdown hook.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import asdict
from typing import Any, Dict, List, Optional, Set

from fastapi import WebSocket, WebSocketDisconnect

import services.state as state
from server import network_collector
from server.fan_out import get_active_result as get_active_fan_out
from server.jobs import JobRecord, get_queue
from server.server_registry import list_servers


log = logging.getLogger("plexmigrate.server.ws")


def _build_servers_network() -> List[Dict[str, Any]]:
    """
    Join every registered server against the network collector and
    return per-server telemetry suitable for the Networking tab.

    Pre-v0.12.0 the Network panel read out of the per-job
    DashboardState - invisible when idle, gone in fan-out. This
    function reads from the process-lifetime collector keyed by
    URL host, then joins with the registry by host so each entry
    carries the end user-friendly server name and the registry
    id needed for the "open server settings" affordance.

    Servers the collector hasn't seen any traffic for still appear
    with empty windows - the UI treats them as "no data yet."
    Telemetry buckets for hosts no longer in the registry (the
    end user removed a row mid-run) are dropped from this payload;
    the buckets themselves persist in the collector until process
    restart, which is intentional - they're harmless and let stale
    references resolve cleanly if the registry row comes back.
    """
    try:
        servers = list_servers(include_tokens=False)
    except Exception:
        return []

    # Normalise registry hosts the same way the collector does so
    # the join key matches even when an end user typed mixed case
    # or a trailing slash into the registry URL.
    by_host: Dict[str, Dict[str, Any]] = {}
    for row in servers:
        url = (row.get("url") or "").strip()
        if not url:
            continue
        host = network_collector._normalise_host(url)
        if host:
            by_host[host] = row

    snapshots = network_collector.collect_network_state()
    out: List[Dict[str, Any]] = []
    matched_hosts = set()
    for snap in snapshots:
        row = by_host.get(snap["host"])
        if row is None:
            # Unregistered host - skip from the UI surface. See
            # docstring for the rationale.
            continue
        matched_hosts.add(snap["host"])
        out.append({
            "server_id": row["id"],
            "server_name": row.get("name") or "",
            "url": row.get("url") or "",
            "host": snap["host"],
            "rps": snap["rps"],
            "avg_ms": snap["avg_ms"],
            "last_ping_ms": snap["last_ping_ms"],
            "last_ping_ok": snap["last_ping_ok"],
            "last_ping_at": snap["last_ping_at"],
            "last_seen_at": snap["last_seen_at"],
            "window_status_counts": snap["window_status_counts"],
            "cumulative_status_counts": snap["cumulative_status_counts"],
            "rate_limit_events": snap["rate_limit_events"],
            "sample_count_in_window": snap["sample_count_in_window"],
        })
    # Registered servers we have no telemetry for yet → emit empty
    # entries so the Networking tab can render their "no data yet"
    # cards instead of having them silently absent.
    for host, row in by_host.items():
        if host in matched_hosts:
            continue
        out.append({
            "server_id": row["id"],
            "server_name": row.get("name") or "",
            "url": row.get("url") or "",
            "host": host,
            "rps": 0.0,
            "avg_ms": None,
            "last_ping_ms": None,
            "last_ping_ok": False,
            "last_ping_at": 0.0,
            "last_seen_at": 0.0,
            "window_status_counts": {},
            "cumulative_status_counts": {},
            "rate_limit_events": [],
            "sample_count_in_window": 0,
        })
    return out


# ── Snapshot construction ────────────────────────────────────────────────────

def _active_job_participants(job_payload: Optional[Dict[str, Any]]) -> Optional[Set[str]]:
    """
    Return the set of server names that participate in the active job.

    Used by :func:`build_dashboard_frame` (PR-2 / Phase C - ex-Phase A activity
    feed scoping) to filter activity entries down to "this job's
    servers." ``None`` means "no active job - emit every entry."
    An empty set means "active job, but no named participants found"
    - in practice that only happens for legacy / malformed jobs and we
    treat it the same as no active job to be conservative.

    The fields inspected are:

      * ``source_server_name`` - snapshotter / direct-transfer source
      * ``dest_server_name``   - single-destination import / direct
      * ``dest_server_names``  - fan-out import / direct
    """
    if not job_payload:
        return None
    state_str = (job_payload.get("state") or "").lower()
    if state_str in ("idle", "completed", "failed", "cancelled", ""):
        return None
    params = job_payload.get("params") or {}
    names: Set[str] = set()
    s = params.get("source_server_name")
    if isinstance(s, str) and s.strip():
        names.add(s.strip())
    d = params.get("dest_server_name")
    if isinstance(d, str) and d.strip():
        names.add(d.strip())
    dlist = params.get("dest_server_names")
    if isinstance(dlist, list):
        for n in dlist:
            if isinstance(n, str) and n.strip():
                names.add(n.strip())
    return names if names else None


def _scope_activity(
    dash: Optional[Dict[str, Any]],
    participants: Optional[Set[str]],
) -> None:
    """
    In-place filter of ``dash["activity"]`` to suppress entries tagged
    for servers that are not active participants in the current job.

    Untagged entries (``server_name == ""``) always pass - that covers
    every existing engine call site untouched. Only entries explicitly
    tagged with a non-participant server name are dropped.

    No-op when ``participants`` is ``None`` (no active job) or when the
    dashboard payload doesn't carry an activity list.
    """
    if dash is None or participants is None:
        return
    activity = dash.get("activity")
    if not isinstance(activity, list):
        return
    dash["activity"] = [
        e for e in activity
        if not e.get("server_name") or e.get("server_name") in participants
    ]


def build_dashboard_frame() -> Dict[str, Any]:
    """
    Compose the JSON payload broadcast to clients on each tick.

    Four components:
      * ``dashboard`` - :func:`DashboardState.to_dashboard_frame` if a job is
        running, else ``None``.
      * ``job``       - slim view of the current :class:`JobRecord`.
      * ``fan_out``   - per-destination dashboards when a fan-out job
        is active (v0.10.0). ``None`` for single-destination jobs.
      * ``server_ts`` - server time at snapshot construction (used by
        the frontend to compute live elapsed times without drifting
        from its own ``Date.now()``).
    """
    dash = None
    if state.get_dashboard() is not None:
        try:
            dash = state.get_dashboard().to_dashboard_frame()
        except Exception:  # pragma: no cover (defensive)
            dash = None

    def _record_to_payload(r: JobRecord) -> Dict[str, Any]:
        # Strip the token before sending to the browser.
        return {
            "job_id": r.job_id,
            "mode": r.mode,
            "state": r.state,
            "queued_at": r.queued_at,
            "started_at": r.started_at,
            "finished_at": r.finished_at,
            "error": r.error,
            "run_log_dir": r.run_log_dir,
            "params": {k: v for k, v in r.params.items() if k != "plex_token"},
        }

    job_record: Optional[JobRecord] = get_queue().current()
    job_payload: Optional[Dict[str, Any]] = None
    if job_record is not None:
        job_payload = _record_to_payload(job_record)

    # PR-8 - Dashboard multi-job sub-tabs. Surface every active + queued
    # job in a single list so the frontend can render one sub-tab per
    # job when there's more than one. Backward-compat: the existing
    # ``job`` field above stays populated with the running record so
    # older clients keep working. Empty list when the worker is idle
    # and nothing is queued.
    jobs_payload: List[Dict[str, Any]] = [
        _record_to_payload(r) for r in get_queue().active_and_queued()
    ]

    # PR-2 / Phase C - activity-feed scoping (ex-Phase A fix).
    # During an active job, suppress feed entries tagged with a server
    # name that isn't a participant. Untagged entries are always
    # included (so the existing call sites need no changes), and the
    # filter is a no-op when no job is active.
    participants = _active_job_participants(job_payload)
    _scope_activity(dash, participants)

    # v0.10.0 - fan-out card array. ``None`` when no fan-out is active
    # so older frontend builds that only know about ``dashboard``
    # continue to work unchanged.
    fan_out_payload: Optional[List[Dict[str, Any]]] = None
    active = get_active_fan_out()
    if active is not None and len(active.destinations) > 1:
        fan_out_payload = []
        for d in active.destinations:
            entry: Dict[str, Any] = {
                "dest_name": d.dest_name,
                "state": d.state,
                "log_dir": d.log_dir,
                "started_at": d.started_at,
                "finished_at": d.finished_at,
                "error": d.error,
                "dashboard": None,
            }
            if d.dashboard is not None:
                try:
                    entry["dashboard"] = d.dashboard.to_dashboard_frame()
                except Exception:  # pragma: no cover (defensive)
                    entry["dashboard"] = None
            # Same scoping applies to each fan-out destination's
            # dashboard activity. The destination's own dest_name is
            # always considered a participant.
            if entry["dashboard"] is not None and participants is not None:
                _scope_activity(entry["dashboard"], participants)
            fan_out_payload.append(entry)

    return {
        "type": "dashboard_frame",
        "server_ts": time.time(),
        "dashboard": dash,
        "job": job_payload,
        # PR-8 - every active + queued job for the multi-job sub-tab
        # strip. ``job`` (above) is the running one only; ``jobs``
        # mirrors what was queued through the public submission API.
        "jobs": jobs_payload,
        "fan_out": fan_out_payload,
        # v0.12.0 - process-lifetime, server-keyed HTTP telemetry.
        # Always present; an idle install still gets entries for
        # every registered server (empty until the ping poll fires
        # or a job runs).
        "servers_network": _build_servers_network(),
    }


# ── Connection registry ──────────────────────────────────────────────────────

class WSManager:
    """
    Tracks connected WebSocket clients and pushes snapshots to them.

    Active connections live in a set guarded by an asyncio lock. We
    use the same lock for connect / disconnect / broadcast so the
    set can never mutate during iteration.
    """

    def __init__(self) -> None:
        self._clients: Set[WebSocket] = set()
        self._lock = asyncio.Lock()
        self._broadcaster_task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self._clients.add(ws)
        # Send an immediate snapshot so the new client doesn't have to
        # wait up to 250 ms for the next tick.
        try:
            await ws.send_text(json.dumps(build_dashboard_frame()))
        except Exception:
            await self.disconnect(ws)

    async def disconnect(self, ws: WebSocket) -> None:
        async with self._lock:
            self._clients.discard(ws)
        try:
            await ws.close()
        except Exception:
            pass

    async def broadcast(self, payload: Dict[str, Any]) -> None:
        text = json.dumps(payload)
        dead: Set[WebSocket] = set()
        async with self._lock:
            for ws in self._clients:
                try:
                    await ws.send_text(text)
                except Exception:
                    # The client went away mid-send. Mark for removal
                    # outside the iteration to avoid mutating the set
                    # while we walk it.
                    dead.add(ws)
            for ws in dead:
                self._clients.discard(ws)

    # ── Background broadcaster task ──────────────────────────────────

    async def start(self) -> None:
        """
        Start the 4 Hz polling task. Safe to call once at app startup.
        """
        if self._broadcaster_task is not None:
            return
        self._stop.clear()
        self._broadcaster_task = asyncio.create_task(self._tick_loop())

    async def stop(self) -> None:
        self._stop.set()
        if self._broadcaster_task is not None:
            await self._broadcaster_task
            self._broadcaster_task = None

    async def _tick_loop(self) -> None:
        """
        Inner loop: build a snapshot, broadcast it, sleep 250 ms.

        Snapshot construction is synchronous and very cheap (it copies
        a small dict). We don't offload it to a thread because the
        DashboardState lock is held for microseconds at a time.
        """
        try:
            while not self._stop.is_set():
                payload = build_dashboard_frame()
                # Only broadcast when there's something interesting to
                # say. When idle and no clients, this is essentially a
                # no-op. When idle and clients are connected, we still
                # send so the frontend can update job-history fields.
                await self.broadcast(payload)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=0.25)
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            pass
        except Exception:  # pragma: no cover (defensive)
            log.exception("WS broadcaster crashed")


# ── Module-level singleton ────────────────────────────────────────────────────
_singleton: Optional[WSManager] = None


def get_manager() -> WSManager:
    global _singleton
    if _singleton is None:
        _singleton = WSManager()
    return _singleton
