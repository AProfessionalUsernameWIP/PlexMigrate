// Job submission form (v0.9.0).
//
// Three modes:
//   * Export  — pick source server, pick libraries, write JSON files.
//   * Import  — pick destination server, pick existing backup files,
//               merge into Plex.
//   * Direct  — pick source AND destination servers side-by-side,
//               pick libraries, transfer in memory without an
//               intermediate file. Source and destination must
//               be different.
//
// Every CLI flag from plexmigrate.py has a clearly labelled form
// control. The form does not submit the Plex URL or token directly
// — those live in the registry the user manages from the Servers tab.

import { useEffect, useRef, useState } from 'react';
import { api, ExportFile, LibraryDescriptor, PingResult, ServerView, SnapshotMessage } from '../api';

// v0.9.1: live status indicator polling cadence for the server pickers.
const PING_INTERVAL_MS = 30_000;

interface Props {
  snapshot: SnapshotMessage | null;
}

type Mode = 'export' | 'import' | 'direct';

export function JobFormPanel({ snapshot }: Props) {
  const [mode, setMode] = useState<Mode>('export');

  // Registry-aware server selection.
  const [servers, setServers] = useState<ServerView[]>([]);
  const [sourceServerName, setSourceServerName] = useState<string>('');
  const [destServerName, setDestServerName] = useState<string>('');
  const [serversError, setServersError] = useState<string | null>(null);

  // v0.9.1: live ping results keyed by server id. The selectors below
  // read this to render a status dot and latency next to each option.
  // Kept separate from ``servers`` so a ping refresh doesn't trigger
  // the library-fetch effect (which depends on ``servers``).
  const [pings, setPings] = useState<Record<string, PingResult>>({});
  const pollTimerRef = useRef<number | null>(null);

  // Library picker (export + direct).
  const [libraries, setLibraries] = useState<LibraryDescriptor[]>([]);
  const [selectedLibs, setSelectedLibs] = useState<Set<string>>(new Set());
  const [librariesError, setLibrariesError] = useState<string | null>(null);

  // Backup file picker (import only).
  const [exports, setExports] = useState<ExportFile[]>([]);
  const [selectedFiles, setSelectedFiles] = useState<Set<string>>(new Set());

  // Common engine flags.
  const [workers, setWorkers] = useState<string>('');
  const [scrobbleWorkers, setScrobbleWorkers] = useState<string>('');
  const [verbose, setVerbose] = useState(false);
  const [outputDir, setOutputDir] = useState<string>('');
  const [logDir, setLogDir] = useState<string>('');

  // Import-only flags.
  const [strictMatch, setStrictMatch] = useState(true);
  const [overwritePlaylists, setOverwritePlaylists] = useState(false);
  const [remapOld, setRemapOld] = useState('');
  const [remapNew, setRemapNew] = useState('');

  // Submission state.
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [submitOk, setSubmitOk] = useState<string | null>(null);

  // Load registered servers on mount.
  // v0.9.1 change: do NOT pre-select source/destination. The previous
  // code auto-selected the first registered server, which is exactly
  // the failure mode the user reported — operations silently used a
  // server the user never picked. The new flow forces the user to
  // make a deliberate selection (and the gated lower panels make this
  // visible).
  useEffect(() => {
    setServersError(null);
    api.listServers()
      .then((rows) => {
        setServers(rows);
      })
      .catch((e) => setServersError(String(e)));
  }, []);

  // v0.9.1: poll each registered server's status every 30 s so the
  // dots in the source/destination selectors stay current. Pings are
  // independent and cheap (single HTTP GET against /identity), so we
  // run them all in parallel each tick.
  useEffect(() => {
    if (servers.length === 0) {
      if (pollTimerRef.current !== null) {
        window.clearInterval(pollTimerRef.current);
        pollTimerRef.current = null;
      }
      return;
    }
    const pingAll = async () => {
      const tasks = servers.map(async (s) => {
        try {
          const result = await api.pingServer(s.id);
          setPings((prev) => ({ ...prev, [s.id]: result }));
        } catch {
          // Drop silently — the next tick retries.
        }
      });
      await Promise.allSettled(tasks);
    };
    pingAll();
    if (pollTimerRef.current !== null) window.clearInterval(pollTimerRef.current);
    pollTimerRef.current = window.setInterval(pingAll, PING_INTERVAL_MS);
    return () => {
      if (pollTimerRef.current !== null) {
        window.clearInterval(pollTimerRef.current);
        pollTimerRef.current = null;
      }
    };
  }, [servers]);

  // Whenever the source server selection changes (and we're in a mode
  // that reads from source) refresh the library list against that
  // server's cache. We hit /api/servers/{id}/libraries which forces a
  // live re-probe; the cached result on the registry row is the same
  // value but might be stale if the server was added a long time ago.
  useEffect(() => {
    if (mode === 'import') return;
    const srv = servers.find((s) => s.name === sourceServerName);
    if (!srv) {
      setLibraries([]);
      return;
    }
    setLibrariesError(null);
    api.listServerLibraries(srv.id)
      .then((libs) => setLibraries(libs))
      .catch((e) => setLibrariesError(String(e)));
  }, [sourceServerName, mode, servers]);

  // Load existing export files when import mode is active.
  useEffect(() => {
    if (mode !== 'import') return;
    api.listExports().then(setExports).catch(() => setExports([]));
  }, [mode]);

  const jobRunning = !!snapshot?.job && snapshot.job.state === 'running';

  // ── Submit handler ────────────────────────────────────────────────
  const submit = async () => {
    setSubmitError(null);
    setSubmitOk(null);
    setSubmitting(true);
    try {
      if (mode === 'export') {
        if (!sourceServerName) throw new Error('Pick a source server first.');
        const payload: Record<string, unknown> = {
          source_server_name: sourceServerName,
          libraries: Array.from(selectedLibs),
        };
        if (outputDir) payload.output_dir = outputDir;
        if (workers) payload.workers = Number(workers);
        if (scrobbleWorkers) payload.scrobble_workers = Number(scrobbleWorkers);
        if (logDir) payload.log_dir = logDir;
        payload.verbose = verbose;
        const r = await api.submitExport(payload);
        setSubmitOk(`Export job ${r.job_id} queued.`);
      } else if (mode === 'import') {
        if (!destServerName) throw new Error('Pick a destination server first.');
        const payload: Record<string, unknown> = {
          dest_server_name: destServerName,
          input_files: Array.from(selectedFiles),
          strict_match: strictMatch,
          overwrite_playlists: overwritePlaylists,
        };
        if (workers) payload.workers = Number(workers);
        if (scrobbleWorkers) payload.scrobble_workers = Number(scrobbleWorkers);
        if (logDir) payload.log_dir = logDir;
        payload.verbose = verbose;
        if (remapOld && remapNew) {
          payload.remap_old = remapOld;
          payload.remap_new = remapNew;
        }
        const r = await api.submitImport(payload);
        setSubmitOk(`Import job ${r.job_id} queued.`);
      } else {
        // direct
        if (!sourceServerName || !destServerName) throw new Error('Pick a source and destination server.');
        if (sourceServerName === destServerName) throw new Error('Source and destination must be different.');
        const payload: Record<string, unknown> = {
          source_server_name: sourceServerName,
          dest_server_name: destServerName,
          libraries: Array.from(selectedLibs),
          strict_match: strictMatch,
        };
        if (workers) payload.workers = Number(workers);
        if (scrobbleWorkers) payload.scrobble_workers = Number(scrobbleWorkers);
        if (logDir) payload.log_dir = logDir;
        payload.verbose = verbose;
        if (remapOld && remapNew) {
          payload.remap_old = remapOld;
          payload.remap_new = remapNew;
        }
        const r = await api.submitDirect(payload);
        setSubmitOk(`Direct transfer ${r.job_id} queued.`);
      }
    } catch (e) {
      setSubmitError(String(e));
    } finally {
      setSubmitting(false);
    }
  };

  const toggleLib = (name: string) => {
    setSelectedLibs((prev) => {
      const next = new Set(prev);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      return next;
    });
  };
  const selectAllLibs = () => setSelectedLibs(new Set(libraries.map((l) => l.name)));
  const clearLibs = () => setSelectedLibs(new Set());

  const toggleFile = (name: string) => {
    setSelectedFiles((prev) => {
      const next = new Set(prev);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      return next;
    });
  };

  return (
    <>
      {submitError && <div className="banner error">{submitError}</div>}
      {submitOk && <div className="banner good">{submitOk}</div>}
      {jobRunning && (
        <div className="banner info">
          A job is currently running. Submitting will queue this job to run after the current one finishes.
        </div>
      )}
      {serversError && (
        <div className="banner error">Could not load servers: {serversError}</div>
      )}
      {servers.length === 0 && !serversError && (
        <div className="banner info">
          No Plex servers registered yet. Open the <strong>Servers</strong> tab to add one before submitting a job.
        </div>
      )}

      {/* ── Server selection area (v0.9.1) ───────────────────────────
            Per spec, the server selectors live at the top of the page
            and gate everything below. The selectors are side-by-side
            with status indicators next to each option; unreachable
            servers still appear but are visually marked as offline.
            For export-only jobs the destination selector is hidden
            and replaced with a note pointing at the output directory.

            All sections below this panel are wrapped in a fieldset
            that goes disabled until the selection prerequisites are
            satisfied for the current mode.                            */}
      <div className="panel">
        <h2>Mode &amp; Servers</h2>
        <label className="field">
          <span className="label">Operation</span>
          <span className="help">
            <strong>Export</strong> writes JSON files. <strong>Import</strong> reads JSON files into Plex.
            <strong> Direct transfer</strong> reads from one Plex and writes straight to another with no
            intermediate file (with automatic chained fallback if the direct path is unavailable).
          </span>
          <select value={mode} onChange={(e) => setMode(e.target.value as Mode)}>
            <option value="export">Export — save data from a Plex server</option>
            <option value="import">Import — restore data to a Plex server</option>
            <option value="direct">Direct transfer — read from one server, write to another</option>
          </select>
        </label>

        <div className="grid-2">
          {/* ── Source selector (left) ──────────────────────────── */}
          {mode === 'import' ? (
            <div className="field">
              <span className="label">Source Server</span>
              <span className="help">
                Not applicable for import — the source is the backup file(s) you pick below.
              </span>
            </div>
          ) : (
            <div className="field">
              <span className="label">Source Server</span>
              <span className="help">
                The Plex server this operation will read from. Status indicators are refreshed every 30 seconds.
              </span>
              <ServerPicker
                value={sourceServerName}
                onChange={setSourceServerName}
                servers={servers}
                pings={pings}
                excludeName={mode === 'direct' ? destServerName : undefined}
              />
            </div>
          )}

          {/* ── Destination selector (right) ─────────────────────── */}
          {mode === 'export' ? (
            <div className="field">
              <span className="label">Destination Server</span>
              <span className="help">
                Not applicable for export — the export will be saved to the configured output directory
                (see <strong>Settings</strong> or override below).
              </span>
            </div>
          ) : (
            <div className="field">
              <span className="label">Destination Server</span>
              <span className="help">
                The Plex server this operation will write into. Additive merge rules apply — nothing on the
                destination is ever deleted or reduced.
              </span>
              <ServerPicker
                value={destServerName}
                onChange={setDestServerName}
                servers={servers}
                pings={pings}
                excludeName={mode === 'direct' ? sourceServerName : undefined}
              />
            </div>
          )}
        </div>

        {/* Selection-state indicator helps the user understand why the
            lower panels are disabled when they're disabled.            */}
        {!serversReady(mode, sourceServerName, destServerName) && (
          <div className="banner info" style={{ marginTop: 8 }}>
            {mode === 'export' && 'Pick a Source Server to continue.'}
            {mode === 'import' && 'Pick a Destination Server to continue.'}
            {mode === 'direct' && 'Pick both a Source Server and a Destination Server to continue.'}
          </div>
        )}
      </div>

      {/* ── Gate everything below until the selectors are satisfied ───
           A disabled <fieldset> non-interactively greys out every form
           control inside it without otherwise changing the layout. */}
      <fieldset
        className="job-form-gate"
        disabled={!serversReady(mode, sourceServerName, destServerName)}
        style={{
          border: 'none', padding: 0, margin: 0, minWidth: 0,
          opacity: serversReady(mode, sourceServerName, destServerName) ? 1 : 0.5,
          pointerEvents: serversReady(mode, sourceServerName, destServerName) ? 'auto' : 'none',
        }}
      >
      {/* ── Library / file picker ──────────────────────────────────── */}
      {(mode === 'export' || mode === 'direct') && (
        <div className="panel">
          <h2>Libraries</h2>
          {librariesError ? (
            <div className="banner error">Could not list libraries: {librariesError}.</div>
          ) : (
            <>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 6 }}>
                <span className="label">Libraries to {mode === 'export' ? 'export' : 'transfer'}</span>
                <div className="row-buttons">
                  <button onClick={selectAllLibs}>All</button>
                  <button onClick={clearLibs}>None</button>
                </div>
              </div>
              <span className="help" style={{ display: 'block', color: 'var(--text-dim)', fontSize: 12, marginBottom: 6 }}>
                Empty selection = {mode === 'export' ? 'every library on the source' : 'every library present on both servers'}.
                Mirrors <code>--libraries "Movies,TV Shows,Music"</code>.
              </span>
              <div className="checkbox-grid">
                {libraries.length === 0 ? (
                  <div className="empty">Pick a server above to load its libraries.</div>
                ) : libraries.map((lib) => (
                  <label key={lib.name} className="switch">
                    <input type="checkbox" checked={selectedLibs.has(lib.name)} onChange={() => toggleLib(lib.name)} />
                    <span>{lib.name}</span>
                    <span className="help">({lib.type}, {lib.count.toLocaleString()})</span>
                  </label>
                ))}
              </div>
            </>
          )}
        </div>
      )}

      {mode === 'import' && (
        <div className="panel">
          <h2>Backup Files</h2>
          <span className="help" style={{ display: 'block', color: 'var(--text-dim)', fontSize: 12, marginBottom: 6 }}>
            Mirrors <code>--input-file</code>. Lists every <code>.plexbackup.json</code> in the output directory.
          </span>
          <div className="checkbox-grid">
            {exports.length === 0 ? (
              <div className="empty">No export files found. Run an export first.</div>
            ) : exports.map((f) => (
              <label key={f.name} className="switch">
                <input type="checkbox" checked={selectedFiles.has(f.name)} onChange={() => toggleFile(f.name)} />
                <span>{f.library ?? f.name}</span>
                <span className="help">{f.name}</span>
              </label>
            ))}
          </div>
        </div>
      )}

      {/* ── Output / Path remap ──────────────────────────────────── */}
      {mode === 'export' && (
        <div className="panel">
          <h2>Output Location</h2>
          <label className="field">
            <span className="label">Output directory</span>
            <span className="help">Where the <code>.plexbackup.json</code> files will be written. Mirrors <code>--output-dir</code>. Leave blank to use the default from Settings.</span>
            <input type="text" value={outputDir} onChange={(e) => setOutputDir(e.target.value)} placeholder="./plex_exports" />
          </label>
        </div>
      )}

      {(mode === 'import' || mode === 'direct') && (
        <div className="panel">
          <h2>Path Remap (cross-platform migrations)</h2>
          <span className="help" style={{ display: 'block', color: 'var(--text-dim)', fontSize: 12, marginBottom: 8 }}>
            Only needed when the media root path on the destination server differs from the stored path
            (e.g. exporting from Windows <code>C:\Media</code>, importing on Linux <code>/mnt/plex</code>).
            Suffix matching handles most cases automatically. Mirrors <code>--remap-path OLD NEW</code>.
          </span>
          <div className="grid-2">
            <label className="field">
              <span className="label">Old root prefix</span>
              <input type="text" value={remapOld} onChange={(e) => setRemapOld(e.target.value)} placeholder="C:\Media\" />
            </label>
            <label className="field">
              <span className="label">New root prefix</span>
              <input type="text" value={remapNew} onChange={(e) => setRemapNew(e.target.value)} placeholder="/mnt/plex/" />
            </label>
          </div>
        </div>
      )}

      {/* ── Performance + behaviour ──────────────────────────────────── */}
      <div className="panel">
        <h2>Resolution &amp; Performance</h2>
        {mode === 'direct' && (
          <div className="banner info">
            Direct transfers hit two Plex servers simultaneously. Start with about <strong>half</strong> the default worker count
            and watch the Failed counter on the dashboard — if it climbs, lower workers further.
          </div>
        )}
        <div className="grid-2">
          <label className="field">
            <span className="label">Worker threads</span>
            <span className="help">How many threads run in parallel. Mirrors <code>--workers</code>. Blank = use default from Settings.</span>
            <input type="number" min={1} max={128} value={workers} onChange={(e) => setWorkers(e.target.value)} placeholder="(default)" />
          </label>
          <label className="field">
            <span className="label">Scrobble workers</span>
            <span className="help">Max simultaneous view-count writes during import or direct transfer. Mirrors <code>--scrobble-workers</code>.</span>
            <input type="number" min={1} max={64} value={scrobbleWorkers} onChange={(e) => setScrobbleWorkers(e.target.value)} placeholder="(default)" />
          </label>
        </div>
        {(mode === 'import' || mode === 'direct') && (
          <>
            <label className="switch">
              <input type="checkbox" checked={strictMatch} onChange={(e) => setStrictMatch(e.target.checked)} />
              <span>Strict match</span>
              <span className="help">Require exactly one fuzzy title match (default). Unchecking is equivalent to <code>--no-strict-match</code>.</span>
            </label>
            {mode === 'import' && (
              <label className="switch">
                <input type="checkbox" checked={overwritePlaylists} onChange={(e) => setOverwritePlaylists(e.target.checked)} />
                <span>Overwrite playlists</span>
                <span className="help">Mirrors <code>--overwrite-playlists</code>. No-op for backward compat — all imports are additive since v0.2.0.</span>
              </label>
            )}
          </>
        )}
      </div>

      {/* ── Logging ──────────────────────────────────────────────── */}
      <div className="panel">
        <h2>Logging</h2>
        <label className="switch">
          <input type="checkbox" checked={verbose} onChange={(e) => setVerbose(e.target.checked)} />
          <span>Verbose logging</span>
          <span className="help">DEBUG-level output. Mirrors <code>--verbose</code>.</span>
        </label>
        <label className="field">
          <span className="label">Log directory</span>
          <span className="help">Where per-run log subdirectories are created (each is prefixed with the server name). Mirrors <code>--log-dir</code>. Blank = use default from Settings.</span>
          <input type="text" value={logDir} onChange={(e) => setLogDir(e.target.value)} placeholder="./plex_logs" />
        </label>
      </div>

      <div className="panel">
        <div className="row-buttons">
          <button className="primary" disabled={submitting || servers.length === 0} onClick={submit}>
            {submitting ? 'Submitting…' :
              mode === 'direct' ? 'Submit Direct Transfer' :
              mode === 'export' ? 'Submit Export Job' :
              'Submit Import Job'}
          </button>
        </div>
      </div>
      </fieldset>
    </>
  );
}

