// Per-user clock-display preference.
//
// The topbar can show the time in one of three modes:
//
//   * ``server`` - server-side wall-clock, default. Tracks the
//     backend's timezone (set via TZ env var in docker-compose).
//     This is the existing behaviour pre-2026-05-12.
//   * ``local``  - the operator's device clock, in the browser's
//     local timezone. Useful when the operator is on the road and
//     the server is in a different zone.
//   * ``custom`` - a user-set offset from server time. The operator
//     types a HH:MM they want "right now" to show as; the panel
//     records the offset and applies it on every render.
//
// The preference + offset are persisted to ``localStorage`` so they
// survive reloads. The Account Settings panel offers the controls.
// Topbar reads the resolved time via ``useClockDisplay()``.

import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react';


export type ClockMode = 'server' | 'local' | 'custom';

const STORAGE_KEY = 'plexmigrate.clock';

interface StoredPref {
  mode: ClockMode;
  // For custom mode: ms offset to ADD to the server's now when
  // computing the displayed time. Server-mode and local-mode ignore
  // this field.
  customOffsetMs: number;
}

function loadPref(): StoredPref {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return { mode: 'server', customOffsetMs: 0 };
    const parsed = JSON.parse(raw);
    if (parsed && typeof parsed === 'object') {
      const mode: ClockMode = parsed.mode === 'local' || parsed.mode === 'custom'
        ? parsed.mode : 'server';
      const offset = typeof parsed.customOffsetMs === 'number' ? parsed.customOffsetMs : 0;
      return { mode, customOffsetMs: offset };
    }
  } catch {
    /* fall through to default */
  }
  return { mode: 'server', customOffsetMs: 0 };
}

function savePref(p: StoredPref): void {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(p));
  } catch {
    /* localStorage full or disabled - preference resets to default
       next reload, no other consequence */
  }
}


interface ClockContextValue {
  mode: ClockMode;
  customOffsetMs: number;
  setMode: (m: ClockMode) => void;
  setCustomOffsetMs: (ms: number) => void;
  /**
   * Resolve a displayed Date given the server's current wall-clock
   * (passed in from App.tsx's existing skew-corrected clock). For
   * ``server`` mode this returns the server now unchanged; for
   * ``local`` it returns the browser's Date.now(); for ``custom`` it
   * returns server now + customOffsetMs.
   */
  resolveDisplay: (serverNowMs: number) => Date;
}

const ClockContext = createContext<ClockContextValue | null>(null);


export function ClockProvider({ children }: { children: ReactNode }) {
  const [pref, setPref] = useState<StoredPref>(loadPref);

  const setMode = useCallback((m: ClockMode) => {
    setPref((p) => {
      const next = { ...p, mode: m };
      savePref(next);
      return next;
    });
  }, []);

  const setCustomOffsetMs = useCallback((ms: number) => {
    setPref((p) => {
      const next = { ...p, customOffsetMs: ms };
      savePref(next);
      return next;
    });
  }, []);

  const resolveDisplay = useCallback((serverNowMs: number): Date => {
    if (pref.mode === 'local') return new Date(Date.now());
    if (pref.mode === 'custom') return new Date(serverNowMs + pref.customOffsetMs);
    return new Date(serverNowMs);
  }, [pref.mode, pref.customOffsetMs]);

  const value = useMemo<ClockContextValue>(() => ({
    mode: pref.mode,
    customOffsetMs: pref.customOffsetMs,
    setMode,
    setCustomOffsetMs,
    resolveDisplay,
  }), [pref.mode, pref.customOffsetMs, setMode, setCustomOffsetMs, resolveDisplay]);

  // Cross-tab sync - if the operator changes the preference in
  // another tab, pull the new value here too. Storage events fire
  // only in OTHER tabs, not the one that wrote them, so this is
  // safe to listen for without an echo loop.
  useEffect(() => {
    const onStorage = (e: StorageEvent) => {
      if (e.key === STORAGE_KEY) setPref(loadPref());
    };
    window.addEventListener('storage', onStorage);
    return () => window.removeEventListener('storage', onStorage);
  }, []);

  return <ClockContext.Provider value={value}>{children}</ClockContext.Provider>;
}


export function useClockDisplay(): ClockContextValue {
  const ctx = useContext(ClockContext);
  if (ctx === null) {
    throw new Error('useClockDisplay must be used inside <ClockProvider>.');
  }
  return ctx;
}
