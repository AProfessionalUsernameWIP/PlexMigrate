// Visibility-aware re-render tick.
//
// Replaces the hand-rolled "useState(0) counter + setInterval that
// increments it once a second to force a re-render" idiom that was
// copy-pasted across the clock / elapsed-timer components. Wrapping
// pausableInterval means the tick also pauses while the tab is hidden:
// a clock nobody is looking at does not need to re-render every second.
//
// Call it in a component body; the returned counter changes on every
// tick, which is what drives the re-render. Pass enabled=false to stop
// ticking (e.g. once an elapsed timer's job has finished).

import { useEffect, useState } from 'react';
import { pausableInterval } from '../utils/pausableInterval';

export function useNowTick(intervalMs = 1000, enabled = true): number {
  const [tick, setTick] = useState(0);
  useEffect(() => {
    if (!enabled) return;
    return pausableInterval(() => setTick((x) => x + 1), intervalMs);
  }, [intervalMs, enabled]);
  return tick;
}
