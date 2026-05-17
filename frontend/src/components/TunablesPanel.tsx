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

import { useContext, useEffect, useMemo, useState } from 'react';
import { api, SettingsView, ServerView } from '../api';
import { TooltipContext } from '../contexts/TooltipContext';
import { InfoTip } from './InfoTip';

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
  // Application-log rotation tunables (services/db_access_log.py and
  // future app-log writers). Sized so a chatty audit trail can still
  // reach a useful history without ballooning disk usage.
  log_rotate_max_size_mb?: number;
  log_rotate_backup_count?: number;
  // Danger Zone
  sqlite_busy_timeout_main_seconds?: number;
  sqlite_busy_timeout_short_seconds?: number;
  http_pool_connections?: number;
  http_pool_maxsize_cap?: number;
  // Activity-feed: how the Plex owner is rendered in dashboard activity
  // entries. "plex_owner" keeps the legacy label; "custom_name" uses
  // the end user-configured display name (falls back to "Plex Owner");
  // "custom_name_owner" appends " (owner)" for disambiguation.
  owner_display_style?: 'plex_owner' | 'custom_name' | 'custom_name_owner';
  // Schedules tab: how the Run-Job-shaped editor renders. "full" keeps
  // the editor open on tab load (bound to selected schedule or a new-
  // schedule draft); "on_create" hides it until New / Edit is clicked.
  schedules_editor_visibility?: 'full' | 'on_create';
  // Danger Zone: developer-mode runtime toggle. Default false.
  // The env var PLEXMIGRATE_DEBUG_MODE takes precedence when set
  // truthy on the backend container, so this tunable only flips the
  // surface when the env var is unset. Gated by the Danger Zone's
  // "I understand" checkbox + root_admin permission.
  developer_mode_enabled?: boolean;
  // UI & Display caps (Phase 3 of the dashboard/log reorg). Numeric
  // tunables consumed by frontend panels:
  // - etr_color_multiplier scales the ETR amber/red thresholds.
  // - restoration_summary_panel_max_items: visible-row cap for the
  //   in-dashboard Restoration Summary panel (rest scrolls inside).
  // - user_count_tooltip_delay_ms: hover delay before user-count
  //   tooltips reveal their list.
  // - user_count_tooltip_max_items: list cap before the "... +N more"
  //   overflow line in user-count tooltips.
  etr_color_multiplier?: number;
  restoration_summary_panel_max_items?: number;
  user_count_tooltip_delay_ms?: number;
  user_count_tooltip_max_items?: number;
  // Servers ▸ Overview table: when true, render a "UID" column showing
  // each server's prefixed id (e.g. plex_a1b2c3 / jellyfin_d4e5f6).
  // End user-facing tunable shipped by developer 2026-05-16; default
  // false (no UI change on upgrade).
  servers_panel_show_server_uid?: boolean;
  // Adaptive ETA / training engine knobs (Danger Zone). Shape the
  // engine-anchored ETA the dashboard's "Estimated remaining" reads.
  // Surfaced under Danger because tuning these can make the
  // displayed remaining misleading; change only with intent.
  eta_wallclock_floor_fraction?: number;
  eta_calibration_ratio_min?: number;
  eta_calibration_ratio_max?: number;
  eta_calibration_blend_threshold?: number;
  eta_almost_done_progress_threshold?: number;
  // Per-label cold-start defaults dict. Not edited inline today;
  // end users who need to tune individual labels must use the
  // settings.json file directly. Surface placeholder so the type
  // round-trips through the panel without dropping the field.
  eta_tier5_defaults_seconds?: Record<string, number>;
  // ── Plan[PLAYLIST-MANAGEMENT] (developer commit 2026-05-16) ──
  playlist_cache_enabled?: boolean;
  playlist_cache_refresh_interval_seconds?: number;
  playlist_cache_snapshot_threshold_seconds?: number;
  playlist_cache_max_age_seconds?: number;
  playlist_cache_background_refresh_enabled?: boolean;
  playlist_mgmt_plex_home_auth_mode?: 'owner_token' | 'per_user_token';
  playlist_mgmt_same_user_behavior?: 'skip' | 'duplicate';
  // ── Plan[MIXED-MEDIA-PLAYLISTS] (developer commit 2026-05-16) ──
  mixed_media_behavior?: 'skip' | 'dominant' | 'split';
  mixed_media_dominance_threshold?: number;
  mixed_media_video_routing?: 'library_agnostic' | 'library_dominant';
  mixed_media_collision_handling?: 'duplicate' | 'suffix' | 'skip';
  mixed_media_logging?: 'full' | 'decisions_only' | 'off';
  // ── USER-MGMT-IDENTITY-AUDIT (developer commit 2026-05-16) ──
  // strict_identity_resolution: when true, the cross-server user
  // resolver refuses to fall back to case-insensitive username
  // matching; only explicit identity_map rows + backend_user_id
  // direct matches route users. log_use_display_name: cosmetic
  // substitution of managed_users.display_name for the raw username
  // in log lines. Both default false to preserve legacy behaviour.
  strict_identity_resolution?: boolean;
  log_use_display_name?: boolean;
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
  log_rotate_max_size_mb: 50,
  log_rotate_backup_count: 5,
  sqlite_busy_timeout_main_seconds: 30,
  sqlite_busy_timeout_short_seconds: 10,
  http_pool_connections: 4,
  http_pool_maxsize_cap: 10,
  owner_display_style: 'plex_owner',
  schedules_editor_visibility: 'full',
  developer_mode_enabled: false,
  etr_color_multiplier: 1.0,
  restoration_summary_panel_max_items: 10,
  user_count_tooltip_delay_ms: 600,
  user_count_tooltip_max_items: 20,
  servers_panel_show_server_uid: false,
  eta_wallclock_floor_fraction: 0.15,
  eta_calibration_ratio_min: 0.25,
  eta_calibration_ratio_max: 4.0,
  eta_calibration_blend_threshold: 0.25,
  eta_almost_done_progress_threshold: 0.85,
  eta_tier5_defaults_seconds: {
    snapshot_watch_history: 120.0,
    snapshot_ratings: 90.0,
    snapshot_playlists: 45.0,
    snapshot_collections: 45.0,
    bulk_fetch_for_filters: 30.0,
    restore_watch_history: 180.0,
    restore_ratings: 120.0,
    restore_playlists: 90.0,
    restore_collections: 90.0,
    direct_library_transfer: 240.0,
  },
  // Plan[PLAYLIST-MANAGEMENT] 2026-05-16 (developer lock):
  playlist_cache_enabled: true,
  playlist_cache_refresh_interval_seconds: 1800,
  playlist_cache_snapshot_threshold_seconds: 900,
  playlist_cache_max_age_seconds: 43200,
  playlist_cache_background_refresh_enabled: false,
  playlist_mgmt_plex_home_auth_mode: 'owner_token',
  playlist_mgmt_same_user_behavior: 'skip',
  // Plan[MIXED-MEDIA-PLAYLISTS] 2026-05-16 (developer lock):
  mixed_media_behavior: 'skip',
  mixed_media_dominance_threshold: 0.60,
  mixed_media_video_routing: 'library_agnostic',
  mixed_media_collision_handling: 'duplicate',
  mixed_media_logging: 'full',
  // USER-MGMT-IDENTITY-AUDIT 2026-05-16 (developer):
  strict_identity_resolution: false,
  log_use_display_name: false,
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
  { key: 'log_rotate_max_size_mb', label: 'App log max size', unit: 'MB', help: 'Application logs (db_access.log today, future auth/network/debug writers) roll to a backup file when they exceed this size. Default 50 MB.', min: 1, step: 1 },
  { key: 'log_rotate_backup_count', label: 'App log backups kept', unit: 'files', help: 'How many rolled-over copies are retained per application log. 0 disables retention beyond the active file. Default 5.', min: 0, step: 1 },
];

