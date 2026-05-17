// ── Per-Run Settings (collapsible) ───────────────────────────────────
//
// Engine tuning, retry behaviour, path remapping, output paths,
// logging - the knobs the average end user never touches but
// occasionally needs to override per run. Collapsed by default;
// clicking the header toggles it.
//
// Two sub-tabs:
//   * General: workers, scrobble workers, strict-match,
//     overwrite-playlists, prebuild-json-sidecar, verbose logging.
//   * Advanced: output directory, log directory, path remap,
//     engine tuning (skip-playlist-prebuild, fast-collection-
//     detection), watch+ratings capture strategy override.
//
// All state is hoisted to the parent (matches Schedules' shape).
// The ``idScope`` prop disambiguates radio ``name`` attributes so
// the panel can be mounted in two places on the same document
// (e.g. Run Job and an inline Schedules editor) without the
// radios competing.

import type { ServerView } from '../api';
import { InfoTip } from './InfoTip';
import { serverSupportsFastCollections } from '../utils/plexVersion';
import type { Mode } from './ModeAndServersPanel';

export type WatchRatingsStrategy = '' | 'smart' | 'force_bulk' | 'force_server_side';
export type PerRunSubTab = 'general' | 'advanced';

// Mixed-media per-run override types. The empty string is the
// "inherit global tunable" sentinel — owner-builder converts it to
// null at submit time.
export type MixedMediaBehavior = '' | 'skip' | 'dominant' | 'split';
export type MixedMediaVideoRouting = '' | 'library_agnostic' | 'library_dominant';
export type MixedMediaLogging = '' | 'full' | 'decisions_only' | 'off';
export type MixedMediaCollisionHandling = '' | 'duplicate' | 'suffix' | 'skip';

interface Props {
  mode: Mode;
  // Source server (for the Plex-version-gated fast-collection
  // toggle). Pass ``null`` when no source server is selected; the
  // toggle disables itself with an "unknown version" badge.
  sourceServer?: ServerView | null;
  // Disambiguates radio name attributes when the panel is mounted
  // in two places on the same document. Pass a unique short string
  // per mount (e.g. 'job', 'sched-${id}').
  idScope: string;

  // Collapse + sub-tab UI state.
  open: boolean;
  onOpenChange: (next: boolean) => void;
  subTab: PerRunSubTab;
  onSubTabChange: (next: PerRunSubTab) => void;

  // General fields.
  workers: string;
  onWorkersChange: (v: string) => void;
  scrobbleWorkers: string;
  onScrobbleWorkersChange: (v: string) => void;
  strictMatch: boolean;
  onStrictMatchChange: (v: boolean) => void;
  overwritePlaylists: boolean;
  onOverwritePlaylistsChange: (v: boolean) => void;
  prebuildJsonSidecar: boolean;
  onPrebuildJsonSidecarChange: (v: boolean) => void;
  verbose: boolean;
  onVerboseChange: (v: boolean) => void;

  // Advanced fields.
  outputDir: string;
  onOutputDirChange: (v: string) => void;
  logDir: string;
  onLogDirChange: (v: string) => void;
  remapOld: string;
  onRemapOldChange: (v: string) => void;
  remapNew: string;
  onRemapNewChange: (v: string) => void;
  skipPlaylistPrebuild: boolean;
  onSkipPlaylistPrebuildChange: (v: boolean) => void;
  fastCollectionDetection: boolean;
  onFastCollectionDetectionChange: (v: boolean) => void;
  watchRatingsStrategy: WatchRatingsStrategy;
  onWatchRatingsStrategyChange: (v: WatchRatingsStrategy) => void;

  // Mixed-media playlist overrides. All five inherit from the
  // global tunable when '' (the "inherit" sentinel). The mixed-media
  // section is rendered only for restore + direct modes (snapshot
  // doesn't write playlists, so overrides have no effect).
  mixedMediaBehavior: MixedMediaBehavior;
  onMixedMediaBehaviorChange: (v: MixedMediaBehavior) => void;
  mixedMediaDominanceThreshold: string;
  onMixedMediaDominanceThresholdChange: (v: string) => void;
  mixedMediaVideoRouting: MixedMediaVideoRouting;
  onMixedMediaVideoRoutingChange: (v: MixedMediaVideoRouting) => void;
  mixedMediaLogging: MixedMediaLogging;
  onMixedMediaLoggingChange: (v: MixedMediaLogging) => void;
  mixedMediaCollisionHandling: MixedMediaCollisionHandling;
  onMixedMediaCollisionHandlingChange: (v: MixedMediaCollisionHandling) => void;
}

