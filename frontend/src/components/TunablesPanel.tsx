// Settings ▸ Tunables sub-tab.
//
// Root-admin-only infrastructure knobs that used to be hardcoded
// literals. Every value is hot-reloadable - services/tunables.py
// mtime-caches the settings.json read, so a save is picked up on the
// next call. The HTTP-related tunables additionally trigger
// services.auth.invalidate_sessions() so the live requests Sessions
// rebuild their adapters without a restart.
//
// Sections:
//   * Networking & Retry - HTTP timeouts, retry budget
//   * Performance Caps - worker ceilings, viewcount increment cap
//   * Polling & Maintenance - ping/scheduler/cleanup intervals
//   * Limits - retention defaults, JWT/refresh TTLs, log read cap
//   * Danger Zone (collapsed) - SQLite busy timeouts, HTTP pool sizes
//
// The Danger Zone section requires the "I understand" checkbox before
// the Save button enables. Wrong values can break Plex connectivity or
// degrade SQLite write throughput; the checkbox forces a conscious
// confirmation step.

import { useEffect, useMemo, useState } from 'react';
import { api, SettingsView, ServerView } from '../api';

// Local mirror of the tunables block. Numbers + strings; all optional.
// Missing keys fall back to the services/tunables.py defaults.
interface TunablesShape {
  // Networking & Retry
  plex_connect_timeout_seconds?: number;
  plex_retry_total_budget?: number;
  plex_retry_backoff_factor?: number;
  server_ping_timeout_seconds?: number;
  server_probe_timeout_seconds?: number;
  scrobble_get_timeout_seconds?: number;
  scrobble_put_timeout_seconds?: number;
  // Performance Caps
  workers_default_cap?: number;
  scrobble_workers_default_cap?: number;
  import_user_workers_cap?: number;
  import_library_workers_cap?: number;
  viewcount_increment_cap?: number;
  // Boolean - plexapi autoreload escape hatch. Default false (the
  // bulk-fetch + serialize paths actively disable plexapi's per-item
  // auto-reload). True returns vanilla plexapi behaviour at the cost
  // of catastrophic slowdowns on unmatched / unrated items.
  plexapi_autoreload_enabled?: boolean;
  // Cache snapshot / direct-transfer payloads into media.db on every
  // run. Default false - the snapshot .db file + JSON sidecar are
  // payload-direct (Rule 1) and don't need media.db. The engine
  // automatically seeds media.db on the first run for an unseeded
  // server regardless of this value, so the resolver Tier 0 GUID
  // cache gets populated once even with caching turned off.
  cache_snapshot_payloads_to_media_db?: boolean;
  // Polling & Maintenance
  frontend_server_ping_interval_ms?: number;
  scheduler_tick_seconds?: number;
  refresh_token_cleanup_interval_seconds?: number;
  // v0.13.x: TTL for the generated ``.plexexport.json`` sidecar.
  // 0 disables the sweep; positive values are seconds-since-mtime
  // after which the background reap deletes the file.
  snapshot_sidecar_ttl_seconds?: number;
  // Limits
  snapshot_retention_global_default?: number;
  refresh_token_ttl_seconds?: number;
  jwt_access_token_ttl_seconds?: number;
  log_read_max_bytes?: number;
  // Danger Zone
  sqlite_busy_timeout_main_seconds?: number;
  sqlite_busy_timeout_short_seconds?: number;
  http_pool_connections?: number;
  http_pool_maxsize_cap?: number;
}