const UI_DISPLAY_FIELDS: FieldDef[] = [
  { key: 'etr_color_multiplier', label: 'ETR colour multiplier', unit: 'x', help: 'Scales the dashboard’s per-phase amber/red thresholds. 1.0 = ship defaults. <1.0 warns sooner; >1.0 is more lenient. Clamped to [0.5, 2.0] at the read boundary.', min: 0.5, step: 0.1 },
  { key: 'restoration_summary_panel_max_items', label: 'Restoration summary cap', unit: 'rows', help: 'Visible-row cap for the in-dashboard Restoration Summary panel. The rest are scrollable inside the panel. Clamped to [1, 500].', min: 1, step: 1 },
  { key: 'user_count_tooltip_delay_ms', label: 'User-count tooltip delay', unit: 'ms', help: 'Hover delay before the user-count tooltip reveals its list. Long enough that incidental hover doesn’t fire, short enough to feel responsive. Clamped to [100, 3000].', min: 100, step: 50 },
  { key: 'user_count_tooltip_max_items', label: 'User-count tooltip cap', unit: 'names', help: 'How many user names the tooltip lists before the "… +N more" overflow line. Clamped to [1, 200].', min: 1, step: 1 },
];

// Six sub-tab structure for the Tunables page. Replaces the old single-
// scroll layout. Each tab renders its own field group + any associated
// boolean / string-enum panels. The Save button operates on the whole
// document regardless of which tab is visible, so values entered on
// one tab persist after switching to another.
type TunablesTab =
  | 'networking'
  | 'performance'
  | 'polling'
  | 'limits'
  | 'ui'
  | 'playlist'
  | 'danger';

const TAB_DEFS: Array<{ id: TunablesTab; label: string; danger?: boolean }> = [
  { id: 'networking',  label: 'Networking' },
  { id: 'performance', label: 'Performance' },
  { id: 'polling',     label: 'Polling' },
  { id: 'limits',      label: 'Limits' },
  { id: 'ui',          label: 'UI & Display' },
  { id: 'playlist',    label: 'Playlist & Mixed-Media' },
  { id: 'danger',      label: 'Danger',      danger: true },
];

// Plan[PLAYLIST-MANAGEMENT] + Plan[MIXED-MEDIA-PLAYLISTS] numeric
// tunables. String-enum fields render as dedicated <select> blocks
// in the tab body (same pattern as owner_display_style).
const PLAYLIST_NUMERIC_FIELDS: FieldDef[] = [
  { key: 'playlist_cache_refresh_interval_seconds', label: 'Cache refresh interval', unit: 's', help: 'How fresh a per-(server, user) playlist cache must be before the UI surfaces a "stale" badge. Default 1800 (30 min). Lower = more API calls but fresher data; higher = fewer calls but operator may see stale lists.', min: 60, step: 60 },
  { key: 'playlist_cache_snapshot_threshold_seconds', label: 'Snapshot cache threshold', unit: 's', help: 'During a snapshot capture, the engine uses the playlist cache if it was refreshed within this window. Default 900 (15 min). Tighter than the general refresh interval so snapshots stay accurate.', min: 60, step: 60 },
  { key: 'playlist_cache_max_age_seconds', label: 'Cache hard invalidate', unit: 's', help: 'Cache rows older than this are dropped on read regardless of refresh interval. Default 43200 (12 hours). Defense against silently-stale data when an operator hasn\'t touched playlists in a while.', min: 60, step: 600 },
  { key: 'mixed_media_dominance_threshold', label: 'Mixed-media dominance threshold', unit: 'ratio', help: 'In "dominant" behavior mode, the playlist\'s top media type must represent at least this fraction of all items for the dominant strategy to apply. Below the threshold, falls back to "split" for the tied types. Default 0.60.', min: 0.5, step: 0.05 },
];

