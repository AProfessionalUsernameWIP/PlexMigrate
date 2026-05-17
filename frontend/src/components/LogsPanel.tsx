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
// end user can retry once the job finishes.

import { useEffect, useMemo, useState } from 'react';
import { api, LogFile, LogRun, ServerView, getAccessToken } from '../api';
import { LogTailer } from './LogTailer';
import {
  BackendTabStrip,
  BackendType,
  backendCounts,
} from './BackendTabStrip';


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

  // Phase C of the backend-filter UI restructure (Finding[BACKEND-
  // FILTER-AUDIT]-2026-05-16.md). Logs are runs, not servers; the
  // filter joins ``run.server_id`` to the registry's service_type
  // client-side. Once developer ships service_type on the log-list
  // response shape (TODO-AGENT-2-3 in the Finding), the join here
  // can be retired in favour of a direct read.
  const [activeBackend, setActiveBackend] = useState<BackendType>('plex');
  const [registeredServers, setRegisteredServers] = useState<ServerView[]>([]);

  // ── Run directory list ─────────────────────────────────────────────────
  const refreshRuns = async () => {
    try { setRuns(await api.listLogRuns()); }
    catch (e) { setError(String(e)); }
  };

  // Fetch the registered server list once for the backend join. The
  // Servers tab's poll keeps the registry warm; here we just need a
  // snapshot for the service_type lookup. Failure is non-fatal: the
  // join falls back to "everything is Plex" so runs still render.
  useEffect(() => {
    api.listServers()
      .then(setRegisteredServers)
      .catch(() => setRegisteredServers([]));
  }, []);
  // Initial fetch + low-cadence auto-poll. 15s is short enough that a
  // job kicked off elsewhere shows up on the end user's Logs tab
  // within a quarter-minute without manual refreshes, but long enough
  // that a hundred Logs-tab visits per day are still negligible load
  // against ``listLogRuns`` (one ``Path.iterdir`` + sizes per call).
  // The Refresh button stays for impatient end users.
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

  // Phase C: backend-of-each-run lookup + filtered visible list.
  // Build a server_id -> backend map from the registry, default
  // unknown ids to 'plex' so historical runs continue to render
  // under the original backend label.
  const idToBackend = useMemo(() => {
    const out: Record<string, BackendType> = {};
    for (const s of registeredServers) {
      const t = ((s as unknown as { service_type?: string }).service_type) || 'plex';
      out[s.id] = (t === 'jellyfin' || t === 'emby') ? t : 'plex';
    }
    return out;
  }, [registeredServers]);

  const runBackend = (r: LogRun): BackendType => {
    const id = r.server_id;
    if (id && idToBackend[id]) return idToBackend[id];
    return 'plex';
  };

  const visibleRuns = useMemo(
    () => runs.filter((r) => runBackend(r) === activeBackend),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [runs, idToBackend, activeBackend],
  );

  // BackendTabStrip's counts represent how many RUNS each backend
  // has (not how many servers). Synthesize one pseudo ServerView per
  // run so the strip's count-in-parens reads "Plex (47)" / "Jellyfin
  // (3)" rather than the registry-derived totals.
  const stripServers = useMemo(() => {
    return runs.map((r) => ({
      id: `run:${r.name}`,
      name: r.name,
      service_type: runBackend(r),
    })) as unknown as ServerView[];
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runs, idToBackend]);

  // Auto-correct activeBackend when its bucket is empty and another
  // backend has runs. Same pattern as the other panels.
  useEffect(() => {
    if (runs.length === 0) return;
    const counts = backendCounts(stripServers);
    if (counts[activeBackend] === 0) {
      const fallback = (['plex', 'jellyfin', 'emby'] as BackendType[])
        .find((b) => counts[b] > 0);
      if (fallback) setActiveBackend(fallback);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [stripServers, activeBackend]);

  // If the currently-selected run is filtered out by the backend
  // change, clear the selection so the file viewer doesn't show
  // stale content.
  useEffect(() => {
    if (!selectedRun) return;
    const stillVisible = visibleRuns.some((r) => r.name === selectedRun);
    if (!stillVisible) {
      setSelectedRun(null);
      setSelectedFile(null);
      setFiles([]);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [visibleRuns]);

  const doDeleteOne = async (name: string) => {
    setError(null);
    setInfo(null);
    try {
      const r = await api.deleteLogRun(name);
      setInfo(
        `Deleted ${r.deleted} (${r.file_count} file${r.file_count === 1 ? '' : 's'})`
        + (r.errors.length ? ` with ${r.errors.length} error(s): ${r.errors.join(' · ')}` : '.'),
      );
      // Clear the selection if the end user just deleted the active run.
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
            {/* Phase C: backend filter strip above the run list.
                Auto-hidden when only one backend has runs. The strip's
                count reflects RUN count per backend (not server count)
                so the end user sees the actual workload distribution
                across backends. */}
            <BackendTabStrip
              servers={stripServers}
              activeBackend={activeBackend}
              onChange={setActiveBackend}
              style={{ marginTop: 8 }}
            />
            {runs.length === 0 ? (
              <div className="empty">No log directories yet. Run a job first.</div>
            ) : visibleRuns.length === 0 ? (
              <div className="empty">
                No log directories for the selected backend. Switch backends
                above or run a job against this backend to populate.
              </div>
            ) : (
              // Run directories list. Renders at its natural height; the
              // surrounding panel uses normal page scroll. The earlier
              // maxHeight + overflowY wrap was removed because nested
              // scroll regions made the page UX feel cramped on hosts
              // with many runs.
              <div style={{ marginTop: 8 }}>
                <table className="list">
                  <thead>
                    <tr><th>Name</th><th>When</th><th>Files</th><th>Result</th><th></th></tr>
                  </thead>
                  <tbody>
                    {visibleRuns.map((r) => (
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

// Exported so the Servers > Logs panel reuses the exact same
// destructive-action modal. Keeping a single ConfirmDeleteModal
// across log surfaces keeps the safety pattern uniform: same typed
// confirm-word UX, same danger-button styling, same cancel
// semantics. A future cleanup could lift it to its own module if a
// third caller appears outside the logging area.
export function ConfirmDeleteModal({
  title,
  body,
  confirmWord,
  danger,
  onCancel,
  onConfirm,
}: {
  title: string;
  body: React.ReactNode;
  // When set, the end user must type this exact string before the
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
