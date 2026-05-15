// PR-13 - Exports panel, registry-backed.
//
// Two sections:
//
//   * Snapshots (registry rows) - one row per captured snapshot,
//     grouped by server. Each row has Download (instant when a
//     prebuilt JSON sidecar exists; on-demand otherwise) + Delete
//     (db_admin gated, removes registry row + .db + sidecar).
//     Per-server group header carries a "Clear all snapshots for
//     this server" danger button.
//
//   * Legacy JSON archives - pre-rename .plexexport.json files moved
//     to snapshots/legacy/ on first boot after PR-13. Read-only
//     browse + download; delete is db_admin gated. Hidden when
//     the legacy directory is empty.
//
// All writes funnel through a shared DbAdminAuthModal that collects
// the db_admin username + password and submits them with the
// destructive request. Modal style matches the User Management
// panel's modal so operators see a consistent destructive surface.

import { useEffect, useRef, useState } from 'react';
import { api, ExportArchive, getAccessToken, Snapshot } from '../api';

type PendingDestructive =
  | { kind: 'delete_snapshot'; id: string; name: string; canKeepJson: boolean }
  | { kind: 'clear_server'; server_id: string; server_name: string; count: number }
  | { kind: 'delete_archive'; name: string }
  | { kind: 'clear_archives'; count: number };

// Animated label frames for the on-demand JSON render. Picked to
// match the user-requested cycle "…, ., .., …". Used only when the
// snapshot has no cached sidecar yet (has_cached_sidecar === false).
// Cached-sidecar downloads use the simpler "Downloading…" label.
const GENERATING_FRAMES = ['Generating…', 'Generating.', 'Generating..', 'Generating…'];
const GENERATING_FRAME_MS = 350;


