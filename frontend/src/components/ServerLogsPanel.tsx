// Servers > Logs sub-tab.
//
// Per-server-grouped view of run logs that mirrors the Exports panel's
// shape: a top-of-panel sub-tab strip with one button per registered
// server (server count next to the name), and a body that lists the
// runs scoped to the selected server. The existing global Settings >
// Logs panel still exists; this is the same data with a different
// grouping lens.
//
// Source of truth: GET /api/logs returns every run dir + the
// extracted server_slug and a reverse-mapped server_id when the slug
// resolves to a registered server. Orphan runs (slug present but no
// matching server) collect under an "Orphan" group keyed by the slug.
// Runs whose dir name does not match the engine's expected format
// (server_slug is null) collect under "Unattributed".

import { useEffect, useState } from 'react';
import { api, getAccessToken, LogFile, LogRun, ServerView } from '../api';
import { LogTailer } from './LogTailer';
import { ConfirmDeleteModal } from './LogsPanel';


// Same blob-download pattern LogsPanel uses; copied verbatim because
// the helper is private to that module. Future refactor could lift
// it into a shared utility if a third caller appears.
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

function formatBytes(n: number): string {
  if (!Number.isFinite(n) || n <= 0) return '-';
  const units = ['B', 'KB', 'MB', 'GB'];
  let v = n;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i++;
  }
  return `${v.toFixed(v >= 100 ? 0 : 1)} ${units[i]}`;
}

function formatTs(epochSeconds: number): string {
  if (!Number.isFinite(epochSeconds) || epochSeconds <= 0) return '-';
  return new Date(epochSeconds * 1000).toLocaleString();
}

const ORPHAN_KEY_PREFIX = '__orphan__|';
const UNATTRIBUTED_KEY = '__unattributed__';


