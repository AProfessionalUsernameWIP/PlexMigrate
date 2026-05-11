// Export browser.
//
// Lists every .plexbackup.json in the configured output directory.
// Each row carries a Download link that hits /api/exports/{filename}
// — the backend streams the file with Content-Disposition: attachment,
// so the browser saves it under its original name.

import { useEffect, useState } from 'react';
import { api, ExportFile } from '../api';

export function ExportsPanel() {
  const [items, setItems] = useState<ExportFile[]>([]);
  const [error, setError] = useState<string | null>(null);
  // Tracks which row is mid-delete so we can disable its buttons and
  // show a "Deleting…" label without freezing the whole table.
  const [deleting, setDeleting] = useState<string | null>(null);

  const refresh = async () => {
    try { setItems(await api.listExports()); setError(null); }
    catch (e) { setError(String(e)); }
  };
  useEffect(() => { refresh(); }, []);

  const removeOne = async (name: string, sizeBytes: number) => {
    const sizeLabel = formatBytes(sizeBytes);
    if (!confirm(`Delete ${name} (${sizeLabel})? This cannot be undone.`)) return;
    setDeleting(name);
    setError(null);
    try {
      await api.deleteExport(name);
      // Optimistic: drop the row locally so the table reflects the
      // change immediately, then re-fetch the list to stay in sync
      // with the directory (catches any concurrent additions).
      setItems((prev) => prev.filter((f) => f.name !== name));
      await refresh();
    } catch (e) {
      setError(String(e));
    } finally {
      setDeleting(null);
    }
  };

  return (
    <>
      {error && <div className="banner error">{error}</div>}
      <div className="panel">
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <h2 style={{ margin: 0 }}>Export Files</h2>
          <button onClick={refresh}>Refresh</button>
        </div>

        {items.length === 0 ? (
          <div className="empty">No <code>.plexbackup.json</code> files in the output directory yet.</div>
        ) : (
          <table className="list">
            <thead>
              <tr>
                <th>Source Server</th>
                <th>Library</th>
                <th>Initiated By</th>
                <th>Filename</th>
                <th>Exported At</th>
                <th>Size</th>
                <th>Modified</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {items.map((f) => (
                <tr key={f.name}>
                  {/* v0.9.3: friendly server name embedded in the backup
                      JSON itself. Older backups (pre-v0.9.3) lack the
                      field and render an em-dash so the column reads
                      clearly. Hover for the URL when present. */}
                  <td title={f.source_server_url || ''}>
                    {f.source_server || <span style={{ color: 'var(--text-dim)' }}>—</span>}
                  </td>
                  <td>{f.library || <em>unknown</em>}</td>
                  <td><TriggerBadge trigger={f.trigger} scheduleName={f.schedule_name} /></td>
                  <td className="mono">{f.name}</td>
                  <td className="mono">{f.exported_at || '—'}</td>
                  <td className="num">{formatBytes(f.size)}</td>
                  <td>{new Date(f.mtime * 1000).toLocaleString()}</td>
                  <td>
                    <div className="row-buttons">
                      <a href={api.exportDownloadUrl(f.name)} download>
                        <button disabled={deleting === f.name}>Download</button>
                      </a>
                      <button
                        className="danger"
                        disabled={deleting === f.name}
                        onClick={() => removeOne(f.name, f.size)}
                      >
                        {deleting === f.name ? 'Deleting…' : 'Delete'}
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </>
  );
}

function TriggerBadge({
  trigger,
  scheduleName,
}: {
  trigger?: string | null;
  scheduleName?: string | null;
}) {
  // Older backups (exported before v0.9.5) don't carry the trigger
  // field at all. Render an em-dash so the column doesn't look broken.
  if (!trigger) {
    return <span style={{ color: 'var(--text-dim)' }}>—</span>;
  }
  if (trigger === 'schedule') {
    const label = scheduleName ? `Scheduled · ${scheduleName}` : 'Scheduled';
    return <span className="tag phase" title={label}>{label}</span>;
  }
  if (trigger === 'manual') {
    return <span className="tag started">Manual</span>;
  }
  // Forward-compat: unknown trigger string still renders so we don't
  // silently hide the value (helps when adding new triggers later).
  return <span className="tag">{trigger}</span>;
}

function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1024 * 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`;
  return `${(n / 1024 / 1024 / 1024).toFixed(2)} GB`;
}
