// Cross-platform preflight Section 3 piece — library type translation
// notes. Backends classify libraries differently (e.g. Plex 'artist'
// vs Jellyfin 'music'); the engine maps types best-effort and surfaces
// any non-trivial decisions here so the end user can audit.

import type { CppLibraryTypeNote } from '../api';

interface Props {
  notes: CppLibraryTypeNote[];
}

export function LibraryTypeNotesList({ notes }: Props) {
  if (notes.length === 0) return null;
  return (
    <div style={{ marginTop: 8 }}>
      <div style={{ fontSize: 12, color: 'var(--text-dim)', marginBottom: 4 }}>
        <strong>Library type translation:</strong>
      </div>
      <ul
        style={{
          listStyle: 'disc',
          paddingLeft: 22,
          margin: 0,
          background: 'var(--panel-alt, #1b2233)',
          border: '1px solid var(--border, #2a3146)',
          borderRadius: 6,
          padding: '8px 8px 8px 28px',
          fontSize: 12,
        }}
      >
        {notes.map((n) => (
          <li key={`${n.source_library}-${n.source_type}-${n.dest_type_used}`} style={{ marginBottom: 4 }}>
            <strong>{n.source_library}</strong>{' '}
            <span style={{ color: 'var(--text-dim)' }}>
              ({n.source_type} → {n.dest_type_used})
            </span>
            : {n.message}
          </li>
        ))}
      </ul>
    </div>
  );
}
