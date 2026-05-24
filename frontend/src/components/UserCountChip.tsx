// Shared user-count chip with delayed hover-to-reveal user list.
//
// End user-facing contract:
// * Renders the count number inline. Looks like an interactive
//   element when ``users`` is non-empty (underline on hover); plain
//   text when no list is available.
// * On deliberate hover (after ``user_count_tooltip_delay_ms``,
//   default 600 ms), reveals a popover listing up to
//   ``user_count_tooltip_max_items`` (default 20) usernames, sorted
//   alphabetically, followed by "… +N more" when the list exceeds
//   the cap.
// * Click pins the popover open so the end user can scroll / copy
//   names without it dismissing. Click outside (or the close button)
//   dismisses.
//
// Tunable resolution:
// * On first mount of any chip, fetch /api/settings once and cache
//   ``tunables.user_count_tooltip_delay_ms`` and
//   ``tunables.user_count_tooltip_max_items`` at module scope.
// * Subsequent mounts read the cache - no per-chip request.
// * Backend clamps both values to safe ranges, so the chip can
//   trust whatever it receives.
// * Both values can be overridden per-instance via props; useful for
//   tests and for surfaces that want to opt out of the end user-set
//   defaults.

import { useEffect, useRef, useState } from 'react';
import { api } from '../api';


const DEFAULT_DELAY_MS = 600;
const DEFAULT_MAX_ITEMS = 20;


// Module-level cache for the tunables. Single fetch on the first
// chip mount; later chips reuse the cached value. ``null`` means
// "not fetched yet"; the chip falls back to DEFAULT_* until the
// first response lands.
let _prefsCache: { delayMs: number; maxItems: number } | null = null;
let _prefsLoading: Promise<void> | null = null;


async function _loadPrefs(): Promise<void> {
  if (_prefsLoading) return _prefsLoading;
  _prefsLoading = (async () => {
    try {
      const settings = (await api.getSettings()) as unknown as Record<string, unknown>;
      const tunables = (settings.tunables ?? {}) as Record<string, unknown>;
      const rawDelay = tunables.user_count_tooltip_delay_ms;
      const rawMax = tunables.user_count_tooltip_max_items;
      const delayMs = typeof rawDelay === 'number' && Number.isFinite(rawDelay)
        ? Math.min(3000, Math.max(100, rawDelay))
        : DEFAULT_DELAY_MS;
      const maxItems = typeof rawMax === 'number' && Number.isFinite(rawMax)
        ? Math.min(200, Math.max(1, rawMax))
        : DEFAULT_MAX_ITEMS;
      _prefsCache = { delayMs, maxItems };
    } catch {
      // Settings call failed (auth, network) - fall back to defaults
      // for the rest of the session. The chip is purely diagnostic;
      // failing to read tunables should never break the surface.
      _prefsCache = { delayMs: DEFAULT_DELAY_MS, maxItems: DEFAULT_MAX_ITEMS };
    }
  })();
  return _prefsLoading;
}


export interface UserCountChipProps {
  /** The numeric value to display. Renders even when ``users`` is empty. */
  count: number;
  /**
   * The names to surface in the hover popover. When absent / empty,
   * the chip renders as a plain non-interactive number so callers
   * don't have to gate it. The order is preserved; the chip
   * sorts internally for the visible list.
   */
  users?: ReadonlyArray<string>;
  /** Override the tunable-driven delay (ms). */
  delayMs?: number;
  /** Override the tunable-driven visible-list cap. */
  maxItems?: number;
  /** Optional aria-label override for the chip element. */
  ariaLabel?: string;
}


