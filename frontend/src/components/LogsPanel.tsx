// Log browser.
//
// Three-pane view:
//   * Run directory list (left)        - every per-run folder under plex_logs/
//   * File list inside selected run    - middle, populated on row click
//   * File contents viewer             - right, populated on file click via <LogTailer>
//
// The live-tail logic itself lives in LogTailer so both LogsPanel and
// the embedded panel on DashboardPanel can use the same component.
//
// PR-12 follow-up - per-run delete and clear-all delete. Both fire
// against shutil.rmtree on the backend; best-effort, with errors
// surfaced in the response banner. A run currently being written to
// by the engine may fail to delete on Windows (file locks); the
// operator can retry once the job finishes.

import { useEffect, useState } from 'react';
import { api, LogFile, LogRun, getAccessToken } from '../api';
import { LogTailer } from './LogTailer';


// Auth-aware blob download. The auth middleware rejects a plain
// <a href> navigation because it can't carry the bearer token, so we
// fetch as a blob, then trigger a save dialog via a synthetic
// <a download> element. Same pattern ExportsPanel uses for snapshot
// downloads. Used for both the per-file "Download" buttons and the
// per-run "Download zip" buttons.
async function downloadBlob(
  url: string,
  filename: string,
  setError: (e: string | null) => void,
): Promise<void> {
  try {
    const token = getAccessToken();
    const headers: Record<string, string> = {};
    if (token) headers['Authorization'] = `Bearer ${token}`;
    const res = await fetch(url, { headers });
    if (!res.ok) {
      const text = await res.text().catch(() => res.statusText);
      throw new Error(`${res.status}: ${text}`);
    }
    const blob = await res.blob();
    const objectUrl = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = objectUrl;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(objectUrl);
  } catch (e) {
    setError(`Download failed: ${e}`);
  }
}

const downloadFullLog = (run: string, file: string, setError: (e: string | null) => void) =>
  downloadBlob(api.logFileDownloadUrl(run, file), file, setError);

const downloadRunZip = (run: string, setError: (e: string | null) => void) =>
  downloadBlob(api.logRunZipUrl(run), `${run}.zip`, setError);

