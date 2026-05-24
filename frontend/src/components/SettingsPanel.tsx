// Settings panel - Hestia-MediaManager app behaviour (Bucket B).
//
// As of the Phase-2 Settings/Servers reorg, this panel holds only the
// settings that describe how the Hestia-MediaManager app itself behaves -
// distinct from the run-level defaults that describe how snapshots
// and direct transfers operate against Plex (those moved to
// Servers ▸ Run Defaults).
//
// What lives here:
//   * Logging toggles (run-log files + db-access audit log)
//   * Library Maintenance (background walk cadence + Prune Missing Items)
//
// What moved to Servers ▸ Run Defaults:
//   * Default Paths (output_dir, log_dir)
//   * Default Performance & Behaviour (workers, scrobble_workers,
//     verbose, strict_match)
//   * Snapshot Defaults (prebuild_json_sidecar_default,
//     watch_ratings_filter_strategy)
//   * Transfer Resolution (allow_filepath_fallback, allow_fuzzy_fallback)
//   * Snapshot Retention (snapshot_retention_global ceiling)
//
// What moved to Settings ▸ Tunables:
//   * Root-admin-only infrastructure knobs (HTTP timeouts, JWT TTL,
//     SQLite busy timeouts, etc.).

import { useEffect, useState } from 'react';
import { api, PrunePreview, PruneResult, RecentRunRow, SettingsView } from '../api';
import { InfoTip } from './InfoTip';

