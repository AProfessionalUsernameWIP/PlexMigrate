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

  const refresh = async () => {
    try { setItems(await api.listExports()); }
    catch (e) { setError(String(e)); }
  };
  useEffect(() => { refresh(); }, []);

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
                  <td className="mono">{f.name}</td>
                  <td className="mono">{f.exported_at || '—'}</td>
                  <td className="num">{formatBytes(f.size)}</td>
                  <td>{new Date(f.mtime * 1000).toLocaleString()}</td>
                  <td>
                    <a href={api.exportDownloadUrl(f.name)} download>
                      <button>Download</button>
                    </a>
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

function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1024 * 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`;
  return `${(n / 1024 / 1024 / 1024).toFixed(2)} GB`;
}