export function PerRunSettingsPanel(props: Props) {
  const {
    mode,
    sourceServer,
    idScope,
    open,
    onOpenChange,
    subTab,
    onSubTabChange,
    workers,
    onWorkersChange,
    scrobbleWorkers,
    onScrobbleWorkersChange,
    strictMatch,
    onStrictMatchChange,
    overwritePlaylists,
    onOverwritePlaylistsChange,
    prebuildJsonSidecar,
    onPrebuildJsonSidecarChange,
    verbose,
    onVerboseChange,
    outputDir,
    onOutputDirChange,
    logDir,
    onLogDirChange,
    remapOld,
    onRemapOldChange,
    remapNew,
    onRemapNewChange,
    skipPlaylistPrebuild,
    onSkipPlaylistPrebuildChange,
    fastCollectionDetection,
    onFastCollectionDetectionChange,
    watchRatingsStrategy,
    onWatchRatingsStrategyChange,
    mixedMediaBehavior,
    onMixedMediaBehaviorChange,
    mixedMediaDominanceThreshold,
    onMixedMediaDominanceThresholdChange,
    mixedMediaVideoRouting,
    onMixedMediaVideoRoutingChange,
    mixedMediaLogging,
    onMixedMediaLoggingChange,
    mixedMediaCollisionHandling,
    onMixedMediaCollisionHandlingChange,
  } = props;

  const wrName = `wr-strategy-${idScope}`;

  return (
    <div className="panel">
      <button
        type="button"
        onClick={() => onOpenChange(!open)}
        aria-expanded={open}
        style={{
          background: 'none', border: 'none', padding: 0,
          font: 'inherit', color: 'inherit', cursor: 'pointer',
          width: '100%', textAlign: 'left',
          display: 'flex', alignItems: 'center', gap: 8,
        }}
      >
        <span style={{ fontSize: 14, color: 'var(--text-dim)' }}>
          {open ? '▾' : '▸'}
        </span>
        <h2 style={{ margin: 0 }}>Per-Run Settings</h2>
      </button>
      <span className="help" style={{ display: 'block', color: 'var(--text-dim)', fontSize: 12, marginTop: 6 }}>
        Override what's set under <strong>Servers ▸ Run Defaults</strong> for this one
        run only. Defaults are right for most operators - start here only if a run
        misbehaves, you need cross-platform path translation, or you're tuning
        per-job for an unusual server.
      </span>

      {open && (
        <div style={{ marginTop: 12 }}>
          {/* Sub-tab strip. Switching tabs is workspace state only -
              doesn't reset any field values. */}
          <nav className="tabs sub-tabs" style={{ marginBottom: 12 }}>
            <button
              type="button"
              className={subTab === 'general' ? 'active' : ''}
              onClick={() => onSubTabChange('general')}
            >
              General
            </button>
            <button
              type="button"
              className={subTab === 'advanced' ? 'active' : ''}
              onClick={() => onSubTabChange('advanced')}
            >
              Advanced
            </button>
          </nav>

          {subTab === 'general' && (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
              {/* ── Resolution & performance ─ */}
              <div>
                <h3 style={{ marginTop: 0 }}>Resolution &amp; performance</h3>
                {mode === 'direct' && (
                  <div className="banner info">
                    Direct transfers hit two Plex servers simultaneously. Start with about <strong>half</strong> the default worker count
                    and watch the Failed counter on the dashboard - if it climbs, lower workers further.
                  </div>
                )}
                <div className="grid-2">
                  <label className="field">
                    <span className="label">
                      Worker threads
                      <InfoTip topicId="worker-threads" />
                    </span>
                    <span className="help">Parallel threads against Plex. Mirrors <code>--workers</code>. Blank = use default.</span>
                    <input type="number" min={1} max={128} value={workers} onChange={(e) => onWorkersChange(e.target.value)} placeholder="(default)" />
                  </label>
                  <label className="field">
                    <span className="label">Scrobble workers</span>
                    <span className="help">Max simultaneous view-count writes during import or direct transfer. Mirrors <code>--scrobble-workers</code>.</span>
                    <input type="number" min={1} max={64} value={scrobbleWorkers} onChange={(e) => onScrobbleWorkersChange(e.target.value)} placeholder="(default)" />
                  </label>
                </div>
                {(mode === 'restore' || mode === 'direct') && (
                  <>
                    <label className="switch">
                      <input type="checkbox" checked={strictMatch} onChange={(e) => onStrictMatchChange(e.target.checked)} />
                      <span>Strict match</span>
                      <span className="help">Require exactly one fuzzy title match (default). Unchecking is equivalent to <code>--no-strict-match</code>.</span>
                    </label>
                    {mode === 'restore' && (
                      <label className="switch">
                        <input type="checkbox" checked={overwritePlaylists} onChange={(e) => onOverwritePlaylistsChange(e.target.checked)} />
                        <span>Overwrite playlists</span>
                        <span className="help">Mirrors <code>--overwrite-playlists</code>. No-op for backward compat - all imports are additive since v0.2.0.</span>
                      </label>
                    )}
                  </>
                )}
                {mode === 'snapshot' && (
                  <label className="switch">
                    <input
                      type="checkbox"
                      checked={prebuildJsonSidecar}
                      onChange={(e) => onPrebuildJsonSidecarChange(e.target.checked)}
                    />
                    <span>Save JSON copy after snapshot</span>
                    <span className="help">
                      Writes a <code>.plexexport.json</code> file next to the snapshot
                      <code>.db</code> at the end of the run. Useful when you want a
                      portable text-format archive ready to download immediately. Off
                      by default - the JSON is otherwise rendered on first
                      <strong> Download</strong> click in the Exports tab and cached
                      from that point on. Adds wall-clock time to the run.
                    </span>
                  </label>
                )}
              </div>

              {/* ── Mixed-media playlists (restore + direct only) ─ */}
              {(mode === 'restore' || mode === 'direct') && (
                <div>
                  <h3 style={{ marginTop: 0 }}>Mixed-media playlists</h3>
                  <span className="help" style={{ display: 'block', color: 'var(--text-dim)', fontSize: 12, marginBottom: 8 }}>
                    Per-run overrides for cross-library playlists (movies + episodes mixed together, etc.).
                    Each option defaults to <strong>Inherit</strong> - takes the value set under
                    <strong> Settings ▸ Tunables ▸ Playlist ▸ Mixed-media</strong>. Touch these only when this
                    specific run needs different behavior.
                  </span>
                  <div className="grid-2">
                    <label className="field">
                      <span className="label">Behavior</span>
                      <span className="help">How the engine handles a playlist whose items span multiple media types.</span>
                      <select
                        value={mixedMediaBehavior}
                        onChange={(e) => onMixedMediaBehaviorChange(e.target.value as MixedMediaBehavior)}
                      >
                        <option value="">Inherit (default)</option>
                        <option value="skip">Skip mixed playlists</option>
                        <option value="dominant">Keep dominant media type</option>
                        <option value="split">Split per media type</option>
                      </select>
                    </label>
                    <label className="field">
                      <span className="label">Dominance threshold</span>
                      <span className="help">Fraction (0-1) required for one media type to count as dominant. Blank = inherit.</span>
                      <input
                        type="number"
                        min={0}
                        max={1}
                        step={0.05}
                        value={mixedMediaDominanceThreshold}
                        onChange={(e) => onMixedMediaDominanceThresholdChange(e.target.value)}
                        placeholder="(inherit)"
                      />
                    </label>
                    <label className="field">
                      <span className="label">Video routing</span>
                      <span className="help">Where mixed-media video items land in <strong>split</strong> mode.</span>
                      <select
                        value={mixedMediaVideoRouting}
                        onChange={(e) => onMixedMediaVideoRoutingChange(e.target.value as MixedMediaVideoRouting)}
                      >
                        <option value="">Inherit (default)</option>
                        <option value="library_agnostic">Library-agnostic</option>
                        <option value="library_dominant">Library-dominant</option>
                      </select>
                    </label>
                    <label className="field">
                      <span className="label">Collision handling</span>
                      <span className="help">What happens when a split-out playlist name already exists on the destination.</span>
                      <select
                        value={mixedMediaCollisionHandling}
                        onChange={(e) => onMixedMediaCollisionHandlingChange(e.target.value as MixedMediaCollisionHandling)}
                      >
                        <option value="">Inherit (default)</option>
                        <option value="duplicate">Allow duplicate</option>
                        <option value="suffix">Add suffix</option>
                        <option value="skip">Skip on collision</option>
                      </select>
                    </label>
                    <label className="field">
                      <span className="label">Logging</span>
                      <span className="help">Verbosity for mixed-media decisions in the run log.</span>
                      <select
                        value={mixedMediaLogging}
                        onChange={(e) => onMixedMediaLoggingChange(e.target.value as MixedMediaLogging)}
                      >
                        <option value="">Inherit (default)</option>
                        <option value="full">Full</option>
                        <option value="decisions_only">Decisions only</option>
                        <option value="off">Off</option>
                      </select>
                    </label>
                  </div>
                </div>
              )}

              {/* ── Logging ─ */}
              <div>
                <h3 style={{ marginTop: 0 }}>Logging</h3>
                <label className="switch">
                  <input type="checkbox" checked={verbose} onChange={(e) => onVerboseChange(e.target.checked)} />
                  <span>Verbose logging</span>
                  <span className="help">DEBUG-level output. Mirrors <code>--verbose</code>.</span>
                </label>
              </div>
            </div>
          )}

          {subTab === 'advanced' && (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
              {/* ── Output location ─ */}
              {mode === 'snapshot' && (
                <div>
                  <h3 style={{ marginTop: 0 }}>Output location</h3>
                  <label className="field">
                    <span className="label">Output directory</span>
                    <span className="help">Where the <code>.plexexport.json</code> files will be written. Mirrors <code>--output-dir</code>. Leave blank to use the default from Run Defaults.</span>
                    <input type="text" value={outputDir} onChange={(e) => onOutputDirChange(e.target.value)} placeholder="./snapshots" />
                  </label>
                  <label className="field">
                    <span className="label">Log directory</span>
                    <span className="help">Where per-run log subdirectories are created. Mirrors <code>--log-dir</code>. Blank = use default from Run Defaults.</span>
                    <input type="text" value={logDir} onChange={(e) => onLogDirChange(e.target.value)} placeholder="./plex_logs" />
                  </label>
                </div>
              )}
              {mode !== 'snapshot' && (
                <div>
                  <h3 style={{ marginTop: 0 }}>Log location</h3>
                  <label className="field">
                    <span className="label">Log directory</span>
                    <span className="help">Where per-run log subdirectories are created. Mirrors <code>--log-dir</code>. Blank = use default from Run Defaults.</span>
                    <input type="text" value={logDir} onChange={(e) => onLogDirChange(e.target.value)} placeholder="./plex_logs" />
                  </label>
                </div>
              )}

              {/* ── Path remap ─ */}
              {(mode === 'restore' || mode === 'direct') && (
                <div>
                  <h3 style={{ marginTop: 0 }}>Path remap (cross-platform migrations)</h3>
                  <span className="help" style={{ display: 'block', color: 'var(--text-dim)', fontSize: 12, marginBottom: 8 }}>
                    Only needed when the media root path on the destination server differs from the stored path
                    (e.g. exporting from Windows <code>C:\Media</code>, importing on Linux <code>/mnt/plex</code>).
                    Suffix matching handles most cases automatically. Mirrors <code>--remap-path OLD NEW</code>.
                  </span>
                  <div className="grid-2">
                    <label className="field">
                      <span className="label">Old root prefix</span>
                      <input type="text" value={remapOld} onChange={(e) => onRemapOldChange(e.target.value)} placeholder="C:\Media\" />
                    </label>
                    <label className="field">
                      <span className="label">New root prefix</span>
                      <input type="text" value={remapNew} onChange={(e) => onRemapNewChange(e.target.value)} placeholder="/mnt/plex/" />
                    </label>
                  </div>
                </div>
              )}

              {/* ── Engine tuning (snapshot/direct) ─ */}
              {(mode === 'snapshot' || mode === 'direct') && (
                <div>
                  <h3 style={{ marginTop: 0 }}>Engine tuning</h3>
                  <label className="switch">
                    <input type="checkbox" checked={skipPlaylistPrebuild} onChange={(e) => onSkipPlaylistPrebuildChange(e.target.checked)} />
                    <span>Skip playlist pre-building</span>
                    <span className="help">
                      Skip the parallel upfront fetch that loads all playlists and their items
                      before snapshot starts. Playlists still snapshot correctly - the data is fetched
                      lazily the first time each server needs it, and the result is shared so each
                      server is still only fetched once per run. Use this to eliminate the
                      "Warming playlist cache" stall at job start without losing any playlist data.
                    </span>
                  </label>
                  {(() => {
                    const ver = sourceServer?.plex_version ?? '';
                    const supported = serverSupportsFastCollections(ver);
                    const unknown = !ver;
                    const disabled = !supported;
                    return (
                      <label
                        className="switch"
                        style={{ opacity: disabled ? 0.55 : 1 }}
                        title={
                          disabled
                            ? unknown
                              ? 'Plex version unknown for this server - refresh it from the Servers tab to enable this option.'
                              : `Requires Plex Media Server ≥ 1.32. Source server reports ${ver}.`
                            : `Plex ${ver} supports librarySectionUserID - fast detection is available.`
                        }
                      >
                        <input
                          type="checkbox"
                          checked={disabled ? false : fastCollectionDetection}
                          disabled={disabled}
                          onChange={(e) => onFastCollectionDetectionChange(e.target.checked)}
                        />
                        <span>
                          Fast collection detection
                          {disabled && (
                            <span className="tag failed" style={{ marginLeft: 8, fontSize: 10 }}>
                              {unknown ? 'unknown version' : 'unsupported'}
                            </span>
                          )}
                          {!disabled && (
                            <span className="tag done" style={{ marginLeft: 8, fontSize: 10 }}>
                              Plex {ver}
                            </span>
                          )}
                        </span>
                        <span className="help">
                          Use Plex's <code>librarySectionUserID</code> attribute to distinguish
                          library-wide from personal collections without a set lookup. Measurably
                          faster on large libraries (300+ collections, 10+ users). Requires
                          Plex Media Server ≥ 1.32; greyed out below that. Defaults ON when the
                          source server supports it.
                        </span>
                      </label>
                    );
                  })()}
                </div>
              )}

              {/* ── Watch+Ratings strategy override (snapshot/direct) ─ */}
              {(mode === 'snapshot' || mode === 'direct') && (
                <div>
                  <h3 style={{ marginTop: 0 }}>Watch+Ratings capture strategy</h3>
                  <span className="help" style={{ display: 'block', color: 'var(--text-dim)', fontSize: 12, marginBottom: 8 }}>
                    Overrides the per-server / Run-Defaults strategy for this run only.
                    Useful when a server is having a 429-storm today (force bulk) or when
                    you specifically want smaller payloads back from Plex (force server-side).
                  </span>
                  <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                    <label className="switch" title="Use the per-server override → Run Defaults → built-in 'smart' value. Recommended unless you have a specific reason to override.">
                      <input type="radio" name={wrName} checked={watchRatingsStrategy === ''} onChange={() => onWatchRatingsStrategyChange('')} />
                      <span>Inherit <em>(recommended)</em></span>
                      <span className="help">Use the per-server override from Servers ▸ Advanced Settings, or the global default from Run Defaults.</span>
                    </label>
                    <label className="switch" title="Engine picks per library - bulk-fetch when both watch+ratings wanted, server-side filter when only one.">
                      <input type="radio" name={wrName} checked={watchRatingsStrategy === 'smart'} onChange={() => onWatchRatingsStrategyChange('smart')} />
                      <span>Smart</span>
                      <span className="help">Engine picks per library. Equivalent to the global default behaviour.</span>
                    </label>
                    <label className="switch" title="Always fetch the full library and filter locally. Best for rate-limited Plex servers - fewer API calls, larger payloads.">
                      <input type="radio" name={wrName} checked={watchRatingsStrategy === 'force_bulk'} onChange={() => onWatchRatingsStrategyChange('force_bulk')} />
                      <span>Force bulk</span>
                      <span className="help">Always bulk-fetch + filter locally. Best for rate-limited / 429-prone Plex servers.</span>
                    </label>
                    <label className="switch" title="Always let Plex filter on its side. Best when wire-traffic back from Plex is the constraint.">
                      <input type="radio" name={wrName} checked={watchRatingsStrategy === 'force_server_side'} onChange={() => onWatchRatingsStrategyChange('force_server_side')} />
                      <span>Force server-side</span>
                      <span className="help">Always use server-side filter scans. Smaller payloads, more API calls.</span>
                    </label>
                  </div>
                </div>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
