// Item 1 (admin-management plan, 2026-05-15): app-wide elevation state.
//
// Tracks whether the current session is currently elevated (a recent
// password re-confirm via /api/auth/elevate) and the unix timestamp
// that elevation expires at. Components that gate destructive
// root-level actions consume this via ``useElevation()`` and call
// ``requireElevation(reason)`` to either proceed (already elevated)
// or open the ElevationModal to collect the password.
//
// The provider keeps a single in-memory copy of the expiry and is
// the only component that opens / closes the modal. Action buttons
// just await ``requireElevation()`` and run their work when the
// promise resolves.

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react';
import type { ReactNode } from 'react';
import { api } from '../api';
import { ElevationModal } from '../components/ElevationModal';

interface ElevationContextValue {
  // Unix-time (seconds) the elevation expires at, or null if not
  // currently elevated. Components can show a countdown by reading
  // ``expiresAt - Math.floor(Date.now() / 1000)``.
  expiresAt: number | null;
  // Convenience: a tick-count that increments every second while
  // elevation is active. Topbar consumers use this to repaint the
  // countdown without re-running the effect chain.
  tick: number;
  // Show the modal collecting the password. Resolves true once
  // elevated, false if the user cancels. Re-promote against a still-
  // valid elevation is a no-op resolved-true.
  requireElevation: (reason: string) => Promise<boolean>;
  // Manually drop elevation (sudo -k equivalent). Idempotent.
  dropElevation: () => Promise<void>;
}

const ElevationContext = createContext<ElevationContextValue>({
  expiresAt: null,
  tick: 0,
  requireElevation: async () => false,
  dropElevation: async () => { /* no-op */ },
});

export function useElevation(): ElevationContextValue {
  return useContext(ElevationContext);
}

// Internal pending-resolver shape so the provider can hand the modal
// a closure that resolves whatever promise the caller is awaiting.
interface PendingRequest {
  reason: string;
  resolve: (ok: boolean) => void;
}

export function ElevationProvider({ children }: { children: ReactNode }) {
  const [expiresAt, setExpiresAt] = useState<number | null>(null);
  const [pending, setPending] = useState<PendingRequest | null>(null);
  const [tick, setTick] = useState(0);

  // Boot probe: ask the backend whether the current session is
  // already elevated (carries through /api/auth/refresh). Failure is
  // silent - assume not elevated, which is the safe default.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const status = await api.authElevationStatus();
        if (!cancelled && status.elevated && typeof status.elevated_until === 'number') {
          setExpiresAt(status.elevated_until);
        }
      } catch {
        /* viewer / end user / manager / admin all 403 here; ignore */
      }
    })();
    return () => { cancelled = true; };
  }, []);

  // Per-second tick while elevated. We don't want a tick when the
  // session is cold because most users never elevate.
  useEffect(() => {
    if (expiresAt === null) return;
    const id = window.setInterval(() => {
      const now = Math.floor(Date.now() / 1000);
      if (now >= expiresAt) {
        setExpiresAt(null);
      } else {
        setTick((t) => t + 1);
      }
    }, 1000);
    return () => window.clearInterval(id);
  }, [expiresAt]);

  // Stable resolver pointer so the modal's callbacks don't capture a
  // stale closure if the provider re-renders mid-await.
  const pendingRef = useRef<PendingRequest | null>(null);
  pendingRef.current = pending;

  const requireElevation = useCallback(
    (reason: string): Promise<boolean> => {
      const now = Math.floor(Date.now() / 1000);
      if (expiresAt !== null && expiresAt > now) {
        return Promise.resolve(true);
      }
      return new Promise<boolean>((resolve) => {
        setPending({ reason, resolve });
      });
    },
    [expiresAt],
  );

  const dropElevation = useCallback(async () => {
    try {
      await api.authElevationClear();
    } catch {
      /* best-effort; we drop the local state regardless */
    }
    setExpiresAt(null);
  }, []);

  const onModalElevated = useCallback((newExpiry: number) => {
    setExpiresAt(newExpiry);
    const p = pendingRef.current;
    if (p) {
      p.resolve(true);
      setPending(null);
    }
  }, []);

  const onModalCancel = useCallback(() => {
    const p = pendingRef.current;
    if (p) {
      p.resolve(false);
      setPending(null);
    }
  }, []);

  const value = useMemo<ElevationContextValue>(
    () => ({ expiresAt, tick, requireElevation, dropElevation }),
    [expiresAt, tick, requireElevation, dropElevation],
  );

  return (
    <ElevationContext.Provider value={value}>
      {children}
      <ElevationModal
        open={pending !== null}
        actionDescription={pending?.reason ?? ''}
        onCancel={onModalCancel}
        onElevated={onModalElevated}
      />
    </ElevationContext.Provider>
  );
}
