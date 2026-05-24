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
// Per-run delete and clear-all delete both fire
// against shutil.rmtree on the backend; best-effort, with errors
// surfaced in the response banner. A run currently being written to
// by the engine may fail to delete on Windows (file locks); the
// user can retry once the job finishes.

import { useEffect, useMemo, useState } from 'react';
import { api, LogFile, LogRun, ServerView, getAccessToken } from '../api';
import { LogTailer } from './LogTailer';
import { Modal } from './Modal';
import { formatBytes, formatTimestamp } from '../utils/format';
import { pausableInterval } from '../utils/pausableInterval';


// Per-server sub-tab view selector. 'all' shows every run for the
// selected server; 'direct' adds the sibling 'Direct Transfers'
// bucket that surfaces combined-slug direct-transfer runs.
type ViewMode = 'all' | 'direct';


// Mirror of server/server_registry.py backend_aware_slug(): ASCII-fold
// the name, collapse non-alphanumeric runs to single hyphens, then
// append the backend. Direct-transfer run dirs encode each end as this
// "<safe-name>-<service>" slug, so the reverse-map must key by the same
// shape, not the raw display name (which would miss any name with a
// space, dot, or other punctuation).
function backendAwareSlug(name: string, serviceType: string): string {
  const folded = (name || '').replace(/[^\x00-\x7F]/g, '');
  const bare =
    folded.replace(/[^A-Za-z0-9]+/g, '-').replace(/^-+|-+$/g, '') || 'server';
  return `${bare}-${(serviceType || 'plex').toLowerCase()}`;
}


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

  // Per-server sub-tabs replace the per-
  // backend filter strip. Each registered server gets its own tab
  // showing only the runs whose server_slug reverse-maps to that
  // server. ``viewMode`` adds a sibling 'Direct Transfers' bucket
  // that surfaces runs whose slug is ``<src>-to-<dst>`` (combined
  // direct-transfer slug emitted by jobs._run_direct); the slug is
  // split + reverse-mapped to BOTH ends, so a single direct-transfer
  // run shows up under whichever server tab the operator picks
  // within the Direct Transfers view.
  const [viewMode, setViewMode] = useState<ViewMode>('all');
  const [selectedServerKey, setSelectedServerKey] = useState<string>('');
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
    return pausableInterval(refreshRuns, 15_000);
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

  // Per-server sub-tabs + a 'Direct Transfers' view that splits
  // combined ``<src>-to-<dst>`` slugs and shows each end under its
  // own server tab. Helpers below classify each run + compute which
  // tab keys exist.

  // Stable key for grouping: prefer server_id when the slug reverse-
  // maps to a registered server, otherwise fall back to the slug
  // itself (so runs from a since-removed server still get their own
  // tab, labelled by slug). The special key "__unknown__" catches
  // runs whose dir-name didn't carry a slug at all.
  const runIsDirect = (r: LogRun): boolean =>
    !!(r.server_slug && r.server_slug.includes('-to-'));

  const directEnds = (
    r: LogRun,
  ): { srcKey: string; srcLabel: string; dstKey: string; dstLabel: string } | null => {
    const slug = r.server_slug || '';
    if (!slug.includes('-to-')) return null;
    const [srcSlug, dstSlug] = slug.split('-to-', 2);
    // Reverse-map each end against the registry. We can't always
    // resolve to an id because backend_aware_slug() is non-trivial;
    // fall back to the slug as the label when the map misses.
    const slugToServer: Record<string, { id: string; name: string }> = {};
    for (const s of registeredServers) {
      // Key by the backend's "<safe-name>-<service>" slug (see
      // backendAwareSlug above) so a server whose display name carries
      // spaces, dots, or other punctuation still resolves. A half that
      // matches no registered server (e.g. a since-removed server)
      // still falls back to the raw slug as its label below.
      slugToServer[backendAwareSlug(s.name, s.service_type || 'plex')] = {
        id: s.id,
        name: s.name,
      };
    }
    const srcMatch = slugToServer[srcSlug];
    const dstMatch = slugToServer[dstSlug];
    return {
      srcKey: srcMatch?.id || `slug:${srcSlug}`,
      srcLabel: srcMatch?.name || srcSlug,
      dstKey: dstMatch?.id || `slug:${dstSlug}`,
      dstLabel: dstMatch?.name || dstSlug,
    };
  };

  // List of server tabs for the currently-selected view. Each tab
  // carries a key (used for routing), a display label, and the count
  // of runs that fall in that bucket. Order: alphabetical by label,
  // with "(no server)" pinned last.
  type ServerTab = { key: string; label: string; count: number };
  const serverTabs: ServerTab[] = useMemo(() => {
    const out: Map<string, ServerTab> = new Map();
    const add = (key: string, label: string) => {
      const cur = out.get(key);
      if (cur) {
        cur.count += 1;
      } else {
        out.set(key, { key, label, count: 1 });
      }
    };
    if (viewMode === 'direct') {
      for (const r of runs) {
        if (!runIsDirect(r)) continue;
        const ends = directEnds(r);
        if (!ends) continue;
        add(ends.srcKey, ends.srcLabel);
        add(ends.dstKey, ends.dstLabel);
      }
    } else {
      // 'all' view groups every run by its single server (or
      // "(no server)" when the run-dir name didn't carry a slug
      // OR when the slug points at a since-removed registration).
      for (const r of runs) {
        if (runIsDirect(r)) {
          // Direct-transfer runs ALSO surface under both ends in the
          // 'all' view, so the operator looking at "Plex-A" sees the
          // direct-transfer run there as well. This matches the
          // operator's "union" framing: the same physical run is
          // visible under either participating server's tab.
          const ends = directEnds(r);
          if (ends) {
            add(ends.srcKey, ends.srcLabel);
            add(ends.dstKey, ends.dstLabel);
          } else {
            add('__unknown__', '(no server)');
          }
          continue;
        }
        const id = r.server_id;
        const name = r.server_name;
        if (id && name) {
          add(id, name);
        } else if (r.server_slug) {
          add(`slug:${r.server_slug}`, r.server_slug);
        } else {
          add('__unknown__', '(no server)');
        }
      }
    }
    const list = Array.from(out.values()).sort((a, b) => {
      if (a.key === '__unknown__') return 1;
      if (b.key === '__unknown__') return -1;
      return a.label.localeCompare(b.label);
    });
    return list;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runs, viewMode, registeredServers]);

  // Auto-pick the first server tab when the current selection is
  // empty or stale (e.g. after switching view mode, or when the only
  // server with runs in this view is different).
  useEffect(() => {
    if (serverTabs.length === 0) {
      if (selectedServerKey) setSelectedServerKey('');
      return;
    }
    const stillExists = serverTabs.some((t) => t.key === selectedServerKey);
    if (!stillExists) {
      setSelectedServerKey(serverTabs[0].key);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [serverTabs]);

  const visibleRuns = useMemo(() => {
    if (!selectedServerKey) return [];
    return runs.filter((r) => {
      if (viewMode === 'direct') {
        if (!runIsDirect(r)) return false;
        const ends = directEnds(r);
        if (!ends) return false;
        return ends.srcKey === selectedServerKey
          || ends.dstKey === selectedServerKey;
      }
      // 'all' view: include the run when EITHER end matches.
      if (runIsDirect(r)) {
        const ends = directEnds(r);
        if (!ends) return false;
        return ends.srcKey === selectedServerKey
          || ends.dstKey === selectedServerKey;
      }
      if (r.server_id) return r.server_id === selectedServerKey;
      if (r.server_slug) return `slug:${r.server_slug}` === selectedServerKey;
      return selectedServerKey === '__unknown__';
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runs, viewMode, selectedServerKey]);

  // If the currently-selected run is filtered out by a view change,
  // clear the selection so the file viewer doesn't show stale content.
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
            {/* Per-server sub-tabs + a
                Direct Transfers view. The top-level mode strip picks
                between "All runs" (every run grouped by server) and
                "Direct Transfers" (only ``<src>-to-<dst>`` runs,
                still grouped per server). The bottom strip is the
                per-server tabs that scope which server's runs the
                table shows. */}
            <nav
              style={{ display: 'flex', gap: 6, marginTop: 8, flexWrap: 'wrap' }}
            >
              <button
                type="button"
                onClick={() => setViewMode('all')}
                className={viewMode === 'all' ? 'primary' : ''}
                title="All logs grouped by server. Direct-transfer runs appear under either participating server's tab."
              >
                All logs
              </button>
              <button
                type="button"
                onClick={() => setViewMode('direct')}
                className={viewMode === 'direct' ? 'primary' : ''}
                title="Only direct-transfer runs (those whose log-dir slug is in the form 'src-to-dst'). Each run shows under either source or destination."
              >
                Direct Transfers
              </button>
            </nav>
            {serverTabs.length > 0 && (
              <nav
                data-testid="log-server-tabs"
                style={{
                  display: 'flex',
                  gap: 4,
                  marginTop: 8,
                  flexWrap: 'wrap',
                  borderBottom: '1px solid var(--border)',
                  paddingBottom: 4,
                }}
              >
                {serverTabs.map((t) => (
                  <button
                    key={t.key}
                    type="button"
                    onClick={() => setSelectedServerKey(t.key)}
                    className={selectedServerKey === t.key ? 'primary' : ''}
                    data-testid={`log-server-tab-${t.key}`}
                    title={
                      viewMode === 'direct'
                        ? `${t.label}: ${t.count} direct-transfer run(s) where this server was source or destination.`
                        : `${t.label}: ${t.count} log run(s) for this server.`
                    }
                  >
                    {t.label} ({t.count})
                  </button>
                ))}
              </nav>
            )}
            {runs.length === 0 ? (
              <div className="empty">No log directories yet. Run a job first.</div>
            ) : serverTabs.length === 0 ? (
              <div className="empty">
                {viewMode === 'direct'
                  ? 'No direct-transfer runs yet. Run a server-to-server transfer to populate.'
                  : 'No log directories yet. Run a job first.'}
              </div>
            ) : visibleRuns.length === 0 ? (
              <div className="empty">
                No runs for the selected server in this view.
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
                          data-testid="log-row"
                          onClick={() => setSelectedRun(r.name)}
                          style={{ cursor: 'pointer', background: r.name === selectedRun ? 'var(--bg-panel)' : undefined }}>
                        <td className="mono">{r.name}</td>
                        <td>{formatTimestamp(r.mtime)}</td>
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
            {/* Escape hatch for log files bigger than
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
    <Modal onClose={onCancel} width={460}>
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
    </Modal>
  );
}


// ── Helpers ──────────────────────────────────────────────────────────────────