// Defaults baked into the engine. Mirrors services/tunables.py _DEFAULTS.
// Used both as the initial form state when a key is absent and as the
// "use default" badge values shown next to each input.
const DEFAULTS: Required<TunablesShape> = {
  plex_connect_timeout_seconds: 120,
  plex_retry_total_budget: 4,
  plex_retry_backoff_factor: 0.5,
  server_ping_timeout_seconds: 10,
  server_probe_timeout_seconds: 15,
  scrobble_get_timeout_seconds: 10,
  scrobble_put_timeout_seconds: 15,
  workers_default_cap: 32,
  scrobble_workers_default_cap: 32,
  import_user_workers_cap: 8,
  import_library_workers_cap: 4,
  viewcount_increment_cap: 200,
  plexapi_autoreload_enabled: false,
  cache_snapshot_payloads_to_media_db: false,
  frontend_server_ping_interval_ms: 30000,
  scheduler_tick_seconds: 30,
  refresh_token_cleanup_interval_seconds: 3600,
  snapshot_sidecar_ttl_seconds: 300,
  snapshot_retention_global_default: 30,
  refresh_token_ttl_seconds: 7 * 24 * 60 * 60,
  jwt_access_token_ttl_seconds: 30 * 60,
  log_read_max_bytes: 16 * 1024 * 1024,
  sqlite_busy_timeout_main_seconds: 30,
  sqlite_busy_timeout_short_seconds: 10,
  http_pool_connections: 4,
  http_pool_maxsize_cap: 10,
};

// Field descriptor drives form rendering. ``min`` is enforced as a
// hard floor at save time; the input shows it as the html5 min.
interface FieldDef {
  key: keyof TunablesShape;
  label: string;
  unit: string;        // shown after the input ("s", "ms", "bytes", etc.)
  help: string;
  min?: number;
  step?: number;
  danger?: boolean;    // member of the Danger Zone section
}

const NETWORKING_FIELDS: FieldDef[] = [
  { key: 'plex_connect_timeout_seconds', label: 'Plex connect timeout', unit: 's', help: 'Connection timeout for new Plex API calls. Lower values surface unreachable servers faster; higher tolerates slow remote Plex setups.', min: 1, step: 1 },
  { key: 'plex_retry_total_budget', label: 'Plex retry total budget', unit: 'attempts', help: 'How many retry attempts urllib3 makes before giving up on a Plex HTTP call. Bump for 429-prone servers.', min: 0, step: 1 },
  { key: 'plex_retry_backoff_factor', label: 'Plex retry backoff factor', unit: 'multiplier', help: 'Exponential delay multiplier between retries. 0.5 means 0.5s, 1s, 2s, 4s … between attempts.', min: 0, step: 0.1 },
  { key: 'server_ping_timeout_seconds', label: 'Server ping timeout', unit: 's', help: 'Timeout for the per-server live-status ping shown in the Servers list.', min: 1, step: 1 },
  { key: 'server_probe_timeout_seconds', label: 'Server probe timeout', unit: 's', help: 'Timeout for the "Test Connection" probe in the Add Server form.', min: 1, step: 1 },
  { key: 'scrobble_get_timeout_seconds', label: 'Scrobble GET timeout', unit: 's', help: 'Timeout for direct-transfer scrobble read calls.', min: 1, step: 1 },
  { key: 'scrobble_put_timeout_seconds', label: 'Scrobble PUT timeout', unit: 's', help: 'Timeout for direct-transfer scrobble write calls.', min: 1, step: 1 },
];

const PERFORMANCE_FIELDS: FieldDef[] = [
  { key: 'workers_default_cap', label: 'Workers ceiling', unit: 'threads', help: 'Hard ceiling for the Worker threads input on Run Defaults / job forms.', min: 1, step: 1 },
  { key: 'scrobble_workers_default_cap', label: 'Scrobble workers ceiling', unit: 'threads', help: 'Hard ceiling for the Scrobble workers input.', min: 1, step: 1 },
  { key: 'import_user_workers_cap', label: 'Import user concurrency', unit: 'users', help: 'Max simultaneous home users processed during an import.', min: 1, step: 1 },
  { key: 'import_library_workers_cap', label: 'Import library concurrency', unit: 'libraries', help: 'Max simultaneous libraries processed during an import.', min: 1, step: 1 },
  { key: 'viewcount_increment_cap', label: 'Viewcount increment cap', unit: 'plays', help: 'Never POST a viewCount increment greater than this in one batch. Safety against pathological values.', min: 1, step: 1 },
];

