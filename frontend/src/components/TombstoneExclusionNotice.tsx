// Cross-platform preflight Section 3 piece — surfaces destination users
// that exist on the destination but are tombstoned (deleted-but-cached
// rows the engine refuses to write to). The picker also filters these
// out; this section makes the exclusion explicit so the end user knows
// why a familiar username isn't selectable.

import type { CppTombstoneNote } from '../api';

interface Props {
  notes: CppTombstoneNote[];
}

export function TombstoneExclusionNotice({ notes }: Props) {
  if (notes.length === 0) return null;
  return (
    <div style={{ marginTop: 8 }}>
      <div style={{ fontSize: 12, color: 'var(--text-dim)', marginBottom: 4 }}>
        <strong>Tombstoned destination users excluded:</strong>
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
          <li key={n.dest_username} style={{ marginBottom: 4 }}>
            <strong>{n.dest_username}</strong>{' '}
            <span style={{ color: 'var(--text-dim)' }}>- {n.reason}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}
