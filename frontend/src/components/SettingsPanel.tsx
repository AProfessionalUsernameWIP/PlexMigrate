// Settings panel — per-run defaults.
//
// Plex server connection details moved to the dedicated Servers tab
// in v0.9.0 (each registered server has its own URL + token now).
// This panel handles only the global defaults: output / log directories
// and worker counts. Any field a Run-Job form leaves blank falls back
// to whatever's saved here.

import { useEffect, useState } from 'react';
import { api, SettingsView } from '../api';

export function SettingsPanel() {
  const [view, setView] = useState<SettingsView | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [ok, setOk] = useState<string | null>(null);

  // Local mirror of the form so the user can type before saving.
  const [outputDir, setOutputDir] = useState('');
  const [logDir, setLogDir] = useState('');
  const [workers, setWorkers] = useState(16);
  const [scrobbleWorkers, setScrobbleWorkers] = useState(8);
  const [verbose, setVerbose] = useState(false);
  const [strictMatch, setStrictMatch] = useState(true);

  const load = async () => {
    try {
      const s = await api.getSettings();
      setView(s);
      setOutputDir(s.output_dir);
      setLogDir(s.log_dir);
      setWorkers(s.workers);
      setScrobbleWorkers(s.scrobble_workers);
      setVerbose(s.verbose);
      setStrictMatch(s.strict_match);
    } catch (e) {
      setError(String(e));
    }
  };
  useEffect(() => { load(); }, []);

  const save = async () => {
    setError(null);
    setOk(null);
    const patch: Record<string, unknown> = {
      output_dir: outputDir,
      log_dir: logDir,
      workers,
      scrobble_workers: scrobbleWorkers,
      verbose,
      strict_match: strictMatch,
    };
    try {
      const updated = await api.saveSettings(patch);
      setView(updated);
      setOk('Settings saved.');
    } catch (e) {
      setError(String(e));
    }
  };

  if (!view) {
    return <div className="panel"><div className="empty">Loading…</div></div>;
  }

  return (
    <>
      {error && <div className="banner error">{error}</div>}
      {ok && <div className="banner good">{ok}</div>}

      <div className="panel">
        <h2>Default Paths</h2>
        <div className="banner info" style={{ fontSize: 12, marginBottom: 12 }}>
          <strong>Docker note:</strong> these paths are inside the backend container, not on your host.
          The defaults <code>./plex_exports</code> and <code>./plex_logs</code> are bind-mounted in
          <code> docker-compose.yml</code> so they appear on the host too. To use an external drive or
          a NAS (e.g. <code>Y:\plexbackups</code>), add a bind mount in <code>docker-compose.yml</code>
          first — Windows host paths typed here will be rejected, because the Linux container has no
          drive letters. See the commented examples at the bottom of <code>docker-compose.yml</code>.
        </div>
        <label className="field">
          <span className="label">Output directory</span>
          <span className="help">Where exports land by default. Job forms can override per-run. Must be a container-visible path (e.g. <code>./plex_exports</code> or <code>/app/nas_exports</code>).</span>
          <input type="text" value={outputDir} onChange={(e) => setOutputDir(e.target.value)} />
        </label>
        <label className="field">
          <span className="label">Log directory</span>
          <span className="help">Where per-run log subdirectories are created. Job forms can override per-run. Same container-path constraint as above.</span>
          <input type="text" value={logDir} onChange={(e) => setLogDir(e.target.value)} />
        </label>
      </div>

      <div className="panel">
        <h2>Default Performance &amp; Behaviour</h2>
        <div className="grid-2">
          <label className="field">
            <span className="label">Worker threads</span>
            <span className="help">Default value for <code>--workers</code>.</span>
            <input type="number" min={1} max={128} value={workers} onChange={(e) => setWorkers(Number(e.target.value))} />
          </label>
          <label className="field">
            <span className="label">Scrobble workers</span>
            <span className="help">Default value for <code>--scrobble-workers</code>.</span>
            <input type="number" min={1} max={64} value={scrobbleWorkers} onChange={(e) => setScrobbleWorkers(Number(e.target.value))} />
          </label>
        </div>
        <label className="switch">
          <input type="checkbox" checked={verbose} onChange={(e) => setVerbose(e.target.checked)} />
          <span>Verbose logging by default</span>
          <span className="help">DEBUG-level console and run log output. Equivalent to <code>--verbose</code>.</span>
        </label>
        <label className="switch">
          <input type="checkbox" checked={strictMatch} onChange={(e) => setStrictMatch(e.target.checked)} />
          <span>Strict match by default</span>
          <span className="help">When unchecked, behaves like <code>--no-strict-match</code> — uses the first fuzzy result on ambiguity.</span>
        </label>
      </div>

      <div className="panel">
        <button className="primary" onClick={save}>Save Settings</button>
      </div>
    </>
  );
}