export function ServerLogsPanel() {
  const [runs, setRuns] = useState<LogRun[]>([]);
  const [servers, setServers] = useState<ServerView[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [info, setInfo] = useState<string | null>(null);
  const [selectedGroupKey, setSelectedGroupKey] = useState<string | null>(null);
  const [selectedRun, setSelectedRun] = useState<string | null>(null);
  const [files, setFiles] = useState<LogFile[]>([]);
  const [selectedFile, setSelectedFile] = useState<string | null>(null);
  // Pending destructive action. ``one`` deletes a single run dir;
  // ``server`` deletes every run dir attributed to the currently-
  // selected server group. Same modal idiom + safety pattern as
  // Settings > Logs.
  const [pendingDelete, setPendingDelete] = useState<
    | { kind: 'one'; name: string }
    | { kind: 'server'; runs: string[]; serverLabel: string }
    | null
  >(null);

  const refresh = async () => {
    setError(null);
    try {
      const [rResult, sResult] = await Promise.allSettled([
        api.listLogRuns(),
        api.listServers(),
      ]);
      if (rResult.status === 'fulfilled') setRuns(rResult.value);
      else setError(String(rResult.reason));
      if (sResult.status === 'fulfilled') setServers(sResult.value);
    } catch (e) {
      setError(String(e));
    }
  };
  useEffect(() => { void refresh(); }, []);

  // Destructive ops. Both fire against /api/logs DELETE primitives;
  // single-run uses the run-specific endpoint, "clear all for this
  // server" loops the per-run endpoint over the filtered list because
  // the existing all-or-nothing endpoint /api/logs DELETE wipes EVERY
  // run on the host - too broad for the per-server panel. Errors
  // accumulate in the result banner; success refreshes the list.
  const doDeleteOne = async (name: string) => {
    setError(null);
    setInfo(null);
    try {
      const r = await api.deleteLogRun(name);
      setInfo(
        `Deleted ${r.deleted} (${r.file_count} file${r.file_count === 1 ? '' : 's'})`
        + (r.errors.length ? ` with ${r.errors.length} error(s): ${r.errors.join(' · ')}` : '.'),
      );
      if (selectedRun === name) {
        setSelectedRun(null);
        setSelectedFile(null);
        setFiles([]);
      }
      await refresh();
    } catch (e) {
      setError(String(e));
    }
  };

  const doDeleteForServer = async (runNames: string[]) => {
    setError(null);
    setInfo(null);
    const errors: string[] = [];
    let deleted = 0;
    for (const name of runNames) {
      try {
        const r = await api.deleteLogRun(name);
        deleted += 1;
        if (r.errors.length) errors.push(...r.errors);
      } catch (e) {
        errors.push(`${name}: ${String(e)}`);
      }
    }
    setInfo(
      `Deleted ${deleted} run director${deleted === 1 ? 'y' : 'ies'}`
      + (errors.length ? ` with ${errors.length} error(s): ${errors.join(' · ')}` : '.'),
    );
    setSelectedRun(null);
    setSelectedFile(null);
    setFiles([]);
    await refresh();
  };

  // Group runs by (server_id when known, else orphan slug, else
  // unattributed). The resulting map's keys are stable across renders
  // so the selection survives a refresh as long as the selected
  // server still has at least one matching run.
  const grouped: Record<
    string,
    { label: string; rows: LogRun[]; representative_server_id: string | null }
  > = {};
  for (const r of runs) {
    let key: string;
    let label: string;
    let rep: string | null = null;
    if (r.server_id) {
      key = r.server_id;
      label = r.server_name || r.server_slug || r.server_id;
      rep = r.server_id;
    } else if (r.server_slug) {
      key = `${ORPHAN_KEY_PREFIX}${r.server_slug}`;
      label = `${r.server_slug} (removed)`;
    } else {
      key = UNATTRIBUTED_KEY;
      label = 'Unattributed';
    }
    if (!grouped[key]) {
      grouped[key] = { label, rows: [], representative_server_id: rep };
    }
    grouped[key].rows.push(r);
  }
  const groupKeys = Object.keys(grouped).sort((a, b) => {
    // Live registered servers sort first (lex on label); orphans next;
    // unattributed last. End user's most-relevant view stays on top.
    const aRank = a === UNATTRIBUTED_KEY ? 2 : a.startsWith(ORPHAN_KEY_PREFIX) ? 1 : 0;
    const bRank = b === UNATTRIBUTED_KEY ? 2 : b.startsWith(ORPHAN_KEY_PREFIX) ? 1 : 0;
    if (aRank !== bRank) return aRank - bRank;
    return grouped[a].label.localeCompare(grouped[b].label);
  });

  // Auto-select first key whenever the selection becomes stale.
  useEffect(() => {
    if (groupKeys.length === 0) {
      if (selectedGroupKey !== null) setSelectedGroupKey(null);
      return;
    }
    if (!selectedGroupKey || !grouped[selectedGroupKey]) {
      setSelectedGroupKey(groupKeys[0]);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runs, servers]);

  // Pull files when the end user picks a run.
  useEffect(() => {
    if (!selectedRun) {
      setFiles([]);
      setSelectedFile(null);
      return;
    }
    let cancelled = false;
    api.listLogFiles(selectedRun)
      .then((f) => { if (!cancelled) setFiles(f); })
      .catch((e) => { if (!cancelled) setError(String(e)); });
    return () => { cancelled = true; };
  }, [selectedRun]);

  return (
    <div className="panel">
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <h2 style={{ margin: 0 }}>Server logs</h2>
        <button onClick={() => void refresh()}>Refresh</button>
      </div>
      <span className="help" style={{ display: 'block', color: 'var(--text-dim)', fontSize: 12, marginBottom: 8 }}>
        Per-server view of every run that wrote logs. The global view
        of all runs (including orphan runs from removed servers) is
        under <strong>Settings &gt; Logs</strong>.
      </span>

      {error && <div className="banner error">{error}</div>}
      {info && <div className="banner good" style={{ fontSize: 12, marginBottom: 6 }}>{info}</div>}

      {groupKeys.length === 0 ? (
        <div className="empty">No runs recorded yet.</div>
      ) : (
        <>
          {/* Per-server sub-tab strip. Matches the LibraryCataloguesPanel
              idiom (nav.tabs.sub-tabs) so the whole Servers tab feels
              like one cohesive UI: same shape on Overview's Library
              Catalogues, same shape on Exports per-server, same shape
              here. Each button shows the friendly name; the count is
              rendered as a small parenthetical for at-a-glance
              distribution without crowding the button. */}
          <nav className="tabs sub-tabs" style={{ marginBottom: 12 }}>
            {groupKeys.map((k) => {
              const g = grouped[k];
              const active = k === selectedGroupKey;
              return (
                <button
                  key={k}
                  onClick={() => { setSelectedGroupKey(k); setSelectedRun(null); }}
                  className={active ? 'active' : ''}
                >
                  {g.label} ({g.rows.length})
                </button>
              );
            })}
          </nav>

          {selectedGroupKey && grouped[selectedGroupKey] && (
            <>
              <div style={{
                display: 'flex',
                justifyContent: 'space-between',
                alignItems: 'center',
                margin: '0 0 6px',
              }}>
                <h3 style={{ fontSize: 13, margin: 0 }}>
                  {grouped[selectedGroupKey].label}
                </h3>
                {/* Clear-all for the SELECTED server. Loops the per-run
                    delete endpoint over this group's runs only; the
                    host-wide /api/logs DELETE would wipe other servers'
                    runs too, which is not what the end user clicking
                    "Clear all for THIS server" expects. */}
                <button
                  onClick={() => setPendingDelete({
                    kind: 'server',
                    runs: grouped[selectedGroupKey].rows.map((r) => r.name),
                    serverLabel: grouped[selectedGroupKey].label,
                  })}
                  style={{ fontSize: 11 }}
                >
                  Clear all {grouped[selectedGroupKey].rows.length} for this server
                </button>
              </div>
              <ServerLogGroup
                key={selectedGroupKey}
                runs={grouped[selectedGroupKey].rows}
                selectedRun={selectedRun}
                onSelectRun={setSelectedRun}
                files={files}
                selectedFile={selectedFile}
                onSelectFile={setSelectedFile}
                onDeleteRun={(run) => setPendingDelete({ kind: 'one', name: run })}
                onDownloadZip={(run) => void downloadBlob(
                  api.logRunZipUrl(run), `${run}.zip`, setError,
                )}
                onDownloadFile={(run, file) => void downloadBlob(
                  api.logFileDownloadUrl(run, file), file, setError,
                )}
              />
            </>
          )}
        </>
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
      {pendingDelete && pendingDelete.kind === 'server' && (
        <ConfirmDeleteModal
          title={`Clear all ${pendingDelete.runs.length} log directories for ${pendingDelete.serverLabel}?`}
          body={
            <>
              Permanently removes every run directory attributed to this
              server. Other servers' run directories are untouched.
              The engine writes new logs on the next job, but historical
              runs from this server are gone.
            </>
          }
          confirmWord="CLEAR"
          danger="Clear all for this server"
          onCancel={() => setPendingDelete(null)}
          onConfirm={async () => {
            await doDeleteForServer(pendingDelete.runs);
            setPendingDelete(null);
          }}
        />
      )}
    </div>
  );
}


interface ServerLogGroupProps {
  runs: LogRun[];
  selectedRun: string | null;
  onSelectRun: (r: string | null) => void;
  files: LogFile[];
  selectedFile: string | null;
  onSelectFile: (f: string | null) => void;
  onDeleteRun: (run: string) => void;
  onDownloadZip: (run: string) => void;
  onDownloadFile: (run: string, file: string) => void;
}

function ServerLogGroup({
  runs,
  selectedRun,
  onSelectRun,
  files,
  selectedFile,
  onSelectFile,
  onDeleteRun,
  onDownloadZip,
  onDownloadFile,
}: ServerLogGroupProps) {
  return (
    <div style={{ display: 'flex', gap: 12 }}>
      {/* Runs list. Renders at natural height; the surrounding panel
          handles overflow. Mirrors the Task A fix to LogsPanel. */}
      <div style={{ flex: '0 0 320px' }}>
        <table className="list" style={{ fontSize: 12 }}>
          <thead>
            <tr><th>Name</th><th>When</th><th>Files</th><th>Result</th><th></th></tr>
          </thead>
          <tbody>
            {runs.map((r) => (
              <tr
                key={r.name}
                onClick={() => onSelectRun(r.name)}
                style={{
                  cursor: 'pointer',
                  background: r.name === selectedRun ? 'var(--bg-panel)' : undefined,
                }}
              >
                <td className="mono">{r.name}</td>
                <td>{formatTs(r.mtime)}</td>
                <td className="num">{r.file_count}</td>
                <td>
                  {r.passed === true && <span className="tag done">PASS</span>}
                  {r.passed === false && <span className="tag error">FAIL</span>}
                  {r.passed === null && <span className="tag phase">RUNNING</span>}
                </td>
                <td>
                  {/* Per-run delete. e.stopPropagation so the row's
                      onSelectRun handler doesn't also fire and load
                      the about-to-be-deleted run's files. */}
                  <button
                    onClick={(e) => { e.stopPropagation(); onDeleteRun(r.name); }}
                    style={{ fontSize: 10 }}
                    title="Delete this run directory and every file inside it."
                  >
                    Delete
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {/* Middle: file list for the selected run. */}
      <div style={{ flex: '0 0 280px' }}>
        {selectedRun ? (
          <>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 6 }}>
              <strong style={{ fontSize: 12 }}>{selectedRun}</strong>
              <button onClick={() => onDownloadZip(selectedRun)} style={{ fontSize: 11 }}>
                Download zip
              </button>
            </div>
            <table className="list" style={{ fontSize: 12 }}>
              <thead>
                <tr><th>File</th><th>Size</th><th></th></tr>
              </thead>
              <tbody>
                {files.map((f) => (
                  <tr
                    key={f.name}
                    onClick={() => onSelectFile(f.name)}
                    style={{
                      cursor: 'pointer',
                      background: f.name === selectedFile ? 'var(--bg-panel)' : undefined,
                    }}
                  >
                    <td className="mono">{f.name}</td>
                    <td className="num">{formatBytes(f.size)}</td>
                    <td>
                      <button
                        onClick={(e) => { e.stopPropagation(); onDownloadFile(selectedRun, f.name); }}
                        style={{ fontSize: 10 }}
                      >
                        Download
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </>
        ) : (
          <div className="empty" style={{ fontSize: 12 }}>Pick a run to see its files.</div>
        )}
      </div>

      {/* Right: file viewer. */}
      <div style={{ flex: '1 1 auto', minWidth: 0 }}>
        {selectedRun && selectedFile ? (
          <LogTailer
            runName={selectedRun}
            fileName={selectedFile}
          />
        ) : (
          <div className="empty" style={{ fontSize: 12 }}>
            Pick a file to view its contents.
          </div>
        )}
      </div>
    </div>
  );
}
