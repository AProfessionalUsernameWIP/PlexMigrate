// Servers ▸ Advanced Settings sub-tab.
//
// Per-server overrides for snapshot-time defaults. The Run-Job form
// seeds its toggles from the resolved value when the operator picks
// a source server; scheduled jobs respect explicit per-schedule
// values first, so the resolution chain ends up:
//
//   per-job request → per-server override (this panel) → global
//   (Settings) → built-in default (Pydantic model)
//
// Layout: one row per registered server, columns per setting. Each
// cell is the override input plus a "use global" reset action. The
// resolved global default is shown as a labeled badge at the top of
// each column so the operator can see what's being inherited.

import { useEffect, useState } from 'react';
import { api, ServerView, SettingsView } from '../api';

// Per-server overrides. Each field is independently optional; absence
// means "use the global default." Mirrors the shape stored under
// settings.snapshot_defaults_per_server on the backend.
interface PerServerOverrides {
  prebuild_json_sidecar?: boolean;
  include_watch_history?: boolean;
  include_ratings?: boolean;
  include_playlists?: boolean;
  include_collections?: boolean;
  skip_playlist_prebuild?: boolean;
  fast_collection_detection?: boolean;
}

// Column descriptor drives the table render. Keeping these declarative
// lets us reuse the same toggle widget for every column without
// branching on field name in the JSX.
interface ColumnDef {
  field: keyof PerServerOverrides;
  label: string;
  // The corresponding global-default key on SettingsView, or null when
  // the field has no global (Pydantic default is the only fallback).
  globalKey: keyof SettingsView | null;
  // The built-in default the engine applies when no override and no
  // global is set. Shown in the column header so operators can see
  // exactly what kicks in for a server with no override.
  builtinDefault: boolean;
  helpText: string;
}

const COLUMNS: ColumnDef[] = [
  {
    field: 'prebuild_json_sidecar',
    label: 'JSON sidecar',
    globalKey: 'prebuild_json_sidecar_default',
    builtinDefault: false,
    helpText: 'Render a .plexexport.json next to the .db at the end of the run.',
  },
  {
    field: 'include_watch_history',
    label: 'Watch history',
    globalKey: null,
    builtinDefault: true,
    helpText: 'Capture view counts + resume positions.',
  },
  {
    field: 'include_ratings',
    label: 'Ratings',
    globalKey: null,
    builtinDefault: true,
    helpText: 'Capture star ratings.',
  },
  {
    field: 'include_playlists',
    label: 'Playlists',
    globalKey: null,
    builtinDefault: true,
    helpText: 'Capture named playlists.',
  },
  {
    field: 'include_collections',
    label: 'Collections',
    globalKey: null,
    builtinDefault: true,
    helpText: 'Capture library collections.',
  },
  {
    field: 'skip_playlist_prebuild',
    label: 'Skip prebuild',
    globalKey: null,
    builtinDefault: false,
    helpText: 'Skip the upfront playlist-cache warm. Eliminates the start-of-run stall.',
  },
  {
    field: 'fast_collection_detection',
    label: 'Fast collections',
    globalKey: null,
    builtinDefault: false,
    helpText: 'Use librarySectionUserID for collection scope. Requires Plex ≥ 1.32.',
  },
];


