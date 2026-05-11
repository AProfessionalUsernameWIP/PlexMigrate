// Scheduled exports — list, create, edit, delete.
//
// Each schedule fires an export run on a recurring trigger. The
// scheduler lives server-side (see server/schedules.py); this panel
// is just a thin CRUD form over /api/schedules. The list at the top
// shows the next firing time so the user can confirm their schedule
// is wired correctly.

import { useEffect, useState } from 'react';
import { api, LibraryDescriptor, Schedule, ServerView } from '../api';

export function SchedulesPanel() {
  const [items, setItems] = useState<Schedule[]>([]);
  const [servers, setServers] = useState<ServerView[]>([]);
  const [libraries, setLibraries] = useState<LibraryDescriptor[]>([]);
  const [editing, setEditing] = useState<Schedule | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = async () => {
    try {
      const [s, srv] = await Promise.all([
        api.listSchedules(),
        api.listServers().catch(() => [] as ServerView[]),
      ]);
      setItems(s);
      setServers(srv);
    } catch (e) {
      setError(String(e));
    }
  };
  useEffect(() => { refresh(); }, []);

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
          <h2 style={{ margin: 0 }}>Scheduled Exports</h2>
          <button className="primary" onClick={startNew}>+ New Schedule</button>
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
                  <td className="mono">{s.next_run_at ? formatTs(s.next_run_at) : '—'}</td>
                  <td>{s.enabled ? '✓' : '—'}</td>
                  <td>
                    <div className="row-buttons">
                      <button onClick={() => setEditing({ ...s })}>Edit</button>
                      <button className="danger" onClick={() => removeOne(s.id)}>Delete</button>
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
  onChange: (s: Schedule) => void;
  onSave: () => void;
  onCancel: () => void;
}) {
  const { schedule, servers, libraries, onChange, onSave, onCancel } = props;
  const set = <K extends keyof Schedule>(k: K, v: Schedule[K]) => onChange({ ...schedule, [k]: v });

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
        <span className="help">Shown in the schedule list. Use anything descriptive — "Nightly full backup", "Music Sundays", etc.</span>
        <input type="text" value={schedule.name} onChange={(e) => set('name', e.target.value)} />
      </label>

      <label className="field">
        <span className="label">Source server</span>
        <span className="help">Which registered server this schedule reads from. Manage servers under the <strong>Servers</strong> tab.</span>
        <select value={schedule.source_server_name || ''} onChange={(e) => set('source_server_name', e.target.value)}>
          <option value="">— pick a server —</option>
          {servers.map((s) => (
            <option key={s.id} value={s.name}>{s.name} ({s.url})</option>
          ))}
        </select>
      </label>

      <span className="label">Libraries to export</span>
      <span className="help" style={{ display: 'block', color: 'var(--text-dim)', fontSize: 12, marginBottom: 6 }}>
        Leave all unchecked = export every library the server reports at fire time.
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
          <span className="help">Where this schedule's <code>.plexbackup.json</code> files land. Blank = use Settings default.</span>
          <input type="text" value={schedule.output_dir || ''} onChange={(e) => set('output_dir', e.target.value || null)} placeholder="./plex_exports" />
        </label>
      </div>

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

      <div className="row-buttons">
        <button
          className="primary"
          onClick={onSave}
          disabled={!schedule.name.trim() || !schedule.source_server_name}
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
