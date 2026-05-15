// Settings panel - per-run defaults.
//
// Plex server connection details moved to the dedicated Servers tab
// in v0.9.0 (each registered server has its own URL + token now).
// This panel handles only the global defaults: output / log directories
// and worker counts. Any field a Run-Job form leaves blank falls back
// to whatever's saved here.
//
// PR-9.1 - the Database Admin Account section was removed from this
// panel and lives under its own ``Accounts`` sub-tab now, alongside
// the Login Account management. See ``AccountsPanel.tsx``.
//
// PR-13 - the snapshot-retention block lives here as the global
// ceiling only. Per-server retention overrides moved to
// Servers ▸ Advanced Settings (along with the rest of the
// per-server defaults) so all per-server tunables share one home.

import { useEffect, useState } from 'react';
import { api, PrunePreview, PruneResult, SettingsView } from '../api';

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

  // Global retention ceiling. Per-server overrides moved to
  // Servers ▸ Advanced Settings and write through their own panel.
  const [globalRetention, setGlobalRetention] = useState<number>(30);
  // Global default for the "Save JSON copy after snapshot" toggle on
  // snapshot jobs + schedules. The Run-Job form seeds its per-job
  // checkbox from this on mount; the operator can still flip it per
  // run without changing the global.
  const [prebuildJsonSidecarDefault, setPrebuildJsonSidecarDefault] = useState<boolean>(false);

  // Direct-transfer resolver-tier policy. Snapshot / import paths
  // are NOT affected by these - they continue to run all four
  // tiers. The toggles below only gate the tiers used during a
  // direct server-to-server transfer.
  const [allowFilepathFallback, setAllowFilepathFallback] = useState<boolean>(true);
  const [allowFuzzyFallback, setAllowFuzzyFallback] = useState<boolean>(false);

  // Rule 2: library-walk cadence + Prune Missing Items.
  const [walkEnabled, setWalkEnabled] = useState<boolean>(true);
  const [walkIntervalHours, setWalkIntervalHours] = useState<number>(24);
  const [staleThresholdDays, setStaleThresholdDays] = useState<number>(7);
  const [pruneServerOpen, setPruneServerOpen] = useState<string | null>(null);
  const [servers, setServers] = useState<{ id: string; name: string }[]>([]);

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
      // Transfer resolution: respect server defaults, fall back to
      // the "permissive Tier 2, restrictive Tier 3" spec defaults if
      // the field is absent on older settings.json files.
      const tr = s.transfer_resolution || {};
      setAllowFilepathFallback(tr.allow_filepath_fallback !== false);
      setAllowFuzzyFallback(tr.allow_fuzzy_fallback === true);
      // Rule 2: library-walk cadence + stale-prune slider default.
      const lw = s.library_walk || {};
      setWalkEnabled(lw.enabled !== false);
      setWalkIntervalHours(
        typeof lw.interval_seconds === 'number' && lw.interval_seconds > 0
          ? Math.max(1, Math.round(lw.interval_seconds / 3600))
          : 24,
      );
      setStaleThresholdDays(
        typeof lw.stale_threshold_days === 'number' && lw.stale_threshold_days >= 1
          ? lw.stale_threshold_days
          : 7,
      );
    } catch (e) {
      setError(String(e));
    }
  };
  useEffect(() => { load(); }, []);

  // Server list for the Prune Missing Items per-server picker. Loaded
  // once on mount; not refreshed on settings save (servers panel owns
  // the canonical list).
  useEffect(() => {
    api.listServers()
      .then((rows) => setServers(rows.map((r) => ({ id: r.id, name: r.name }))))
      .catch(() => { /* non-fatal; prune action falls back to manual id */ });
  }, []);

  const save = async () => {
    setError(null);
    setOk(null);
    // Per-server retention overrides are deliberately NOT sent from
    // this patch - the new Advanced Settings panel is the sole writer
    // for that map. Including it here would clobber overrides the
    // operator just saved through that panel.
    const patch: Record<string, unknown> = {
      output_dir: outputDir,
      log_dir: logDir,
      workers,
      scrobble_workers: scrobbleWorkers,
      verbose,
      strict_match: strictMatch,
      snapshot_retention_global: Math.max(1, Math.floor(Number(globalRetention) || 30)),
      prebuild_json_sidecar_default: prebuildJsonSidecarDefault,
      transfer_resolution: {
        allow_filepath_fallback: allowFilepathFallback,
        allow_fuzzy_fallback: allowFuzzyFallback,
      },
      // Rule 2: persist the walk cadence + stale slider default.
      // Interval is stored in seconds at the backend; the UI works
      // in hours.
      library_walk: {
        enabled: walkEnabled,
        interval_seconds: Math.max(1, Math.floor(Number(walkIntervalHours) || 24)) * 3600,
        stale_threshold_days: Math.max(1, Math.floor(Number(staleThresholdDays) || 7)),
      },
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
        <span className="help" style={{ display: 'block', color: 'var(--text-dim)', fontSize: 12, marginBottom: 12 }}>
          Defaults applied to every snapshot job + scheduled run. The Run-Job
          form seeds its checkbox from the value here so the operator picks up
          the global without thinking about it; they can still flip the
          per-run toggle without touching this page.
        </span>
        <label className="switch">
          <input
            type="checkbox"
            checked={prebuildJsonSidecarDefault}
            onChange={(e) => setPrebuildJsonSidecarDefault(e.target.checked)}
          />
          <span>Save JSON copy after every snapshot</span>
          <span className="help">
            When on, every snapshot run also writes a <code>.plexexport.json</code>
            next to the <code>.db</code>. Adds wall-clock time to each run; off
            by default because the JSON is also built on demand from the
            <strong> Exports</strong> tab's Download button.
          </span>
        </label>
      </div>

      <div className="panel">
        <h2>Transfer Resolution</h2>
        <span className="help" style={{ display: 'block', color: 'var(--text-dim)', fontSize: 12, marginBottom: 12 }}>
          Controls which fallback tiers the resolver tries during a
          direct server-to-server transfer. Tier 0 (DB GUID cache)
          and Tier 1 (live API GUID match) are always active.
          Snapshot and import paths are unaffected by these toggles -
          they continue to run all four tiers regardless.
        </span>
        <label className="switch">
          <input
            type="checkbox"
            checked={allowFilepathFallback}
            onChange={(e) => setAllowFilepathFallback(e.target.checked)}
          />
          <span>
            Allow filepath-suffix fallback (Tier 2)
            <span className="help" style={{ display: 'block', color: 'var(--text-dim)', fontSize: 11, marginTop: 2 }}>
              Matches by normalised 3-part path suffix. Cross-platform
              safe; resolves Windows ↔ Linux migrations. Reliable when
              the file layout is consistent across servers.
            </span>
          </span>
        </label>
        <label className="switch" style={{ marginTop: 10 }}>
          <input
            type="checkbox"
            checked={allowFuzzyFallback}
            onChange={(e) => setAllowFuzzyFallback(e.target.checked)}
          />
          <span>
            <strong style={{ color: 'var(--warn, #d97706)' }}>Allow fuzzy title fallback (Tier 3)</strong> — may produce incorrect matches
            <span className="help" style={{ display: 'block', color: 'var(--text-dim)', fontSize: 11, marginTop: 2 }}>
              Last-resort match by title. Two items with similar titles
              on different servers can match each other when they
              shouldn't, causing the wrong destination item to be
              updated. Off by default. When any item matches via this
              tier, the Dashboard shows a yellow warning so the
              operator can review.
            </span>
          </span>
        </label>
      </div>

      {/* Rule 2: library walk cadence + Prune Missing Items. */}
      <div className="panel">
        <h2>Library Maintenance</h2>
        <span className="help" style={{ display: 'block', color: 'var(--text-dim)', fontSize: 12, marginBottom: 12 }}>
          A background <strong>library walk</strong> ticks the
          last-seen timestamp on every item the server still reports.
          Items not seen in a while become candidates for the
          <strong> Prune Missing Items</strong> action below. <em>Pruning never
          runs automatically</em> — the operator picks the day threshold
          and confirms each sweep with database-admin credentials.
        </span>
        <label className="switch">
          <input
            type="checkbox"
            checked={walkEnabled}
            onChange={(e) => setWalkEnabled(e.target.checked)}
          />
          <span>Enable background library walk</span>
          <span className="help">When off, the scheduler stops firing; the Servers panel's per-server Walk button still works.</span>
        </label>
        <div className="grid-2">
          <label className="field" style={{ maxWidth: 240 }}>
            <span className="label">Walk cadence (hours)</span>
            <span className="help">How often the scheduler fires per server. Floored at 1h on the backend.</span>
            <input
              type="number"
              min={1}
              max={720}
              value={walkIntervalHours}
              onChange={(e) => setWalkIntervalHours(Number(e.target.value))}
            />
          </label>
          <label className="field" style={{ maxWidth: 240 }}>
            <span className="label">Stale threshold (days)</span>
            <span className="help">Default value for the Prune Missing Items slider — operators can override per sweep.</span>
            <input
              type="number"
              min={1}
              max={3650}
              value={staleThresholdDays}
              onChange={(e) => setStaleThresholdDays(Number(e.target.value))}
            />
          </label>
        </div>
        <h3 style={{ fontSize: 14, margin: '12px 0 4px 0' }}>Prune Missing Items</h3>
        <span className="help" style={{ display: 'block', color: 'var(--text-dim)', fontSize: 12, marginBottom: 8 }}>
          Pick a registered server, preview the items it hasn't reported
          recently, and (after a db-admin confirm) remove their cache
          rows. Snapshots are never touched — they're historical records
          of what existed at capture time.
        </span>
        {servers.length === 0 ? (
          <div className="empty" style={{ fontSize: 12 }}>No servers registered.</div>
        ) : (
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
            {servers.map((s) => (
              <button key={s.id} onClick={() => setPruneServerOpen(s.id)}>
                Prune… {s.name}
              </button>
            ))}
          </div>
        )}
        {pruneServerOpen && (
          <PruneMissingItemsModal
            serverId={pruneServerOpen}
            serverName={
              servers.find((s) => s.id === pruneServerOpen)?.name ?? pruneServerOpen
            }
            initialDays={staleThresholdDays}
            onClose={() => setPruneServerOpen(null)}
          />
        )}
      </div>

      {/* PR-13 - snapshot retention controls. */}
      <div className="panel">
        <h2>Snapshot Retention</h2>
        <span className="help" style={{ display: 'block', color: 'var(--text-dim)', fontSize: 12, marginBottom: 12 }}>
          Each snapshot job writes a per-server <code>.db</code> indexed in the Exports
          panel. Retention caps how many of those rows live alongside each server;
          older snapshots beyond the cap are deleted (file + registry row) after every
          new capture. Per-server values only apply when <em>strictly lower</em> than
          the global - a per-server number higher than the global is ignored and the
          global ceiling wins.
        </span>

        <label className="field" style={{ maxWidth: 240 }}>
          <span className="label">Global retention (snapshots per server)</span>
          <input
            type="number"
            min={1}
            max={10000}
            value={globalRetention}
            onChange={(e) => setGlobalRetention(Math.max(1, Math.floor(Number(e.target.value) || 1)))}
          />
        </label>

        <div className="help" style={{ marginTop: 12, fontSize: 12, color: 'var(--text-dim)' }}>
          Per-server retention overrides moved to
          <strong> Servers ▸ Advanced Settings</strong>, alongside the
          other snapshot-time per-server defaults (JSON sidecar,
          data-type filters, engine tuning).
        </div>
      </div>

      <div className="panel">
        <button className="primary" onClick={save}>Save Settings</button>
      </div>
    </>
  );
}

