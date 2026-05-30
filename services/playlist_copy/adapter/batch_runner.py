"""Minimal batch-execution helpers for per-source semaphore caps and cancel-aware acquisition."""

from __future__ import annotations

import threading
from typing import Dict, Iterable, Optional

from services.tunables import playlist_mgmt_batch_per_source_workers


# Module-level registry of per-source-server semaphores. Lazy-created on
# first use, keyed by source server_id. Protected by _PER_SOURCE_LOCK;
# semaphore depth is captured at creation from playlist_mgmt_batch_per_source_workers().
# Single-copy REST paths (list_user_playlists, get_playlist_detail, legacy
# copy_playlist endpoint) bypass this cap because they are one-shot operator
# actions, not fan-outs that risk thrashing a source.

_PER_SOURCE_SEMAPHORES: Dict[str, threading.Semaphore] = {}
_PER_SOURCE_LOCK = threading.Lock()


def _per_source_semaphore_for(source_server_id: str) -> threading.Semaphore:
    """Return the (lazily-built) semaphore for this source server.
    Thread-safe; safe to call from multiple batch workers concurrently."""
    key = source_server_id or "<unknown>"
    with _PER_SOURCE_LOCK:
        sem = _PER_SOURCE_SEMAPHORES.get(key)
        if sem is None:
            depth = playlist_mgmt_batch_per_source_workers()
            sem = threading.Semaphore(depth)
            _PER_SOURCE_SEMAPHORES[key] = sem
        return sem


def _reset_per_source_semaphores_for_tests() -> None:
    """Test-only hook to drop the cached semaphores so each test starts
    fresh (the depth is captured at creation time, so tuning the
    underlying tunable mid-test requires this reset)."""
    with _PER_SOURCE_LOCK:
        _PER_SOURCE_SEMAPHORES.clear()


def acquire_with_cancel(
    sem: threading.Semaphore,
    cancel_events: Iterable[Optional[threading.Event]],
    *,
    poll_interval: float = 0.1,
) -> bool:
    """Acquire ``sem`` while polling ``cancel_events``.

    Returns True on a successful acquire, False if any cancel event
    fires first. ``None`` entries in ``cancel_events`` are ignored, so
    callers can pass an optional whole-batch stop event alongside an
    optional per-item event without juggling Nones themselves. The
    semaphore is polled with ``poll_interval`` (default 0.1s) so a
    cancel issued while a worker waits for a slot drops the work item
    promptly instead of blocking on a busy source.

    The events are checked in iteration order on each loop, then the
    semaphore acquire is tried; on a tie (one event already set when
    the call enters) the cancel wins.
    """
    while True:
        for ev in cancel_events:
            if ev is not None and ev.is_set():
                return False
        if sem.acquire(timeout=poll_interval):
            return True
