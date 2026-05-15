// Servers ▸ Run Defaults sub-tab.
//
// Houses the per-run defaults moved out of Settings ▸ General Settings
// in Phase 2 of the Settings/Servers reorg. These describe HOW
// snapshots and direct transfers operate against Plex (paths, worker
// counts, snapshot defaults, transfer resolver tiers, global snapshot
// retention ceiling).
//
// Per-server overrides for snapshot defaults + retention live one tab
// over on Servers ▸ Advanced Settings; the resolution chain is
// per-job → per-server → values on this page → built-in default.

import { useEffect, useState } from 'react';
import { api, SettingsView } from '../api';
import { RestoreModeSelector, RestoreMode, MergeWatchStrategy } from './RestoreModeSelector';

type WrStrategy = 'smart' | 'force_bulk' | 'force_server_side';

export function RunDefaultsPanel() {
  const [view, setView] = useState<SettingsView | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [ok, setOk] = useState<string | null>(null);

  // Local form mirror — typed before save.
  const [outputDir, setOutputDir] = useState('');
  const [logDir, setLogDir] = useState('');
  const [workers, setWorkers] = useState(16);
  const [scrobbleWorkers, setScrobbleWorkers] = useState(8);
  const [verbose, setVerbose] = useState(false);
  const [strictMatch, setStrictMatch] = useState(true);
  const [globalRetention, setGlobalRetention] = useState<number>(30);
  const [prebuildJsonSidecarDefault, setPrebuildJsonSidecarDefault] = useState<boolean>(false);
  // Watch+ratings capture strategy. "smart" = engine picks (bulk when
  // both wanted, server-side filter when only one); "force_bulk" =
  // always bulk-fetch + local filter (kindest to rate-limited Plex
  // servers); "force_server_side" = always server-side filter scans.
  const [wrStrategy, setWrStrategy] = useState<WrStrategy>('smart');
  const [allowFilepathFallback, setAllowFilepathFallback] = useState<boolean>(true);
  const [allowFuzzyFallback, setAllowFuzzyFallback] = useState<boolean>(false);

  // v0.13.x: Restore-side defaults (Merge / Replace + auto-capture
  // safety belt). Bottom of the resolution chain - the per-job form
  // (Run Job + Schedules) wins, per-server overrides on Advanced
  // Settings come next, then these.
  const [restoreMode, setRestoreMode] = useState<RestoreMode>('merge');
  const [autoCaptureBeforeReplace, setAutoCaptureBeforeReplace] = useState<boolean>(true);
  const [mergeWatchStrategy, setMergeWatchStrategy] = useState<MergeWatchStrategy>('higher');

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
      setGlobalRetention(
        typeof s.snapshot_retention_global === 'number' && s.snapshot_retention_global >= 1
          ? s.snapshot_retention_global
          : 30,
      );
      setPrebuildJsonSidecarDefault(s.prebuild_json_sidecar_default === true);
      const wr = s.watch_ratings_filter_strategy;
      setWrStrategy(wr === 'force_bulk' || wr === 'force_server_side' ? wr : 'smart');
      const tr = s.transfer_resolution || {};
      setAllowFilepathFallback(tr.allow_filepath_fallback !== false);
      setAllowFuzzyFallback(tr.allow_fuzzy_fallback === true);
      const rd = s.restore_defaults || {};
      setRestoreMode(rd.mode === 'replace' ? 'replace' : 'merge');
      setAutoCaptureBeforeReplace(rd.auto_capture_before_replace !== false);
      setMergeWatchStrategy(rd.merge_watch_strategy === 'sum' ? 'sum' : 'higher');
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
      snapshot_retention_global: Math.max(1, Math.floor(Number(globalRetention) || 30)),
      prebuild_json_sidecar_default: prebuildJsonSidecarDefault,
      watch_ratings_filter_strategy: wrStrategy,
      transfer_resolution: {
        allow_filepath_fallback: allowFilepathFallback,
        allow_fuzzy_fallback: allowFuzzyFallback,
      },
      restore_defaults: {
        mode: restoreMode,
        auto_capture_before_replace: autoCaptureBeforeReplace,
        merge_watch_strategy: mergeWatchStrategy,
      },
    };
    try {
      const updated = await api.saveSettings(patch);
      setView(updated);
      setOk('Run defaults saved.');
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
          The defaults <code>./snapshots</code> and <code>./plex_logs</code> are bind-mounted in
          <code> docker-compose.yml</code> so they appear on the host too. To use an external drive or
          a NAS (e.g. <code>Y:\plexexports</code>), add a bind mount in <code>docker-compose.yml</code>
          first - Windows host paths typed here will be rejected, because the Linux container has no
          drive letters. See the commented examples at the bottom of <code>docker-compose.yml</code>.
        </div>
        <label className="field">
          <span className="label">Output directory</span>
          <span className="help">Where snapshots land by default. Job forms can override per-run. Must be a container-visible path (e.g. <code>./snapshots</code> or <code>/app/nas_exports</code>).</span>
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
          <span className="help">When unchecked, behaves like <code>--no-strict-match</code> - uses the first fuzzy result on ambiguity.</span>
        </label>
      </div>

      <div className="panel">
        <h2>Snapshot Defaults</h2>
        <label className="switch">
          <input
            type="checkbox"
            checked={prebuildJsonSidecarDefault}
            onChange={(e) => setPrebuildJsonSidecarDefault(e.target.checked)}
          />
          <span>Pre-build JSON sidecar by default</span>
          <span className="help">
            Renders a <code>.plexexport.json</code> next to the snapshot <code>.db</code> at the
            end of every snapshot run. Per-job toggles in the Run Job form override this.
          </span>
        </label>
        <div className="field" style={{ marginTop: 16 }}>
          <span className="label">Watch+Ratings capture strategy</span>
          <span className="help">
            Controls how the owner phase fetches items when both watch-history and ratings
            are wanted. Per-server overrides on the <strong>Advanced Settings</strong> tab
            take precedence over this value.
          </span>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 8, marginTop: 8 }}>
            <label
              className="switch"
              title="Engine picks per library: bulk-fetch when both watch+ratings wanted (1 Plex scan), server-side filter when only one wanted (smaller payload). Recommended for most setups."
            >
              <input
                type="radio"
                name="wr-strategy"
                checked={wrStrategy === 'smart'}
                onChange={() => setWrStrategy('smart')}
              />
              <span>Smart <em>(default)</em></span>
              <span className="help">
                Engine picks per library — bulk when both watch+ratings wanted, server-side filter when only one. Recommended.
              </span>
            </label>
            <label
              className="switch"
              title="Always fetch the full library and filter locally. Best when your Plex server is rate-limited or returning 429s. Higher wire traffic, lower API-call volume."
            >
              <input
                type="radio"
                name="wr-strategy"
                checked={wrStrategy === 'force_bulk'}
                onChange={() => setWrStrategy('force_bulk')}
              />
              <span>Force bulk</span>
              <span className="help">
                Always bulk-fetch + filter locally, even for single-type runs. Best for rate-limited Plex servers (fewer API calls, larger payloads).
              </span>
            </label>
            <label
              className="switch"
              title="Always let Plex filter on its side. Best when bandwidth back from the server is the constraint. Smaller payloads, more API calls."
            >
              <input
                type="radio"
                name="wr-strategy"
                checked={wrStrategy === 'force_server_side'}
                onChange={() => setWrStrategy('force_server_side')}
              />
              <span>Force server-side</span>
              <span className="help">
                Always use server-side filter scans. Best when wire-traffic from Plex is the constraint (smaller payloads, more API calls).
              </span>
            </label>
          </div>
        </div>
      </div>

      <div className="panel">
        <h2>Restore Defaults</h2>
        <span className="help" style={{ display: 'block', color: 'var(--text-dim)', fontSize: 12, marginBottom: 8 }}>
          Default restoration mode for restore + direct-transfer jobs. The per-job
          form (Run Job, Schedules) and per-server overrides take precedence — this
          is the bottom-of-chain fallback. <strong>Merge</strong> is additive and
          safe to re-run; <strong>Replace</strong> overwrites the destination to
          match the snapshot exactly and requires typed-REPLACE confirmation on
          submit.
        </span>
        <RestoreModeSelector
          mode={restoreMode}
          autoCaptureBeforeReplace={autoCaptureBeforeReplace}
          mergeWatchStrategy={mergeWatchStrategy}
          onMergeWatchStrategyChange={setMergeWatchStrategy}
          onModeChange={setRestoreMode}
          onAutoCaptureChange={setAutoCaptureBeforeReplace}
          idPrefix="run-defaults"
        />
      </div>

      <div className="panel">
        <h2>Transfer Resolution</h2>
        <div className="banner info" style={{ fontSize: 12, marginBottom: 12 }}>
          These toggles ONLY affect direct server-to-server transfers. Snapshot and import paths
          continue to run every resolver tier regardless of what's set here.
        </div>
        <label className="switch">
          <input
            type="checkbox"
            checked={allowFilepathFallback}
            onChange={(e) => setAllowFilepathFallback(e.target.checked)}
          />
          <span>Allow filepath suffix fallback (Tier 2)</span>
          <span className="help">
            When Tiers 0/1 (DB cache + live GUID lookup) miss, fall back to matching by the last
            N components of the file path. Default <strong>on</strong> — safe for most catalogues.
          </span>
        </label>
        <label className="switch">
          <input
            type="checkbox"
            checked={allowFuzzyFallback}
            onChange={(e) => setAllowFuzzyFallback(e.target.checked)}
          />
          <span>Allow fuzzy title fallback (Tier 3)</span>
          <span className="help">
            Last-resort match by fuzzy title. Default <strong>off</strong> — can produce
            incorrect matches; enable only when you've verified your catalogue tolerates it.
          </span>
        </label>
      </div>

      <div className="panel">
        <h2>Snapshot Retention</h2>
        <div className="banner info" style={{ fontSize: 12, marginBottom: 12 }}>
          Global ceiling for the number of snapshots kept per server. Per-server overrides
          (set on the <strong>Advanced Settings</strong> tab) apply only when strictly lower
          than this value — the global is a ceiling, never a floor.
        </div>
        <label className="field">
          <span className="label">Keep at most</span>
          <span className="help">Oldest snapshots past this count are deleted automatically.</span>
          <input
            type="number"
            min={1}
            value={globalRetention}
            onChange={(e) => setGlobalRetention(Number(e.target.value))}
          />
        </label>
      </div>

      <div className="panel" style={{ display: 'flex', justifyContent: 'flex-end' }}>
        <button onClick={save} className="primary">Save Run Defaults</button>
      </div>
    </>
  );
}
