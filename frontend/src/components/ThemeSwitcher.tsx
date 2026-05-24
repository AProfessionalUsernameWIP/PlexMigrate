// Theme switcher widget for the topbar. Four themes share one set of
// CSS custom properties via [data-theme="…"] selectors in styles.css:
//   dark (default), light, true-dark, warm-dark.
//
// The initial theme is applied before React mounts by the inline
// script in index.html so users don't see a flash of the wrong theme
// on first paint. This component reads what was already applied,
// renders the dropdown, and persists changes back to localStorage.

import { useEffect, useRef, useState } from 'react';

const STORAGE_KEY = 'app.theme';

export const THEMES = [
  { id: 'dark',      label: 'Dark',      meta: 'Default',            swatchClass: 'theme-swatch-dark' },
  { id: 'true-dark', label: 'True dark', meta: 'OLED / pure black',  swatchClass: 'theme-swatch-true-dark' },
  { id: 'tron',      label: 'Tron',      meta: 'Neon grid',          swatchClass: 'theme-swatch-tron' },
  { id: 'hearth',    label: 'Hearth',    meta: 'Cobblestone + fire', swatchClass: 'theme-swatch-hearth' },
  { id: 'chameleon', label: 'Chameleon', meta: 'Adapts to backend',  swatchClass: 'theme-swatch-chameleon' },
  { id: 'warm-dark', label: 'Warm dark', meta: 'All embers',         swatchClass: 'theme-swatch-warm-dark' },
  { id: 'paper',     label: 'Paper',     meta: 'Warm cream',         swatchClass: 'theme-swatch-paper' },
] as const;

export type ThemeId = typeof THEMES[number]['id'];

function readCurrentTheme(): ThemeId {
  const attr = document.documentElement.getAttribute('data-theme');
  if (attr && THEMES.some((t) => t.id === attr)) return attr as ThemeId;
  try {
    const saved = window.localStorage.getItem(STORAGE_KEY);
    if (saved && THEMES.some((t) => t.id === saved)) return saved as ThemeId;
  } catch { /* localStorage blocked */ }
  return 'dark';
}

function applyTheme(id: ThemeId) {
  document.documentElement.setAttribute('data-theme', id);
  try { window.localStorage.setItem(STORAGE_KEY, id); } catch { /* non-fatal */ }
}

export function ThemeSwitcher() {
  const [current, setCurrent] = useState<ThemeId>(() => readCurrentTheme());
  const [open, setOpen] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onDocClick = (e: MouseEvent) => {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') setOpen(false); };
    document.addEventListener('click', onDocClick);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('click', onDocClick);
      document.removeEventListener('keydown', onKey);
    };
  }, [open]);

  const choose = (id: ThemeId) => {
    applyTheme(id);
    setCurrent(id);
    setOpen(false);
  };

  const active = THEMES.find((t) => t.id === current) ?? THEMES[0];

  return (
    <div className="theme-switcher" ref={containerRef}>
      <button
        type="button"
        className="theme-switcher-button"
        aria-haspopup="true"
        aria-expanded={open}
        aria-label="Change theme"
        onClick={(e) => { e.stopPropagation(); setOpen((v) => !v); }}
      >
        <span className={`theme-switcher-swatch ${active.swatchClass}`} />
        <span>{active.label}</span>
      </button>
      <div className="theme-switcher-menu" role="menu" hidden={!open}>
        {THEMES.map((t) => (
          <button
            key={t.id}
            type="button"
            role="menuitemradio"
            aria-checked={t.id === current}
            className={`theme-switcher-option${t.id === current ? ' active' : ''}`}
            onClick={() => choose(t.id)}
          >
            <span className={`theme-switcher-swatch ${t.swatchClass}`} />
            <span className="theme-switcher-option-label">{t.label}</span>
            <span className="theme-switcher-option-meta">{t.meta}</span>
          </button>
        ))}
      </div>
    </div>
  );
}
