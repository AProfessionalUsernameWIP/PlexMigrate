// ── Registered-snapshot picker ─────────────────────────────────────────
//
// Lists rows from snapshots.db, newest first. When a destination is
// picked, each row carries an overlap chip showing how many of its
// captured libraries also exist on the destination - rows with zero
// overlap are not hidden (the end user may have a reason to import
// anyway) but are visually dimmed and ranked last.

import type { Snapshot } from '../api';

export function SnapshotPicker({
  rows,
  selectedId,
  onSelect,
  destLibraries,
}: {
  rows: Snapshot[];
  selectedId: string | null;
  onSelect: (id: string) => void;
  destLibraries: Set<string>;
}) {
  const filterActive = destLibraries.size > 0;
  // Compute overlap once per row + sort: compatible (overlap > 0) first,
  // then by captured_at desc. When the filter isn't active, fall back
  // to pure captured_at desc.
  const enriched = rows
    .map((r) => {
      const libs = r.libraries || [];
      let overlap = 0;
      for (const lib of libs) {
        if (destLibraries.has(lib)) overlap += 1;
      }
      return { row: r, overlap, total: libs.length };
    })
    .sort((a, b) => {
      if (filterActive && (a.overlap > 0) !== (b.overlap > 0)) {
        return a.overlap > 0 ? -1 : 1;
      }
      return (b.row.captured_at || 0) - (a.row.captured_at || 0);
    });

  return (
    <div style={{ maxHeight: 360, overflowY: 'auto' }}>
      <table className="list" style={{ width: '100%' }}>
        <thead>
          <tr>
            <th></th>
            <th>Snapshot</th>
            <th>Server</th>
            <th>Captured</th>
            <th>Libraries</th>
            {filterActive && <th>Compat</th>}
          </tr>
        </thead>
        <tbody>
          {enriched.map(({ row, overlap, total }) => {
            const dim = filterActive && overlap === 0;
            return (
              <tr
                key={row.id}
                data-testid={`job-snapshot-picker-row-${row.id}`}
                onClick={() => onSelect(row.id)}
                style={{
                  cursor: 'pointer',
                  opacity: dim ? 0.55 : 1,
                  background: row.id === selectedId ? 'var(--bg-panel)' : undefined,
                }}
              >
                <td>
                  <input
                    type="radio"
                    checked={row.id === selectedId}
                    onChange={() => onSelect(row.id)}
                  />
                </td>
                <td className="mono">{row.snapshot_name}</td>
                <td>{row.server_name}</td>
                <td>{row.captured_at ? new Date(row.captured_at * 1000).toLocaleString() : '-'}</td>
                <td>{total}</td>
                {filterActive && (
                  <td>
                    <span className={`tag ${overlap > 0 ? 'done' : 'error'}`} style={{ fontSize: 11 }}>
                      {overlap}/{total}
                    </span>
                  </td>
                )}
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