const DANGER_FIELDS: FieldDef[] = [
  { key: 'sqlite_busy_timeout_main_seconds', label: 'SQLite busy timeout (main pool)', unit: 's', help: 'How long a write call waits for a held SQLite lock before raising. Too short = transient write failures under load; too long = stuck connections.', min: 1, step: 1, danger: true },
  { key: 'sqlite_busy_timeout_short_seconds', label: 'SQLite busy timeout (short-lived conns)', unit: 's', help: 'Lower-latency timeout for the short-lived read connections snapshot_registry uses for get-style queries.', min: 1, step: 1, danger: true },
  { key: 'http_pool_connections', label: 'HTTP pool connections', unit: 'pools', help: 'requests.adapters.HTTPAdapter pool_connections - how many distinct host pools the Session keeps. Wrong values waste FDs or starve concurrent Plex calls.', min: 1, step: 1, danger: true },
  { key: 'http_pool_maxsize_cap', label: 'HTTP pool max-size cap', unit: 'connections', help: 'requests.adapters.HTTPAdapter pool_maxsize - how many simultaneous connections fit in one host pool.', min: 1, step: 1, danger: true },
  // Adaptive ETA / training engine. These shape the dashboard's
  // "Estimated remaining" headline.
  { key: 'eta_wallclock_floor_fraction', label: 'ETA wall-clock floor', unit: 'fraction', help: 'Floor for the predicted ETR during the pre-discovery window (before the engine has discovered work to anchor against). Expressed as a fraction of the original prediction. Default 0.15 prevents an undershot cold-start prediction from decaying to 0 and surfacing "Almost done" prematurely.', min: 0, step: 0.05, danger: true },
  { key: 'eta_calibration_ratio_min', label: 'ETA calibration ratio (min)', unit: 'x', help: 'Lower clamp on the actual/expected ratio used to calibrate the anchored ETR mid-run. 0.25 means the displayed remaining never drops below 25% of the engine\'s predicted remaining, regardless of how fast the run appears to be going.', min: 0.05, step: 0.05, danger: true },
  { key: 'eta_calibration_ratio_max', label: 'ETA calibration ratio (max)', unit: 'x', help: 'Upper clamp on the actual/expected ratio. 4.0 means the displayed remaining never exceeds 4x the engine\'s predicted remaining; protects against one anomalous slow phase teleporting the ETA.', min: 1, step: 0.5, danger: true },
  { key: 'eta_calibration_blend_threshold', label: 'ETA calibration blend point', unit: 'fraction', help: 'Completion fraction at which the run-pace calibration reaches full weight. Smaller = reacts to actual pace faster but jumpier on noisy early ticks; larger = trusts the cold-start prediction longer.', min: 0.05, step: 0.05, danger: true },
  { key: 'eta_almost_done_progress_threshold', label: 'Almost-done progress gate', unit: 'fraction', help: 'Library-completion fraction below which the dashboard refuses to display "Almost done", even if the projected ETR drops below 5 seconds. Raising this requires more progress evidence before the copy fires.', min: 0, step: 0.05, danger: true },
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
  // Six sub-tab navigation: which group of tunables is visible.
  // Save persists every field; switching tabs never discards local edits.
  const [activeTab, setActiveTab] = useState<TunablesTab>('networking');
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

  // Item 5 + Item 2-followup + Item 3-followup: three top-level
  // boolean toggles. These live OUTSIDE the ``tunables`` nested block
  // because they're persisted as top-level settings keys.
  const [tooltipsEnabled, setTooltipsEnabled] = useState(true);
  const [autoRotateOnRefresh, setAutoRotateOnRefresh] = useState(false);
  const [pinUsernameFallback, setPinUsernameFallback] = useState(false);
  const tooltipCtx = useContext(TooltipContext);

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
      // Item 5 toggles. Defaults match persistence.py: tooltips on,
      // rotation off, username-fallback off.
      setTooltipsEnabled(s.tooltips_enabled ?? true);
      setAutoRotateOnRefresh(s.auto_rotate_tokens_on_refresh ?? false);
      setPinUsernameFallback(s.pin_migration_allow_username_fallback ?? false);
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

  // Did the end user touch any Danger Zone field? Drives the "I
  // understand" gate on the Save button. The numeric DANGER_FIELDS
  // are checked first; the developer_mode_enabled boolean toggle
  // counts as a Danger Zone touch whenever it's flipped on.
  const dangerTouched = useMemo(() => {
    const numericTouched = DANGER_FIELDS.some((f) => {
      const v = values[f.key];
      return v !== undefined && v !== (DEFAULTS[f.key] as number);
    });
    const devModeTouched = (
      values.developer_mode_enabled !== undefined
      && values.developer_mode_enabled !== DEFAULTS.developer_mode_enabled
    );
    return numericTouched || devModeTouched;
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
        tooltips_enabled: tooltipsEnabled,
        auto_rotate_tokens_on_refresh: autoRotateOnRefresh,
        pin_migration_allow_username_fallback: pinUsernameFallback,
      };
      const updated = await api.saveSettings(patch);
      setView(updated);
      setDangerAck(false);
      // Push the tooltips toggle into the live context so the (?)
      // icons update across the rest of the UI without a reload.
      tooltipCtx.setEnabled(tooltipsEnabled);
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

      {/* Sub-tab navigation. Each tab scopes the visible field groups
          below. Save is shared across tabs - edits made on one tab
          stay in the `values` state and ship with the next save no
          matter which tab is active at click time. */}
      <div className="panel" style={{ padding: '8px 12px' }}>
        <div role="tablist" style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
          {TAB_DEFS.map((t) => {
            const isActive = activeTab === t.id;
            return (
              <button
                key={t.id}
                type="button"
                role="tab"
                aria-selected={isActive}
                onClick={() => setActiveTab(t.id)}
                style={{
                  padding: '6px 14px',
                  border: '1px solid ' + (
                    t.danger
                      ? 'var(--warn, #d97706)'
                      : (isActive ? 'var(--accent, #3b82f6)' : 'var(--border, #444)')
                  ),
                  background: isActive
                    ? (t.danger ? 'rgba(217, 119, 6, 0.12)' : 'rgba(59, 130, 246, 0.10)')
                    : 'transparent',
                  color: t.danger ? 'var(--warn, #d97706)' : undefined,
                  borderRadius: 4,
                  fontWeight: isActive ? 600 : 400,
                  cursor: 'pointer',
                }}
              >
                {t.label}
              </button>
            );
          })}
        </div>
      </div>

      {activeTab === 'networking' && (
        <FieldGroup
          title="Networking &amp; Retry"
          fields={NETWORKING_FIELDS}
          values={values}
          onChange={setOneNumeric}
        />
      )}

      {activeTab === 'performance' && (
        <FieldGroup
          title="Performance Caps"
          fields={PERFORMANCE_FIELDS}
          values={values}
          onChange={setOneNumeric}
        />
      )}

      {/* plexapi autoreload escape hatch. Lives inside the Performance
          Caps section conceptually but uses a checkbox (boolean) so
          it's rendered as its own small panel rather than retrofitted
          into FieldRow's numeric input. Default false (autoreload
          disabled - the right answer in 99% of cases). Flip true only
          as a diagnostic, knowing it can multiply snapshot wall-time
          by 100x on libraries with lots of unmatched / unrated items. */}
      {activeTab === 'performance' && (
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
      )}

      {/* media.db caching toggle. Default off - the snapshot .db file +
          JSON sidecar are payload-direct (Rule 1) and don't need
          media.db. First-run auto-seed kicks in regardless of this
          value so the resolver Tier 0 GUID cache gets populated once
          per server even with caching turned off. */}
      {activeTab === 'performance' && (
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
      )}

      {/* Activity-feed owner display style. String-enum, not numeric -
          rendered as a small dedicated select so it lives in the
          UI & Display tab next to the other operator-visible knobs. */}
      {activeTab === 'ui' && (
      <div className="panel">
        <h2>Activity Feed: Owner Display</h2>
        {(() => {
          const fallback = DEFAULTS.owner_display_style;
          const current = values.owner_display_style;
          const effective = current === undefined ? fallback : current;
          const isCustom = current !== undefined && current !== fallback;
          return (
            <div className="field" style={{ marginTop: 8 }}>
              <span className="label">
                Owner label style
                {isCustom && (
                  <span className="tag" style={{ marginLeft: 6, fontSize: 10 }}>custom</span>
                )}
                <span style={{ color: 'var(--text-dim)', marginLeft: 6, fontSize: 11 }}>
                  (default {fallback})
                </span>
              </span>
              <span className="help">
                How the Plex owner is rendered in dashboard activity feed
                entries and the per-run log lines that show owner-phase work.
                Engine logs, export files, and database rows always use raw
                Plex identifiers regardless of this choice.
                <br />
                <strong>plex_owner</strong> shows the literal "Plex Owner" always.
                <br />
                <strong>custom_name</strong> substitutes the operator-configured
                display name (Servers ▸ Managed Users) for the owner's Plex.tv
                email when one is set. Falls back to "Plex Owner" otherwise.
                <br />
                <strong>custom_name_owner</strong> as above plus a " (owner)"
                suffix so the owner stays unambiguous when a managed user has
                a similar custom name.
              </span>
              <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                <select
                  value={effective}
                  onChange={(e) => {
                    const v = e.target.value as TunablesShape['owner_display_style'];
                    setOne('owner_display_style', v);
                  }}
                  style={{ maxWidth: 240 }}
                >
                  <option value="plex_owner">plex_owner (default)</option>
                  <option value="custom_name">custom_name</option>
                  <option value="custom_name_owner">custom_name_owner</option>
                </select>
                {isCustom && (
                  <button
                    type="button"
                    onClick={() => setOne('owner_display_style', undefined)}
                    style={{ fontSize: 11, padding: '2px 8px' }}
                    title="Reset to engine default"
                  >
                    Reset
                  </button>
                )}
              </div>
            </div>
          );
        })()}
      </div>
      )}

      {/* Schedules sub-tab editor visibility. String-enum, rendered
          alongside the other end user-visible UI knobs. The Schedules
          rebuild (Run-Job-mirror layout) is the primary consumer; the
          tunable's effect lights up once SchedulesPanel is wired to
          read it. */}
      {activeTab === 'ui' && (
      <div className="panel">
        <h2>Schedules: Editor Visibility</h2>
        {(() => {
          const fallback = DEFAULTS.schedules_editor_visibility;
          const current = values.schedules_editor_visibility;
          const effective = current === undefined ? fallback : current;
          const isCustom = current !== undefined && current !== fallback;
          return (
            <div className="field" style={{ marginTop: 8 }}>
              <span className="label">
                Editor mode on Schedules tab
                {isCustom && (
                  <span className="tag" style={{ marginLeft: 6, fontSize: 10 }}>custom</span>
                )}
                <span style={{ color: 'var(--text-dim)', marginLeft: 6, fontSize: 11 }}>
                  (default {fallback})
                </span>
              </span>
              <span className="help">
                Controls how the Run-Job-shaped editor renders on the Schedules
                sub-tab.
                <br />
                <strong>full</strong> keeps the editor open on tab load, bound
                to the currently selected schedule (or to a "new schedule"
                draft if no row is selected). The full job spec is visible at
                a glance without an extra click.
                <br />
                <strong>on_create</strong> hides the editor until "+ New
                Schedule" or "Edit" is clicked on a saved row. Matches the
                pre-redesign inline-editor flow.
              </span>
              <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                <select
                  value={effective}
                  onChange={(e) => {
                    const v = e.target.value as TunablesShape['schedules_editor_visibility'];
                    setOne('schedules_editor_visibility', v);
                  }}
                  style={{ maxWidth: 240 }}
                >
                  <option value="full">full (default)</option>
                  <option value="on_create">on_create</option>
                </select>
                {isCustom && (
                  <button
                    type="button"
                    onClick={() => setOne('schedules_editor_visibility', undefined)}
                    style={{ fontSize: 11, padding: '2px 8px' }}
                    title="Reset to engine default"
                  >
                    Reset
                  </button>
                )}
              </div>
            </div>
          );
        })()}
      </div>
      )}

      {activeTab === 'ui' && (
        <FieldGroup
          title="UI &amp; Display caps"
          fields={UI_DISPLAY_FIELDS}
          values={values}
          onChange={setOneNumeric}
        />
      )}

      {activeTab === 'polling' && (
        <FieldGroup
          title="Polling &amp; Maintenance"
          fields={POLLING_FIELDS}
          values={values}
          onChange={setOneNumeric}
        />
      )}

      {activeTab === 'limits' && (
        <FieldGroup
          title="Limits"
          fields={LIMITS_FIELDS}
          values={values}
          onChange={setOneNumeric}
        />
      )}

      {/* Item 1 + 2-followup + 3-followup + 5: top-level boolean
          toggles that don't fit the FieldGroup numeric shape. Each
          governs a real behaviour described in the InfoTip; all four
          ship root_admin-only because they live alongside the
          settings.tunables permission. */}
      {activeTab === 'ui' && (
      <div className="panel">
        <h2 style={{ marginTop: 0 }}>Admin &amp; UX toggles</h2>
        <span className="help" style={{ display: 'block', color: 'var(--text-dim)', fontSize: 12, marginBottom: 12 }}>
          A small group of boolean preferences that don't fit the numeric tunable shape.
          All four are root-admin-only and save together with the rest of the page.
        </span>
        <label className="switch">
          <input
            type="checkbox"
            checked={tooltipsEnabled}
            onChange={(e) => setTooltipsEnabled(e.target.checked)}
          />
          <span>
            Tooltips enabled
            <InfoTip topicId="tooltips-enabled" />
          </span>
          <span className="help">When off, the small (?) icons hide site-wide. The Help tab still has every topic.</span>
        </label>
        <label className="switch">
          <input
            type="checkbox"
            checked={autoRotateOnRefresh}
            onChange={(e) => setAutoRotateOnRefresh(e.target.checked)}
          />
          <span>
            Auto-rotate user tokens on Refresh
            <InfoTip topicId="auto-rotate-tokens-on-refresh" />
          </span>
          <span className="help">When off (default) Refresh only adds missing tokens. When on, it also overwrites changed tokens.</span>
        </label>
        <label className="switch">
          <input
            type="checkbox"
            checked={pinUsernameFallback}
            onChange={(e) => setPinUsernameFallback(e.target.checked)}
          />
          <span>
            PIN migration: username-string fallback
            <InfoTip topicId="pin-migration-username-fallback" />
          </span>
          <span className="help">When off (default) PIN migration matches users by Plex user ID only. When on, plain username matches are also suggested.</span>
        </label>
        <label className="switch">
          <input
            type="checkbox"
            checked={values.servers_panel_show_server_uid ?? DEFAULTS.servers_panel_show_server_uid}
            onChange={(e) => setOne('servers_panel_show_server_uid', e.target.checked)}
          />
          <span>Servers panel: show server UID column</span>
          <span className="help">
            Adds a UID column to the Servers ▸ Overview table showing each server's stable id
            (format: <code>&lt;backend&gt;_&lt;uuid&gt;</code>). Useful when diagnosing schedules,
            API calls, or log lines that reference servers by id. Off by default.
          </span>
        </label>
      </div>
      )}

      {/* USER-MGMT-IDENTITY-AUDIT (developer, 2026-05-16): identity +
          log presentation toggles. Both are root-admin only and
          default false. Grouped together because they are the two
          knobs end users reach for when tuning how the engine names
          and routes users across servers. */}
      {activeTab === 'ui' && (
      <div className="panel">
        <h2 style={{ marginTop: 0 }}>Identity &amp; log presentation</h2>
        <span className="help" style={{ display: 'block', color: 'var(--text-dim)', fontSize: 12, marginBottom: 12 }}>
          How the engine resolves users across servers and how user references read in log
          lines. Identity-map routing is the authoritative path either way; these toggles
          decide what happens when the map does not have a row for a user.
        </span>
        {(() => {
          const fallback = DEFAULTS.strict_identity_resolution as boolean;
          const current = values.strict_identity_resolution;
          const effective = current === undefined ? fallback : current;
          const isCustom = current !== undefined && current !== fallback;
          return (
            <label className="switch">
              <input
                type="checkbox"
                checked={effective}
                onChange={(e) => setOne('strict_identity_resolution', e.target.checked)}
              />
              <span>
                Strict identity resolution
                {isCustom && (
                  <span className="tag" style={{ marginLeft: 6, fontSize: 10 }}>custom</span>
                )}
                <span style={{ color: 'var(--text-dim)', marginLeft: 6, fontSize: 11 }}>
                  (default {fallback ? 'on' : 'off'})
                </span>
              </span>
              <span className="help">
                When <strong>off</strong> (default), the cross-server user resolver runs its full
                5-step chain: per-job override &rarr; identity_map &rarr; backend_user_id direct
                match &rarr; case-insensitive username &rarr; single-admin owner. The case-
                insensitive username step catches users that have not been explicitly mapped yet.
                <br /><br />
                When <strong>on</strong>, the resolver stops after the backend_user_id step.
                Users not covered by an identity_map row (manual or auto_copy) AND not matched
                by their backend-native user ID are explicitly skipped and surfaced in the run
                log. Use this when every cross-server routing decision must come from an
                explicit operator-authored mapping or a confirmed cross-platform preflight.
              </span>
            </label>
          );
        })()}
        {(() => {
          const fallback = DEFAULTS.log_use_display_name as boolean;
          const current = values.log_use_display_name;
          const effective = current === undefined ? fallback : current;
          const isCustom = current !== undefined && current !== fallback;
          return (
            <label className="switch">
              <input
                type="checkbox"
                checked={effective}
                onChange={(e) => setOne('log_use_display_name', e.target.checked)}
              />
              <span>
                Use display name in logs
                {isCustom && (
                  <span className="tag" style={{ marginLeft: 6, fontSize: 10 }}>custom</span>
                )}
                <span style={{ color: 'var(--text-dim)', marginLeft: 6, fontSize: 11 }}>
                  (default {fallback ? 'on' : 'off'})
                </span>
              </span>
              <span className="help">
                Cosmetic only. When <strong>off</strong> (default), log lines that reference a
                user show the raw handle the backend issued (the Plex.tv username, Jellyfin
                login name, or Emby account name). When <strong>on</strong>, the engine
                substitutes the stored <em>display name</em> from the user&apos;s managed_users
                row when present and falls back to the raw handle otherwise.
                <br /><br />
                Identity resolution, identity_map lookups, and write-time addressing all
                continue to use the raw handle regardless of this setting. The only thing that
                changes is the human-readable string that lands in log files and run-history
                fields.
              </span>
            </label>
          );
        })()}
      </div>
      )}

      {/* Per-server overrides for the "Both" tunables. Two fields only
          today (plex_connect_timeout_seconds, viewcount_increment_cap)
          - both are networking-related so this lives on the Networking
          tab. The global value lives in the section above; per-server
          rows below apply only when set. */}
      {activeTab === 'networking' && (
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
      )}

      {/* Playlist & Mixed-Media tab — developer's Plan[PLAYLIST-MANAGEMENT]
          + Plan[MIXED-MEDIA-PLAYLISTS] 2026-05-16 commits. Surfaces
          the 11 new tunables end users want to dial. */}
      {activeTab === 'playlist' && (
        <FieldGroup
          title="Playlist cache + mixed-media handling (numeric)"
          fields={PLAYLIST_NUMERIC_FIELDS}
          values={values}
          onChange={setOneNumeric}
        />
      )}

      {activeTab === 'playlist' && (
      <div className="panel">
        <h2 style={{ marginTop: 0 }}>Playlist cache toggles</h2>
        <span className="help" style={{ display: 'block', color: 'var(--text-dim)', fontSize: 12, marginBottom: 12 }}>
          Boolean preferences for the playlist cache layer.
          See Plan[PLAYLIST-MANAGEMENT]-2026-05-16.md for the full design.
        </span>
        <label className="switch">
          <input
            type="checkbox"
            checked={(values.playlist_cache_enabled ?? DEFAULTS.playlist_cache_enabled) === true}
            onChange={(e) => setOne('playlist_cache_enabled', e.target.checked)}
          />
          <span>
            Playlist cache enabled
            <span style={{ color: 'var(--text-dim)', marginLeft: 6, fontSize: 11 }}>
              (default {String(DEFAULTS.playlist_cache_enabled)})
            </span>
          </span>
          <span className="help">
            Master switch. When off, every playlist read hits the live backend API
            (slower, more API load). The cache DB stays on disk; flipping back to
            on resumes reads from it.
          </span>
        </label>
        <label className="switch" style={{ marginTop: 8 }}>
          <input
            type="checkbox"
            checked={(values.playlist_cache_background_refresh_enabled ?? DEFAULTS.playlist_cache_background_refresh_enabled) === true}
            onChange={(e) => setOne('playlist_cache_background_refresh_enabled', e.target.checked)}
          />
          <span>
            Background refresh enabled
            <span style={{ color: 'var(--text-dim)', marginLeft: 6, fontSize: 11 }}>
              (default {String(DEFAULTS.playlist_cache_background_refresh_enabled)})
            </span>
          </span>
          <span className="help">
            When on, a background thread refreshes stale caches automatically.
            Off by default to avoid surprise API load before operators have
            tuned the refresh interval to their preference.
          </span>
        </label>
      </div>
      )}

      {/* String-enum tunables: render as dedicated <select> blocks.
          Same pattern as owner_display_style above. */}
      {activeTab === 'playlist' && (
      <div className="panel">
        <h2 style={{ marginTop: 0 }}>Playlist Transfer: Plex Home auth mode</h2>
        {(() => {
          const fallback = DEFAULTS.playlist_mgmt_plex_home_auth_mode;
          const current = values.playlist_mgmt_plex_home_auth_mode;
          const effective = current === undefined ? fallback : current;
          const isCustom = current !== undefined && current !== fallback;
          return (
            <div className="field" style={{ marginTop: 8 }}>
              <span className="label">
                Auth path
                {isCustom && (
                  <span className="tag" style={{ marginLeft: 6, fontSize: 10 }}>custom</span>
                )}
                <span style={{ color: 'var(--text-dim)', marginLeft: 6, fontSize: 11 }}>
                  (default {fallback})
                </span>
              </span>
              <span className="help">
                How Playlist Transfer authenticates copies for Plex Home users.
                <br />
                <strong>owner_token</strong> uses the owner's token + the Home user's
                UserId in the URL/body. No PIN required. Matches the existing
                per-user-block pattern the engine uses for everything else.
                <br />
                <strong>per_user_token</strong> uses the Home user's own PIN-unlocked
                token from <code>managed_users</code>. More isolated; requires the
                user's PIN to be stored. Bonus debuggability when one mode hits a bug.
              </span>
              <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                <select
                  value={effective}
                  onChange={(e) => setOne('playlist_mgmt_plex_home_auth_mode', e.target.value as TunablesShape['playlist_mgmt_plex_home_auth_mode'])}
                  style={{ maxWidth: 260 }}
                >
                  <option value="owner_token">owner_token (default)</option>
                  <option value="per_user_token">per_user_token</option>
                </select>
                {isCustom && (
                  <button
                    type="button"
                    onClick={() => setOne('playlist_mgmt_plex_home_auth_mode', undefined)}
                    style={{ fontSize: 11, padding: '2px 8px' }}
                  >
                    Reset
                  </button>
                )}
              </div>
            </div>
          );
        })()}
      </div>
      )}

      {activeTab === 'playlist' && (
      <div className="panel">
        <h2 style={{ marginTop: 0 }}>Playlist Transfer: Same-user behavior</h2>
        {(() => {
          const fallback = DEFAULTS.playlist_mgmt_same_user_behavior;
          const current = values.playlist_mgmt_same_user_behavior;
          const effective = current === undefined ? fallback : current;
          const isCustom = current !== undefined && current !== fallback;
          return (
            <div className="field" style={{ marginTop: 8 }}>
              <span className="label">
                When source and destination resolve to the same user
              </span>
              <span className="help" style={{ display: 'block', color: 'var(--text-dim)', fontSize: 12, marginBottom: 8 }}>
                A managed user copying a playlist to themselves on the same server is
                usually unintended (caught when dragging onto themselves in fan-out
                mode). Default skips the no-op early. Switch to "duplicate" to
                intentionally fork a playlist for editing one side.
              </span>
              <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                <select
                  value={effective}
                  onChange={(e) => setOne('playlist_mgmt_same_user_behavior', e.target.value as TunablesShape['playlist_mgmt_same_user_behavior'])}
                  style={{ maxWidth: 260 }}
                >
                  <option value="skip">skip (default) — recognize + skip silently</option>
                  <option value="duplicate">duplicate — create a second playlist</option>
                </select>
                {isCustom && (
                  <button
                    type="button"
                    onClick={() => setOne('playlist_mgmt_same_user_behavior', undefined)}
                    style={{ fontSize: 11, padding: '2px 8px' }}
                  >
                    Reset
                  </button>
                )}
              </div>
            </div>
          );
        })()}
      </div>
      )}

      {activeTab === 'playlist' && (
      <div className="panel">
        <h2 style={{ marginTop: 0 }}>Mixed-Media behavior</h2>
        <span className="help" style={{ display: 'block', color: 'var(--text-dim)', fontSize: 12, marginBottom: 12 }}>
          How mixed-media (audio + video) playlists are handled when
          the destination is Plex (which forbids mixed playlists). See
          Plan[MIXED-MEDIA-PLAYLISTS]-2026-05-16.md.
        </span>
        {(['mixed_media_behavior', 'mixed_media_video_routing', 'mixed_media_collision_handling', 'mixed_media_logging'] as const).map((key) => {
          const labels: Record<typeof key, string> = {
            mixed_media_behavior: 'Behavior on Plex destination',
            mixed_media_video_routing: 'Video routing (movies + TV tie-break)',
            mixed_media_collision_handling: 'Name collision handling',
            mixed_media_logging: 'Decision logging verbosity',
          };
          const options: Record<typeof key, Array<{ value: string; label: string }>> = {
            mixed_media_behavior: [
              { value: 'skip', label: 'skip (default) — drop mixed playlists with logging' },
              { value: 'dominant', label: 'dominant — write a single playlist of the dominant type' },
              { value: 'split', label: 'split — write N type-suffixed playlists' },
            ],
            mixed_media_video_routing: [
              { value: 'library_agnostic', label: 'library_agnostic (default) — cross-library single playlist' },
              { value: 'library_dominant', label: 'library_dominant — pick the larger library' },
            ],
            mixed_media_collision_handling: [
              { value: 'duplicate', label: 'duplicate (default) — engine-historical behavior' },
              { value: 'suffix', label: 'suffix — append " (2)"' },
              { value: 'skip', label: 'skip — don\'t write; warn' },
            ],
            mixed_media_logging: [
              { value: 'full', label: 'full (default) — every decision logged' },
              { value: 'decisions_only', label: 'decisions_only — non-default actions only' },
              { value: 'off', label: 'off — silent except errors' },
            ],
          };
          const fallback = DEFAULTS[key];
          const current = values[key];
          const effective = current === undefined ? fallback : current;
          const isCustom = current !== undefined && current !== fallback;
          return (
            <div key={key} className="field" style={{ marginTop: 8 }}>
              <span className="label">
                {labels[key]}
                {isCustom && (
                  <span className="tag" style={{ marginLeft: 6, fontSize: 10 }}>custom</span>
                )}
                <span style={{ color: 'var(--text-dim)', marginLeft: 6, fontSize: 11 }}>
                  (default {String(fallback)})
                </span>
              </span>
              <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                <select
                  value={String(effective)}
                  onChange={(e) => setOne(key, e.target.value as never)}
                  style={{ maxWidth: 360 }}
                >
                  {options[key].map((o) => (
                    <option key={o.value} value={o.value}>{o.label}</option>
                  ))}
                </select>
                {isCustom && (
                  <button
                    type="button"
                    onClick={() => setOne(key, undefined)}
                    style={{ fontSize: 11, padding: '2px 8px' }}
                  >
                    Reset
                  </button>
                )}
              </div>
            </div>
          );
        })}
      </div>
      )}

      {/* Danger Zone tab - only the dedicated 'danger' tab reveals
          this panel. The internal "I understand" gate still applies
          before Save accepts danger-field edits. */}
      {activeTab === 'danger' && (
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

            {/* Developer-mode runtime toggle. Default OFF. When the
                end user flips this on (and confirms below), the
                in-app Developer tab + /api/dev/* endpoints become
                accessible without an env-var change. The backend
                still treats PLEXMIGRATE_DEBUG_MODE as the precedent
                gate: if the env var is set truthy, this tunable
                is ignored and the tab stays on. */}
            <div
              className="field"
              style={{
                marginTop: 18,
                paddingTop: 12,
                borderTop: '1px solid var(--warn, #d97706)',
              }}
            >
              {(() => {
                const fallback = DEFAULTS.developer_mode_enabled as boolean;
                const current = values.developer_mode_enabled;
                const effective = current === undefined ? fallback : current;
                const isCustom = current !== undefined && current !== fallback;
                return (
                  <label className="switch" style={{ marginTop: 0 }}>
                    <input
                      type="checkbox"
                      checked={effective}
                      onChange={(e) => setOne('developer_mode_enabled', e.target.checked)}
                    />
                    <span>
                      <strong>Enable Developer Mode (runtime)</strong>
                      {isCustom && (
                        <span className="tag" style={{ marginLeft: 6, fontSize: 10 }}>custom</span>
                      )}
                      <span style={{ color: 'var(--text-dim)', marginLeft: 6, fontSize: 11 }}>
                        (default {fallback ? 'on' : 'off'})
                      </span>
                    </span>
                    <span className="help">
                      When <strong>off</strong> (default), the Developer tab is hidden and
                      <code> /api/dev/*</code> endpoints return 403.{' '}
                      When <strong>on</strong>, the Developer tab appears in the top nav
                      after the next browser refresh, and the unit-test runner / dev
                      endpoints become accessible. Equivalent to setting
                      <code> PLEXMIGRATE_DEBUG_MODE=1</code> on the backend container,
                      but flippable at runtime without a restart.
                      <br />
                      <strong style={{ color: 'var(--warn, #d97706)' }}>
                        Never enable this in production.
                      </strong>{' '}
                      The Developer tab can run unit tests against the live database;
                      live-mode tests hit real Plex. If the env var is set on the
                      container, the env var wins and this toggle is ignored.
                    </span>
                  </label>
                );
              })()}
            </div>

            {dangerTouched && (
              <label className="switch" style={{ marginTop: 14 }}>
                <input
                  type="checkbox"
                  checked={dangerAck}
                  onChange={(e) => setDangerAck(e.target.checked)}
                />
                <span><strong>I understand the risks</strong> - Danger Zone changes may cause SQLite lock contention, break HTTP pooling, or expose developer-only surfaces. Save is blocked until this is checked.</span>
              </label>
            )}
          </div>
        )}
      </div>
      )}

      <OrphanSnapshotMerge />

      <div className="panel" style={{ display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
        <button onClick={() => void load()} disabled={saving}>Reset</button>
        <button className="primary" onClick={() => void save()} disabled={!canSave}>
          {saving ? 'Saving…' : 'Save Tunables'}
        </button>
      </div>
    </>
  );
}


// ── Orphan-snapshot merge action ─────────────────────────────────────────
//
// Surfaces a per-server "Merge orphan snapshots" button. The Exports
// panel already merges orphans into one display tab at render time;
// this button is for operators who want the underlying registry rows
// rewritten so the database state matches the displayed state. Useful
// before exporting/migrating snapshots.db, useful for tidy registries.
// Non-destructive: rewrites server_id only, never deletes files.

function OrphanSnapshotMerge() {
  const [servers, setServers] = useState<ServerView[] | null>(null);
  const [selectedId, setSelectedId] = useState<string>('');
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<
    | { checked: number; reassigned: number; no_op: number }
    | null
  >(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.listServers()
      .then((list) => setServers(list))
      .catch((e) => setError(String(e)));
  }, []);

  const submit = async () => {
    if (!selectedId) return;
    setBusy(true);
    setResult(null);
    setError(null);
    try {
      const out = await api.mergeOrphanSnapshots(selectedId);
      setResult(out);
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="panel">
      <h3 style={{ marginTop: 0 }}>Merge orphan snapshots</h3>
      <span className="help" style={{ display: 'block', color: 'var(--text-dim)', fontSize: 12, marginBottom: 8 }}>
        Reassign snapshot rows whose ``server_id`` points at a removed
        server but whose stored ``server_name`` matches the target
        server below. The Exports panel already groups these together
        at display time; this button rewrites the underlying rows for
        operators who want the registry to mirror the displayed state.
        Non-destructive: only the ``server_id`` column is updated.
      </span>
      <div className="field">
        <span className="label">Target server</span>
        <select
          value={selectedId}
          onChange={(e) => setSelectedId(e.target.value)}
          disabled={busy || !servers}
          style={{ minWidth: 240 }}
        >
          <option value="">- pick a server -</option>
          {(servers || []).map((s) => (
            <option key={s.id} value={s.id}>{s.name}</option>
          ))}
        </select>
        <button
          onClick={() => void submit()}
          disabled={busy || !selectedId}
          style={{ marginLeft: 8 }}
        >
          {busy ? 'Merging…' : 'Merge orphan snapshots'}
        </button>
      </div>
      {result && (
        <div className="banner good" style={{ fontSize: 12, marginTop: 8 }}>
          Checked {result.checked} snapshot(s) for this name;
          reassigned {result.reassigned}; left {result.no_op} alone
          (already correctly attached).
        </div>
      )}
      {error && (
        <div className="banner error" style={{ fontSize: 12, marginTop: 8 }}>
          {error}
        </div>
      )}
    </div>
  );
}
