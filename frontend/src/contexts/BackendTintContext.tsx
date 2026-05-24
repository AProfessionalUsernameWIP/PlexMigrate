// Backend-context tinting for the Chameleon theme. When a panel or
// component is actively working with a specific media backend (Plex
// / Jellyfin / Emby) it sets the persistent tint; when the user
// hovers an individual backend-specific card (a server row, a
// playlist row, etc.) it sets the transient hover tint. Hover takes
// precedence over persistent while active so the UI follows the
// pointer; on hover-end the persistent tint takes back over.
// Multi-backend views (or sets that include items from multiple
// backends) use 'mixed' for a tri-color animation. Backend-neutral
// panels (settings, account, dashboard) call setTint(null) on mount
// so the persistent tint clears back to default chameleon pearl.
//
// Implementation: the effective tint (hover ?? persistent) sets
// data-backend-tint on <html>. CSS in styles.css uses
// [data-theme="chameleon"][data-backend-tint="..."] selectors so the
// tints only fire when chameleon is the active theme. Other themes
// (dark, tron, paper, hearth, etc.) are unaffected even when
// data-backend-tint is set, so we don't need to clear the attribute
// when the user switches themes.
//
// Perf: tint + hoverTint are kept in refs, not state. Nothing renders
// from them - they exist only to drive two data-* attributes on
// <html> - so setTint / setHoverTint write those attributes
// imperatively. A per-card hover therefore costs one attribute write
// instead of a provider re-render that fans out to every consumer.

import { createContext, useCallback, useContext, useEffect, useMemo, useRef } from 'react';
import type { ReactNode } from 'react';

export type BackendTint = 'plex' | 'jellyfin' | 'emby' | 'mixed' | null;

interface BackendTintContextValue {
  setTint: (next: BackendTint) => void;
  setHoverTint: (next: BackendTint) => void;
}

const Ctx = createContext<BackendTintContextValue>({
  setTint: () => {},
  setHoverTint: () => {},
});

export function BackendTintProvider({ children }: { children: ReactNode }) {
  // tint = persistent (a committed BackendTabStrip selection).
  // hoverTint = transient (a per-card hover). Both live in refs: the
  // only thing that depends on either is the pair of data-* attributes
  // below, so a change writes the DOM directly with no re-render.
  const tintRef = useRef<BackendTint>(null);
  const hoverTintRef = useRef<BackendTint>(null);

  // Push both attributes onto <html>:
  //
  //   data-backend-tint            = effective tint (hover ?? persistent).
  //                                  Drives the moving accent + pulse on
  //                                  the element you're touching.
  //   data-backend-tint-persistent = persistent tint only. Drives the
  //                                  FULL identity shift: panel tone,
  //                                  borders, clickable wood color.
  //
  // Hover therefore can't trigger the full identity shift - only a
  // committed backend selection (tab click) does.
  const applyAttrs = useCallback(() => {
    const el = document.documentElement;
    const effective: BackendTint = hoverTintRef.current ?? tintRef.current;
    if (effective === null) {
      el.removeAttribute('data-backend-tint');
    } else {
      el.setAttribute('data-backend-tint', effective);
    }
    if (tintRef.current === null) {
      el.removeAttribute('data-backend-tint-persistent');
    } else {
      el.setAttribute('data-backend-tint-persistent', tintRef.current);
    }
  }, []);

  const setTint = useCallback((next: BackendTint) => {
    if (tintRef.current === next) return;
    tintRef.current = next;
    applyAttrs();
  }, [applyAttrs]);

  const setHoverTint = useCallback((next: BackendTint) => {
    if (hoverTintRef.current === next) return;
    hoverTintRef.current = next;
    applyAttrs();
  }, [applyAttrs]);

  // On mount, sync the attributes to the (null) ref state - this
  // clears any tint left on <html> by a prior provider instance
  // (e.g. across a logout / login cycle).
  useEffect(() => {
    applyAttrs();
  }, [applyAttrs]);

  const value = useMemo(() => ({ setTint, setHoverTint }), [setTint, setHoverTint]);
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useBackendTintContext() {
  return useContext(Ctx);
}

/**
 * Component-level helper. Sets the PERSISTENT tint to `backend`
 * while this component is mounted, and clears it on unmount.
 * Pass `null` to actively clear the tint while mounted (useful for
 * settings / account / dashboard panels that have no backend
 * context). Pass `undefined` to leave the current tint alone.
 *
 * The hook is idempotent across re-renders - it only writes when
 * the backend value actually changes, so it's safe to call with a
 * derived value that re-computes each render.
 */
export function useBackendTint(backend: BackendTint | undefined) {
  const { setTint } = useBackendTintContext();
  const last = useRef<BackendTint | undefined>(undefined);

  useEffect(() => {
    if (backend === undefined) return;
    if (last.current === backend) return;
    last.current = backend;
    setTint(backend);
  }, [backend, setTint]);

  // Clear on unmount so navigating away from a backend-aware panel
  // returns to default chameleon (or whatever theme is active).
  useEffect(() => {
    return () => {
      if (last.current !== undefined && last.current !== null) {
        setTint(null);
      }
    };
  }, [setTint]);
}

/**
 * Per-card hover helper. Returns the two event handlers a backend-
 * specific card (server row, playlist row, etc.) should spread into
 * its own onMouseEnter / onMouseLeave / onFocus / onBlur:
 *
 *   const hover = useBackendHover();
 *   <button {...hover('plex')}> ... </button>
 *
 * The returned object exposes a single function `bind(backend)`
 * that produces the handler set bound to that backend. Hover sets
 * the transient hover tint; leave clears it; focus + blur mirror
 * for keyboard navigation.
 */
export function useBackendHover() {
  const { setHoverTint } = useBackendTintContext();
  const bind = useCallback((backend: BackendTint) => ({
    onMouseEnter: () => setHoverTint(backend),
    onMouseLeave: () => setHoverTint(null),
    onFocus: () => setHoverTint(backend),
    onBlur: () => setHoverTint(null),
  }), [setHoverTint]);
  return { bind };
}
