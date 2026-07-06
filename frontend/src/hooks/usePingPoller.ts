// Background ping poller for the registered-server list. Two panels
// (ServersPanel, JobFormPanel) need identical "kick a ping on every
// server, refresh every N ms, re-arm when the list changes" semantics
// to keep their status dots fresh. The two inline useEffect copies
// were near-identical (41-line and 29-line) and drifted in error-
// handling comments; this hook consolidates them so the timing,
// re-arm, and cleanup rules cannot diverge again.
//
// Returns the per-server-id map of the most-recent PingResult. The
// hook owns its own state + timer ref internally; callers just render
// from the returned map.

import { useEffect, useRef, useState } from 'react';
import { api } from '../api';
import type { PingResult } from '../api';
import { pausableInterval } from '../utils/pausableInterval';


export function usePingPoller<S extends { id: string }>(
  servers: S[],
  intervalMs: number,
): Record<string, PingResult> {
  const [pings, setPings] = useState<Record<string, PingResult>>({});
  const pollTimerRef = useRef<(() => void) | null>(null);

  useEffect(() => {
    if (servers.length === 0) {
      if (pollTimerRef.current !== null) {
        pollTimerRef.current();
        pollTimerRef.current = null;
      }
      return;
    }

    const pingAll = async () => {
      // Ping every server in parallel; each call is independent and
      // the registry row update is the same regardless of order.
      const tasks = servers.map(async (s) => {
        try {
          const result = await api.pingServer(s.id);
          setPings((prev) => ({ ...prev, [s.id]: result }));
        } catch {
          // Network or server-side error - fall through; the next
          // tick will retry. The cached status row still shows
          // whatever the last successful ping recorded.
        }
      });
      await Promise.allSettled(tasks);
    };

    // Fire one ping right now so the dots aren't grey on first paint
    // for the full interval until the first tick fires.
    pingAll();
    if (pollTimerRef.current !== null) pollTimerRef.current();
    pollTimerRef.current = pausableInterval(pingAll, intervalMs);

    return () => {
      if (pollTimerRef.current !== null) {
        pollTimerRef.current();
        pollTimerRef.current = null;
      }
    };
  }, [servers, intervalMs]);

  return pings;
}