export function UserCountChip({
  count,
  users,
  delayMs,
  maxItems,
  ariaLabel,
}: UserCountChipProps) {
  const hasList = Array.isArray(users) && users.length > 0;
  const [prefs, setPrefs] = useState<{ delayMs: number; maxItems: number }>(() => (
    _prefsCache ?? { delayMs: DEFAULT_DELAY_MS, maxItems: DEFAULT_MAX_ITEMS }
  ));
  const [open, setOpen] = useState<boolean>(false);
  const [pinned, setPinned] = useState<boolean>(false);
  const hoverTimer = useRef<number | null>(null);
  const wrapperRef = useRef<HTMLSpanElement | null>(null);

  // Resolve prefs on first mount when we don't already have them.
  useEffect(() => {
    if (_prefsCache) return;
    let mounted = true;
    void _loadPrefs().then(() => {
      if (mounted && _prefsCache) setPrefs(_prefsCache);
    });
    return () => {
      mounted = false;
    };
  }, []);

  // Effective values: per-instance override beats tunable-driven cache.
  const effDelay = delayMs ?? prefs.delayMs;
  const effMax = maxItems ?? prefs.maxItems;

  // Outside-click handler dismisses a pinned popover. Effective only
  // when pinned + open; the listener is torn down between pin states
  // so we don't accidentally close a hover-only popover.
  useEffect(() => {
    if (!pinned || !open) return;
    const onDocClick = (ev: MouseEvent) => {
      const target = ev.target as Node | null;
      if (wrapperRef.current && target && wrapperRef.current.contains(target)) {
        return;
      }
      setPinned(false);
      setOpen(false);
    };
    document.addEventListener('mousedown', onDocClick);
    return () => document.removeEventListener('mousedown', onDocClick);
  }, [pinned, open]);

  const cancelHover = () => {
    if (hoverTimer.current !== null) {
      window.clearTimeout(hoverTimer.current);
      hoverTimer.current = null;
    }
  };

  const onEnter = () => {
    if (!hasList) return;
    if (pinned) return;  // pinned popover ignores hover events
    cancelHover();
    hoverTimer.current = window.setTimeout(() => {
      setOpen(true);
    }, effDelay);
  };

  const onLeave = () => {
    if (pinned) return;
    cancelHover();
    setOpen(false);
  };

  const onClick = () => {
    if (!hasList) return;
    cancelHover();
    if (pinned) {
      setPinned(false);
      setOpen(false);
    } else {
      setPinned(true);
      setOpen(true);
    }
  };

  // Render-time computed: sorted alphabetical list, with the cap +
  // overflow line applied. Done in render rather than memoised
  // because the chip is small and the list typically <100 names.
  const sorted = hasList ? Array.from(users!).slice().sort((a, b) => a.localeCompare(b)) : [];
  const visible = sorted.slice(0, effMax);
  const overflow = sorted.length - visible.length;

  return (
    <span
      ref={wrapperRef}
      style={{ position: 'relative', display: 'inline-block' }}
      onMouseEnter={onEnter}
      onMouseLeave={onLeave}
    >
      <span
        role={hasList ? 'button' : undefined}
        tabIndex={hasList ? 0 : undefined}
        aria-label={ariaLabel}
        aria-expanded={hasList ? open : undefined}
        onClick={onClick}
        onKeyDown={(ev) => {
          if (!hasList) return;
          if (ev.key === 'Enter' || ev.key === ' ') {
            ev.preventDefault();
            onClick();
          } else if (ev.key === 'Escape' && pinned) {
            setPinned(false);
            setOpen(false);
          }
        }}
        style={{
          cursor: hasList ? 'pointer' : 'default',
          textDecoration: hasList ? 'underline dotted' : undefined,
          textUnderlineOffset: 2,
        }}
        title={hasList ? undefined : 'No user list available'}
      >
        {count}
      </span>
      {open && hasList && (
        <span
          role="dialog"
          aria-label={ariaLabel ? `${ariaLabel} list` : 'User list'}
          style={{
            position: 'absolute',
            top: '100%',
            left: 0,
            marginTop: 4,
            zIndex: 50,
            background: 'var(--bg-elev, #1f1f1f)',
            border: '1px solid var(--border, #444)',
            borderRadius: 4,
            padding: '6px 10px',
            minWidth: 160,
            maxWidth: 320,
            boxShadow: '0 2px 8px rgba(0,0,0,0.4)',
            fontSize: 12,
            color: 'var(--text, #ddd)',
            whiteSpace: 'normal',
          }}
        >
          <div
            style={{
              display: 'flex',
              justifyContent: 'space-between',
              alignItems: 'center',
              gap: 8,
              marginBottom: 4,
            }}
          >
            <span style={{ fontWeight: 600 }}>
              {sorted.length} user{sorted.length === 1 ? '' : 's'}
              {pinned && (
                <span style={{ marginLeft: 6, fontSize: 10, color: 'var(--text-dim)' }}>
                  (pinned)
                </span>
              )}
            </span>
            {pinned && (
              <button
                type="button"
                onClick={(ev) => {
                  ev.stopPropagation();
                  setPinned(false);
                  setOpen(false);
                }}
                style={{ fontSize: 10, padding: '1px 6px' }}
                title="Dismiss (Esc)"
              >
                close
              </button>
            )}
          </div>
          <ul
            style={{
              listStyle: 'none',
              padding: 0,
              margin: 0,
              maxHeight: 280,
              overflowY: 'auto',
            }}
          >
            {visible.map((name) => (
              <li key={name} style={{ padding: '1px 0' }}>{name}</li>
            ))}
          </ul>
          {overflow > 0 && (
            <div style={{ marginTop: 4, color: 'var(--text-dim)' }}>
              … +{overflow} more
            </div>
          )}
          {!pinned && (
            <div style={{ marginTop: 6, fontSize: 10, color: 'var(--text-dim)' }}>
              Click to pin
            </div>
          )}
        </span>
      )}
    </span>
  );
}