const POLLING_FIELDS: FieldDef[] = [
  { key: 'frontend_server_ping_interval_ms', label: 'Frontend server ping interval', unit: 'ms', help: 'How often the Servers panel pings registered servers for live status. Picked up on next ServersPanel mount.', min: 1000, step: 500 },
  { key: 'scheduler_tick_seconds', label: 'Scheduler tick', unit: 's', help: 'How often the scheduler loop checks for due jobs. Re-read at top of every iteration - change takes effect within one tick.', min: 1, step: 1 },
  { key: 'refresh_token_cleanup_interval_seconds', label: 'Refresh-token cleanup cadence', unit: 's', help: 'How often the background sweep purges expired refresh tokens from auth.db.', min: 60, step: 60 },
  { key: 'snapshot_sidecar_ttl_seconds', label: 'Snapshot sidecar TTL', unit: 's', help: 'How long a generated .plexexport.json sidecar is kept on disk before the background sweep reaps it. The sweep runs once a minute, so the actual cutoff is TTL + up to 60 s. Sidecars are regenerated on demand from the snapshot .db, so reaping is non-destructive. Default 300 (5 min). Set to 0 to disable the sweep entirely (sidecars then persist until the snapshot row is removed).', min: 0, step: 30 },
];

const LIMITS_FIELDS: FieldDef[] = [
  { key: 'snapshot_retention_global_default', label: 'Snapshot retention seed', unit: 'snapshots', help: 'Initial value seeded into snapshot_retention_global on first install. After that the top-level setting on Run Defaults wins.', min: 1, step: 1 },
  { key: 'refresh_token_ttl_seconds', label: 'Refresh token TTL', unit: 's', help: 'How long a refresh-cookie session stays valid before the user must log in again. Default 7 days (604800).', min: 60, step: 3600 },
  { key: 'jwt_access_token_ttl_seconds', label: 'JWT access token TTL', unit: 's', help: 'How long an access JWT is valid before the frontend silently rotates it via /refresh. Default 30 minutes (1800).', min: 60, step: 60 },
  { key: 'log_read_max_bytes', label: 'Log viewer read cap', unit: 'bytes', help: 'Maximum bytes returned per /api/logs read. Larger files are tail-truncated in the viewer; the download endpoint always streams the full file.', min: 1024, step: 1024 },
];

const DANGER_FIELDS: FieldDef[] = [
  { key: 'sqlite_busy_timeout_main_seconds', label: 'SQLite busy timeout (main pool)', unit: 's', help: 'How long a write call waits for a held SQLite lock before raising. Too short = transient write failures under load; too long = stuck connections.', min: 1, step: 1, danger: true },
  { key: 'sqlite_busy_timeout_short_seconds', label: 'SQLite busy timeout (short-lived conns)', unit: 's', help: 'Lower-latency timeout for the short-lived read connections snapshot_registry uses for get-style queries.', min: 1, step: 1, danger: true },
  { key: 'http_pool_connections', label: 'HTTP pool connections', unit: 'pools', help: 'requests.adapters.HTTPAdapter pool_connections - how many distinct host pools the Session keeps. Wrong values waste FDs or starve concurrent Plex calls.', min: 1, step: 1, danger: true },
  { key: 'http_pool_maxsize_cap', label: 'HTTP pool max-size cap', unit: 'connections', help: 'requests.adapters.HTTPAdapter pool_maxsize - how many simultaneous connections fit in one host pool.', min: 1, step: 1, danger: true },
];


function FieldRow({
  def,
  value,
  onChange,
}: {
  def: FieldDef;
  value: number | undefined;
  onChange: (v: number | undefined) => void;
}) {
  const fallback = DEFAULTS[def.key] as number;
  const isCustom = value !== undefined && value !== fallback;
  return (
    <div className="field" style={{ marginBottom: 14 }}>
      <span className="label" title={def.help}>
        {def.label}
        {isCustom && (
          <span className="tag" style={{ marginLeft: 6, fontSize: 10 }}>custom</span>
        )}
      </span>
      <span className="help">
        {def.help}
        <span style={{ color: 'var(--text-dim)', marginLeft: 4 }}>
          (default {fallback.toLocaleString()} {def.unit})
        </span>
      </span>
      <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
        <input
          type="number"
          min={def.min}
          step={def.step}
          value={value ?? fallback}
          onChange={(e) => {
            const raw = e.target.value;
            if (raw === '') {
              onChange(undefined);
              return;
            }
            const n = Number(raw);
            if (Number.isFinite(n)) onChange(n);
          }}
          style={{ maxWidth: 180 }}
        />
        <span style={{ fontSize: 12, color: 'var(--text-dim)' }}>{def.unit}</span>
        {isCustom && (
          <button
            type="button"
            onClick={() => onChange(undefined)}
            style={{ fontSize: 11, padding: '2px 8px' }}
            title="Reset to engine default"
          >
            Reset
          </button>
        )}
      </div>
    </div>
  );
}


