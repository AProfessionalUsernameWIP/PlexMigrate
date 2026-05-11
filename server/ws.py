"""
WebSocket broadcaster for live dashboard state.

A single async task polls :func:`services.state._dashboard.snapshot`
at 4 Hz (matching the CLI dashboard's refresh rate) and pushes the
result to every connected WebSocket client. New clients receive an
immediate snapshot on connect so the dashboard never shows a blank
screen while waiting for the first tick.

Why poll instead of pushing on every state change? Because the engine
is heavily multi-threaded and every worker increments counters
several times per second — pushing on each mutation would saturate
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
from typing import Any, Dict, Optional, Set

from fastapi import WebSocket, WebSocketDisconnect

import services.state as state
from server.jobs import JobRecord, get_queue


log = logging.getLogger("plexmigrate.server.ws")


# ── Snapshot construction ────────────────────────────────────────────────────

def build_snapshot() -> Dict[str, Any]:
    """
    Compose the JSON payload broadcast to clients on each tick.

    Three components:
      * ``dashboard`` — :func:`DashboardState.snapshot` if a job is
        running, else ``None``.
      * ``job``       — slim view of the current :class:`JobRecord`.
      * ``server_ts`` — server time at snapshot construction (used by
        the frontend to compute live elapsed times without drifting
        from its own ``Date.now()``).
    """
    dash = None
    if state._dashboard is not None:
        try:
            dash = state._dashboard.snapshot()
        except Exception:  # pragma: no cover (defensive)
            dash = None

    job_record: Optional[JobRecord] = get_queue().current()
    job_payload: Optional[Dict[str, Any]] = None
    if job_record is not None:
        job_payload = {
            "job_id": job_record.job_id,
            "mode": job_record.mode,
            "state": job_record.state,
            "queued_at": job_record.queued_at,
            "started_at": job_record.started_at,
            "finished_at": job_record.finished_at,
            "error": job_record.error,
            "run_log_dir": job_record.run_log_dir,
            "params": {
                # Strip the token before sending to the browser.
                k: v for k, v in job_record.params.items() if k != "plex_token"
            },
        }

    return {
        "type": "snapshot",
        "server_ts": time.time(),
        "dashboard": dash,
        "job": job_payload,
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
            await ws.send_text(json.dumps(build_snapshot()))
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
                payload = build_snapshot()
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
