// Scheduled snapshots - list, create, edit, delete.
//
// Each schedule fires an snapshot run on a recurring trigger. The
// scheduler lives server-side (see server/schedules.py); this panel
// is just a thin CRUD form over /api/schedules. The list at the top
// shows the next firing time so the user can confirm their schedule
// is wired correctly.

import { useEffect, useState } from 'react';
import { api, LibraryDescriptor, Schedule, ServerTime, ServerView } from '../api';
import { usePermission } from '../hooks/usePermission';
import { serverSupportsFastCollections } from '../utils/plexVersion';

export function SchedulesPanel() {
  // PR-A4 - read/write gate. ``schedules.view`` is implied to reach
  // this panel at all (App.tsx hides the sub-tab without it).
  // ``schedules.edit`` is what gates the destructive controls.
  const canEditSchedules = usePermission('schedules.edit');
  const [items, setItems] = useState<Schedule[]>([]);
  const [servers, setServers] = useState<ServerView[]>([]);
  const [libraries, setLibraries] = useState<LibraryDescriptor[]>([]);
  const [editing, setEditing] = useState<Schedule | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [serverTime, setServerTime] = useState<ServerTime | null>(null);

  const refresh = async () => {
    try {
      const [s, srv, st] = await Promise.all([
        api.listSchedules(),
        api.listServers().catch(() => [] as ServerView[]),
        api.getServerTime().catch(() => null as ServerTime | null),
      ]);
      setItems(s);
      setServers(srv);
      setServerTime(st);
    } catch (e) {
      setError(String(e));
    }
  };
  useEffect(() => { refresh(); }, []);

  // Re-fetch the server clock every 30 s so the displayed wallclock
  // doesn't drift while the panel is open.
  useEffect(() => {
    const tick = window.setInterval(() => {
      api.getServerTime().then(setServerTime).catch(() => { /* keep prior */ });
    }, 30_000);
    return () => window.clearInterval(tick);
  }, []);

  // Whenever the editor opens we (re)fetch the source server's library
  // catalogue so the picker shows the right list.
  useEffect(() => {
    if (!editing || !editing.source_server_name) {
      setLibraries([]);
      return;
    }
    const srv = servers.find((s) => s.name === editing.source_server_name);
    if (!srv) {
      setLibraries([]);
      return;
    }
    api.listServerLibraries(srv.id).then(setLibraries).catch(() => setLibraries([]));
  }, [editing, servers]);

  const startNew = () => setEditing({
    name: '',
    source_server_name: servers[0]?.name ?? '',
    libraries: [],
    output_dir: '',
    frequency: 'daily',
    hour: 3,
    minute: 0,
    day_of_week: 0,
    enabled: true,
    // PR-3 / Phase D - four-flag data-type filter on schedules.
    // Defaults match the Run-Job form: every data type migrated.
    include_watch_history: true,
    include_ratings: true,
    include_playlists: true,
    include_collections: true,
    prebuild_json_sidecar: false,
  });

  const cancelEdit = () => setEditing(null);

  const saveEdit = async () => {
    if (!editing) return;
    setError(null);
    try {
      if (editing.id) {
        await api.updateSchedule(editing.id, editing);
      } else {
        await api.createSchedule(editing);
      }
      setEditing(null);
      await refresh();
    } catch (e) {
      setError(String(e));
    }
  };

  const removeOne = async (id?: string) => {
    if (!id) return;
    if (!confirm('Delete this schedule?')) return;
    try {
      await api.deleteSchedule(id);
      await refresh();
    } catch (e) {
      setError(String(e));
    }
  };

  return (
    <>
      {error && <div className="banner error">{error}</div>}
      <div className="panel">
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12 }}>
          <h2 style={{ margin: 0 }}>Scheduled Snapshots</h2>
          {canEditSchedules && (
            <button className="primary" onClick={startNew}>+ New Schedule</button>
          )}
        </div>

        {items.length === 0 ? (
          <div className="empty">No schedules yet. Click <strong>+ New Schedule</strong> to create one.</div>
        ) : (
          <table className="list">
            <thead>
              <tr>
                <th>Name</th>
                <th>Source server</th>
                <th>Libraries</th>
                <th>Frequency</th>
                <th>Time</th>
                <th>Output</th>
                <th>Next run</th>
                <th>Enabled</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {items.map((s) => (
                <tr key={s.id}>
                  <td>{s.name}</td>
                  <td>{s.source_server_name || <span style={{ color: 'var(--bad)' }}>(not set)</span>}</td>
                  <td>{s.libraries.length === 0 ? <em>(all)</em> : s.libraries.join(', ')}</td>
                  <td>{s.frequency}{s.frequency === 'weekly' && ` (${DAYS[s.day_of_week]})`}</td>
                  <td className="mono">{pad2(s.hour)}:{pad2(s.minute)}</td>
                  <td className="mono">{s.output_dir || '(default)'}</td>
                  <td className="mono">{s.next_run_at ? formatTs(s.next_run_at) : '-'}</td>
                  <td>{s.enabled ? '✓' : '-'}</td>
                  <td>
                    <div className="row-buttons">
                      <button onClick={() => setEditing({ ...s })}>
                        {canEditSchedules ? 'Edit' : 'View'}
                      </button>
                      {canEditSchedules && (
                        <button className="danger" onClick={() => removeOne(s.id)}>Delete</button>
                      )}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {editing && (
        <ScheduleEditor
          schedule={editing}
          servers={servers}
          libraries={libraries}
          serverTime={serverTime}
          onChange={setEditing}
          onSave={saveEdit}
          onCancel={cancelEdit}
        />
      )}
    </>
  );
}

// ── Editor form ──────────────────────────────────────────────────────────────

function ScheduleEditor(props: {
  schedule: Schedule;
  servers: ServerView[];
  libraries: LibraryDescriptor[];
  serverTime: ServerTime | null;
  onChange: (s: Schedule) => void;
  onSave: () => void;
  onCancel: () => void;
}) {
  const { schedule, servers, libraries, serverTime, onChange, onSave, onCancel } = props;
  const set = <K extends keyof Schedule>(k: K, v: Schedule[K]) => onChange({ ...schedule, [k]: v });

  // Per-Run Settings collapse + sub-tab state. Workspace-only; never
  // persisted to the schedule row.
  const [perRunOpen, setPerRunOpen] = useState(false);
  const [perRunSubTab, setPerRunSubTab] = useState<'general' | 'advanced'>('general');

  // v0.14 — Fast Collection Detection version gating. When the
  // schedule's source server is at Plex ≥ 1.32, default the toggle
  // ON; below 1.32 (or unknown version) force it OFF and disable
  // the input. Re-runs every time the source server changes so
  // pointing a schedule at a different server resets the toggle.
  const sourceServer = servers.find((s) => s.name === schedule.source_server_name);
  const fastSupportVersion = sourceServer?.plex_version ?? '';
  const fastSupported = serverSupportsFastCollections(fastSupportVersion);
  useEffect(() => {
    if (!schedule.source_server_name) return;
    // Don't clobber a saved value when the operator is editing an
    // existing schedule (schedule.id present) and the value the row
    // already carries matches what we'd set. The auto-default only
    // applies on fresh server picks where the field is unset.
    if (schedule.fast_collection_detection === undefined) {
      set('fast_collection_detection', fastSupported);
    } else if (!fastSupported && schedule.fast_collection_detection) {
      // Unsupported server with stale on-flag → force off.
      set('fast_collection_detection', false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [schedule.source_server_name, fastSupported]);

  const toggleLib = (name: string) => {
    const cur = new Set(schedule.libraries);
    if (cur.has(name)) cur.delete(name); else cur.add(name);
    set('libraries', Array.from(cur));
  };

  return (
    <div className="panel">
      <h2>{schedule.id ? 'Edit Schedule' : 'New Schedule'}</h2>
      <label className="field">
        <span className="label">Name</span>
        <span className="help">Shown in the schedule list. Use anything descriptive - "Nightly full export", "Music Sundays", etc.</span>
        <input type="text" value={schedule.name} onChange={(e) => set('name', e.target.value)} />
      </label>

      <label className="field">
        <span className="label">Source server</span>
        <span className="help">Which registered server this schedule reads from. Manage servers under the <strong>Servers</strong> tab.</span>
        <select value={schedule.source_server_name || ''} onChange={(e) => set('source_server_name', e.target.value)}>
          <option value="">- pick a server -</option>
          {servers.map((s) => (
            <option key={s.id} value={s.name}>{s.name} ({s.url})</option>
          ))}
        </select>
      </label>

      <span className="label">Libraries to snapshot</span>
      <span className="help" style={{ display: 'block', color: 'var(--text-dim)', fontSize: 12, marginBottom: 6 }}>
        Leave all unchecked = snapshot every library the server reports at fire time.
      </span>
      <div className="checkbox-grid" style={{ marginBottom: 12 }}>
        {libraries.length === 0 ? (
          <div className="empty">No libraries reachable. Set Plex URL/token under Settings.</div>
        ) : libraries.map((lib) => (
          <label key={lib.name} className="switch">
            <input type="checkbox" checked={schedule.libraries.includes(lib.name)} onChange={() => toggleLib(lib.name)} />
            <span>{lib.name}</span>
            <span className="help">({lib.type})</span>
          </label>
        ))}
      </div>

      <div className="grid-2">
        <label className="field">
          <span className="label">Frequency</span>
          <select value={schedule.frequency} onChange={(e) => set('frequency', e.target.value as Schedule['frequency'])}>
            <option value="hourly">Hourly</option>
            <option value="daily">Daily</option>
            <option value="weekly">Weekly</option>
          </select>
        </label>
        <label className="field">
          <span className="label">Output directory</span>
          <span className="help">Where this schedule's <code>.plexexport.json</code> files land. Blank = use Settings default. Must be a path inside the backend container - Windows host paths (e.g. <code>Y:\…</code>) are rejected; bind-mount external drives in <code>docker-compose.yml</code> first.</span>
          <input type="text" value={schedule.output_dir || ''} onChange={(e) => set('output_dir', e.target.value || null)} placeholder="./snapshots" />
        </label>
      </div>

      {serverTime && (
        <div
          className="banner"
          style={{
            background: 'var(--panel-alt, #1b2233)',
            border: '1px solid var(--border, #2a3146)',
            padding: '8px 10px',
            borderRadius: 6,
            marginBottom: 10,
            fontSize: 12,
            color: 'var(--text-dim)',
          }}
        >
          Schedule times are interpreted in the <strong>backend's</strong> timezone:{' '}
          <strong style={{ color: 'var(--text)' }}>
            {formatServerWallclock(serverTime)}
          </strong>{' '}
          <span>({serverTime.tz}{serverTime.tz_abbrev && serverTime.tz_abbrev !== serverTime.tz ? ` · ${serverTime.tz_abbrev}` : ''})</span>.
          {serverTime.tz === 'UTC' && (
            <span style={{ display: 'block', marginTop: 4, color: 'var(--warn, #d39e3c)' }}>
              Heads up: the backend is on UTC. Set the <code>TZ</code> env var on the backend container (e.g. <code>America/Los_Angeles</code>) so this matches your local clock.
            </span>
          )}
        </div>
      )}

      <div className="grid-2">
        {schedule.frequency !== 'hourly' && (
          <label className="field">
            <span className="label">Hour (0–23, 24-hour clock)</span>
            <input type="number" min={0} max={23} value={schedule.hour} onChange={(e) => set('hour', Number(e.target.value))} />
          </label>
        )}
        <label className="field">
          <span className="label">Minute (0–59)</span>
          <input type="number" min={0} max={59} value={schedule.minute} onChange={(e) => set('minute', Number(e.target.value))} />
        </label>
        {schedule.frequency === 'weekly' && (
          <label className="field">
            <span className="label">Day of week</span>
            <select value={schedule.day_of_week} onChange={(e) => set('day_of_week', Number(e.target.value))}>
              {DAYS.map((d, i) => <option key={i} value={i}>{d}</option>)}
            </select>
          </label>
        )}
      </div>

      <label className="switch">
        <input type="checkbox" checked={schedule.enabled} onChange={(e) => set('enabled', e.target.checked)} />
        <span>Enabled</span>
        <span className="help">Disabled schedules stay in the list but the scheduler skips them.</span>
      </label>

      {/* PR-3 / Phase D - same four-flag data-type filter as the
          Run-Job form. Pre-Phase-D schedule rows arrive with these
          fields ``undefined`` and the ?? true default treats them as
          all-on (matches the pre-Phase-D every-type behaviour). */}
      <fieldset className="field" style={{ borderRadius: 8, padding: '10px 12px', margin: 0 }}>
        <legend style={{ padding: '0 6px', fontWeight: 600 }}>Data to migrate</legend>
        <span className="help" style={{ marginTop: 0 }}>
          Pick which data types this schedule's snapshots include. All four checked is the
          pre-v0.13 behaviour. At least one must remain checked.
        </span>
        <label className="switch">
          <input
            type="checkbox"
            checked={schedule.include_watch_history ?? true}
            onChange={(e) => set('include_watch_history', e.target.checked)}
          />
          <span>Watch history</span>
        </label>
        <label className="switch">
          <input
            type="checkbox"
            checked={schedule.include_ratings ?? true}
            onChange={(e) => set('include_ratings', e.target.checked)}
          />
          <span>Ratings</span>
        </label>
        <label className="switch">
          <input
            type="checkbox"
            checked={schedule.include_playlists ?? true}
            onChange={(e) => set('include_playlists', e.target.checked)}
          />
          <span>Playlists</span>
        </label>
        <label className="switch">
          <input
            type="checkbox"
            checked={schedule.include_collections ?? true}
            onChange={(e) => set('include_collections', e.target.checked)}
          />
          <span>Collections</span>
        </label>
        <label className="switch" style={{ marginTop: 8 }}>
          <input
            type="checkbox"
            checked={schedule.prebuild_json_sidecar ?? false}
            onChange={(e) => set('prebuild_json_sidecar', e.target.checked)}
          />
          <span>
            Save JSON copy after snapshot
            <span className="help" style={{ display: 'block', color: 'var(--text-dim)', fontSize: 11, marginTop: 2 }}>
              Writes a <code>.plexexport.json</code> next to the snapshot
              <code>.db</code> at the end of each scheduled run. Off by
              default - JSON is otherwise rendered on first Download click
              in the Exports tab.
            </span>
          </span>
        </label>
      </fieldset>

      {/* v0.14 — Per-Run Settings on schedules. Mirrors the Run-Job
          form's collapsible panel with the same General / Advanced
          sub-tab split. Each saved field overrides the corresponding
          global / per-server value when this schedule fires. */}
      <div className="panel" style={{ marginTop: 12 }}>
        <button
          type="button"
          onClick={() => setPerRunOpen((o) => !o)}
          aria-expanded={perRunOpen}
          style={{
            background: 'none', border: 'none', padding: 0,
            font: 'inherit', color: 'inherit', cursor: 'pointer',
            width: '100%', textAlign: 'left',
            display: 'flex', alignItems: 'center', gap: 8,
          }}
        >
          <span style={{ fontSize: 14, color: 'var(--text-dim)' }}>
            {perRunOpen ? '▾' : '▸'}
          </span>
          <h3 style={{ margin: 0 }}>Per-Run Settings</h3>
        </button>
        <span className="help" style={{ display: 'block', color: 'var(--text-dim)', fontSize: 12, marginTop: 6 }}>
          Per-fire overrides for the values set under <strong>Servers ▸ Run Defaults</strong>.
          Leave blank to inherit the global / per-server defaults at fire time.
        </span>

        {perRunOpen && (
          <div style={{ marginTop: 12 }}>
            <nav className="tabs sub-tabs" style={{ marginBottom: 12 }}>
              <button
                type="button"
                className={perRunSubTab === 'general' ? 'active' : ''}
                onClick={() => setPerRunSubTab('general')}
              >
                General
              </button>
              <button
                type="button"
                className={perRunSubTab === 'advanced' ? 'active' : ''}
                onClick={() => setPerRunSubTab('advanced')}
              >
                Advanced
              </button>
            </nav>

            {perRunSubTab === 'general' && (
              <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
                <div className="grid-2">
                  <label className="field">
                    <span className="label">Worker threads</span>
                    <span className="help">Blank = use Run Defaults.</span>
                    <input
                      type="number"
                      min={1}
                      max={128}
                      value={schedule.workers ?? ''}
                      onChange={(e) => {
                        const v = e.target.value;
                        set('workers', v === '' ? null : Number(v));
                      }}
                      placeholder="(default)"
                    />
                  </label>
                  <label className="field">
                    <span className="label">Scrobble workers</span>
                    <span className="help">Blank = use Run Defaults.</span>
                    <input
                      type="number"
                      min={1}
                      max={64}
                      value={schedule.scrobble_workers ?? ''}
                      onChange={(e) => {
                        const v = e.target.value;
                        set('scrobble_workers', v === '' ? null : Number(v));
                      }}
                      placeholder="(default)"
                    />
                  </label>
                </div>
                <label className="switch">
                  <input
                    type="checkbox"
                    checked={schedule.verbose ?? false}
                    onChange={(e) => set('verbose', e.target.checked)}
                  />
                  <span>Verbose logging</span>
                  <span className="help">DEBUG-level output for every fire of this schedule.</span>
                </label>
              </div>
            )}

            {perRunSubTab === 'advanced' && (
              <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
                <label className="field">
                  <span className="label">Log directory</span>
                  <span className="help">Where per-fire log subdirectories are created. Blank = use Run Defaults.</span>
                  <input
                    type="text"
                    value={schedule.log_dir ?? ''}
                    onChange={(e) => set('log_dir', e.target.value || null)}
                    placeholder="./plex_logs"
                  />
                </label>
                <label className="switch">
                  <input
                    type="checkbox"
                    checked={schedule.skip_playlist_prebuild ?? false}
                    onChange={(e) => set('skip_playlist_prebuild', e.target.checked)}
                  />
                  <span>Skip playlist pre-building</span>
                  <span className="help">Eliminates the start-of-run "Warming playlist cache" stall.</span>
                </label>
                <label
                  className="switch"
                  style={{ opacity: fastSupported ? 1 : 0.55 }}
                  title={
                    fastSupported
                      ? `Plex ${fastSupportVersion} supports librarySectionUserID — fast detection is available.`
                      : fastSupportVersion
                        ? `Requires Plex Media Server ≥ 1.32. Source server reports ${fastSupportVersion}.`
                        : 'Plex version unknown for this server — refresh it from the Servers tab to enable this option.'
                  }
                >
                  <input
                    type="checkbox"
                    checked={fastSupported && (schedule.fast_collection_detection ?? false)}
                    disabled={!fastSupported}
                    onChange={(e) => set('fast_collection_detection', e.target.checked)}
                  />
                  <span>
                    Fast collection detection
                    {!fastSupported && (
                      <span className="tag failed" style={{ marginLeft: 8, fontSize: 10 }}>
                        {fastSupportVersion ? 'unsupported' : 'unknown version'}
                      </span>
                    )}
                    {fastSupported && (
                      <span className="tag done" style={{ marginLeft: 8, fontSize: 10 }}>
                        Plex {fastSupportVersion}
                      </span>
                    )}
                  </span>
                  <span className="help">
                    Uses <code>librarySectionUserID</code>. Requires Plex ≥ 1.32. Defaults ON when
                    the source server supports it; greyed out below that.
                  </span>
                </label>

                <fieldset className="field" style={{ borderRadius: 8, padding: '10px 12px', margin: 0 }}>
                  <legend style={{ padding: '0 6px', fontWeight: 600 }}>Watch+Ratings capture strategy</legend>
                  <span className="help" style={{ marginTop: 0 }}>
                    Overrides the per-server / Run-Defaults value for every fire of this schedule.
                  </span>
                  <label className="switch">
                    <input
                      type="radio"
                      name={`wr-strategy-sched-${schedule.id || 'new'}`}
                      checked={!schedule.watch_ratings_filter_strategy}
                      onChange={() => set('watch_ratings_filter_strategy', '')}
                    />
                    <span>Inherit <em>(recommended)</em></span>
                  </label>
                  <label className="switch">
                    <input
                      type="radio"
                      name={`wr-strategy-sched-${schedule.id || 'new'}`}
                      checked={schedule.watch_ratings_filter_strategy === 'smart'}
                      onChange={() => set('watch_ratings_filter_strategy', 'smart')}
                    />
                    <span>Smart — engine picks per library</span>
                  </label>
                  <label className="switch">
                    <input
                      type="radio"
                      name={`wr-strategy-sched-${schedule.id || 'new'}`}
                      checked={schedule.watch_ratings_filter_strategy === 'force_bulk'}
                      onChange={() => set('watch_ratings_filter_strategy', 'force_bulk')}
                    />
                    <span>Force bulk — fewer API calls (rate-limited servers)</span>
                  </label>
                  <label className="switch">
                    <input
                      type="radio"
                      name={`wr-strategy-sched-${schedule.id || 'new'}`}
                      checked={schedule.watch_ratings_filter_strategy === 'force_server_side'}
                      onChange={() => set('watch_ratings_filter_strategy', 'force_server_side')}
                    />
                    <span>Force server-side — smaller payloads, more API calls</span>
                  </label>
                </fieldset>
              </div>
            )}
          </div>
        )}
      </div>

      <div className="row-buttons">
        <button
          className="primary"
          onClick={onSave}
          disabled={
            !schedule.name.trim() ||
            !schedule.source_server_name ||
            !(
              (schedule.include_watch_history ?? true) ||
              (schedule.include_ratings ?? true) ||
              (schedule.include_playlists ?? true) ||
              (schedule.include_collections ?? true)
            )
          }
        >
          Save
        </button>
        <button onClick={onCancel}>Cancel</button>
      </div>
    </div>
  );
}

// ── Helpers ──────────────────────────────────────────────────────────────────

const DAYS = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'];
function pad2(n: number) { return n < 10 ? `0${n}` : String(n); }
function formatTs(ts: number) {
  const d = new Date(ts * 1000);
  return d.toLocaleString();
}

// Extract HH:MM from a backend ISO-with-offset string so we render the
// server's wallclock, not the browser's. Server sends e.g.
// "2026-05-11T14:32:11-07:00"; we want "14:32".
function formatServerWallclock(st: ServerTime): string {
  const m = st.iso.match(/T(\d{2}):(\d{2})/);
  return m ? `${m[1]}:${m[2]}` : st.iso;
}