function FieldGroup({
  title,
  fields,
  values,
  onChange,
  danger = false,
}: {
  title: string;
  fields: FieldDef[];
  values: TunablesShape;
  onChange: (key: keyof TunablesShape, v: number | undefined) => void;
  danger?: boolean;
}) {
  return (
    <div className="panel" style={danger ? {
      border: '1px solid var(--warn, #d97706)',
      background: 'rgba(217, 119, 6, 0.04)',
    } : undefined}>
      <h2>{title}</h2>
      {fields.map((def) => (
        <FieldRow
          key={def.key}
          def={def}
          // Field defs in this component reference numeric tunables only;
          // the lone boolean (``plexapi_autoreload_enabled``) is rendered
          // separately as a checkbox. Cast is safe by construction.
          value={values[def.key] as number | undefined}
          onChange={(v) => onChange(def.key, v)}
        />
      ))}
    </div>
  );
}


// Per-server overrides for the two "Both" tunables on the user's
// checklist (plex_connect_timeout_seconds, viewcount_increment_cap).
// Map: server_id → {key: value}. Empty entries are dropped on save.
interface PerServerTunable {
  plex_connect_timeout_seconds?: number;
  viewcount_increment_cap?: number;
}

const PER_SERVER_FIELDS: Array<{
  key: keyof PerServerTunable;
  label: string;
  unit: string;
  help: string;
  min: number;
}> = [
  {
    key: 'plex_connect_timeout_seconds',
    label: 'Plex connect timeout',
    unit: 's',
    help: 'Per-server connect timeout override. Slow or remote Plex servers benefit from higher values; fast LAN servers can lower it.',
    min: 1,
  },
  {
    key: 'viewcount_increment_cap',
    label: 'Viewcount increment cap',
    unit: 'plays',
    help: 'Per-server batch cap. Weaker servers benefit from a lower number; powerful servers tolerate the global default.',
    min: 1,
  },
];


