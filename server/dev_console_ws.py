"""
server/dev_console_ws.py - WebSocket channel for the "Server Commands"
developer console.

The dashboard socket (``server/ws.py``) polls global job state at
4 Hz. The dev console needs something different: it must push a
discrete event the moment a console command completes, AND surface
background-job state changes (a snapshot / restore launched from
another tab) without flooding the client.

:class:`DevConsoleWSManager` does both:

* ``publish(event)`` is a synchronous, thread-safe enqueue. The REST
  handlers in ``server/dev_console_router.py`` call it after every
  successful write - they run on the request thread, not the event
  loop, so they cannot ``await`` a broadcast directly. Command events
  are drained and broadcast to every client.
* The heartbeat is scoped per client. Each client tells the manager
  which server it is currently viewing (``set_watch``); the manager
  then sends that client a heartbeat at the ACTIVE cadence
  (``dev_console_heartbeat_active_seconds``) carrying the current job
  record. A client with no server selected gets the slower IDLE
  cadence. The point is to surface a concurrent snapshot / restore
  for the panel actually on screen without spamming idle clients.

Both cadences are tunables, read live each tick.

Started / stopped by the FastAPI lifecycle hooks in ``server/app.py``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from typing import Any, Deque, Dict, Optional, Set

from fastapi import WebSocket

log = logging.getLogger("plexmigrate.server.dev_console_ws")

# 4 Hz drain loop. Heartbeats fire off this loop but at the much
# slower, tunable per-client cadence.
_TICK_SECONDS = 0.25


def _current_job_slim() -> Optional[Dict[str, Any]]:
    """Slim view of the running job so the console can show that a
    snapshot / restore is mutating state underneath it. None when the
    worker is idle."""
    try:
        from server.jobs import get_queue
        rec = get_queue().current()
    except Exception:
        return None
    if rec is None:
        return None
    return {
        "job_id": rec.job_id,
        "mode": rec.mode,
        "state": rec.state,
        "started_at": rec.started_at,
        "finished_at": rec.finished_at,
    }


def _heartbeat_rates() -> tuple:
    """(active_seconds, idle_seconds), read live from tunables."""
    try:
        from services import tunables
        return (
            tunables.dev_console_heartbeat_active_seconds(),
            tunables.dev_console_heartbeat_idle_seconds(),
        )
    except Exception:
        return (10, 60)


class DevConsoleWSManager:
    """Tracks dev-console WebSocket clients, broadcasts command events,
    and sends each client a per-server-scoped heartbeat."""

    def __init__(self) -> None:
        self._clients: Set[WebSocket] = set()
        self._lock = asyncio.Lock()
        # Per-client viewed server id ("" = none selected) and the
        # monotonic timestamp of that client's last heartbeat.
        self._watch: Dict[WebSocket, str] = {}
        self._last_hb: Dict[WebSocket, float] = {}
        # deque.append / popleft are atomic under CPython's GIL, so the
        # synchronous publish() needs no extra lock.
        self._pending: Deque[Dict[str, Any]] = deque(maxlen=512)
        self._broadcaster_task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()

    # ── Connection registry ──────────────────────────────────────────

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self._clients.add(ws)
            self._watch[ws] = ""
            # 0.0 forces an immediate first heartbeat on the next tick.
            self._last_hb[ws] = 0.0
        try:
            await ws.send_text(json.dumps({
                "type": "hello",
                "server_ts": time.time(),
                "job": _current_job_slim(),
            }))
        except Exception:
            await self.disconnect(ws)

    async def disconnect(self, ws: WebSocket) -> None:
        async with self._lock:
            self._clients.discard(ws)
            self._watch.pop(ws, None)
            self._last_hb.pop(ws, None)
        try:
            await ws.close()
        except Exception:
            pass

    def set_watch(self, ws: WebSocket, server_id: str) -> None:
        """Record which server a client is viewing. Called from the
        WebSocket route when the client sends ``{"watch": "<id>"}``.
        Resets the heartbeat clock so the freshly-viewed server gets a
        prompt heartbeat."""
        if ws not in self._watch:
            return
        self._watch[ws] = (server_id or "").strip()
        self._last_hb[ws] = 0.0

    async def broadcast(self, payload: Dict[str, Any]) -> None:
        text = json.dumps(payload)
        dead: Set[WebSocket] = set()
        async with self._lock:
            for ws in self._clients:
                try:
                    await ws.send_text(text)
                except Exception:
                    dead.add(ws)
            for ws in dead:
                self._drop_locked(ws)

    def _drop_locked(self, ws: WebSocket) -> None:
        self._clients.discard(ws)
        self._watch.pop(ws, None)
        self._last_hb.pop(ws, None)

    # ── Event publishing ─────────────────────────────────────────────

    def publish(self, event: Dict[str, Any]) -> None:
        """Synchronous, thread-safe. Enqueue one command event for the
        next drain tick. Safe to call from the request thread."""
        try:
            payload = dict(event)
            payload.setdefault("type", "command")
            payload["server_ts"] = time.time()
            self._pending.append(payload)
        except Exception:
            log.debug("dev_console_ws: publish failed", exc_info=True)

    def pending_count(self) -> int:
        """Number of command events waiting to drain. Exposed for tests."""
        return len(self._pending)

    # ── Background broadcaster ───────────────────────────────────────

    async def start(self) -> None:
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
        try:
            while not self._stop.is_set():
                # CONSOLE-15: wrap the per-tick work so one unhandled
                # exception logs and the loop CONTINUES -- a single bad
                # tick must not terminate the broadcaster permanently.
                try:
                    # Drain every queued command event to all clients.
                    while self._pending:
                        try:
                            event = self._pending.popleft()
                        except IndexError:
                            break
                        await self.broadcast(event)
                    await self._send_due_heartbeats()
                except asyncio.CancelledError:
                    # A real cancel must still propagate and exit cleanly.
                    raise
                except Exception:  # pragma: no cover (defensive)
                    log.exception(
                        "dev-console WS broadcaster: tick failed, continuing"
                    )
                try:
                    await asyncio.wait_for(
                        self._stop.wait(), timeout=_TICK_SECONDS,
                    )
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            pass
        except Exception:  # pragma: no cover (defensive)
            # Final safety net. The inner per-tick handler keeps the
            # loop alive across a bad tick; this catches whatever the
            # loop scaffolding itself can still raise - notably
            # self._stop.wait() when the manager singleton outlives the
            # asyncio loop it was bound to (a test-harness reuse case).
            # The task MUST always finish cleanly: stop() does
            # `await self._broadcaster_task`, so an escaped exception
            # would re-raise straight into the app-shutdown hook.
            log.exception("dev-console WS broadcaster crashed")

    async def _send_due_heartbeats(self) -> None:
        """Send each client a heartbeat once its per-client cadence has
        elapsed. The cadence is ACTIVE when the client is viewing a
        server, IDLE otherwise."""
        active_s, idle_s = _heartbeat_rates()
        now = time.monotonic()
        job = _current_job_slim()
        dead: Set[WebSocket] = set()
        async with self._lock:
            for ws in list(self._clients):
                viewed = self._watch.get(ws, "")
                rate = active_s if viewed else idle_s
                if now - self._last_hb.get(ws, 0.0) < rate:
                    continue
                self._last_hb[ws] = now
                try:
                    await ws.send_text(json.dumps({
                        "type": "heartbeat",
                        "server_ts": time.time(),
                        "server_id": viewed or None,
                        "job": job,
                    }))
                except Exception:
                    dead.add(ws)
            for ws in dead:
                self._drop_locked(ws)


# ── Module-level singleton ───────────────────────────────────────────────────
_singleton: Optional[DevConsoleWSManager] = None


def get_dev_console_manager() -> DevConsoleWSManager:
    global _singleton
    if _singleton is None:
        _singleton = DevConsoleWSManager()
    return _singleton
