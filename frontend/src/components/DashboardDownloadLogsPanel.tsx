// "Download logs" affordance for the current dashboard run. Lives as
// a sibling of DashboardPanel + RuntimeBreakdownPanel - mounted in
// App.tsx underneath the runtime-breakdown panel. Reads
// ``job.run_log_dir`` from the live
// dashboard snapshot, derives the run name (basename), and exposes:
//
//   * A single "Download all logs (.zip)" button that bundles every
//     log file the engine has written so far for this run.
//   * A short list of individual files with per-file download
//     buttons so the end user can grab one without the surrounding
//     zip.
//
// Behaviour intentionally mirrors LogsPanel's download path (same
// ``downloadBlob`` helper, same endpoints) so the post-mortem-via-
// Logs-tab experience stays consistent.
//
// Persistence: the panel renders as long as the dashboard is still
// showing a job (the dashboard freezes the last-known job after
// completion until a new one starts). When a new run begins,
// ``job.run_log_dir`` flips to the new directory and the panel
// auto-updates.

import { useEffect, useMemo, useRef, useState } from 'react';
import { api, JobPayload, getAccessToken, LogFile } from '../api';
import { formatBytes } from '../utils/format';
import { pausableInterval } from '../utils/pausableInterval';


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


interface Props {
  job: JobPayload | null;
}

export function DashboardDownloadLogsPanel({ job }: Props) {
  // Derive the run name (basename of run_log_dir). Normalise both
  // Linux + Windows path separators so docker / native installs
  // behave the same.
  const runName = useMemo(() => {
    const raw = job?.run_log_dir ?? null;
    if (!raw) return null;
    const parts = raw.replace(/\\/g, '/').split('/').filter(Boolean);
    return parts[parts.length - 1] || null;
  }, [job?.run_log_dir]);

  const [files, setFiles] = useState<LogFile[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [listError, setListError] = useState<string | null>(null);
  const refreshTokenRef = useRef(0);

  // Fetch the file list. Re-fetch on a short tick while the job is
  // live so per-library .log files appearing mid-run become
  // downloadable without a page reload.
  const jobIsLive =
    !!job && (job.state === 'queued' || job.state === 'running' || job.state === 'stopping');

  useEffect(() => {
    if (!runName) {
      setFiles([]);
      return;
    }
    const myToken = ++refreshTokenRef.current;
    let cancelled = false;
    const load = () => {
      api.listLogFiles(runName)
        .then((r) => {
          if (cancelled || myToken !== refreshTokenRef.current) return;
          setFiles(Array.isArray(r) ? r : []);
          setListError(null);
        })
        .catch((e) => {
          if (cancelled || myToken !== refreshTokenRef.current) return;
          setListError(String(e));
        });
    };
    load();
    if (jobIsLive) {
      const stop = pausableInterval(load, 5000);
      return () => { cancelled = true; stop(); };
    }
    return () => { cancelled = true; };
  }, [runName, jobIsLive]);

  if (!runName) {
    return null;
  }

  const totalBytes = files.reduce((sum, f) => sum + (f.size || 0), 0);
  const downloadZip = () =>
    downloadBlob(api.logRunZipUrl(runName), `${runName}.zip`, setError);

  return (
    <div className="panel" style={{ marginTop: 12 }}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', flexWrap: 'wrap', gap: 8 }}>
        <h2 style={{ margin: 0, fontSize: 14 }}>
          Download logs
          <span style={{ marginLeft: 8, color: 'var(--text-dim)', fontSize: 12, fontWeight: 'normal' }}>
            {jobIsLive
              ? '(this run is still active; files keep growing)'
              : '(last completed run; persists until a new run starts)'}
          </span>
        </h2>
        <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
          {files.length > 0 && (
            <span style={{ fontSize: 11, color: 'var(--text-dim)' }}>
              {files.length} file{files.length === 1 ? '' : 's'} · {formatBytes(totalBytes)}
            </span>
          )}
          <button
            type="button"
            className="primary"
            onClick={() => void downloadZip()}
            disabled={files.length === 0}
            title="Bundle every log file in this run directory into a single zip."
          >
            Download all logs (.zip)
          </button>
        </div>
      </div>
      <div style={{ fontSize: 11, color: 'var(--text-dim)', marginTop: 4, fontFamily: 'monospace' }}>
        {runName}
      </div>
      {(error || listError) && (
        <div className="banner error" style={{ marginTop: 8, fontSize: 12 }}>
          {error || listError}
        </div>
      )}
      {files.length > 0 && (
        <details style={{ marginTop: 10 }}>
          <summary style={{ cursor: 'pointer', fontSize: 12, color: 'var(--text-dim)' }}>
            Individual files ({files.length})
          </summary>
          <table className="list" style={{ width: '100%', marginTop: 6, fontSize: 12 }}>
            <thead>
              <tr>
                <th style={{ textAlign: 'left' }}>File</th>
                <th style={{ textAlign: 'right' }}>Size</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {files.map((f) => (
                <tr key={f.name}>
                  <td style={{ fontFamily: 'monospace' }}>{f.name}</td>
                  <td style={{ textAlign: 'right' }}>{formatBytes(f.size || 0)}</td>
                  <td style={{ textAlign: 'right' }}>
                    <button
                      type="button"
                      onClick={() =>
                        void downloadBlob(api.logFileDownloadUrl(runName, f.name), f.name, setError)
                      }
                    >
                      Download
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </details>
      )}
    </div>
  );
}
