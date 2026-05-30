// Cross-platform preflight Section 3 piece — Smart playlists are
// always skipped on cross-platform transfers (smart-playlist rules
// don't translate across backends). The badge surfaces the count;
// the disclosure expands to the full name list so the end user can
// audit which ones won't transfer.

import { useState } from 'react';

interface Props {
  skipped: number;
  names: string[];
}

export function SmartPlaylistDisclosure({ skipped, names }: Props) {
  const [open, setOpen] = useState(false);
  if (skipped === 0) return null;
  return (
    <div style={{ marginTop: 8 }}>
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        style={{
          background: 'none', border: 'none', padding: 0, font: 'inherit',
          color: 'var(--text-dim)', cursor: 'pointer', textAlign: 'left',
          display: 'flex', alignItems: 'center', gap: 6, fontSize: 12,
        }}
      >
        <span>{open ? '▾' : '▸'}</span>
        <span>
          <strong>{skipped}</strong> smart {skipped === 1 ? 'playlist' : 'playlists'} will
          be skipped (smart-playlist rules don't translate across backends).
        </span>
      </button>
      {open && (
        <ul
          style={{
            listStyle: 'disc',
            paddingLeft: 22,
            marginTop: 6,
            maxHeight: 160,
            overflowY: 'auto',
            background: 'var(--panel-alt, #1b2233)',
            border: '1px solid var(--border, #2a3146)',
            borderRadius: 6,
            padding: '8px 8px 8px 28px',
            fontSize: 12,
          }}
        >
          {names.map((n) => (
            <li key={n} style={{ marginBottom: 2 }}>{n}</li>
          ))}
        </ul>
      )}
    </div>
  );
}