// Rule 2: Prune Missing Items confirmation modal. Two-step UX -
// fetch a dry-run preview first, show the count + sample list, then
// gate the destructive POST behind a db-admin credential entry.
function PruneMissingItemsModal({
  serverId,
  serverName,
  initialDays,
  onClose,
}: {
  serverId: string;
  serverName: string;
  initialDays: number;
  onClose: () => void;
}) {
  const [days, setDays] = useState<number>(initialDays);
  const [preview, setPreview] = useState<PrunePreview | null>(null);
  const [loading, setLoading] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const [dbAdminUsername, setDbAdminUsername] = useState<string>('');
  const [dbAdminPassword, setDbAdminPassword] = useState<string>('');
  const [executing, setExecuting] = useState<boolean>(false);
  const [result, setResult] = useState<PruneResult | null>(null);

  const refresh = async (d: number) => {
    setLoading(true);
    setError(null);
    try {
      const p = await api.prunePreview(serverId, d);
      setPreview(p);
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { refresh(days); /* eslint-disable-next-line react-hooks/exhaustive-deps */ }, []);

  const execute = async () => {
    setExecuting(true);
    setError(null);
    try {
      const r = await api.pruneStaleItems(serverId, {
        db_admin_username: dbAdminUsername,
        db_admin_password: dbAdminPassword,
        older_than_days: days,
        dry_run: false,
      });
      setResult(r);
    } catch (e) {
      setError(String(e));
    } finally {
      setExecuting(false);
    }
  };

  const lastWalk = preview?.last_walk;
  const walkStale = lastWalk && lastWalk.started_at
    ? (Date.now() / 1000 - lastWalk.started_at) > days * 86400
    : true;

  return (
    <div
      style={{
        position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.5)',
        display: 'flex', alignItems: 'center', justifyContent: 'center',
        zIndex: 1000,
      }}
      onClick={onClose}
    >
      <div
        className="panel"
        style={{
          maxWidth: 640, maxHeight: '90vh', overflow: 'auto',
          background: 'var(--bg, #181818)',
        }}
        onClick={(e) => e.stopPropagation()}
      >
        <h2>Prune Missing Items — {serverName}</h2>
        <span className="help" style={{ display: 'block', color: 'var(--text-dim)', fontSize: 12, marginBottom: 12 }}>
          Items a library walk last confirmed present <em>before</em> the
          threshold below will have their watch / rating /
          playlist-member / collection-member rows removed for this
          server. Items never confirmed by a walk are left alone — a
          missing <code>last_seen_at</code> is not evidence the item is
          gone. Items shared with other registered servers stay intact.
          Snapshots are never touched.
        </span>
        {result ? (
          <div className="banner good" style={{ fontSize: 13 }}>
            <strong>Done.</strong> Removed {result.server_items} server_items,{' '}
            {result.watch_events} watch events, {result.ratings} ratings.{' '}
            {result.playlists_touched} playlist(s) and {result.collections_touched} collection(s) trimmed.{' '}
            {result.items_orphaned} orphaned item row(s) cleaned.
            <div style={{ marginTop: 8 }}>
              <button onClick={onClose}>Close</button>
            </div>
          </div>
        ) : (
          <>
            <label className="field" style={{ maxWidth: 240 }}>
              <span className="label">Older than (days)</span>
              <input
                type="number"
                min={1}
                max={3650}
                value={days}
                onChange={(e) => setDays(Number(e.target.value))}
                onBlur={() => refresh(days)}
              />
            </label>
            {walkStale && (
              <div className="banner" style={{
                background: 'rgba(217, 119, 6, 0.12)',
                border: '1px solid var(--warn, #d97706)',
                color: 'var(--warn, #d97706)',
                padding: '8px 12px', borderRadius: 6, marginBottom: 8, fontSize: 12,
              }}>
                <strong>Stale walk data.</strong> The most recent library walk on
                this server {lastWalk
                  ? `finished ${new Date((lastWalk.finished_at || lastWalk.started_at) * 1000).toLocaleString()}`
                  : 'has never completed'}.
                Run a fresh walk from the Servers panel before pruning to
                avoid removing items that simply weren't sighted yet.
              </div>
            )}
            {loading && <div className="empty">Loading preview…</div>}
            {preview && !loading && (
              <>
                <p style={{ fontSize: 13, margin: '6px 0' }}>
                  <strong>{preview.count}</strong> item(s) would be removed.{' '}
                  {preview.sample_items.length > 0 && (
                    <>Showing first {preview.sample_items.length}:</>
                  )}
                </p>
                {preview.sample_items.length > 0 && (
                  <ul style={{ fontSize: 12, maxHeight: 200, overflow: 'auto', margin: '6px 0', paddingLeft: 18 }}>
                    {preview.sample_items.map((it) => (
                      <li key={`${it.rating_key}-${it.item_id}`}>
                        <strong>{it.title}</strong>
                        {it.year ? ` (${it.year})` : ''}
                        {' — '}
                        <span style={{ color: 'var(--text-dim)' }}>
                          {it.last_seen_at
                            ? `last seen ${new Date(it.last_seen_at * 1000).toLocaleDateString()}`
                            : 'never sighted by walk'}
                        </span>
                      </li>
                    ))}
                  </ul>
                )}
                {preview.count === 0 ? (
                  <div className="empty" style={{ fontSize: 12 }}>Nothing to prune at this threshold.</div>
                ) : (
                  <>
                    <h3 style={{ fontSize: 14, margin: '12px 0 4px 0' }}>Confirm with database admin credentials</h3>
                    <div className="grid-2">
                      <label className="field">
                        <span className="label">db-admin username</span>
                        <input
                          type="text"
                          value={dbAdminUsername}
                          onChange={(e) => setDbAdminUsername(e.target.value)}
                          autoComplete="username"
                        />
                      </label>
                      <label className="field">
                        <span className="label">db-admin password</span>
                        <input
                          type="password"
                          value={dbAdminPassword}
                          onChange={(e) => setDbAdminPassword(e.target.value)}
                          autoComplete="current-password"
                        />
                      </label>
                    </div>
                  </>
                )}
              </>
            )}
            {error && <div className="banner error" style={{ marginTop: 8 }}>{error}</div>}
            <div style={{ display: 'flex', gap: 6, marginTop: 12 }}>
              <button onClick={onClose}>Cancel</button>
              <button
                className="danger"
                onClick={execute}
                disabled={
                  executing || !preview || preview.count === 0
                  || !dbAdminUsername || !dbAdminPassword
                }
              >
                {executing ? 'Pruning…' : `Remove ${preview?.count ?? 0} item(s)`}
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