export function TunablesPanel() {
  const [view, setView] = useState<SettingsView | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [ok, setOk] = useState<string | null>(null);
  const [values, setValues] = useState<TunablesShape>({});
  const [dangerOpen, setDangerOpen] = useState(false);
  const [dangerAck, setDangerAck] = useState(false);
  const [saving, setSaving] = useState(false);

  // Per-server tunable overrides (root_admin only - gated by the same
  // settings.tunables perm the rest of this panel uses). Loaded
  // alongside the global tunables block.
  const [servers, setServers] = useState<ServerView[]>([]);
  const [perServer, setPerServer] = useState<Record<string, PerServerTunable>>({});

  const load = async () => {
    setError(null);
    try {
      const [s, srv] = await Promise.all([api.getSettings(), api.listServers()]);
      setView(s);
      setServers(srv);
      const raw = (s as unknown as Record<string, unknown>).tunables;
      const t = raw && typeof raw === 'object' ? (raw as TunablesShape) : {};
      setValues({ ...t });
      const rawPs = (s as unknown as Record<string, unknown>).tunables_per_server;
      const ps = rawPs && typeof rawPs === 'object'
        ? (rawPs as Record<string, PerServerTunable>)
        : {};
      setPerServer({ ...ps });
    } catch (e) {
      setError(String(e));
    }
  };
  useEffect(() => { void load(); }, []);

  // Update one server's override for one per-server tunable.
  const setPerServerField = (
    serverId: string,
    field: keyof PerServerTunable,
    value: number | undefined,
  ) => {
    setPerServer((prev) => {
      const next = { ...prev };
      const row: PerServerTunable = { ...(next[serverId] || {}) };
      if (value === undefined || !Number.isFinite(value)) {
        delete row[field];
      } else {
        row[field] = value;
      }
      if (Object.keys(row).length === 0) {
        delete next[serverId];
      } else {
        next[serverId] = row;
      }
      return next;
    });
  };

  // Generic so booleans (plexapi_autoreload_enabled) and numbers
  // (everything else) both type-check cleanly against TunablesShape.
  function setOne<K extends keyof TunablesShape>(
    key: K,
    v: TunablesShape[K] | undefined,
  ) {
    setValues((prev) => {
      const next: TunablesShape = { ...prev };
      if (v === undefined) {
        delete next[key];
      } else {
        next[key] = v;
      }
      return next;
    });
  }

  // Adapter for FieldGroup which is typed against numeric fields only.
  const setOneNumeric = (key: keyof TunablesShape, v: number | undefined) => {
    setOne(key, v as TunablesShape[typeof key]);
  };

  // Did the operator touch any Danger Zone field? Drives the "I
  // understand" gate on the Save button.
  const dangerTouched = useMemo(() => {
    return DANGER_FIELDS.some((f) => {
      const v = values[f.key];
      return v !== undefined && v !== (DEFAULTS[f.key] as number);
    });
  }, [values]);

  const canSave = !saving && (!dangerTouched || dangerAck);

  const save = async () => {
    setError(null);
    setOk(null);
    setSaving(true);
    try {
      const patch: Record<string, unknown> = {
        tunables: values,
        tunables_per_server: perServer,
      };
      const updated = await api.saveSettings(patch);
      setView(updated);
      setDangerAck(false);
      setOk('Tunables saved. Hot-reload applied to live readers; the next request uses the new values.');
    } catch (e) {
      setError(String(e));
    } finally {
      setSaving(false);
    }
  };

  if (!view) {
    return <div className="panel"><div className="empty">Loading…</div></div>;
  }

  return (
    <>
      {error && <div className="banner error">{error}</div>}
      {ok && <div className="banner good">{ok}</div>}

      <div className="banner info" style={{ fontSize: 13 }}>
        <strong>Root admin only.</strong> Every tunable here is hot-reloadable - a save
        flushes the in-memory cache and (for HTTP-related values) rebuilds live Plex
        sessions on the next request. No restart required.
      </div>

      <FieldGroup
        title="Networking &amp; Retry"
        fields={NETWORKING_FIELDS}
        values={values}
        onChange={setOneNumeric}
      />

      <FieldGroup
        title="Performance Caps"
        fields={PERFORMANCE_FIELDS}
        values={values}
        onChange={setOneNumeric}
      />

      {/* plexapi autoreload escape hatch. Lives inside the Performance
          Caps section conceptually but uses a checkbox (boolean) so
          it's rendered as its own small panel rather than retrofitted
          into FieldRow's numeric input. Default false (autoreload
          disabled - the right answer in 99% of cases). Flip true only
          as a diagnostic, knowing it can multiply snapshot wall-time
          by 100x on libraries with lots of unmatched / unrated items. */}
      <div className="panel">
        <h2>plexapi Auto-Reload</h2>
        {(() => {
          const fallback = DEFAULTS.plexapi_autoreload_enabled as boolean;
          const current = values.plexapi_autoreload_enabled;
          const effective = current === undefined ? fallback : current;
          const isCustom = current !== undefined && current !== fallback;
          return (
            <label className="switch" style={{ marginTop: 8 }}>
              <input
                type="checkbox"
                checked={effective}
                onChange={(e) => setOne('plexapi_autoreload_enabled', e.target.checked)}
              />
              <span>
                Enable plexapi auto-reload
                {isCustom && (
                  <span className="tag" style={{ marginLeft: 6, fontSize: 10 }}>custom</span>
                )}
                <span style={{ color: 'var(--text-dim)', marginLeft: 6, fontSize: 11 }}>
                  (default {fallback ? 'on' : 'off'})
                </span>
              </span>
              <span className="help">
                When <strong>off</strong> (default), the engine actively disables plexapi's per-item
                auto-reload on bulk-fetched lists and serialised items. This keeps snapshot runs
                fast even on libraries where most items are unmatched, unwatched, or unrated.
                When <strong>on</strong>, plexapi will reload partial objects the moment you read
                an attribute whose value is <code>None</code> - bundling <code>includeMarkers</code>
                + <code>includeChapters</code> which can trigger Plex intro/chapter analysis at
                <strong> 20–30 seconds per item</strong>. Only flip this on as a diagnostic if a
                bulk response is genuinely missing data you expect to be present.
              </span>
            </label>
          );
        })()}
      </div>

      {/* media.db caching toggle. Default off - the snapshot .db file +
          JSON sidecar are payload-direct (Rule 1) and don't need
          media.db. First-run auto-seed kicks in regardless of this
          value so the resolver Tier 0 GUID cache gets populated once
          per server even with caching turned off. */}
      <div className="panel">
        <h2>media.db Caching</h2>
        {(() => {
          const fallback = DEFAULTS.cache_snapshot_payloads_to_media_db as boolean;
          const current = values.cache_snapshot_payloads_to_media_db;
          const effective = current === undefined ? fallback : current;
          const isCustom = current !== undefined && current !== fallback;
          return (
            <label className="switch" style={{ marginTop: 8 }}>
              <input
                type="checkbox"
                checked={effective}
                onChange={(e) => setOne('cache_snapshot_payloads_to_media_db', e.target.checked)}
              />
              <span>
                Cache snapshot payloads to media.db
                {isCustom && (
                  <span className="tag" style={{ marginLeft: 6, fontSize: 10 }}>custom</span>
                )}
                <span style={{ color: 'var(--text-dim)', marginLeft: 6, fontSize: 11 }}>
                  (default {fallback ? 'on' : 'off'})
                </span>
              </span>
              <span className="help">
                When <strong>off</strong> (default), snapshot + direct-transfer runs do <strong>not</strong> ingest
                payloads into <code>media.db</code>. The snapshot <code>.db</code> file and JSON
                sidecar are unaffected - they're built directly from the live payloads (Rule 1).
                A <strong>one-shot auto-seed</strong> still ingests the first run for any unseeded
                server so the resolver Tier 0 GUID cache gets populated; subsequent runs skip the
                write until you flip this on. Turn on to keep <code>media.db</code> in sync on
                every run (useful for cross-run dedup, faster restore/direct resolution on
                changing libraries, and future sync features). Turn off when you want media.db
                to stay a one-time cache rather than a continuously-updated store.
              </span>
            </label>
          );
        })()}
      </div>

      <FieldGroup
        title="Polling &amp; Maintenance"
        fields={POLLING_FIELDS}
        values={values}
        onChange={setOneNumeric}
      />

      <FieldGroup
        title="Limits"
        fields={LIMITS_FIELDS}
        values={values}
        onChange={setOneNumeric}
      />

      {/* Per-server overrides for the "Both" tunables. Two fields only
          today (plex_connect_timeout_seconds, viewcount_increment_cap)
          - the global value lives in the sections above; per-server
          rows below apply only when set. */}
      <div className="panel">
        <h2>Per-server tunable overrides</h2>
        <span className="help" style={{ display: 'block', color: 'var(--text-dim)', fontSize: 12, marginBottom: 12 }}>
          A handful of tunables benefit from per-server values - a slow remote Plex
          server gets a higher connect timeout than a fast LAN server; a weaker server
          gets a lower viewcount batch cap. Blank cells inherit the global value above.
        </span>
        {servers.length === 0 ? (
          <div className="empty">No registered servers yet.</div>
        ) : (
          <div style={{ overflowX: 'auto' }}>
            <table className="list" style={{ width: '100%' }}>
              <thead>
                <tr>
                  <th style={{ minWidth: 160, textAlign: 'left' }}>Server</th>
                  {PER_SERVER_FIELDS.map((f) => (
                    <th key={f.key} title={f.help} style={{ minWidth: 160 }}>
                      <div style={{ fontSize: 12 }}>{f.label}</div>
                      <div style={{ fontSize: 10, fontWeight: 400, color: 'var(--text-dim)' }}>
                        global = {(values[f.key as keyof TunablesShape] ?? (DEFAULTS[f.key as keyof TunablesShape] as number)).toLocaleString()} {f.unit}
                      </div>
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {servers.map((s) => {
                  const row = perServer[s.id] || {};
                  return (
                    <tr key={s.id}>
                      <td style={{ fontWeight: 600 }}>
                        {s.name}
                        <div style={{ fontSize: 11, color: 'var(--text-dim)', fontWeight: 400 }}>{s.url}</div>
                      </td>
                      {PER_SERVER_FIELDS.map((f) => {
                        const v = row[f.key];
                        return (
                          <td key={f.key}>
                            <div style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
                              <input
                                type="number"
                                min={f.min}
                                value={v ?? ''}
                                placeholder="(inherit)"
                                onChange={(e) => {
                                  const raw = e.target.value;
                                  if (raw === '') {
                                    setPerServerField(s.id, f.key, undefined);
                                  } else {
                                    const n = Number(raw);
                                    setPerServerField(s.id, f.key, Number.isFinite(n) ? n : undefined);
                                  }
                                }}
                                style={{ maxWidth: 110 }}
                              />
                              <span style={{ fontSize: 11, color: 'var(--text-dim)' }}>{f.unit}</span>
                              {v !== undefined && (
                                <button
                                  type="button"
                                  onClick={() => setPerServerField(s.id, f.key, undefined)}
                                  style={{ fontSize: 10, padding: '2px 6px' }}
                                  title="Clear override and inherit the global value"
                                >
                                  Clear
                                </button>
                              )}
                            </div>
                          </td>
                        );
                      })}
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* Danger Zone - collapsed by default. Warning banner + the
          "I understand" gate inside. */}
      <div className="panel" style={{
        border: '1px solid var(--warn, #d97706)',
        background: 'rgba(217, 119, 6, 0.04)',
      }}>
        <h2 style={{ color: 'var(--warn, #d97706)' }}>
          ⚠ Danger Zone
        </h2>
        <div className="banner" style={{
          background: 'rgba(217, 119, 6, 0.12)',
          border: '1px solid var(--warn, #d97706)',
          color: 'var(--warn, #d97706)',
          padding: '8px 12px', borderRadius: 6, marginBottom: 12, fontSize: 12,
        }}>
          The values below control SQLite locking behaviour and the HTTP connection
          pool. Wrong values can degrade write throughput or starve concurrent Plex
          calls. Read each tooltip before changing anything.
        </div>
        <button type="button" onClick={() => setDangerOpen(!dangerOpen)}>
          {dangerOpen ? 'Hide Danger Zone fields' : 'Show Danger Zone fields'}
        </button>
        {dangerOpen && (
          <div style={{ marginTop: 12 }}>
            {DANGER_FIELDS.map((def) => (
              <FieldRow
                key={def.key}
                def={def}
                value={values[def.key] as number | undefined}
                onChange={(v) => setOneNumeric(def.key, v)}
              />
            ))}
            {dangerTouched && (
              <label className="switch" style={{ marginTop: 6 }}>
                <input
                  type="checkbox"
                  checked={dangerAck}
                  onChange={(e) => setDangerAck(e.target.checked)}
                />
                <span><strong>I understand the risks</strong> - Danger Zone values may cause SQLite lock contention or break HTTP pooling. Save is blocked until this is checked.</span>
              </label>
            )}
          </div>
        )}
      </div>

      <div className="panel" style={{ display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
        <button onClick={() => void load()} disabled={saving}>Reset</button>
        <button className="primary" onClick={() => void save()} disabled={!canSave}>
          {saving ? 'Saving…' : 'Save Tunables'}
        </button>
      </div>
    </>
  );
}
