// Log browser.
//
// Three-pane view:
//   * Run directory list (left)        — every per-run folder under plex_logs/
//   * File list inside selected run    — middle, populated on row click
//   * File contents viewer             — right, populated on file click via <LogTailer>
//
// The live-tail logic itself lives in LogTailer so both LogsPanel and
// the embedded panel on DashboardPanel can use the same component.

import { useEffect, useState } from 'react';
import { api, LogFile, LogRun } from '../api';
import { LogTailer } from './LogTailer';

export function LogsPanel() {
  const [runs, setRuns] = useState<LogRun[]>([]);
  const [selectedRun, setSelectedRun] = useState<string | null>(null);
  const [files, setFiles] = useState<LogFile[]>([]);
  const [selectedFile, setSelectedFile] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  // ── Run directory list ─────────────────────────────────────────────────
  const refreshRuns = async () => {
    try { setRuns(await api.listLogRuns()); }
    catch (e) { setError(String(e)); }
  };
  useEffect(() => { refreshRuns(); }, []);

  // When the user picks a run, fetch its file list and clear the file
  // body viewer so we don't show stale content.
  useEffect(() => {
    if (!selectedRun) return;
    setSelectedFile(null);
    api.listLogFiles(selectedRun)
      .then(setFiles)
      .catch((e) => setError(String(e)));
  }, [selectedRun]);

  // A run dir whose name ends in _PASS / _FAIL is final — its log
  // files won't grow further, so LogTailer should freeze instead of
  // polling forever.
  const runIsFinished =
    !!selectedRun && (selectedRun.endsWith('_PASS') || selectedRun.endsWith('_FAIL'));

  return (
    <>
      {error && <div className="banner error">{error}</div>}

      <div className="row">
        <div className="col" style={{ minWidth: 280 }}>
          <div className="panel">
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
              <h2 style={{ margin: 0 }}>Run Directories</h2>
              {/* This button reloads the run *list* (left pane). The
                  file viewer below has its own Reload button + Live
                  tail toggle for refreshing the open file. */}
              <button onClick={refreshRuns}>Refresh runs</button>
            </div>
            {runs.length === 0 ? (
              <div className="empty">No log directories yet. Run a job first.</div>
            ) : (
              <table className="list">
                <thead>
                  <tr><th>Name</th><th>When</th><th>Files</th><th>Result</th></tr>
                </thead>
                <tbody>
                  {runs.map((r) => (
                    <tr key={r.name}
                        onClick={() => setSelectedRun(r.name)}
                        style={{ cursor: 'pointer', background: r.name === selectedRun ? 'var(--bg-panel)' : undefined }}>
                      <td className="mono">{r.name}</td>
                      <td>{formatTs(r.mtime)}</td>
                      <td className="num">{r.file_count}</td>
                      <td>
                        {r.passed === true && <span className="tag done">PASS</span>}
                        {r.passed === false && <span className="tag error">FAIL</span>}
                        {r.passed === null && <span className="tag phase">RUNNING</span>}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        </div>

        <div className="col" style={{ minWidth: 280 }}>
          <div className="panel">
            <h2>Files {selectedRun && <small style={{ color: 'var(--text-dim)' }}>in {selectedRun}</small>}</h2>
            {!selectedRun ? (
              <div className="empty">Pick a run directory on the left.</div>
            ) : files.length === 0 ? (
              <div className="empty">No files in this directory.</div>
            ) : (
              <table className="list">
                <thead><tr><th>Name</th><th>Size</th></tr></thead>
                <tbody>
                  {files.map((f) => (
                    <tr key={f.name}
                        onClick={() => setSelectedFile(f.name)}
                        style={{ cursor: 'pointer', background: f.name === selectedFile ? 'var(--bg-panel)' : undefined }}>
                      <td className="mono">{f.name}</td>
                      <td className="num">{formatBytes(f.size)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        </div>
      </div>

      {selectedRun && selectedFile && (
        <div className="panel">
          <h2 style={{ marginBottom: 8 }}>{selectedFile}</h2>
          <LogTailer
            runName={selectedRun}
            fileName={selectedFile}
            height={520}
            externalFreeze={runIsFinished}
          />
        </div>
      )}
    </>
  );
}

// ── Helpers ──────────────────────────────────────────────────────────────────

function formatTs(ts: number): string {
  return new Date(ts * 1000).toLocaleString();
}
function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1024 * 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`;
  return `${(n / 1024 / 1024 / 1024).toFixed(2)} GB`;
}