// ── Helpers (v0.9.1) ─────────────────────────────────────────────────────────

/**
 * Per-mode rule for whether the lower panels are interactive.
 * Export needs a source server. Import needs a destination. Direct
 * needs both, and they must be different. Mirrors the same logic
 * applied on the backend in :func:`server.jobs.JobQueue._run_*`.
 */
function serversReady(mode: Mode, src: string, dst: string): boolean {
  if (mode === 'export') return !!src;
  if (mode === 'import') return !!dst;
  return !!src && !!dst && src !== dst;
}

// ── Sub-component: server picker (v0.9.1) ───────────────────────────────────
//
// Each option appears as a clickable card with a status dot, friendly
// name, URL, and (when reachable) the current ping in milliseconds.
// Unreachable servers still appear in the list but are visually
// marked: red dot, "offline" instead of milliseconds, and a dimmed
// background. Clicking selects the server.
//
// The previous build used a native ``<select>`` which doesn't allow
// rich content inside <option>. The clickable-card list is required
// by the v0.9.1 spec ("live connection status indicator next to each
// option").

function ServerPicker(props: {
  value: string;
  onChange: (name: string) => void;
  servers: ServerView[];
  pings: Record<string, PingResult>;
  excludeName?: string;
}) {
  const { value, onChange, servers, pings, excludeName } = props;
  if (servers.length === 0) {
    return (
      <div className="empty" style={{ marginTop: 4 }}>
        No registered servers. Open the <strong>Servers</strong> tab to add one.
      </div>
    );
  }
  return (
    <div className="server-picker">
      {servers.map((s) => {
        const ping = pings[s.id];
        // Effective status: live ping result if we have one, else
        // the cached value from the registry.
        const status = ping?.status ?? s.last_status;
        const ms = ping?.response_ms ?? s.last_response_ms ?? null;
        const isReachable = status === 'ok';
        const isExcluded = excludeName === s.name;
        const isSelected = value === s.name;
        const dotClass = status === 'ok' ? 'green'
          : status === 'auth_error' || status === 'unreachable' ? 'red'
          : 'amber';
        // Compose the metadata string shown on the right of the card.
        const metaText = isExcluded
          ? '(picked as opposite)'
          : status === 'ok' && ms !== null
            ? `${ms.toFixed(0)} ms`
            : status === 'auth_error'
              ? 'auth error'
              : status === 'unreachable'
                ? 'offline'
                : '…';
        return (
          <button
            type="button"
            key={s.id}
            className={`server-card${isSelected ? ' selected' : ''}${isReachable ? '' : ' offline'}`}
            disabled={isExcluded}
            onClick={() => onChange(s.name)}
            title={ping?.detail ?? s.last_status_detail ?? ''}
          >
            <span className={`dot ${dotClass}`} />
            <span className="server-card-body">
              <span className="server-card-name">{s.name}</span>
              <span className="server-card-url">{s.url}</span>
            </span>
            <span className="server-card-meta">{metaText}</span>
          </button>
        );
      })}
    </div>
  );
}
