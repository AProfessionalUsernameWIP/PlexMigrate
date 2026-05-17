"""
Timing & ETR engine for PlexMigrate.

Discover-don't-predict model. There is no pre-run estimate: a
snapshot's real work is "the items that have actually been watched /
rated / placed in a playlist", and Plex only reveals those counts
when the engine queries them. Any attempt to predict total work from
library size up front is wrong by orders of magnitude, so we don't.

Instead:

  * Each :class:`ETRTracker` starts with a total of zero.
  * As the engine enumerates real work (``snapshot_ratings`` finding
    751 rated items, etc.) it grows the tracker's total via
    ``grow_total``.
  * Per-item completion ticks the tracker via ``tick``.
  * ``etr_seconds`` projects ``remaining / observed_rate`` from a
    rolling weighted window of real throughput samples - never from a
    stored guess.

Until at least three samples have landed, ``etr_seconds`` returns
``None`` and the UI shows "Calculating...". That window is short
(seconds) and an honest "still measuring" beats a confidently-wrong
number.

This module is pure computation over counts and timestamps - no Plex
API calls, no HTTP, no disk.
"""

from __future__ import annotations

import time
from collections import deque
from typing import Deque, Optional, Tuple


# Rolling window size for the throughput tracker. Large enough to
# absorb single-item stutters, small enough to react to a phase
# change inside one run.
_ROLLING_WINDOW: int = 50

# Exponential-decay display smoothing applied to the rolling ETR so
# the UI doesn't jitter on every tick. ``new = OLD*old + NEW*raw``.
# Heavily weighted toward the existing value: the underlying estimate
# can be jumpy, the displayed one should not be.
_SMOOTHING_OLD: float = 0.9
_SMOOTHING_NEW: float = 0.1

# Hard per-update clamp on the displayed value. Even if the raw
# estimate teleports, the smoothed display can't move more than this
# fraction away from its previous reading in a single call. The first
# reading (no previous) is unclamped so the display seeds directly.
_DISPLAY_CLAMP_FRACTION: float = 0.25

# Floor below which we render "Almost done" instead of a numeric
# countdown. Prevents flickering single-digit seconds.
ETR_FLOOR_SECONDS: float = 5.0