export function ServerAdvancedSettingsPanel() {
  const [view, setView] = useState<SettingsView | null>(null);
  const [servers, setServers] = useState<ServerView[]>([]);
  const [overrides, setOverrides] = useState<Record<string, PerServerOverrides>>({});
  const [retentionOverrides, setRetentionOverrides] = useState<Record<string, number>>({});
  const [error, setError] = useState<string | null>(null);
  const [ok, setOk] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const load = async () => {
    setError(null);
    try {
      const [s, srv] = await Promise.all([api.getSettings(), api.listServers()]);
      setView(s);
      setServers(srv);
      // settings.snapshot_defaults_per_server isn't on the typed
      // SettingsView interface yet (it's a free-form map) - read it
      // off the response with a cast. Empty/missing → empty map.
      const raw = (s as unknown as Record<string, unknown>).snapshot_defaults_per_server;
      setOverrides(
        raw && typeof raw === 'object' ? (raw as Record<string, PerServerOverrides>) : {},
      );
      setRetentionOverrides({ ...(s.snapshot_retention_per_server || {}) });
    } catch (e) {
      setError(String(e));
    }
  };
  useEffect(() => { void load(); }, []);

  // Update one server's override for one field. Tri-state cycle:
  // unset → true → false → unset. Passing ``undefined`` resets to
  // "use global default."
  const setField = (serverId: string, field: keyof PerServerOverrides, value: boolean | undefined) => {
    setOverrides((prev) => {
      const next = { ...prev };
      const row: PerServerOverrides = { ...(next[serverId] || {}) };
      if (value === undefined) {
        delete row[field];
      } else {
        row[field] = value;
      }
      // Drop the server's entry entirely once every field is unset so
      // the saved JSON stays clean.
      if (Object.keys(row).length === 0) {
        delete next[serverId];
      } else {
        next[serverId] = row;
      }
      return next;
    });
  };

  const setRetention = (serverId: string, value: number | null) => {
    setRetentionOverrides((prev) => {
      const next = { ...prev };
      if (value === null) {
        delete next[serverId];
      } else {
        next[serverId] = Math.max(1, Math.floor(value));
      }
      return next;
    });
  };

  const save = async () => {
    setError(null);
    setOk(null);
    setSaving(true);
    try {
      // Send both maps so the backend can persist atomically. The
      // existing snapshot_retention_per_server field stays separate
      // from snapshot_defaults_per_server - the resolver reads each
      // from its own map.
      const patch: Record<string, unknown> = {
        snapshot_defaults_per_server: overrides,
        snapshot_retention_per_server: retentionOverrides,
      };
      const updated = await api.saveSettings(patch);
      setView(updated);
      setOk('Advanced settings saved.');
    } catch (e) {
      setError(String(e));
    } finally {
      setSaving(false);
    }
  };

  // Resolve "what would happen for this server if I never touched the
  // override" - used to label the tri-state inherit option.
  const inheritedValue = (col: ColumnDef): boolean => {
    if (col.globalKey && view) {
      const v = view[col.globalKey];
      if (typeof v === 'boolean') return v;
    }
    return col.builtinDefault;
  };

  if (!view) {
    return <div className="panel"><div className="empty">Loading…</div></div>;
  }

  const globalRetention =
    typeof view.snapshot_retention_global === 'number' && view.snapshot_retention_global >= 1
      ? view.snapshot_retention_global
      : 30;

  return (
    <>
      {error && <div className="banner error">{error}</div>}
      {ok && <div className="banner good">{ok}</div>}

      <div className="panel">
        <h2 style={{ marginTop: 0 }}>Per-server snapshot defaults</h2>
        <span className="help" style={{ display: 'block', color: 'var(--text-dim)', fontSize: 12, marginBottom: 12 }}>
          Override snapshot-time defaults on a per-server basis. The
          <strong> Run Job</strong> form seeds its toggles from the
          resolved value here when the operator picks a source server -
          they can still flip the per-job toggle without touching this
          page. Scheduled snapshots respect their own saved values
          first; clear the schedule's checkbox to fall through to the
          per-server default.
        </span>

        {servers.length === 0 ? (
          <div className="empty">
            No registered servers yet. Add one from the <strong>Servers</strong>
            tab before configuring per-server defaults.
          </div>
        ) : (
          <div style={{ overflowX: 'auto' }}>
            <table className="list" style={{ width: '100%', tableLayout: 'fixed' }}>
              <thead>
                <tr>
                  <th style={{ minWidth: 160, textAlign: 'left' }}>Server</th>
                  {COLUMNS.map((col) => {
                    const inherited = inheritedValue(col);
                    return (
                      <th key={col.field} title={col.helpText} style={{ minWidth: 130 }}>
                        <div style={{ fontSize: 12 }}>{col.label}</div>
                        <div style={{ fontSize: 10, fontWeight: 400, color: 'var(--text-dim)' }}>
                          inherits <span className={`tag ${inherited ? 'done' : 'skipped'}`} style={{ fontSize: 10 }}>{inherited ? 'on' : 'off'}</span>
                        </div>
                      </th>
                    );
                  })}
                  <th style={{ minWidth: 130 }}>
                    <div style={{ fontSize: 12 }}>Retention</div>
                    <div style={{ fontSize: 10, fontWeight: 400, color: 'var(--text-dim)' }}>
                      global = {globalRetention}
                    </div>
                  </th>
                </tr>
              </thead>
              <tbody>
                {servers.map((s) => (
                  <tr key={s.id}>
                    <td style={{ fontWeight: 600 }}>
                      {s.name}
                      <div style={{ fontSize: 11, color: 'var(--text-dim)', fontWeight: 400 }}>{s.url}</div>
                    </td>
                    {COLUMNS.map((col) => {
                      const row = overrides[s.id] || {};
                      const value = row[col.field];
                      return (
                        <td key={col.field}>
                          <TriStateToggle
                            value={value}
                            inheritedLabel={inheritedValue(col) ? 'on' : 'off'}
                            onChange={(v) => setField(s.id, col.field, v)}
                          />
                        </td>
                      );
                    })}
                    <td>
                      <RetentionInput
                        value={retentionOverrides[s.id]}
                        global={globalRetention}
                        onChange={(v) => setRetention(s.id, v)}
                      />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        <div className="row-buttons" style={{ marginTop: 16 }}>
          <button className="primary" onClick={() => void save()} disabled={saving}>
            {saving ? 'Saving…' : 'Save'}
          </button>
          <button onClick={() => void load()} disabled={saving}>Reset</button>
        </div>
      </div>

      <div className="panel">
        <h2 style={{ marginTop: 0 }}>How resolution works</h2>
        <span className="help" style={{ display: 'block', color: 'var(--text-dim)', fontSize: 12 }}>
          For every snapshot-time setting, the engine walks this chain at fire-time
          and uses the first non-empty value:
        </span>
        <ol style={{ fontSize: 13, lineHeight: 1.7, color: 'var(--text-dim)' }}>
          <li><strong>Per-job value</strong> from the Run Job form or schedule row.</li>
          <li><strong>Per-server override</strong> from this page.</li>
          <li><strong>Global default</strong> from <em>Settings → Snapshot Defaults</em>.</li>
          <li><strong>Built-in default</strong> baked into the engine.</li>
        </ol>
      </div>
    </>
  );
}


// ── Tri-state widget ────────────────────────────────────────────────────────
// Each per-server override cell cycles: inherit → on → off → inherit.
// Inherit shows the value that'd be used today as a faint badge.

function TriStateToggle({
  value,
  inheritedLabel,
  onChange,
}: {
  value: boolean | undefined;
  inheritedLabel: string;
  onChange: (v: boolean | undefined) => void;
}) {
  // Three discrete buttons rather than a click-cycle - cycling makes
  // the operator hunt for the right state on a wide table; explicit
  // buttons are scannable and require one click to land anywhere.
  return (
    <div style={{ display: 'inline-flex', gap: 4, fontSize: 11 }}>
      <button
        type="button"
        className={value === undefined ? 'primary' : ''}
        onClick={() => onChange(undefined)}
        style={{ padding: '3px 8px', fontSize: 11 }}
        title={`Use global / built-in default (${inheritedLabel})`}
      >
        inherit
      </button>
      <button
        type="button"
        className={value === true ? 'primary' : ''}
        onClick={() => onChange(true)}
        style={{ padding: '3px 8px', fontSize: 11 }}
      >
        on
      </button>
      <button
        type="button"
        className={value === false ? 'primary' : ''}
        onClick={() => onChange(false)}
        style={{ padding: '3px 8px', fontSize: 11 }}
      >
        off
      </button>
    </div>
  );
}


// ── Retention input ─────────────────────────────────────────────────────────
// Number input + "use global" reset link. Matches the semantics of
// snapshot_registry.effective_retention_for: per-server value only
// applies when strictly lower than global.

function RetentionInput({
  value,
  global,
  onChange,
}: {
  value: number | undefined;
  global: number;
  onChange: (v: number | null) => void;
}) {
  const hasOverride = typeof value === 'number';
  const wouldBeCapped = hasOverride && (value as number) > global;
  return (
    <div style={{ display: 'inline-flex', flexDirection: 'column', gap: 2 }}>
      <div style={{ display: 'inline-flex', gap: 4, alignItems: 'center' }}>
        <input
          type="number"
          min={1}
          max={10000}
          placeholder={`global = ${global}`}
          value={hasOverride ? (value as number) : ''}
          onChange={(e) => {
            const raw = e.target.value;
            if (raw === '') {
              onChange(null);
            } else {
              const n = Number(raw);
              onChange(Number.isFinite(n) && n >= 1 ? Math.floor(n) : null);
            }
          }}
          style={{ width: 80, padding: '3px 6px', fontSize: 12 }}
        />
        {hasOverride && (
          <button
            type="button"
            onClick={() => onChange(null)}
            style={{ padding: '3px 6px', fontSize: 11 }}
            title="Clear override - use the global retention value."
          >
            reset
          </button>
        )}
      </div>
      {wouldBeCapped && (
        <span style={{ fontSize: 10, color: 'var(--text-dim)' }}>
          capped at {global}
        </span>
      )}
    </div>
  );
}
