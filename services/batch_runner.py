"""Minimal batch-execution helpers shared by the playlist batch runner.

Currently sized for one caller (``services.playlist_copy.copy_playlist_batch``);
kept deliberately tiny on purpose. This is the seam future batch-style
callers (the restorer engines, the resolver pool, fan-out) can grow into
without touching ``playlist_copy.py``. Two pieces live here:

* A per-source-server semaphore registry that caps in-flight work
  against one source regardless of total batch parallelism.
* A cancel-aware acquire helper that polls cancel signals while
  waiting for a semaphore slot, so an operator Stop or per-item
  Cancel drops the item promptly instead of blocking on a busy
  source.

Extracted from ``playlist_copy.py``; the original names
(``_per_source_semaphore_for`` / ``_reset_per_source_semaphores_for_tests``)
are preserved so the existing test fixture in
``test_playlist_copy.py`` keeps working through the
``playlist_copy`` re-export.
"""

from __future__ import annotations

import threading
from typing import Dict, Iterable, Optional

from services.tunables import playlist_mgmt_batch_per_source_workers


# Module-level registry of per-source-server semaphores. Lazy-created on
# first use, keyed by source server_id. The semaphore depth comes from
# ``playlist_mgmt_batch_per_source_workers`` at the time the semaphore
# is built; live retuning takes effect on next process boot (cheap to
# clear by restart - production end users don't change this).
#
# Used by ``copy_playlist_batch``'s ``_run_one``: single-copy REST paths
# (``list_user_playlists`` / ``get_playlist_detail`` / direct
# ``copy_playlist`` invocations from the legacy single-copy endpoint)
# bypass the cap because they're one-shot operator actions, not
# operator-submitted fan-outs that risk thrashing one source.

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
