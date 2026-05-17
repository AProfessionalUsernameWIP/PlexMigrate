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
import { InfoTip } from './InfoTip';

type WrStrategy = 'smart' | 'force_bulk' | 'force_server_side';

export function RunDefaultsPanel() {
  const [view, setView] = useState<SettingsView | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [ok, setOk] = useState<string | null>(null);

  // Local form mirror - typed before save.
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
  // v0.13.x: concurrency tunables. Two orthogonal axes:
  //   - restoreLibraryWorkers: libraries-within-one-restore parallelism.
  //     Default 3 preserves today's hardcoded cap; lower to 1 if Plex
  //     rate-limits multi-library API bursts.
  //   - fanOutDestinationWorkers: destinations-within-one-fan-out
  //     parallelism. Default 0 = no cap (today's behavior - one thread
  //     per destination). Set to 1 to serialise destinations.
  const [restoreLibraryWorkers, setRestoreLibraryWorkers] = useState<number>(3);
  const [fanOutDestinationWorkers, setFanOutDestinationWorkers] = useState<number>(0);
  // Snapshot library concurrency. 0 = inherit the Worker threads
  // value above (today's behavior, preserved on upgrade).
  const [snapshotLibraryWorkers, setSnapshotLibraryWorkers] = useState<number>(0);

  // Feature 2: snapshot integrity validation toggles. Two flags so
  // post-capture and pre-restore behave independently. Defaults per
  // D4 / D5: after-capture ON, before-restore OFF.
  const [validateAfterCapture, setValidateAfterCapture] = useState<boolean>(true);
  const [validateBeforeRestore, setValidateBeforeRestore] = useState<boolean>(false);

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
      setRestoreLibraryWorkers(
        typeof s.restore_library_workers === 'number' && s.restore_library_workers >= 1
          ? s.restore_library_workers
          : 3,
      );
      setFanOutDestinationWorkers(
        typeof s.fan_out_destination_workers === 'number' && s.fan_out_destination_workers >= 0
          ? s.fan_out_destination_workers
          : 0,
      );
      setSnapshotLibraryWorkers(
        typeof s.snapshot_library_workers === 'number' && s.snapshot_library_workers >= 0
          ? s.snapshot_library_workers
          : 0,
      );
      const sRec = s as unknown as Record<string, unknown>;
      setValidateAfterCapture(
        typeof sRec.validate_snapshot_after_capture === 'boolean'
          ? sRec.validate_snapshot_after_capture as boolean
          : true,
      );
      setValidateBeforeRestore(
        typeof sRec.validate_snapshot_before_restore === 'boolean'
          ? sRec.validate_snapshot_before_restore as boolean
          : false,
      );
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
      restore_library_workers: Math.max(1, Math.min(16, Math.floor(Number(restoreLibraryWorkers) || 3))),
      fan_out_destination_workers: Math.max(0, Math.min(32, Math.floor(Number(fanOutDestinationWorkers) || 0))),
      snapshot_library_workers: Math.max(0, Math.min(16, Math.floor(Number(snapshotLibraryWorkers) || 0))),
      validate_snapshot_after_capture: validateAfterCapture,
      validate_snapshot_before_restore: validateBeforeRestore,
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
          <span className="label">
            Output directory
            <InfoTip topicId="output-dir" />
          </span>
          <span className="help">Where snapshots land by default. Container-visible path.</span>
          <input type="text" value={outputDir} onChange={(e) => setOutputDir(e.target.value)} />
        </label>
        <label className="field">
          <span className="label">
            Log directory
            <InfoTip topicId="log-dir" />
          </span>
          <span className="help">Where per-run log subdirectories land. Same path constraint.</span>
          <input type="text" value={logDir} onChange={(e) => setLogDir(e.target.value)} />
        </label>
      </div>

      <div className="panel">
        <h2>Default Performance &amp; Behaviour</h2>
        <div className="grid-2">
          <label className="field">
            <span className="label">
              Worker threads
              <InfoTip topicId="worker-threads" />
            </span>
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
          <span>
            Verbose logging by default
            <InfoTip topicId="verbose-logging" />
          </span>
          <span className="help">DEBUG-level output. Equivalent to <code>--verbose</code>.</span>
        </label>
        <label className="switch">
          <input type="checkbox" checked={strictMatch} onChange={(e) => setStrictMatch(e.target.checked)} />
          <span>
            Strict match by default
            <InfoTip topicId="strict-match" />
          </span>
          <span className="help">Unchecked = behaves like <code>--no-strict-match</code>.</span>
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
          <span>
            Pre-build JSON sidecar by default
            <InfoTip topicId="json-sidecar" />
          </span>
          <span className="help">
            Renders a <code>.plexexport.json</code> sidecar at capture time. Per-job toggles override.
          </span>
        </label>
        <div className="field" style={{ marginTop: 16 }}>
          <span className="label">
            Watch+Ratings capture strategy
            <InfoTip topicId="watch-ratings-strategy" />
          </span>
          <span className="help">
            How the owner phase fetches items. Per-server overrides take precedence.
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
                Engine picks per library - bulk when both watch+ratings wanted, server-side filter when only one. Recommended.
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
        <h2>Snapshot Integrity Validation</h2>
        <span className="help" style={{ display: 'block', color: 'var(--text-dim)', fontSize: 12, marginBottom: 8 }}>
          Structural validator runs against a temp copy of the snapshot
          <code>.db</code>; the original file is never modified. Errors abort the
          surrounding job; warnings log without aborting.
        </span>
        <label className="switch">
          <input
            type="checkbox"
            checked={validateAfterCapture}
            onChange={(e) => setValidateAfterCapture(e.target.checked)}
          />
          <span>
            Validate after capture <em>(default on)</em>
            <InfoTip topicId="validate-after-capture" />
          </span>
          <span className="help">
            Cheap structural check at the end of capture. Catches malformed snapshots at source.
          </span>
        </label>
        <label className="switch" style={{ marginTop: 8 }}>
          <input
            type="checkbox"
            checked={validateBeforeRestore}
            onChange={(e) => setValidateBeforeRestore(e.target.checked)}
          />
          <span>
            Validate before restore
            <InfoTip topicId="validate-before-restore" />
          </span>
          <span className="help">
            Checks the snapshot <code>.db</code> when a restore-from-snapshot
            request is submitted. Adds wall time to every restore; turn on for
            belt-and-suspenders against a snapshot that was hand-edited or
            corrupted in transit.
          </span>
        </label>
      </div>

      <div className="panel">
        <h2>
          Restore Defaults
          <InfoTip topicId="restore-default-mode" />
        </h2>
        <span className="help" style={{ display: 'block', color: 'var(--text-dim)', fontSize: 12, marginBottom: 8 }}>
          Default mode for restore + direct-transfer jobs. Per-job and per-server overrides take precedence.
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

      {/* v0.13.x: Concurrency tunables. Two orthogonal axes that let
          the end user pull back from default parallelism when Plex
          rate-limits the multi-library / multi-destination API bursts.
          Both default to today's behavior so an unchanged install is a
          no-op. */}
      <div className="panel">
        <h2>Concurrency</h2>
        <div className="banner info" style={{ fontSize: 12, marginBottom: 12 }}>
          Pull these down when Plex rate-limits the multi-library / multi-destination
          API bursts. Defaults preserve today's behavior; lowering them trades
          wall-clock time for fewer concurrent API calls.
        </div>
        <label className="field">
          <span className="label">Snapshot: libraries in parallel</span>
          <span className="help">
            How many libraries the snapshot pipeline gathers at once.{' '}
            <strong>0</strong> (default) inherits from <em>Worker threads</em> above -
            today's coupled behavior preserved on upgrade. A positive value caps
            libraries-in-parallel <em>independently</em> of the per-library HTTP
            worker count, so you can serialise snapshot libraries (<strong>1</strong>)
            without throttling the within-library workers that fetch from Plex.
          </span>
          <input
            type="number"
            min={0}
            max={16}
            value={snapshotLibraryWorkers}
            onChange={(e) => setSnapshotLibraryWorkers(Number(e.target.value))}
          />
        </label>
        <label className="field">
          <span className="label">
            Restore: libraries in parallel
            <InfoTip topicId="restore-library-workers" />
          </span>
          <span className="help">
            Default <strong>3</strong>. Set to 1 to serialise (helps with 429s).
          </span>
          <input
            type="number"
            min={1}
            max={16}
            value={restoreLibraryWorkers}
            onChange={(e) => setRestoreLibraryWorkers(Number(e.target.value))}
          />
        </label>
        <label className="field">
          <span className="label">
            Fan-out: destinations in parallel
            <InfoTip topicId="fanout-workers" />
          </span>
          <span className="help">
            <strong>0</strong> (default) = no cap. <strong>1</strong> serialises destinations.
          </span>
          <input
            type="number"
            min={0}
            max={32}
            value={fanOutDestinationWorkers}
            onChange={(e) => setFanOutDestinationWorkers(Number(e.target.value))}
          />
        </label>
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
          <span>
            Allow filepath suffix fallback (Tier 2)
            <InfoTip topicId="filepath-fallback" />
          </span>
          <span className="help">
            Fall back to trailing path components when GUID lookup misses. Default <strong>on</strong>.
          </span>
        </label>
        <label className="switch">
          <input
            type="checkbox"
            checked={allowFuzzyFallback}
            onChange={(e) => setAllowFuzzyFallback(e.target.checked)}
          />
          <span>
            Allow fuzzy title fallback (Tier 3)
            <InfoTip topicId="fuzzy-fallback" />
          </span>
          <span className="help">
            Last-resort fuzzy title match. Default <strong>off</strong>: can produce wrong matches.
          </span>
        </label>
      </div>

      <div className="panel">
        <h2>Snapshot Retention</h2>
        <div className="banner info" style={{ fontSize: 12, marginBottom: 12 }}>
          Global ceiling for the number of snapshots kept per server. Per-server overrides
          (set on the <strong>Advanced Settings</strong> tab) apply only when strictly lower
          than this value - the global is a ceiling, never a floor.
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
