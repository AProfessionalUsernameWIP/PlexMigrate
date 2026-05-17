// Item 5 (admin-management plan, 2026-05-15): global tooltip toggle.
//
// The Settings panel writes ``tooltips_enabled`` (root_admin only). The
// rest of the UI reads it from this context so we don't fetch
// /api/settings on every render of an <InfoTip />.
//
// The provider is mounted high in App.tsx; it loads the current value
// from /api/settings once and exposes a setter so the SettingsPanel
// can flip the value optimistically. A POST back to /api/settings
// persists the change.

import { createContext, useEffect, useState } from 'react';
import type { ReactNode } from 'react';
import { api } from '../api';

interface TooltipContextValue {
  enabled: boolean;
  // Optimistic local set; the SettingsPanel calls this immediately
  // on toggle so the icons hide/show without waiting for the
  // /api/settings round-trip.
  setEnabled: (next: boolean) => void;
}

export const TooltipContext = createContext<TooltipContextValue>({
  enabled: true,
  setEnabled: () => { /* default no-op until provider mounts */ },
});

export function TooltipProvider({ children }: { children: ReactNode }) {
  const [enabled, setEnabled] = useState(true);

  // Load the current setting once on mount. Failures default to
  // ``true`` (tooltips on) - we'd rather show too much help than
  // accidentally hide it after a transient API hiccup.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const s = await api.getSettings();
        if (!cancelled && typeof s.tooltips_enabled === 'boolean') {
          setEnabled(s.tooltips_enabled);
        }
      } catch {
        // Keep the optimistic default of true.
      }
    })();
    return () => { cancelled = true; };
  }, []);

  return (
    <TooltipContext.Provider value={{ enabled, setEnabled }}>
      {children}
    </TooltipContext.Provider>
  );
}