export function LogsPanel() {
  const [runs, setRuns] = useState<LogRun[]>([]);
  const [selectedRun, setSelectedRun] = useState<string | null>(null);
  const [files, setFiles] = useState<LogFile[]>([]);
  const [selectedFile, setSelectedFile] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [info, setInfo] = useState<string | null>(null);
  // Pending destructive action - opens a typed-confirmation modal.
  const [pendingDelete, setPendingDelete] = useState<
    | { kind: 'one'; name: string }
    | { kind: 'all'; count: number }
    | null
  >(null);

  // ── Run directory list ─────────────────────────────────────────────────
  const refreshRuns = async () => {
    try { setRuns(await api.listLogRuns()); }
    catch (e) { setError(String(e)); }
  };
  // Initial fetch + low-cadence auto-poll. 15s is short enough that a
  // job kicked off elsewhere shows up on the operator's Logs tab
  // within a quarter-minute without manual refreshes, but long enough
  // that a hundred Logs-tab visits per day are still negligible load
  // against ``listLogRuns`` (one ``Path.iterdir`` + sizes per call).
  // The Refresh button stays for impatient operators.
  useEffect(() => {
    refreshRuns();
    const id = window.setInterval(refreshRuns, 15_000);
    return () => window.clearInterval(id);
  }, []);

  // When the user picks a run, fetch its file list and clear the file
  // body viewer so we don't show stale content.
  useEffect(() => {
    if (!selectedRun) return;
    setSelectedFile(null);
    api.listLogFiles(selectedRun)
      .then(setFiles)
      .catch((e) => setError(String(e)));
  }, [selectedRun]);

  // A run dir whose name ends in _PASS / _FAIL is final - its log
  // files won't grow further, so LogTailer should freeze instead of
  // polling forever.
  const runIsFinished =
    !!selectedRun && (selectedRun.endsWith('_PASS') || selectedRun.endsWith('_FAIL'));

  const doDeleteOne = async (name: string) => {
    setError(null);
    setInfo(null);
    try {
      const r = await api.deleteLogRun(name);
      setInfo(
        `Deleted ${r.deleted} (${r.file_count} file${r.file_count === 1 ? '' : 's'})`
        + (r.errors.length ? ` with ${r.errors.length} error(s): ${r.errors.join(' · ')}` : '.'),
      );
      // Clear the selection if the operator just deleted the active run.
      if (selectedRun === name) {
        setSelectedRun(null);
        setSelectedFile(null);
        setFiles([]);
      }
      await refreshRuns();
    } catch (e) {
      setError(String(e));
    }
  };

  const doDeleteAll = async () => {
    setError(null);
    setInfo(null);
    try {
      const r = await api.deleteAllLogRuns();
      setInfo(
        `Deleted ${r.deleted} run director${r.deleted === 1 ? 'y' : 'ies'}`
        + (r.errors.length ? ` with ${r.errors.length} error(s): ${r.errors.join(' · ')}` : '.'),
      );
      setSelectedRun(null);
      setSelectedFile(null);
      setFiles([]);
      await refreshRuns();
    } catch (e) {
      setError(String(e));
    }
  };

  return (
    <>
      {error && <div className="banner error">{error}</div>}
      {info && <div className="banner good">{info}</div>}

      <div className="row">
        <div className="col" style={{ minWidth: 280 }}>
          <div className="panel">
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
              <h2 style={{ margin: 0 }}>Run Directories</h2>
              {/* This button reloads the run *list* (left pane). The
                  file viewer below has its own Reload button + Live
                  tail toggle for refreshing the open file. */}
              <div style={{ display: 'inline-flex', gap: 8 }}>
                <button onClick={refreshRuns}>Refresh runs</button>
                <button
                  className="danger"
                  disabled={runs.length === 0}
                  onClick={() => setPendingDelete({ kind: 'all', count: runs.length })}
                  title="Permanently remove every log run directory."
                >
                  Clear all logs
                </button>
              </div>
            </div>
            {runs.length === 0 ? (
              <div className="empty">No log directories yet. Run a job first.</div>
            ) : (
              // Cap the list at ~10 visible rows. With a few hundred
              // runs the page would otherwise scroll the entire app
              // instead of the table - which moves the Refresh /
              // Clear-all controls offscreen. Internal overflow-y
              // keeps the panel a fixed height; the table header
              // stays in view because it's part of the same scroll
              // container the operator looks at.
              <div style={{ maxHeight: 380, overflowY: 'auto', marginTop: 8 }}>
                <table className="list">
                  <thead>
                    <tr><th>Name</th><th>When</th><th>Files</th><th>Result</th><th></th></tr>
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
                        <td style={{ textAlign: 'right', whiteSpace: 'nowrap' }}>
                          <button
                            onClick={(e) => {
                              e.stopPropagation();
                              void downloadRunZip(r.name, setError);
                            }}
                            title={`Download ${r.name} as a zip of every file in the run directory.`}
                            style={{ fontSize: 11, marginRight: 4 }}
                          >
                            Download
                          </button>
                          <button
                            className="danger"
                            onClick={(e) => {
                              // Stop the row's onClick from also firing
                              // (which would select the run we're about
                              // to delete).
                              e.stopPropagation();
                              setPendingDelete({ kind: 'one', name: r.name });
                            }}
                            title={`Delete ${r.name}`}
                            style={{ fontSize: 11 }}
                          >
                            Delete
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
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
              <div style={{ maxHeight: 380, overflowY: 'auto', marginTop: 8 }}>
                <table className="list">
                  <thead><tr><th>Name</th><th>Size</th><th></th></tr></thead>
                  <tbody>
                    {files.map((f) => (
                      <tr key={f.name}
                          onClick={() => setSelectedFile(f.name)}
                          style={{ cursor: 'pointer', background: f.name === selectedFile ? 'var(--bg-panel)' : undefined }}>
                        <td className="mono">{f.name}</td>
                        <td className="num">{formatBytes(f.size)}</td>
                        <td style={{ textAlign: 'right', whiteSpace: 'nowrap' }}>
                          <button
                            onClick={(e) => {
                              e.stopPropagation();
                              void downloadFullLog(selectedRun!, f.name, setError);
                            }}
                            title={`Download ${f.name}`}
                            style={{ fontSize: 11 }}
                          >
                            Download
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        </div>
      </div>

      {selectedRun && selectedFile && (
        <div className="panel">
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8, gap: 8, flexWrap: 'wrap' }}>
            <h2 style={{ margin: 0 }}>{selectedFile}</h2>
            {/* PR-13 fix #5 - escape hatch for log files bigger than
                the 16 MB live-tail cap. Fetches with the auth bearer
                so the auth middleware accepts the request, then
                triggers a save dialog via a synthetic <a> element. */}
            <button
              onClick={() => void downloadFullLog(selectedRun, selectedFile, setError)}
              title="Stream the complete log file. Use this when the live viewer shows the truncated banner."
            >
              Download full log
            </button>
          </div>
          <LogTailer
            runName={selectedRun}
            fileName={selectedFile}
            height={520}
            externalFreeze={runIsFinished}
          />
        </div>
      )}

      {pendingDelete && pendingDelete.kind === 'one' && (
        <ConfirmDeleteModal
          title={`Delete run ${pendingDelete.name}?`}
          body={
            <>
              Permanently removes the run directory and every log file
              inside it. This cannot be undone.
            </>
          }
          confirmWord={null}
          danger="Delete run"
          onCancel={() => setPendingDelete(null)}
          onConfirm={async () => {
            await doDeleteOne(pendingDelete.name);
            setPendingDelete(null);
          }}
        />
      )}
      {pendingDelete && pendingDelete.kind === 'all' && (
        <ConfirmDeleteModal
          title={`Clear all ${pendingDelete.count} log directories?`}
          body={
            <>
              Permanently removes every run directory under the
              configured log directory. The engine writes new logs on
              the next job, but historical runs are gone for good.
            </>
          }
          confirmWord="CLEAR"
          danger="Clear all logs"
          onCancel={() => setPendingDelete(null)}
          onConfirm={async () => {
            await doDeleteAll();
            setPendingDelete(null);
          }}
        />
      )}
    </>
  );
}


// ── Confirm modal (shared by both delete paths) ────────────────────────────

function ConfirmDeleteModal({
  title,
  body,
  confirmWord,
  danger,
  onCancel,
  onConfirm,
}: {
  title: string;
  body: React.ReactNode;
  // When set, the operator must type this exact string before the
  // danger button enables. Used for the clear-all path; single-run
  // delete uses ``null`` (one-click confirm).
  confirmWord: string | null;
  danger: string;
  onCancel: () => void;
  onConfirm: () => Promise<void>;
}) {
  const [typed, setTyped] = useState('');
  const [submitting, setSubmitting] = useState(false);

  const canSubmit =
    !submitting && (confirmWord === null || typed === confirmWord);

  const submit = async () => {
    if (!canSubmit) return;
    setSubmitting(true);
    try {
      await onConfirm();
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div
      onClick={onCancel}
      style={{
        position: 'fixed', inset: 0,
        background: 'rgba(0,0,0,0.55)',
        zIndex: 1000,
        display: 'flex', alignItems: 'flex-start', justifyContent: 'center',
        paddingTop: '8vh',
      }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        className="panel"
        style={{ width: 460, maxWidth: '92vw' }}
      >
        <h2 style={{ marginTop: 0 }}>{title}</h2>
        <div className="banner error" style={{ fontSize: 12 }}>{body}</div>
        {confirmWord && (
          <label className="field" style={{ marginTop: 12 }}>
            <span className="label">Type the word to confirm</span>
            <span className="help">
              Type <code className="mono">{confirmWord}</code> exactly.
            </span>
            <input
              type="text"
              value={typed}
              onChange={(e) => setTyped(e.target.value)}
              autoComplete="off"
              autoFocus
            />
          </label>
        )}
        <div className="row-buttons" style={{ marginTop: 12 }}>
          <button className="danger" disabled={!canSubmit} onClick={submit}>
            {submitting ? 'Deleting…' : danger}
          </button>
          <button onClick={onCancel} disabled={submitting}>Cancel</button>
        </div>
      </div>
    </div>
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