export function ExportsPanel() {
  const [snapshots, setSnapshots] = useState<Snapshot[] | null>(null);
  // JSON archives: standalone .plexexport.json files in
  // <output_dir>/legacy/. Populated by the keep-JSON path during
  // snapshot delete and by any pre-PR-13 files relocated at startup.
  const [archives, setArchives] = useState<ExportArchive[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [info, setInfo] = useState<string | null>(null);
  const [pending, setPending] = useState<PendingDestructive | null>(null);
  const [downloading, setDownloading] = useState<string | null>(null);
  // Tick counter for the animated "Generating…" label. Increments
  // every GENERATING_FRAME_MS while ``downloading`` is non-null and
  // resets to 0 between clicks. Module-level frame index is
  // ``generatingTick % GENERATING_FRAMES.length``.
  const [generatingTick, setGeneratingTick] = useState(0);
  const tickTimerRef = useRef<number | null>(null);
  useEffect(() => {
    if (downloading === null) {
      setGeneratingTick(0);
      return;
    }
    tickTimerRef.current = window.setInterval(() => {
      setGeneratingTick((t) => t + 1);
    }, GENERATING_FRAME_MS);
    return () => {
      if (tickTimerRef.current !== null) {
        window.clearInterval(tickTimerRef.current);
        tickTimerRef.current = null;
      }
    };
  }, [downloading]);

  const refresh = async () => {
    setError(null);
    try {
      const [snapsResult, archiveResult] = await Promise.allSettled([
        api.listSnapshots(),
        api.listLegacySnapshots(),
      ]);
      if (snapsResult.status === 'fulfilled') {
        setSnapshots(snapsResult.value.snapshots);
      } else {
        setError(String(snapsResult.reason));
      }
      // Archives missing is non-fatal - empty list is a valid state.
      setArchives(archiveResult.status === 'fulfilled' ? archiveResult.value : []);
    } catch (e) {
      setError(String(e));
    }
  };
  useEffect(() => { void refresh(); }, []);

  // Common pattern: fetch through JS so the Authorization header
  // rides along (a plain <a href> wouldn't carry the bearer token
  // and the auth middleware would 401). Receive as blob, save via
  // a synthetic <a> element.
  //
  // Diagnostic console.log lines are intentional: when the operator
  // reports a download failure, the browser console gives us the
  // status / size / first-bytes signal the network tab also shows
  // but is easier to copy-paste back. Remove later if noise becomes
  // an issue.
  const downloadBlob = async (url: string, fileName: string) => {
    setDownloading(url);
    setError(null);
    // eslint-disable-next-line no-console
    console.log('[Exports] download start', { url, fileName });
    try {
      const token = getAccessToken();
      const headers: Record<string, string> = {};
      if (token) headers['Authorization'] = `Bearer ${token}`;
      else {
        // eslint-disable-next-line no-console
        console.warn('[Exports] no access token in memory at click time');
      }
      const res = await fetch(url, { headers, credentials: 'same-origin' });
      // eslint-disable-next-line no-console
      console.log('[Exports] response', {
        status: res.status,
        ok: res.ok,
        contentType: res.headers.get('content-type'),
        contentLength: res.headers.get('content-length'),
      });
      if (!res.ok) {
        const text = await res.text().catch(() => res.statusText);
        throw new Error(`${res.status}: ${text}`);
      }
      const blob = await res.blob();
      // eslint-disable-next-line no-console
      console.log('[Exports] blob received', { size: blob.size, type: blob.type });
      if (blob.size === 0) {
        throw new Error('Server returned a zero-byte response.');
      }
      const objectUrl = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = objectUrl;
      a.download = fileName;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(objectUrl);
      // eslint-disable-next-line no-console
      console.log('[Exports] download triggered', fileName);
    } catch (e) {
      // eslint-disable-next-line no-console
      console.error('[Exports] download failed', e);
      setError(`Download failed: ${e}`);
    } finally {
      setDownloading(null);
      void refresh();
    }
  };

  const submitDestructive = async (
    creds: { username: string; password: string },
    options: { keepJson: boolean },
  ) => {
    if (!pending) return;
    setError(null);
    setInfo(null);
    try {
      if (pending.kind === 'delete_snapshot') {
        const r = await api.deleteSnapshot(pending.id, {
          db_admin_username: creds.username,
          db_admin_password: creds.password,
          keep_json: options.keepJson,
        });
        const suffix = r.json_archived
          ? ' (JSON kept as archive)'
          : (r.errors && r.errors.length ? ` (${r.errors.length} file errors)` : '');
        setInfo(`Deleted ${pending.name}${suffix}.`);
      } else if (pending.kind === 'clear_server') {
        const r = await api.deleteAllSnapshotsForServer(pending.server_id, {
          db_admin_username: creds.username,
          db_admin_password: creds.password,
        });
        setInfo(
          `Cleared ${r.deleted} snapshot${r.deleted === 1 ? '' : 's'} for ${pending.server_name}` +
          (r.errors ? ` with ${r.errors} file error${r.errors === 1 ? '' : 's'}.` : '.'),
        );
      } else if (pending.kind === 'delete_archive') {
        await api.deleteLegacySnapshot(pending.name, {
          db_admin_username: creds.username,
          db_admin_password: creds.password,
        });
        setInfo(`Deleted JSON archive ${pending.name}.`);
      } else if (pending.kind === 'clear_archives') {
        const r = await api.deleteAllLegacyArchives({
          db_admin_username: creds.username,
          db_admin_password: creds.password,
        });
        setInfo(
          `Cleared ${r.deleted} JSON archive${r.deleted === 1 ? '' : 's'}` +
          (r.errors && r.errors.length
            ? ` with ${r.errors.length} file error${r.errors.length === 1 ? '' : 's'}.`
            : '.'),
        );
      }
      setPending(null);
      await refresh();
    } catch (e) {
      setError(String(e));
      throw e;  // keep the modal open
    }
  };

  // Group snapshots by server for the per-server section + bulk button.
  const grouped: Record<string, { server_name: string; rows: Snapshot[] }> = {};
  if (snapshots) {
    for (const s of snapshots) {
      const key = s.server_id || '__unknown__';
      if (!grouped[key]) {
        grouped[key] = { server_name: s.server_name || '(unknown server)', rows: [] };
      }
      grouped[key].rows.push(s);
    }
  }
  const groupKeys = Object.keys(grouped).sort((a, b) => {
    return grouped[a].server_name.localeCompare(grouped[b].server_name);
  });

  return (
    <>
      {error && <div className="banner error">{error}</div>}
      {info && <div className="banner good">{info}</div>}

      <div className="panel">
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <h2 style={{ margin: 0 }}>Snapshots</h2>
          <button onClick={refresh}>Refresh</button>
        </div>

        {snapshots === null ? (
          <div className="empty">Loading…</div>
        ) : snapshots.length === 0 ? (
          <div className="empty">
            No snapshots captured yet. Run a snapshot job from{' '}
            <strong>Run Job</strong> to create one, or check the
            Legacy section below if you have pre-rename
            <code> .plexexport.json</code> files.
          </div>
        ) : (
          groupKeys.map((sid) => (
            <ServerGroup
              key={sid}
              serverId={sid}
              serverName={grouped[sid].server_name}
              rows={grouped[sid].rows}
              downloadingUrl={downloading}
              generatingTick={generatingTick}
              onDownloadJson={(snap) =>
                void downloadBlob(
                  api.snapshotDownloadUrl(snap.id),
                  `${snap.snapshot_name}.plexexport.json`,
                )
              }
              onDownloadDb={(snap) =>
                void downloadBlob(
                  api.snapshotDbDownloadUrl(snap.id),
                  `${snap.snapshot_name}.db`,
                )
              }
              onDelete={(snap) =>
                setPending({
                  kind: 'delete_snapshot',
                  id: snap.id,
                  name: snap.snapshot_name,
                  // Keep-JSON is only offered when the .db is still
                  // on disk - otherwise there's nothing to render
                  // from. Dead rows already disable the Delete button
                  // via the row-level guard above (Remove entry path).
                  canKeepJson: snap.available,
                })
              }
              onClearAll={() =>
                setPending({
                  kind: 'clear_server',
                  server_id: sid,
                  server_name: grouped[sid].server_name,
                  count: grouped[sid].rows.length,
                })
              }
            />
          ))
        )}
      </div>

      {/* JSON archives panel - standalone .plexexport.json files in
          <output_dir>/legacy/. Populated by the keep-JSON path during
          snapshot delete and by any pre-PR-13 files relocated at
          startup. Hidden entirely when empty. */}
      {archives.length > 0 && (
        <ArchiveSection
          items={archives}
          downloadingUrl={downloading}
          onDownload={(item) =>
            void downloadBlob(api.legacySnapshotDownloadUrl(item.name), item.name)
          }
          onDelete={(item) => setPending({ kind: 'delete_archive', name: item.name })}
          onClearAll={() => setPending({ kind: 'clear_archives', count: archives.length })}
        />
      )}

      {pending && (
        <DbAdminAuthModal
          action={describePending(pending)}
          onCancel={() => setPending(null)}
          onSubmit={submitDestructive}
          showKeepJson={pending.kind === 'delete_snapshot' && pending.canKeepJson}
          keepJsonHint={
            pending.kind === 'delete_snapshot' && pending.canKeepJson
              ? 'Move the JSON sidecar into the JSON Archives panel instead of deleting it. If no sidecar exists yet, one is rendered from the .db before the move.'
              : undefined
          }
        />
      )}
    </>
  );
}


// ── Per-server group block ──────────────────────────────────────────────────

function ServerGroup({
  serverName,
  rows,
  downloadingUrl,
  generatingTick,
  onDownloadJson,
  onDownloadDb,
  onDelete,
  onClearAll,
}: {
  serverId: string;
  serverName: string;
  rows: Snapshot[];
  downloadingUrl: string | null;
  generatingTick: number;
  // Two distinct download actions per row. ``.db`` is the canonical
  // binary artifact (instant FileResponse); JSON is the rendered
  // sidecar (cached on first click, dual-state button label).
  onDownloadJson: (snap: Snapshot) => void;
  onDownloadDb: (snap: Snapshot) => void;
  onDelete: (snap: Snapshot) => void;
  onClearAll: () => void;
}) {
  return (
    <div style={{ marginTop: 16 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8 }}>
        <h3 style={{ margin: 0, fontSize: 14, color: 'var(--text-dim)' }}>
          {serverName}{' '}
          <span style={{ fontSize: 12, opacity: 0.7 }}>
            ({rows.length} snapshot{rows.length === 1 ? '' : 's'})
          </span>
        </h3>
        <button className="danger" onClick={onClearAll} style={{ fontSize: 12 }}>
          Clear all snapshots for this server
        </button>
      </div>
      <table className="list" style={{ width: '100%' }}>
        <thead>
          <tr>
            <th>Name</th>
            <th>Captured</th>
            <th>Libraries</th>
            <th>Users</th>
            <th title=".db file on disk - canonical binary artifact.">.db Size</th>
            <th title=".plexexport.json sidecar size on disk. Dash when not yet generated.">JSON Size</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          {rows.map((s) => {
            const dbUrl = api.snapshotDbDownloadUrl(s.id);
            const jsonUrl = api.snapshotDownloadUrl(s.id);
            const dbBusy = downloadingUrl === dbUrl;
            const jsonBusy = downloadingUrl === jsonUrl;
            // Recovered rows are reconciled orphans - the file was on
            // disk but the registry row was missing (process crashed
            // between step 4 and step 5 of the snapshot pipeline). The
            // chip flags them so the operator knows the metadata is
            // best-effort (no libraries list captured).
            const recovered = s.snapshot_name.endsWith(' (recovered)');
            // Dead rows have a registry entry but no .db on disk.
            // Both downloads disabled; Remove-entry tears the row
            // out via the existing delete endpoint (which is
            // tolerant of missing files - just drops the row).
            const dead = !s.available;
            return (
              <tr key={s.id} style={dead ? { opacity: 0.65 } : undefined}>
                <td>
                  {/* Composed display: "{server} / {libs} / {YYYY-MM-DD HH:MM}".
                      Built client-side from the registry fields the row already
                      carries, so the same string lights up for old captures
                      (with the pre-rename snapshot_name) and new ones (which
                      already bake the friendly format into snapshot_name). The
                      raw snapshot_name still appears in the row's title
                      attribute as a diagnostic. */}
                  <span title={`Internal name: ${s.snapshot_name}`}>
                    {formatSnapshotDisplay(s)}
                  </span>
                  {recovered && (
                    <span className="tag phase" style={{ marginLeft: 6, fontSize: 10 }} title="Recovered from an orphan .db on disk at startup">
                      recovered
                    </span>
                  )}
                  {dead && (
                    <span className="tag error" style={{ marginLeft: 6, fontSize: 10 }} title={`File missing: ${s.file_path}`}>
                      file missing
                    </span>
                  )}
                </td>
                <td>{new Date((s.captured_at || 0) * 1000).toLocaleString()}</td>
                <td>{(s.libraries || []).length || <em>-</em>}</td>
                <td>{s.user_count ?? <em>-</em>}</td>
                <td className="num">{dead ? <em style={{ color: 'var(--text-dim)' }}>-</em> : formatBytes(s.file_size || 0)}</td>
                <td className="num">
                  {s.sidecar_size !== null
                    ? formatBytes(s.sidecar_size)
                    : <em style={{ color: 'var(--text-dim)' }} title="JSON sidecar not generated yet - click Generate JSON to render it.">-</em>}
                </td>
                <td>
                  <div className="row-buttons">
                    {/* .db download - canonical binary artifact, no
                        rendering, instant FileResponse. Disabled on
                        dead rows where the file is gone. */}
                    <button
                      disabled={dbBusy || dead}
                      title={
                        dead
                          ? 'The .db file is no longer on disk. Use Remove entry to clean the orphan row.'
                          : 'Download the snapshot .db file - canonical binary, drop into another install\'s snapshots/ directory.'
                      }
                      onClick={() => onDownloadDb(s)}
                    >
                      {dbBusy ? 'Downloading .db…' : 'Download .db'}
                    </button>
                    {/* JSON download - rendered sidecar. Dual-state:
                        "Generate" when no cache exists yet, "Download"
                        once cached. Busy label animates while a
                        non-cached generate is in flight. */}
                    <button
                      disabled={jsonBusy || dead}
                      title={
                        dead
                          ? 'The .db file is no longer on disk - JSON cannot be rendered.'
                          : s.has_cached_sidecar
                            ? 'JSON sidecar is cached on the server - downloads instantly.'
                            : 'No JSON sidecar yet - first click renders one from the .db, then streams it. Future clicks are instant.'
                      }
                      onClick={() => onDownloadJson(s)}
                    >
                      {s.has_cached_sidecar
                        ? (jsonBusy ? 'Downloading JSON…' : 'Download JSON')
                        : (jsonBusy
                            ? GENERATING_FRAMES[generatingTick % GENERATING_FRAMES.length]
                            : 'Generate JSON')}
                    </button>
                    <button
                      className="danger"
                      onClick={() => onDelete(s)}
                      title={dead ? 'Remove the orphaned registry entry.' : `Delete ${s.snapshot_name}`}
                    >
                      {dead ? 'Remove entry' : 'Delete'}
                    </button>
                  </div>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}


// ── JSON Archives section ────────────────────────────────────────────────────
//
// Renders standalone .plexexport.json archives - either kept from a
// snapshot delete (operator checked "Keep JSON archive") or relocated
// from a pre-PR-13 layout at startup. Hidden entirely when the archive
// directory is empty, so a clean install doesn't show a no-op section.

function ArchiveSection({
  items,
  downloadingUrl,
  onDownload,
  onDelete,
  onClearAll,
}: {
  items: ExportArchive[];
  downloadingUrl: string | null;
  onDownload: (item: ExportArchive) => void;
  onDelete: (item: ExportArchive) => void;
  onClearAll: () => void;
}) {
  return (
    <div className="panel">
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
        <div>
          <h2 style={{ margin: 0 }}>
            JSON Archives{' '}
            <small style={{ color: 'var(--text-dim)' }}>
              (<code>.plexexport.json</code> files in <code>snapshots/legacy/</code>)
            </small>
          </h2>
          <span className="help" style={{ display: 'block', color: 'var(--text-dim)', fontSize: 12, marginTop: 4 }}>
            Standalone JSON exports kept around for import / portability. Files land here when you delete a snapshot with <strong>Keep JSON archive</strong> checked, or when you drop a <code>.plexexport.json</code> in <code>snapshots/legacy/</code> manually. The JobForm's <em>From JSON archive</em> option imports from this same directory.
          </span>
        </div>
        <button className="danger" onClick={onClearAll} style={{ fontSize: 12 }}>
          Clear all archives
        </button>
      </div>
      <table className="list" style={{ width: '100%', marginTop: 12 }}>
        <thead>
          <tr>
            <th>Source Server</th>
            <th>Library</th>
            <th>Filename</th>
            <th>Captured</th>
            <th>Size</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          {items.map((f) => {
            const dlUrl = api.legacySnapshotDownloadUrl(f.name);
            const dlBusy = downloadingUrl === dlUrl;
            return (
              <tr key={f.name}>
                <td title={f.source_server_url || ''}>
                  {f.source_server || <span style={{ color: 'var(--text-dim)' }}>-</span>}
                </td>
                <td>{f.library || <em>unknown</em>}</td>
                <td className="mono">{f.name}</td>
                <td className="mono">{f.captured_at || '-'}</td>
                <td className="num">{formatBytes(f.size)}</td>
                <td>
                  <div className="row-buttons">
                    <button disabled={dlBusy} onClick={() => onDownload(f)}>
                      {dlBusy ? 'Downloading…' : 'Download'}
                    </button>
                    <button className="danger" onClick={() => onDelete(f)}>
                      Delete
                    </button>
                  </div>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}


// ── Modal + helpers ────────────────────────────────────────────────────────

function describePending(p: PendingDestructive): string {
  switch (p.kind) {
    case 'delete_snapshot': return `Delete snapshot ${p.name}`;
    case 'clear_server':    return `Clear all ${p.count} snapshot(s) for ${p.server_name}`;
    case 'delete_archive':  return `Delete JSON archive ${p.name}`;
    case 'clear_archives':  return `Clear all ${p.count} JSON archive(s)`;
  }
}


function DbAdminAuthModal({
  action,
  onCancel,
  onSubmit,
  showKeepJson = false,
  keepJsonHint,
}: {
  action: string;
  onCancel: () => void;
  // ``keepJson`` is forwarded only when ``showKeepJson`` is true; the
  // caller can ignore it for non-snapshot delete flows.
  onSubmit: (
    creds: { username: string; password: string },
    options: { keepJson: boolean },
  ) => Promise<void>;
  // When true, render a "Keep JSON archive" checkbox above the
  // confirm button. Defaults off. Used by the snapshot delete flow
  // so the operator can move the .plexexport.json sidecar into the
  // JSON archives panel instead of deleting it with the .db.
  showKeepJson?: boolean;
  // Short paragraph rendered next to the checkbox to explain what
  // it does. Caller-supplied so the wording can match the specific
  // destructive action.
  keepJsonHint?: string;
}) {
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [keepJson, setKeepJson] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const canSubmit = !submitting && username.length > 0 && password.length > 0;

  const submit = async () => {
    if (!canSubmit) return;
    setError(null);
    setSubmitting(true);
    try {
      await onSubmit({ username, password }, { keepJson });
    } catch (e) {
      setError(String(e));
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
        style={{ width: 480, maxWidth: '92vw' }}
      >
        <h2 style={{ marginTop: 0 }}>Confirm with database admin</h2>
        <span className="help" style={{ display: 'block', color: 'var(--text-dim)', fontSize: 12, marginBottom: 12 }}>
          <strong>{action}</strong> requires Database Admin Account
          credentials. Set up or rotate these under
          Settings → Account Management → Database Admin Account.
        </span>
        {error && <div className="banner error">{error}</div>}
        <label className="field">
          <span className="label">Database admin username</span>
          <input
            type="text"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            autoComplete="username"
            autoFocus
          />
        </label>
        <label className="field">
          <span className="label">Database admin password</span>
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete="current-password"
          />
        </label>
        {showKeepJson && (
          <label className="switch" style={{ marginTop: 12 }}>
            <input
              type="checkbox"
              checked={keepJson}
              onChange={(e) => setKeepJson(e.target.checked)}
            />
            <span>
              Keep JSON archive
              {keepJsonHint && (
                <span className="help" style={{ display: 'block', color: 'var(--text-dim)', fontSize: 11, marginTop: 2 }}>
                  {keepJsonHint}
                </span>
              )}
            </span>
          </label>
        )}
        <div className="row-buttons" style={{ marginTop: 12 }}>
          <button className="primary" disabled={!canSubmit} onClick={submit}>
            {submitting ? 'Confirming…' : 'Confirm'}
          </button>
          <button onClick={onCancel} disabled={submitting}>Cancel</button>
        </div>
      </div>
    </div>
  );
}


function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1024 * 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`;
  return `${(n / 1024 / 1024 / 1024).toFixed(2)} GB`;
}


/**
 * Compose a snapshot's display name from the registry fields:
 * ``{server} / {lib, lib, lib} / {YYYY-MM-DD HH:MM}``.
 *
 * Mirrors ``snapshot_registry.format_snapshot_display`` on the
 * backend so the UI and any server-side log lines that reference the
 * same shape stay in sync. Built client-side rather than fetched as
 * an additional field because every input (server_name, libraries,
 * captured_at) is already on the row.
 */
function formatSnapshotDisplay(s: Snapshot): string {
  const libs = (s.libraries || []).length > 0
    ? (s.libraries || []).join(', ')
    : 'no libraries';
  const ts = s.captured_at ? new Date(s.captured_at * 1000) : null;
  const pad = (n: number) => String(n).padStart(2, '0');
  const tsLabel = ts && !isNaN(ts.getTime())
    ? `${ts.getFullYear()}-${pad(ts.getMonth() + 1)}-${pad(ts.getDate())} ${pad(ts.getHours())}:${pad(ts.getMinutes())}`
    : 'unknown time';
  return `${s.server_name || '(unknown server)'} / ${libs} / ${tsLabel}`;
}