class ETRTracker:
    """
    Rolling weighted-window throughput tracker.

    One instance per "scope": the total-run dashboard holds one,
    per-batch Process List rows hold one each. They never share
    samples - each batch's homogeneous workload keeps its own rate
    stable, and the top-level tracker measures honest aggregate
    throughput (parallelism already baked in).

    Lifecycle:
      * Constructed with ``total_items=0``.
      * ``grow_total(n)`` is called as the engine discovers real work.
      * ``tick(n)`` records each chunk of completed work.
      * ``etr_seconds`` reads the smoothed projection, or ``None``
        when fewer than three samples have landed.
    """

    def __init__(self, total_items: int = 0) -> None:
        self.total: int = max(0, int(total_items))
        self.completed: int = 0
        # Each sample is ``(timestamp_seconds, items_processed_in_tick)``.
        self.window: Deque[Tuple[float, int]] = deque(maxlen=_ROLLING_WINDOW)
        # Last smoothed ETR for the exponential-decay display
        # smoothing + per-update clamp. ``None`` means "no displayed
        # value yet" - the first valid reading seeds it directly.
        self._smoothed_etr: Optional[float] = None

    def grow_total(self, n: int) -> None:
        """
        Increase the tracker's total by ``n``. Called as the engine
        enumerates real work - the discover-don't-predict entry point.
        Negative / zero ``n`` is a no-op.
        """
        if n > 0:
            self.total += int(n)

    def tick(self, items_just_processed: int, timestamp: Optional[float] = None) -> None:
        """
        Record one tick of progress. Multiple items can land per tick
        (e.g. a batch of 5 scrobbles fired together); the per-tick
        count weights the throughput calculation correctly.
        """
        if items_just_processed <= 0:
            return
        ts = float(timestamp if timestamp is not None else time.time())
        self.completed += int(items_just_processed)
        self.window.append((ts, int(items_just_processed)))

    @property
    def raw_etr_seconds(self) -> Optional[float]:
        """
        Unsmoothed projection. Returns ``None`` when the window has
        fewer than 3 samples - the caller treats that as "still
        measuring" and the UI shows "Calculating...".

        Also returns ``None`` when ``self.total <= 0`` (the engine
        has not yet enumerated any work to do) or when
        ``self.completed > self.total`` (a momentary state when the
        engine ticks faster than it grows the total). Both cases used
        to return ``0`` here, which the smoother then published as
        "Almost done" on the dashboard - a misleading signal that
        produced a real end user-facing bug: a 6-minute run flashing
        "Almost done" within seconds. The right answer when we have
        insufficient evidence to project is "Calculating...", not
        "you are about to finish."
        """
        if len(self.window) < 3:
            return None
        if self.total <= 0:
            # Engine has not yet enumerated any work. Whatever the
            # rate window is showing, we have no denominator to
            # project from.
            return None
        if self.completed > self.total:
            # Momentary state where the engine ticked faster than it
            # grew the total. The reverse will fire in a tick or
            # two; in the meantime do not claim "done."
            return None
        # Weighted average of (count / inter-sample-delta). Each
        # subsequent sample is weighted ``i + 1`` so the most recent
        # samples dominate. The first sample is skipped because it has
        # no predecessor to compute a delta against.
        timestamps = [ts for ts, _ in self.window]
        total_weight = 0.0
        weighted_rate = 0.0
        for i, (ts, count) in enumerate(self.window):
            if i == 0:
                continue
            elapsed = ts - timestamps[i - 1]
            if elapsed > 0:
                weight = float(i + 1)
                weighted_rate += (count / elapsed) * weight
                total_weight += weight
        if total_weight <= 0:
            return None
        rate = weighted_rate / total_weight
        if rate <= 0:
            return None
        remaining = self.total - self.completed
        # remaining is now guaranteed >= 0 by the guards above.
        return remaining / rate

    @property
    def etr_seconds(self) -> Optional[float]:
        """
        Display value: raw ETR run through the exponential-decay
        smoother and then a hard per-update clamp. The first valid
        reading seeds the smoother directly so the initial display
        doesn't lag at zero; every reading after that is both smoothed
        and clamped so the displayed number can't teleport between
        wildly different magnitudes.
        """
        raw = self.raw_etr_seconds
        if raw is None:
            return None
        if self._smoothed_etr is None:
            # First valid reading: seed directly, no smoothing / clamp.
            self._smoothed_etr = raw
            return self._smoothed_etr
        prev = self._smoothed_etr
        smoothed = _SMOOTHING_OLD * prev + _SMOOTHING_NEW * raw
        # Hard clamp: the displayed value can't move more than
        # _DISPLAY_CLAMP_FRACTION away from the previous reading in one
        # update, regardless of how far the raw estimate jumped.
        lo = prev * (1.0 - _DISPLAY_CLAMP_FRACTION)
        hi = prev * (1.0 + _DISPLAY_CLAMP_FRACTION)
        self._smoothed_etr = max(lo, min(hi, smoothed))
        return self._smoothed_etr

    def update_total(self, total_items: int) -> None:
        """
        Set the total to an absolute value, floored at ``completed``
        so the tracker never reports negative remaining work. Kept for
        callers that learn an authoritative total in one shot;
        ``grow_total`` is the incremental discover-as-you-go path.
        """
        self.total = max(self.completed, int(total_items))


def format_etr_for_display(seconds: Optional[float]) -> str:
    """
    Turn an ETR value into the end user-facing string. ``None``
    becomes ``"Calculating..."`` (still measuring); below-floor values
    become ``"Almost done"``; everything else becomes ``"HH:MM:SS"``
    (or ``"MM:SS"`` when under an hour).
    """
    if seconds is None:
        return "Calculating..."
    if seconds < 0:
        return "-"
    if seconds < ETR_FLOOR_SECONDS:
        return "Almost done"
    sec = int(seconds)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"