export function SettingsPanel() {
  const [view, setView] = useState<SettingsView | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [ok, setOk] = useState<string | null>(null);

  // Logging controls. ``runLoggingEnabled`` is a plain global toggle
  // that rides the normal Save button. ``auditLogEnabled`` is shown
  // as a status flag here but flipped only through a db_admin-gated
  // modal - a plain settings PATCH silently strips it server-side so
  // the audit trail can't be killed by a casual checkbox.
  const [runLoggingEnabled, setRunLoggingEnabled] = useState<boolean>(true);
  const [auditLogEnabled, setAuditLogEnabled] = useState<boolean>(true);
  const [auditToggleOpen, setAuditToggleOpen] = useState<boolean>(false);

  // Rule 2: library-walk cadence + Prune Missing Items.
  const [walkEnabled, setWalkEnabled] = useState<boolean>(true);
  const [walkIntervalHours, setWalkIntervalHours] = useState<number>(24);
  const [staleThresholdDays, setStaleThresholdDays] = useState<number>(7);
  const [pruneServerOpen, setPruneServerOpen] = useState<string | null>(null);
  const [servers, setServers] = useState<{ id: string; name: string }[]>([]);

  // ETR colour multiplier. Scales the dashboard's per-phase
  // amber/red stall thresholds. 1.0 = ship defaults; <1.0 warns
  // sooner; >1.0 is more lenient. Clamped to [0.5, 2.0] at the
  // backend read boundary too.
  const [etrMultiplier, setEtrMultiplier] = useState<number>(1.0);

  // Resolved on-disk paths of every app-level log
  // file the build writes. Surfaced as a "Log file locations"
  // subsection of the Logging panel so the operator can tail / grep
  // them from a shell without first navigating to the Application
  // Logs viewer for each category.
  const [logPaths, setLogPaths] = useState<{ data_dir: string; paths: Record<string, string> } | null>(null);

  // Run History subtab state. activeTab toggles which top-level
  // panel set renders. recentRuns is the run_history listing; the
  // end user-facing inspection surface for past job runs (snapshot
  // / restore / direct).
  const [activeTab, setActiveTab] = useState<'settings' | 'history' | 'databases'>('settings');
  const [recentRuns, setRecentRuns] = useState<RecentRunRow[]>([]);
  const [runsLoading, setRunsLoading] = useState<boolean>(false);

  const load = async () => {
    try {
      const s = await api.getSettings();
      setView(s);
      // Logging toggles. ``run_logging_enabled`` defaults to true on
      // missing (existing installs keep writing per-run files);
      // ``audit_log_enabled`` likewise defaults to true.
      setRunLoggingEnabled(s.run_logging_enabled !== false);
      setAuditLogEnabled(s.audit_log_enabled !== false);
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
      // ETR colour multiplier.
      const m = (s as { etr_color_multiplier?: number }).etr_color_multiplier;
      if (typeof m === 'number' && Number.isFinite(m)) {
        setEtrMultiplier(Math.max(0.5, Math.min(2.0, m)));
      }
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

  // Log-file paths. Best-effort; non-fatal if the fetch fails (the
  // panel renders a fallback hint instead).
  useEffect(() => {
    api.listLogPaths()
      .then((r) => setLogPaths(r))
      .catch(() => setLogPaths(null));
  }, []);

  const refreshRecentRuns = async () => {
    setRunsLoading(true);
    try {
      const r = await api.listRecentRunHistory({ limit: 100 });
      setRecentRuns(r.runs);
    } catch {
      setRecentRuns([]);
    } finally {
      setRunsLoading(false);
    }
  };
  useEffect(() => { void refreshRecentRuns(); }, []);

  const save = async () => {
    setError(null);
    setOk(null);
    const patch: Record<string, unknown> = {
      // run_logging_enabled rides the normal Save button; audit_log_enabled
      // is INTENTIONALLY OMITTED here - the backend silently strips it
      // from a plain PATCH so this checkbox-style page can never flip
      // the audit trail. Use the db_admin-gated modal below for that.
      run_logging_enabled: runLoggingEnabled,
      // Rule 2: persist the walk cadence + stale slider default.
      // Interval is stored in seconds at the backend; the UI works
      // in hours.
      library_walk: {
        enabled: walkEnabled,
        interval_seconds: Math.max(1, Math.floor(Number(walkIntervalHours) || 24)) * 3600,
        stale_threshold_days: Math.max(1, Math.floor(Number(staleThresholdDays) || 7)),
      },
      // ETR colour multiplier. Backend clamps too.
      etr_color_multiplier: Math.max(0.5, Math.min(2.0, Number(etrMultiplier) || 1.0)),
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

      {/* Settings subtab nav. App Settings (logging, library walk,
          ETR multiplier) vs Run History (recent jobs + ETA training
          inventory + recovery actions). */}
      <div className="panel" style={{ padding: '6px 8px', marginBottom: 8 }}>
        <div className="row-buttons" style={{ display: 'flex', gap: 4 }}>
          <button
            className={activeTab === 'settings' ? 'primary' : ''}
            onClick={() => setActiveTab('settings')}
          >
            App Settings
          </button>
          <button
            className={activeTab === 'history' ? 'primary' : ''}
            onClick={() => setActiveTab('history')}
          >
            Run History
          </button>
          <button
            className={activeTab === 'databases' ? 'primary' : ''}
            onClick={() => setActiveTab('databases')}
          >
            Databases
          </button>
        </div>
      </div>

      {activeTab === 'settings' && (
        <>

      <div className="banner info" style={{ fontSize: 12 }}>
        Run-level defaults (paths, performance, snapshot defaults, transfer resolution,
        retention ceiling) moved to <strong>Servers ▸ Run Defaults</strong>. Root-admin
        infrastructure knobs (HTTP timeouts, JWT TTL, etc.) live under <strong>Settings ▸ Tunables</strong>.
      </div>

      <div className="panel">
        <h2>Logging</h2>
        <span className="help" style={{ display: 'block', color: 'var(--text-dim)', fontSize: 12, marginBottom: 12 }}>
          Operational log files vs the forensic audit log. The first is
          your convenience - turn it off and the engine still runs and
          the dashboard still updates, you just don't get per-run files
          on disk. The second is the security audit trail; toggling it
          requires the database-admin credential and the transition is
          self-documenting in the audit log itself.
        </span>
        <label className="switch">
          <input
            type="checkbox"
            checked={runLoggingEnabled}
            onChange={(e) => setRunLoggingEnabled(e.target.checked)}
          />
          <span>
            Write per-run log files (<code>runtime.log</code> / <code>errors.log</code> / <code>media.log</code>)
            <InfoTip topicId="run-logging" />
          </span>
          <span className="help">
            Default on. When off, engine + dashboard run; only the on-disk per-run files are suppressed.
          </span>
        </label>
        <div className="field" style={{ marginTop: 12 }}>
          <span className="label">
            DB-access audit log:{' '}
            <span className={`tag ${auditLogEnabled ? 'done' : 'failed'}`} style={{ marginLeft: 6 }}>
              {auditLogEnabled ? 'enabled' : 'DISABLED'}
            </span>
            <InfoTip topicId="audit-log" />
          </span>
          <span className="help">
            Forensic record of every DB mutation. Toggling requires the db_admin credential.
          </span>
          <div style={{ marginTop: 6 }}>
            <button type="button" onClick={() => setAuditToggleOpen(true)}>
              {auditLogEnabled ? 'Disable audit log…' : 'Re-enable audit log…'}
            </button>
          </div>
        </div>

        {/* Log file locations - resolved on-disk paths for every
            application-level log this build writes. Lives here (not
            in Settings > Application Logs) so an operator wanting to
            tail / grep a log from a shell finds the path at a glance
            without first opening a category viewer. */}
        <div style={{ marginTop: 18 }}>
          <h3 style={{ fontSize: 14, margin: '0 0 4px' }}>Log file locations</h3>
          <span className="help" style={{ display: 'block', color: 'var(--text-dim)', fontSize: 12, marginBottom: 8 }}>
            On-disk paths of every application-level log file the server writes. Useful for
            shell-side tail / grep (e.g. <code>tail -f &lt;path&gt;</code>). Per-run job logs
            (snapshot / restore / direct) live under <strong>Servers ▸ Logs</strong>.
          </span>
          {!logPaths && (
            <div style={{ color: 'var(--text-dim)', fontSize: 12 }}>
              Loading paths…
            </div>
          )}
          {logPaths && (
            <>
              <div style={{ fontSize: 11, color: 'var(--text-dim)', marginBottom: 8 }}>
                Data directory: <code style={{ userSelect: 'all' }}>{logPaths.data_dir}</code>
              </div>
              <table style={{ width: '100%', fontSize: 12 }}>
                <thead>
                  <tr>
                    <th style={{ textAlign: 'left', padding: '4px 8px', borderBottom: '1px solid var(--border)' }}>Log</th>
                    <th style={{ textAlign: 'left', padding: '4px 8px', borderBottom: '1px solid var(--border)' }}>Path</th>
                    <th style={{ padding: '4px 8px', borderBottom: '1px solid var(--border)' }} />
                  </tr>
                </thead>
                <tbody>
                  {[
                    { key: 'app',            label: 'App (server-wide INFO+)' },
                    { key: 'db-access',      label: 'Database Access' },
                    { key: 'playlist-cache', label: 'Playlist Cache' },
                    { key: 'sync',           label: 'Sync Activity' },
                  ].map((row) => {
                    const p = logPaths.paths[row.key];
                    if (!p) return null;
                    return (
                      <tr key={row.key}>
                        <td style={{ padding: '4px 8px', verticalAlign: 'top' }}>{row.label}</td>
                        <td style={{ padding: '4px 8px', fontFamily: 'monospace' }}>
                          <code style={{ userSelect: 'all' }}>{p}</code>
                        </td>
                        <td style={{ padding: '4px 8px', textAlign: 'right' }}>
                          <button
                            type="button"
                            onClick={() => { if (p) void navigator.clipboard?.writeText(p); }}
                            style={{ fontSize: 11, padding: '0 6px' }}
                            title="Copy path to clipboard"
                          >
                            Copy
                          </button>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </>
          )}
        </div>
      </div>

      {/* Rule 2: library walk cadence + Prune Missing Items. */}
      <div className="panel">
        <h2>Library Maintenance</h2>
        <span className="help" style={{ display: 'block', color: 'var(--text-dim)', fontSize: 12, marginBottom: 12 }}>
          A background <strong>library walk</strong> ticks the
          last-seen timestamp on every item the server still reports.
          Items not seen in a while become candidates for the
          <strong> Prune Missing Items</strong> action below. <em>Pruning never
          runs automatically</em> - the operator picks the day threshold
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
            <span className="help">Default value for the Prune Missing Items slider - operators can override per sweep.</span>
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
          rows. Snapshots are never touched - they're historical records
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

      {auditToggleOpen && (
        <AuditLogToggleModal
          currentlyEnabled={auditLogEnabled}
          onClose={() => setAuditToggleOpen(false)}
          onToggled={(newEnabled) => {
            setAuditLogEnabled(newEnabled);
            setAuditToggleOpen(false);
            setOk(
              newEnabled
                ? 'DB-access audit log re-enabled.'
                : 'DB-access audit log disabled.',
            );
          }}
        />
      )}

      {/* Dashboard ETR colour-switch timing. */}
      <div className="panel">
        <h2>Dashboard Stall Colours</h2>
        <span className="help" style={{ display: 'block', color: 'var(--text-dim)', fontSize: 12, marginBottom: 12 }}>
          Scales the per-phase amber/red thresholds the Dashboard's <strong>Currently
          Processing</strong> table uses to flag stuck phases. <strong>1.0</strong> uses the ship
          defaults; values <strong>below 1.0</strong> are more sensitive (warns sooner); values
          <strong> above 1.0</strong> are more lenient. Clamped to [0.5, 2.0]. Takes effect on the
          next Dashboard mount.
        </span>
        <label className="field" style={{ maxWidth: 320 }}>
          <span className="label">ETR colour multiplier: <strong>{etrMultiplier.toFixed(2)}×</strong></span>
          <input
            type="range"
            min={0.5}
            max={2.0}
            step={0.05}
            value={etrMultiplier}
            onChange={(e) => setEtrMultiplier(Number(e.target.value))}
          />
          <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 11, color: 'var(--text-dim)' }}>
            <span>0.5× (sensitive)</span>
            <span>1.0× (default)</span>
            <span>2.0× (lenient)</span>
          </div>
        </label>
        {/* Live preview at the current multiplier. Each row renders
            the phase name three ways - as the normal tag the
            Dashboard's Currently Processing table uses (left column),
            then the stall-amber and stall-red variants the same phase
            transitions into once its age crosses the thresholds. The
            seconds columns show the actual amber / red windows at
            the current multiplier so operators can see both the
            value and the colour change in one place. */}
        <div style={{ marginTop: 12, fontSize: 12 }}>
          <strong>Preview at {etrMultiplier.toFixed(2)}×:</strong>
          <table className="list" style={{ marginTop: 6, maxWidth: 720 }}>
            <thead>
              <tr>
                <th>Normal</th>
                <th>Amber after</th>
                <th>Red after</th>
              </tr>
            </thead>
            <tbody>
              {([
                ['fetching', 45, 120, 'phase'],
                ['capturing', 30, 60, 'capturing'],
                ['indexing', 45, 90, 'phase'],
                ['resolving', 30, 75, 'phase'],
                ['scrobbling', 20, 45, 'merged'],
                ['rating', 15, 30, 'rated'],
                ['merging', 30, 60, 'appended'],
              ] as Array<[string, number, number, string]>).map(([name, a, r, normalCls]) => (
                <tr key={name}>
                  <td><span className={`tag ${normalCls}`}>{name}</span></td>
                  <td>
                    <span className="tag stall-amber" style={{ marginRight: 6 }}>{name}</span>
                    <span style={{ color: 'var(--text-dim)' }}>{(a * etrMultiplier).toFixed(0)}s</span>
                  </td>
                  <td>
                    <span className="tag stall-red" style={{ marginRight: 6 }}>{name}</span>
                    <span style={{ color: 'var(--text-dim)' }}>{(r * etrMultiplier).toFixed(0)}s</span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      <div className="panel">
        <button className="primary" onClick={save}>Save Settings</button>
      </div>
        </>
      )}

      {activeTab === 'history' && (
        <>
      <div className="panel">
        <h2>Recent Runs</h2>
        <p style={{ fontSize: 13, color: 'var(--text-dim)', marginTop: 4 }}>
          Every snapshot, restore, and direct-transfer job appears here
          with its server, library scope, duration, and final state.
          Sourced from <code>server_data/run_timings.db</code> /
          <code>run_history</code>; survives docker rebuilds via the
          bind mount in docker-compose.yml.
        </p>
        <div style={{ display: 'flex', gap: 8, alignItems: 'center', marginTop: 8 }}>
          <button onClick={() => void refreshRecentRuns()} disabled={runsLoading}>
            {runsLoading ? 'Refreshing…' : 'Refresh'}
          </button>
        </div>
        {recentRuns.length === 0 ? (
          <div className="empty" style={{ marginTop: 12 }}>
            {runsLoading ? 'Loading…' : 'No runs recorded yet.'}
          </div>
        ) : (
          <table className="list" style={{ width: '100%', fontSize: 12, marginTop: 12 }}>
            <thead>
              <tr>
                <th style={{ textAlign: 'left' }}>Started</th>
                <th style={{ textAlign: 'left' }}>Type</th>
                <th style={{ textAlign: 'left' }}>Server</th>
                <th style={{ textAlign: 'left' }}>Libraries</th>
                <th style={{ textAlign: 'right' }}>Users</th>
                <th style={{ textAlign: 'right' }}>Duration</th>
                <th style={{ textAlign: 'left' }}>State</th>
              </tr>
            </thead>
            <tbody>
              {recentRuns.map((r) => {
                const stateColor = r.state === 'completed' ? 'var(--success, #16a34a)'
                  : r.state === 'failed' ? 'var(--danger, #dc2626)'
                  : 'var(--text-dim)';
                const durSec = (r.duration_ms || 0) / 1000;
                const durStr = durSec >= 3600
                  ? `${Math.floor(durSec/3600)}h ${Math.round((durSec%3600)/60)}m`
                  : durSec >= 90
                  ? `${Math.round(durSec/60)} min`
                  : `${Math.round(durSec)} sec`;
                const libs = (r.libraries || []).join(', ');
                return (
                  <tr key={r.run_id}>
                    <td title={r.run_id}>{new Date((r.started_at || 0) * 1000).toLocaleString()}</td>
                    <td>{r.job_type}</td>
                    <td>{r.server_name || <em style={{ color: 'var(--text-dim)' }}>-</em>}</td>
                    <td title={libs} style={{ maxWidth: 240, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                      {libs || <em style={{ color: 'var(--text-dim)' }}>-</em>}
                    </td>
                    <td style={{ textAlign: 'right' }}>{r.users_affected}</td>
                    <td style={{ textAlign: 'right' }}>{durStr}</td>
                    <td style={{ color: stateColor }}>{r.state}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>

        </>
      )}

      {activeTab === 'databases' && (
        <DatabasePanel servers={servers} />
      )}

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
        <h2>Prune Missing Items - {serverName}</h2>
        <span className="help" style={{ display: 'block', color: 'var(--text-dim)', fontSize: 12, marginBottom: 12 }}>
          Items a library walk last confirmed present <em>before</em> the
          threshold below will have their watch / rating /
          playlist-member / collection-member rows removed for this
          server. Items never confirmed by a walk are left alone - a
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
                        {' - '}
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


// ── Audit-log toggle modal ─────────────────────────────────────────────────
// Flips the global db-access audit log on/off. The action is the
// security-equivalent of a destructive op: it can blind every future
// destructive call (prune, server purge, credential delete) so it's
// gated behind the db_admin credential, not just the JWT. Backend
// writes a self-documenting "DISABLED by <user>" line as the last
// audit entry on disable, and a matching "RE-ENABLED by <user>" line
// as the first entry on re-enable - the trail always records that the
// off period was an explicit, attributable act.

function AuditLogToggleModal({
  currentlyEnabled,
  onClose,
  onToggled,
}: {
  currentlyEnabled: boolean;
  onClose: () => void;
  onToggled: (newEnabled: boolean) => void;
}) {
  const [dbAdminUsername, setDbAdminUsername] = useState<string>('');
  const [dbAdminPassword, setDbAdminPassword] = useState<string>('');
  const [confirmAcknowledged, setConfirmAcknowledged] = useState<boolean>(false);
  const [submitting, setSubmitting] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);

  // The "target state" is the opposite of the current state - the
  // end user clicked the button to flip it.
  const targetEnabled = !currentlyEnabled;
  const disabling = currentlyEnabled;  // we're about to turn it off

  const submit = async () => {
    setSubmitting(true);
    setError(null);
    try {
      const r = await api.toggleAuditLog({
        db_admin_username: dbAdminUsername,
        db_admin_password: dbAdminPassword,
        enabled: targetEnabled,
      });
      onToggled(r.audit_log_enabled);
    } catch (e) {
      setError(String(e));
    } finally {
      setSubmitting(false);
    }
  };

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
          maxWidth: 560, maxHeight: '90vh', overflow: 'auto',
          background: 'var(--bg, #181818)',
        }}
        onClick={(e) => e.stopPropagation()}
      >
        <h2>
          {disabling ? 'Disable' : 'Re-enable'} DB-access audit log
        </h2>
        {disabling ? (
          <div
            className="banner"
            style={{
              background: 'rgba(217, 119, 6, 0.12)',
              border: '1px solid var(--warn, #d97706)',
              color: 'var(--warn, #d97706)',
              padding: '10px 14px', borderRadius: 6, marginBottom: 12, fontSize: 13,
            }}
          >
            <strong>Read this before continuing.</strong>{' '}
            With the audit log off, every destructive media.db operation
            (prune missing items, purge server data, delete managed user,
            tombstone changes) will <strong>succeed without leaving a record</strong> of
            who did it or when. The audit trail will resume only when an
            admin explicitly re-enables it through this dialog. The act
            of turning it off is itself logged as the final entry - under
            your db-admin username below - so the off period is always
            attributable. Continue only if you understand the
            accountability trade-off.
          </div>
        ) : (
          <span className="help" style={{ display: 'block', color: 'var(--text-dim)', fontSize: 12, marginBottom: 12 }}>
            Re-enables the db-access audit log. The first entry after
            re-enable will record your db-admin username and the time so
            the resumption is itself audited.
          </span>
        )}

        {error && (
          <div className="banner error" style={{ marginBottom: 12 }}>{error}</div>
        )}

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

        {disabling && (
          <label className="switch" style={{ marginTop: 8 }}>
            <input
              type="checkbox"
              checked={confirmAcknowledged}
              onChange={(e) => setConfirmAcknowledged(e.target.checked)}
            />
            <span>
              I understand that disabling the audit log removes the record
              of who performs destructive database operations until it is
              re-enabled.
            </span>
          </label>
        )}

        <div className="row-buttons" style={{ marginTop: 16, display: 'flex', gap: 8 }}>
          <button onClick={onClose} disabled={submitting}>Cancel</button>
          <button
            className={disabling ? 'danger' : 'primary'}
            onClick={() => void submit()}
            disabled={
              submitting
              || !dbAdminUsername
              || !dbAdminPassword
              || (disabling && !confirmAcknowledged)
            }
          >
            {submitting
              ? (disabling ? 'Disabling…' : 'Re-enabling…')
              : (disabling ? 'Disable audit log' : 'Re-enable audit log')}
          </button>
        </div>
      </div>
    </div>
  );
}


// Database export / import panel under Settings > Run History.
// Exposes every operational table (run_timings, eta_buckets,
// run_history, snapshots, media.db tables) as a JSON dump for
// backup + interop, and accepts replace-only imports behind a
// typed REPLACE confirmation. Includes a per-server pivot that
// filters identity-related tables (servers, server_users,
// managed_users, user_identity_map, etc.) by server_id so the
// end user can inspect or archive one server's state in isolation.
function DatabasePanel({ servers }: { servers: { id: string; name: string }[] }) {
  const [tables, setTables] = useState<Array<{
    table_id: string;
    db_file: string;
    table_name: string;
    row_count: number | null;
    available: boolean;
    per_server: boolean;
  }>>([]);
  const [tablesLoading, setTablesLoading] = useState(false);
  const [perServerId, setPerServerId] = useState<string>('');
  const [perServerTables, setPerServerTables] = useState<Array<{
    table_id: string;
    db_file: string;
    table_name: string;
    row_count: number;
  }>>([]);
  const [perServerLoading, setPerServerLoading] = useState(false);
  const [importOpen, setImportOpen] = useState<null | { kind: 'table'; tableId: string } | { kind: 'archive' }>(null);
  const [busy, setBusy] = useState(false);
  const [actionMsg, setActionMsg] = useState<string | null>(null);

  const refresh = async () => {
    setTablesLoading(true);
    try {
      const r = await api.listDatabaseTables();
      setTables(r.tables);
    } catch {
      setTables([]);
    } finally {
      setTablesLoading(false);
    }
  };
  useEffect(() => { void refresh(); }, []);

  useEffect(() => {
    if (!perServerId) {
      setPerServerTables([]);
      return;
    }
    let cancelled = false;
    setPerServerLoading(true);
    api.listDatabasePerServer(perServerId)
      .then((r) => { if (!cancelled) setPerServerTables(r.tables); })
      .catch(() => { if (!cancelled) setPerServerTables([]); })
      .finally(() => { if (!cancelled) setPerServerLoading(false); });
    return () => { cancelled = true; };
  }, [perServerId]);

  const downloadJson = (obj: Record<string, unknown>, filename: string) => {
    const blob = new Blob([JSON.stringify(obj, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  };

  const tsStamp = () => {
    const d = new Date();
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}_${String(d.getHours()).padStart(2, '0')}-${String(d.getMinutes()).padStart(2, '0')}`;
  };

  const exportTable = async (tableId: string) => {
    setBusy(true);
    setActionMsg(null);
    try {
      const payload = await api.exportDatabaseTable(tableId);
      downloadJson(payload, `plexbackup_${tableId}_${tsStamp()}.json`);
      setActionMsg(`Exported ${tableId} → plexbackup_${tableId}_${tsStamp()}.json`);
    } catch (e) {
      setActionMsg(`Export failed: ${e}`);
    } finally {
      setBusy(false);
    }
  };

  const exportArchive = async () => {
    setBusy(true);
    setActionMsg(null);
    try {
      const payload = await api.exportDatabaseArchive();
      downloadJson(payload, `plexbackup_archive_${tsStamp()}.json`);
      setActionMsg(`Exported full archive → plexbackup_archive_${tsStamp()}.json`);
    } catch (e) {
      setActionMsg(`Export failed: ${e}`);
    } finally {
      setBusy(false);
    }
  };

  const exportPerServer = async () => {
    if (!perServerId) return;
    setBusy(true);
    setActionMsg(null);
    try {
      const payload = await api.exportDatabasePerServer(perServerId);
      downloadJson(payload, `plexbackup_server_${perServerId}_${tsStamp()}.json`);
      setActionMsg(`Exported ${perServerId} archive`);
    } catch (e) {
      setActionMsg(`Export failed: ${e}`);
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <div className="panel">
        <h2>Operational databases</h2>
        <p style={{ fontSize: 13, color: 'var(--text-dim)', marginTop: 4 }}>
          Per-table JSON export for every operational SQLite DB
          (run_timings, snapshots, media). Use Export to back up;
          use Import to restore from a previous export. Import is
          replace-only (the target table is wiped first) and gated
          by a typed REPLACE confirmation. The auth DB and keyfile
          are intentionally excluded.
        </p>
        <div style={{ display: 'flex', gap: 8, marginTop: 8, flexWrap: 'wrap' }}>
          <button onClick={() => void refresh()} disabled={tablesLoading}>
            {tablesLoading ? 'Refreshing…' : 'Refresh'}
          </button>
          <button onClick={() => void exportArchive()} disabled={busy}>
            Download full archive
          </button>
          <button
            className="danger"
            onClick={() => setImportOpen({ kind: 'archive' })}
            disabled={busy}
          >
            Restore from archive…
          </button>
        </div>
        {actionMsg && (
          <div className="banner info" style={{ marginTop: 8, fontSize: 12 }}>
            {actionMsg}
          </div>
        )}
        <table className="list" style={{ width: '100%', fontSize: 12, marginTop: 12 }}>
          <thead>
            <tr>
              <th style={{ textAlign: 'left' }}>Table</th>
              <th style={{ textAlign: 'left' }}>DB file</th>
              <th style={{ textAlign: 'right' }}>Rows</th>
              <th style={{ textAlign: 'left' }}>Per-server</th>
              <th style={{ textAlign: 'right' }}>Actions</th>
            </tr>
          </thead>
          <tbody>
            {tables.map((t) => (
              <tr key={t.table_id}>
                <td style={{ fontWeight: 600 }}>{t.table_id}</td>
                <td><code>{t.db_file}</code></td>
                <td style={{ textAlign: 'right' }}>
                  {t.row_count != null ? t.row_count.toLocaleString() : <em>-</em>}
                </td>
                <td>{t.per_server ? '✓' : ''}</td>
                <td style={{ textAlign: 'right' }}>
                  <button
                    style={{ marginRight: 4 }}
                    onClick={() => void exportTable(t.table_id)}
                    disabled={busy || !t.available}
                    title={t.available ? 'Download as JSON' : 'DB file missing'}
                  >
                    Export
                  </button>
                  <button
                    className="danger"
                    onClick={() => setImportOpen({ kind: 'table', tableId: t.table_id })}
                    disabled={busy}
                  >
                    Import
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="panel">
        <h2>Per-server identity DB pivot</h2>
        <p style={{ fontSize: 13, color: 'var(--text-dim)', marginTop: 4 }}>
          Inspect or archive one server's identity-related rows
          across every operational table that carries a{' '}
          <code>server_id</code> column (servers, server_users,
          managed_users, user_identity_map, server_items,
          watch_events, ratings, library_sections, run_history,
          ETA buckets, snapshot registry, etc.). Pick a server to
          see its row counts; use Download per-server archive to
          dump everything filtered to that server.
        </p>
        <div style={{ display: 'flex', gap: 8, marginTop: 8, alignItems: 'center' }}>
          <label style={{ fontSize: 12 }}>Server:</label>
          <select
            value={perServerId}
            onChange={(e) => setPerServerId(e.target.value)}
            style={{ minWidth: 200 }}
          >
            <option value="">(select a server)</option>
            {servers.map((s) => (
              <option key={s.id} value={s.id}>{s.name} ({s.id})</option>
            ))}
          </select>
          <button
            onClick={() => void exportPerServer()}
            disabled={busy || !perServerId}
          >
            Download per-server archive
          </button>
        </div>
        {perServerId && (perServerLoading ? (
          <div className="empty" style={{ marginTop: 12 }}>Loading…</div>
        ) : (
          <table className="list" style={{ width: '100%', fontSize: 12, marginTop: 12 }}>
            <thead>
              <tr>
                <th style={{ textAlign: 'left' }}>Table</th>
                <th style={{ textAlign: 'left' }}>DB file</th>
                <th style={{ textAlign: 'right' }}>Rows for this server</th>
              </tr>
            </thead>
            <tbody>
              {perServerTables.length === 0 ? (
                <tr><td colSpan={3} style={{ color: 'var(--text-dim)' }}>No filterable tables, or this server has zero rows in every operational table.</td></tr>
              ) : (
                perServerTables.map((t) => (
                  <tr key={t.table_id}>
                    <td style={{ fontWeight: 600 }}>{t.table_id}</td>
                    <td><code>{t.db_file}</code></td>
                    <td style={{ textAlign: 'right' }}>{t.row_count.toLocaleString()}</td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        ))}
      </div>

      {importOpen && (
        <DbImportModal
          target={importOpen}
          onClose={() => setImportOpen(null)}
          onDone={() => { setImportOpen(null); void refresh(); }}
        />
      )}
    </>
  );
}


// Replace-only import dialog. Operator pastes (or pastes-from-file)
// previously-exported JSON, types REPLACE to confirm,
// and the target table is wiped + repopulated atomically.
function DbImportModal({
  target,
  onClose,
  onDone,
}: {
  target: { kind: 'table'; tableId: string } | { kind: 'archive' };
  onClose: () => void;
  onDone: () => void;
}) {
  const [text, setText] = useState<string>('');
  const [confirm, setConfirm] = useState<string>('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<null | {
    table_id?: string;
    deleted?: number;
    inserted?: number;
    per_table?: Record<string, { deleted?: number; inserted?: number; error?: string }>;
    total_deleted?: number;
    total_inserted?: number;
    errors?: string[];
  }>(null);

  const onFile = (file: File) => {
    file.text().then(setText).catch((e) => setError(String(e)));
  };

  const submit = async () => {
    // FEUI-09: enforce the typed-REPLACE gate in the handler itself,
    // not just via the submit button's disabled attribute.
    if (confirm.trim() !== 'REPLACE') {
      setError('Type REPLACE to confirm.');
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      let payload: Record<string, unknown>;
      try {
        payload = JSON.parse(text);
      } catch (e) {
        setError(`Invalid JSON: ${e}`);
        setSubmitting(false);
        return;
      }
      let r: ReturnType<typeof Promise.resolve> extends Promise<infer _> ? _ : never;
      if (target.kind === 'table') {
        r = await api.importDatabaseTable(target.tableId, payload);
      } else {
        r = await api.importDatabaseArchive(payload);
      }
      setResult(r as any);
    } catch (e) {
      setError(String(e));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div
      style={{
        position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.5)',
        display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 50,
      }}
      onClick={onClose}
    >
      <div
        className="panel"
        style={{ maxWidth: 640, width: '90%', maxHeight: '90vh', overflowY: 'auto' }}
        onClick={(e) => e.stopPropagation()}
      >
        <h2>
          {target.kind === 'table'
            ? `Restore ${target.tableId} from JSON`
            : 'Restore full archive from JSON'}
        </h2>
        {result ? (
          <>
            {target.kind === 'table' && result.table_id ? (
              <div className="banner good" style={{ marginTop: 12 }}>
                Replaced {result.deleted} row(s) with {result.inserted} from the import.
              </div>
            ) : (
              <div className="banner good" style={{ marginTop: 12 }}>
                Archive restore: {result.total_deleted} rows replaced with{' '}
                {result.total_inserted} across {Object.keys(result.per_table || {}).length} table(s).
                {result.errors && result.errors.length > 0 && (
                  <div style={{ marginTop: 6, color: 'var(--warning, #d97706)' }}>
                    Errors: {result.errors.join('; ')}
                  </div>
                )}
              </div>
            )}
            <div className="row-buttons" style={{ marginTop: 16 }}>
              <button className="primary" onClick={onDone}>Close</button>
            </div>
          </>
        ) : (
          <>
            <p style={{ fontSize: 13, color: 'var(--text-dim)' }}>
              Replace-only restore. The target {target.kind === 'table' ? 'table' : 'tables'} will be wiped before the supplied rows are inserted.
            </p>
            <label className="field" style={{ marginTop: 8 }}>
              <span className="label">Load JSON file:</span>
              <input
                type="file"
                accept="application/json,.json"
                onChange={(e) => {
                  const f = e.target.files?.[0];
                  if (f) onFile(f);
                }}
              />
            </label>
            <label className="field" style={{ marginTop: 8 }}>
              <span className="label">Or paste JSON:</span>
              <textarea
                rows={6}
                value={text}
                onChange={(e) => setText(e.target.value)}
                placeholder='{"format": "plexbackup.dbexport.v1", ...}'
                style={{ fontFamily: 'monospace', fontSize: 11 }}
              />
            </label>
            <label className="field" style={{ marginTop: 8 }}>
              <span className="label">Type REPLACE to confirm</span>
              <input
                type="text"
                value={confirm}
                onChange={(e) => setConfirm(e.target.value)}
                placeholder="REPLACE"
              />
            </label>
            {error && (
              <div className="banner error" style={{ marginTop: 8 }}>{error}</div>
            )}
            <div className="row-buttons" style={{ marginTop: 16, display: 'flex', gap: 8 }}>
              <button onClick={onClose} disabled={submitting}>Cancel</button>
              <button
                className="danger"
                onClick={() => void submit()}
                disabled={submitting || confirm.trim() !== 'REPLACE' || !text.trim()}
              >
                {submitting ? 'Restoring…' : 'Restore'}
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
