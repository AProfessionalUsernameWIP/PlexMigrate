// Thin REST + WebSocket client for the PlexMigrate server.
//
// All endpoints live under /api/ and the live dashboard stream is at
// /ws/dashboard. Both are same-origin in production (nginx proxies
// them in the frontend container) and same-origin in dev (Vite's
// proxy in vite.config.ts forwards to localhost:8000).
//
// This module snapshots:
//   * Typed wrappers around every REST endpoint.
//   * A `DashboardWsClient` class that owns the WebSocket lifecycle
//     (connect / reconnect / dispatch). The UI subscribes to it via
//     React state - see App.tsx.

// ── Type definitions ─────────────────────────────────────────────────────────
// These mirror the snapshot shape DashboardState produces in Python
// (services/dashboard.py :: DashboardState.to_dashboard_frame). We keep them
// narrow - only the fields the UI actually reads - so changes in the
// engine don't force frontend type churn.

export interface DashboardState {
  libraries: LibraryProgress[];
  activity: ActivityEntry[];
  completed: number;
  skipped: number;
  failed: number;
  guid_hits: number;
  filepath_hits: number;
  suffix_hits: number;
  fuzzy_hits: number;
  unresolved: number;
  // v0.9.3: run coverage - added so the dashboard can show how many
  // users / watched items / playlists / collections / ratings the
  // current run is operating on. Optional for forward-compat with an
  // older backend that hasn't been upgraded yet.
  home_user_count?: number;
  watch_count?: number;
  playlist_count?: number;
  collection_count?: number;
  rating_count?: number;
  // v0.9.3: per-worker "currently processing" rows. Each entry is one
  // active worker thread's in-flight item. Optional for forward-compat.
  current_items?: CurrentItem[];
  threads: Record<string, string>;
  paused: boolean;
  start_time: number;
  log_dir: string;
  now: number;
  // v0.9.6 Feature 1: raw identifier of the user whose data is being
  // processed right now (owner email or managed username). Null when
  // the run has no per-user phase active. Frontend resolves through
  // ``user_display_names`` before rendering.
  current_user?: string | null;
  // v0.9.6 Feature 3: source server's display-name map, copied at
  // run start. Keys: raw Plex identifier. Values: operator-chosen
  // display name. Empty / missing = render raw identifiers as-is.
  user_display_names?: Record<string, string>;
  // v0.9.6 Feature 2: HTTP telemetry - status code histogram and
  // 60-second rolling rate / latency series, partitioned by library
  // name with a ``__all__`` cumulative bucket. Optional for
  // forward-compat with an older backend.
  http_status_counts?: Record<string, Record<string, number>>;
  http_latency_series?: Record<string, Array<{ t: number; rps: number; avg_ms: number | null }>>;
  http_rate_limits?: { count: number; retries: number; backing_off: boolean };
  rate_limit_events?: Array<{
    timestamp: string;
    library: string;
    status_code: number;
    retry_after_seconds: number | null;
    detail: string;
  }>;
  // Step 6: per-tier resolver match counts for the active run. Keys are
  // tier names ("guid_db", "guid_api", "filepath", "suffix", "fuzzy").
  // Surfaced live so the Dashboard can warn when Tier 3 (fuzzy) fires
  // during a direct-transfer run.
  tier_counts?: Record<string, number>;
  // Rule 4: per-container restoration summary built during import.
  // Each playlist / collection contributes one entry once its merge
  // step finishes. Empty on snapshot / direct-transfer runs.
  container_summary?: {
    playlists?: ContainerResult[];
    collections?: ContainerResult[];
  };
  // Timing engine (discover-don't-predict). ``rolling_etr_seconds`` is
  // the single headline ETR, projected from a rolling window of real
  // throughput samples. ``null`` means the tracker doesn't have enough
  // samples yet; the dashboard renders that as "Calculating...". There
  // is no pre-run estimate.
  rolling_etr_seconds?: number | null;
  // Per-batch independent ETRs. Each key is a batch label ("watch",
  // "rating", "playlist", "collection") and each value carries the
  // batch's current progress + smoothed ETR. ``total`` grows as the
  // engine discovers real work; empty until the first batch tick.
  batch_etrs?: Record<string, {
    total: number;
    completed: number;
    etr_seconds: number | null;
  }>;
  // Part B: run-level finalize phase. Set by the job runner during
  // the post-engine close-out (close logs / run-dir finalize /
  // snapshot-DB capture) - the window where every library row already
  // reads "Done" but the job hasn't flipped to COMPLETED. ``null`` /
  // absent = not finalizing; a string is the current sub-step, shown
  // as "Finalizing — <label>".
  finalizing?: string | null;
}

// Rule 4: one restoration row per playlist / collection in an import.
// ``restored / total`` drives the "15 / 20 items restored" badge;
// ``skipped_items`` carries the per-member miss list (capped at 25 -
// the backend leaves the full set in the run log). ``smart=true``
// flags a smart playlist that was skipped entirely; ``reason`` is
// the operator-facing one-liner when restored < total or the whole
// container was skipped.
export interface ContainerResult {
  name: string;
  library: string;
  user_handle?: string;
  total: number;
  restored: number;
  skipped: number;
  skipped_items: Array<{
    title: string;
    type?: string;
    reason: string;
  }>;
  skipped_items_truncated?: number;
  smart?: boolean;
  reason?: string;
}

export interface CurrentItem {
  library: string;
  type: string;
  title: string;
  started_at: number;
  // v0.9.3: short verb describing what this thread is doing on the
  // item - "resolving" / "scrobbling" / "rating" / "merging" /
  // "capturing" / "indexing" / "fetching". Optional for forward-compat
  // with older snapshots; missing renders as an em-dash.
  phase?: string;
  // Spec Section 4.5 - phase age clock. Resets whenever the worker
  // transitions to a new phase, so the dashboard can colour-code the
  // row by how long this specific phase has been running rather than
  // the whole item's elapsed time. Unix seconds. Missing on older
  // backends; frontend treats absence as "no clock yet" (neutral).
  phase_started_at?: number;
}

export interface LibraryProgress {
  name: string;
  total: number;
  completed: number;
  status: 'queued' | 'active' | 'done' | 'error';
  phase: string;
  start_time: number;
}

export interface ActivityEntry {
  timestamp: string;
  action_type: string;
  library: string;
  title: string;
  // PR-2 / Phase C - empty string = unscoped (always shown). A
  // non-empty value tags this entry to a specific server; the WS
  // payload filters these out for non-participants during an active
  // job so the operator only sees the source / destination(s)
  // they're actually running against.
  server_name?: string;
}

export interface JobPayload {
  job_id: string;
  // v0.9.0 added 'direct' alongside the original two modes. The
  // server-side JobRecord uses the same literal strings.
  mode: 'snapshot' | 'restore' | 'direct';
  // 'stopping' (v0.9.3) is the intermediate state between user click
  // and engine return - see server/jobs.py :: STATE_STOPPING.
  state: 'idle' | 'queued' | 'running' | 'stopping' | 'completed' | 'failed' | 'cancelled';
  queued_at: number;
  started_at: number | null;
  finished_at: number | null;
  error: string | null;
  run_log_dir: string | null;
  params: Record<string, unknown>;
  // Step 6: captured at job completion in the worker-loop finally
  // block. ``tier_counts`` mirrors the live DashboardState field so the
  // Job History view can show post-hoc which tiers matched. Rule 4 (B):
  // ``container_summary`` is the same shape the live DashboardState
  // exposes - frozen at job completion so the JobHistory tab can
  // render restoration stats even after the live frame is gone.
  summary?: {
    tier_counts?: Record<string, number>;
    container_summary?: {
      playlists?: ContainerResult[];
      collections?: ContainerResult[];
    };
    // Wall-clock facts captured at completion. No estimate fields -
    // the discover-don't-predict model has no pre-run baseline to
    // compare against.
    timing?: {
      started_at: number | null;
      finished_at: number | null;
      actual_seconds: number | null;
    };
  } | null;
}

// v0.10.0 - one card per destination in a fan-out job. ``dashboard``
// is the per-destination DashboardState snapshot once that destination
// is running. ``state`` advances queued → running → completed/failed/
// cancelled independently of sibling destinations.
export interface FanOutDestState {
  dest_name: string;
  state: 'queued' | 'running' | 'completed' | 'failed' | 'cancelled';
  log_dir: string;
  started_at: number | null;
  finished_at: number | null;
  error: string | null;
  dashboard: DashboardState | null;
}

// v0.12.0 - per-server HTTP telemetry, always present, populated by
// both the engine response hook (during jobs) and the 30 s ping poll
// (when idle). One entry per registered server, joined by URL host.
export interface ServerNetworkState {
  server_id: string;
  server_name: string;
  url: string;
  host: string;
  // Rolling 60-second window stats. ``avg_ms`` is null when no
  // samples have landed in the window - the UI shows '-' rather
  // than zero so an idle channel reads correctly.
  rps: number;
  avg_ms: number | null;
  sample_count_in_window: number;
  // Most recent ping result (from the 30 s poll). Independent of
  // engine traffic - populated even when no job is running.
  last_ping_ms: number | null;
  last_ping_ok: boolean;
  last_ping_at: number;
  last_seen_at: number;
  // Status code histograms. ``window`` is the trailing-60s view;
  // ``cumulative`` runs from process start so the operator can spot
  // a server that's been chronically returning 429s.
  window_status_counts: Record<string, number>;
  cumulative_status_counts: Record<string, number>;
  rate_limit_events: Array<{
    timestamp: number;
    status_code: number;
    retry_after_seconds: number | null;
  }>;
  // Per-second time series for the trailing 60 seconds. Each entry is
  // a 1-second bucket: ``rps`` = sample count in that second,
  // ``avg_ms`` = mean latency of those samples (null when empty).
  // Consumed by the Dashboard inline NetworkPanel for both the line
  // chart and the 3-second instantaneous tile values. The standalone
  // Networking tab ignores this field.
  series: Array<{
    t: number;          // unix-second timestamp at the start of the bucket
    rps: number;        // sample count in this second
    avg_ms: number | null;
  }>;
}

export interface DashboardFrame {
  type: 'dashboard_frame';
  server_ts: number;
  dashboard: DashboardState | null;
  job: JobPayload | null;
  // PR-8 - every active + queued job. The Dashboard tab grows a
  // sub-tab strip when this has more than one entry, one tab per
  // job. Optional for backward compat with older backends.
  jobs?: JobPayload[];
  // v0.10.0: present when a fan-out job is in flight with more than
  // one destination. ``null`` for single-destination jobs so the
  // existing single-card render path is fully unchanged.
  fan_out: FanOutDestState[] | null;
  // v0.12.0: per-server HTTP telemetry, always present.
  servers_network: ServerNetworkState[];
}

export interface SettingsView {
  plex_url: string;
  has_token: boolean;
  output_dir: string;
  log_dir: string;
  workers: number;
  scrobble_workers: number;
  verbose: boolean;
  strict_match: boolean;
  // Global on/off for per-run log FILES (runtime.log / errors.log /
  // media.log). When false the engine still runs and the dashboard
  // / activity feed still update; only the per-run files on disk
  // are suppressed. Default true.
  run_logging_enabled?: boolean;
  // Global on/off for the db-access audit log. Default true. Read
  // here as a status flag; the value is changed only through the
  // db_admin-gated /api/settings/audit-log-toggle endpoint - a
  // plain PATCH on /api/settings silently strips this field.
  audit_log_enabled?: boolean;
  // PR-13 - snapshot retention. ``global`` is the ceiling; the per-
  // server override map only applies when an entry is strictly LOWER
  // than the global. Higher per-server values are ignored.
  snapshot_retention_global?: number;
  snapshot_retention_per_server?: Record<string, number>;
  // Global default for the snapshot JSON-sidecar toggle. When true,
  // every snapshot job + schedule that hasn't explicitly set its own
  // value uses this as the initial state of the per-job toggle.
  prebuild_json_sidecar_default?: boolean;
  // Owner-phase watch+ratings capture strategy. "smart" (default) lets
  // the engine pick; "force_bulk" always bulk-fetches; "force_server_side"
  // always uses server-side filter scans. Per-server overrides accepted
  // under snapshot_defaults_per_server[server_id].
  watch_ratings_filter_strategy?: 'smart' | 'force_bulk' | 'force_server_side';
  // System Tunables (root_admin only). Free-form map; see
  // services/tunables.py for the recognised keys + defaults. Surfaced
  // via Settings ▸ Tunables.
  tunables?: Record<string, number | string>;
  // Per-server tunable overrides (root_admin only). Only meaningful
  // for the small set of tunables that have a sensible per-server
  // interpretation — currently plex_connect_timeout_seconds and
  // viewcount_increment_cap. Map: server_id → {key: value}.
  tunables_per_server?: Record<string, Record<string, number>>;
  // ETR colour multiplier for the dashboard stall thresholds (Phase 4).
  // Clamped to [0.5, 2.0] at the read boundary. Default 1.0.
  etr_color_multiplier?: number;
  // Direct-transfer resolver-tier policy. Tiers 0/1 (DB + API GUID
  // matching) are always active and never appear here. Tier 2 is
  // the filepath-suffix fallback (default ON). Tier 3 is fuzzy
  // title matching (default OFF - can produce incorrect matches).
  // Snapshot / import paths are unaffected by these flags.
  transfer_resolution?: {
    allow_filepath_fallback?: boolean;
    allow_fuzzy_fallback?: boolean;
  };
  // media.db retention + cascade-delete policy. The two prune_*_days
  // fields are placeholders for a future background sweep; the
  // cascade flags drive what happens to per-server rows in media.db
  // when the operator removes a server from the registry.
  media_db_retention?: {
    cascade_delete_on_server_remove?: boolean;
    prevent_cascade_delete?: boolean;
    prune_stale_watch_events_days?: number;
    prune_stale_server_data_days?: number;
  };
  // Rule 2: library-walk job. ``enabled`` toggles the background
  // scheduler; ``interval_seconds`` is the cadence (floored at 3600
  // on the backend); ``stale_threshold_days`` is the default value
  // the Prune Missing Items slider opens at.
  library_walk?: {
    enabled?: boolean;
    interval_seconds?: number;
    stale_threshold_days?: number;
  };
  // v0.13.x — Restore-side run defaults (Merge / Replace). Per-run +
  // per-server overrides take precedence; this is the bottom-of-chain
  // fallback. Defaults: {"mode": "merge", "auto_capture_before_replace": true}.
  restore_defaults?: {
    mode?: 'merge' | 'replace';
    auto_capture_before_replace?: boolean;
    // v0.13.x: Merge sub-strategy for watch-count math. "higher"
    // (default) = destination ends at max(stored, current); "sum" =
    // current + stored. Operator opt-in. Ignored when mode=='replace'.
    merge_watch_strategy?: 'higher' | 'sum';
  };
}

// Rule 2: walk + prune types.
export interface LibraryWalkRow {
  id: number;
  server_id: string;
  started_at: number;
  finished_at: number | null;
  status: 'running' | 'completed' | 'failed' | 'cancelled';
  items_seen: number;
  libraries_seen: number;
  error_message: string | null;
}

export interface LibraryWalkStatus {
  running: boolean;
  last: LibraryWalkRow | null;
  recent: LibraryWalkRow[];
}

export interface PrunePreview {
  count: number;
  sample_items: Array<{
    item_id: number;
    title: string;
    media_type: string;
    year: number | null;
    filepath_suffix: string | null;
    rating_key: number;
    last_seen_at: number | null;
  }>;
  sample_truncated_at: number;
  last_walk: LibraryWalkRow | null;
  older_than_days: number;
}

export interface PruneResult {
  server_items: number;
  watch_events: number;
  ratings: number;
  playlists_touched: number;
  collections_touched: number;
  items_orphaned: number;
  dry_run: boolean;
  older_than_days: number;
}

export interface LibraryDescriptor {
  name: string;
  type: string;
  key: string | number;
  // Top-level entry count (movies / shows / artists). Same value
  // `sec.totalSize` reports on the Plex side. Kept as the primary
  // count because it is universally meaningful across library types.
  count: number;
  // Timing-spec leaf counts. Episodes for show libraries; tracks for
  // artist libraries (music + audio-books). Movies have no entry.
  // Optional because older registry rows captured before the v0.13
  // expansion don't have it; the timing estimator treats absence as
  // "fall back to the top-level count, mark accuracy degraded."
  leaf_counts?: {
    episodes?: number;
    tracks?: number;
  };
}

// Multi-server registry types (v0.9.0).
// v0.9.1 added ``last_response_ms`` for the live ping indicator.
export interface ServerView {
  id: string;
  name: string;
  url: string;
  has_token: boolean;
  last_status: 'ok' | 'unreachable' | 'auth_error' | 'unknown';
  last_status_detail: string;
  last_checked_at: number;
  last_libraries: LibraryDescriptor[];
  owner_name: string;
  // Latency of the last lightweight ping in milliseconds. ``null``
  // if no ping has ever been recorded for this server.
  last_response_ms: number | null;
  // v0.9.6 Feature 3: operator-chosen friendly names for users on
  // this server. Keys: raw Plex identifier (owner email or managed
  // username). Empty / missing = no custom names assigned. The
  // Servers tab uses this to populate the inline owner-display-name
  // input and to render display names alongside managed users.
  user_display_names?: Record<string, string>;
  // v0.10.0: identity captured from the Plex server itself. Used by
  // the Servers tab to surface "this row is pointing at server X" and
  // by the backend to reject re-registering the same physical Plex.
  // Empty string when the row was added before v0.10.0 and hasn't been
  // refreshed yet.
  machine_identifier?: string;
  friendly_name?: string;
  // v0.14 — Plex Media Server software version (e.g. "1.32.5.7349").
  // Captured at probe / refresh time. Used by the JobForm + Schedule
  // forms to gate Fast Collection Detection (which requires Plex ≥1.32).
  // Empty string when the row pre-dates this field or hasn't been
  // refreshed yet — treat unknown as "unsupported" so we never enable
  // a feature against an unverified server.
  plex_version?: string;
  // Timing-spec inputs. Server-wide counts the ETR estimator needs.
  // ``null`` (or missing) means we never captured them; the next
  // Refresh / Test populates them. The frontend renders unknown
  // counts as `?` rather than `0` so the operator can tell stale
  // metadata from genuine empties.
  playlist_count?: number | null;
  collection_count?: number | null;
  counts_refreshed_at?: number | null;
}

// v0.10.0: result of probing a URL+token without registering it.
// Returned by /api/servers/test-unsaved. ``name_mismatch`` flags
// "you typed X but the server identifies as Y"; ``duplicate_of``
// carries the friendly name of the existing registered server that
// shares this server's machine_identifier (null when no collision).
export interface ProbeUnsavedResult {
  ok: boolean;
  status: 'ok' | 'unreachable' | 'auth_error' | 'unknown';
  detail: string;
  friendly_name: string;
  machine_identifier: string;
  owner_name: string;
  libraries: LibraryDescriptor[];
  response_ms: number | null;
  name_mismatch: boolean;
  duplicate_of: string | null;
  // Timing-spec counts surfaced from the unsaved probe so the
  // operator can see "this would register a server with 3955 movies,
  // 12483 episodes, etc." before clicking Save.
  playlist_count?: number | null;
  collection_count?: number | null;
  counts_refreshed_at?: number | null;
}

// v0.9.6 Feature 3: one row in the per-server Users panel.
// ``raw_name`` is the canonical identifier (email for owner, username
// for managed). ``display_name`` is the operator's chosen friendly
// name from the server's ``user_display_names`` map, or empty if none.
export interface ServerUser {
  kind: 'owner' | 'managed';
  plex_id: string;
  raw_name: string;
  display_name: string;
}

export interface ServerUsersResponse {
  users: ServerUser[];
  // Non-null when ``systemAccounts()`` failed but the owner row is
  // still present - UI renders "No managed users found" plus a
  // tooltip with the underlying reason.
  error: string | null;
}

// Response shape of POST /api/servers/{id}/ping (v0.9.1).
export interface PingResult {
  ok: boolean;
  response_ms: number;
  status: 'ok' | 'unreachable' | 'auth_error' | 'unknown';
  detail: string;
}

export interface ServerIn {
  name: string;
  url: string;
  token?: string;
}

// v0.9.5: returned by ``DELETE /api/servers/{id}`` after a cascading
// remove. Lets the UI render a "Deleted My Server (2 schedules, 47
// snapshots, 12 log dirs cleaned up)" toast instead of a blank success.
// Best-effort: a non-empty ``errors`` array lists per-file failures
// that did not block the rest of the sweep.
export interface ServerDeleteSummary {
  deleted: boolean;
  id: string;
  name: string;
  slug: string;
  schedules: number;
  snapshots: number;
  exports_failed: number;
  log_dirs: number;
  log_dirs_failed: number;
  errors: string[];
}

// Returned by ``GET /api/servers/{id}/cascade-preview``. Same fields
// as the post-delete summary minus the failure counts.
export interface ServerCascadePreview {
  id: string;
  name: string;
  slug: string;
  schedules: number;
  snapshots: number;
  log_dirs: number;
}

export interface Schedule {
  id?: string;
  name: string;
  source_server_name?: string;
  libraries: string[];
  output_dir?: string | null;
  frequency: 'hourly' | 'daily' | 'weekly';
  hour: number;
  minute: number;
  day_of_week: number;
  enabled: boolean;
  next_run_at?: number;
  last_fired_at?: number;
  last_job_id?: string;
  // PR-3 / Phase D - four-flag data-type filter on schedules. All
  // four default true server-side; older schedules saved before this
  // field landed read as undefined here and the UI defaults them on.
  include_watch_history?: boolean;
  include_ratings?: boolean;
  include_playlists?: boolean;
  include_collections?: boolean;
  prebuild_json_sidecar?: boolean;
  // v0.14 Per-Run Settings on schedules. Mirrors the same per-job
  // knobs the Run Job form exposes — when set, each scheduled fire
  // forwards them to the snapshot job request as overrides on the
  // global / per-server defaults.
  workers?: number | null;
  scrobble_workers?: number | null;
  verbose?: boolean;
  log_dir?: string | null;
  skip_playlist_prebuild?: boolean;
  fast_collection_detection?: boolean;
  watch_ratings_filter_strategy?: 'smart' | 'force_bulk' | 'force_server_side' | '';
}

export interface LogRun {
  name: string;
  mtime: number;
  size: number;
  passed: boolean | null;
  file_count: number;
}

export interface LogFile {
  name: string;
  size: number;
  mtime: number;
}

export interface LogFileContent {
  content: string;
  truncated: boolean;
  size: number;
  // v0.9.3: byte offset to pass back as ?since= on the next poll.
  // Always equal to the file's current size after a successful read.
  next_offset: number;
}

// v0.12.0: media-state database health snapshot returned by
// GET /api/db/stats. Tables tracked: items, watch_events, ratings,
// playlists, collections, servers. ``last_updated_at`` is per
// content table (servers omitted because it has no updated_at).
// ``schema_version`` is the highest applied migration version -
// useful for diagnosing a stuck upgrade.
export interface DbStats {
  schema_version: number;
  row_counts: Record<string, number>;
  last_updated_at: Record<string, number | null>;
  size_bytes: number;
  path: string;
}

export interface ServerTime {
  // UNIX timestamp the backend produced this response at.
  now: number;
  // IANA zone name (e.g. "America/Los_Angeles") if $TZ is set on the
  // backend; falls back to the OS abbreviation ("PDT") otherwise.
  tz: string;
  // Short abbreviation always populated when the OS knows it.
  tz_abbrev: string;
  // ISO-8601 wallclock in the backend's local zone.
  iso: string;
}

// Legacy on-disk ``.plexexport.json`` files relocated to
// ``snapshots/legacy/`` by the PR-13 migration. Read-only browse +
// download + db_admin-gated delete.
export interface ExportArchive {
  name: string;
  size: number;
  mtime: number;
  library: string | null;
  captured_at: string | null;
  // v0.9.3: friendly name + URL of the server that produced this
  // export. ``null`` for exports created before this field was added.
  source_server?: string | null;
  source_server_url?: string | null;
  // v0.9.5: how the run was initiated. "manual" for a GUI submission,
  // "schedule" for a scheduler fire (``schedule_name`` carries the
  // schedule's display name). ``null`` on exports captured before
  // these fields existed.
  trigger?: string | null;
  schedule_name?: string | null;
}

// PR-13 - registry row backing one captured snapshot. The Exports
// panel renders one row per entry; the ``file_path`` and
// ``prebuilt_json_path`` are server-side details the UI doesn't
// surface directly, but the download endpoint reads them.
export interface Snapshot {
  id: string;
  server_id: string;
  server_name: string;
  snapshot_name: string;
  file_path: string;
  captured_at: number;            // unix seconds
  libraries: string[];
  user_count: number | null;
  row_counts: Record<string, number>;
  file_size: number | null;
  prebuilt_json_path: string | null;
  // True when the .db at file_path is on disk. False rows are
  // rendered with a "File missing" badge + disabled Download +
  // a Remove-entry button so the operator can clean dead rows.
  available: boolean;
  // True when a .plexexport.json sidecar has already been rendered
  // for this snapshot (either via the prebuild-on-capture toggle or
  // by a prior Download click). The Exports panel uses this to
  // label the action button: "Download" when a cached file exists
  // (instant stream), "Generate" when no sidecar exists yet (the
  // click triggers an on-demand render then streams).
  has_cached_sidecar: boolean;
  // Size of the cached .plexexport.json sidecar on disk, in bytes.
  // Null when no cache exists yet; the UI labels the column
  // accordingly so the operator can compare .db vs JSON size at
  // a glance once both are present.
  sidecar_size: number | null;
  // Which data types the originating run actually gathered. Subset
  // of {"watch_history","ratings","playlists","collections"}. The
  // import UI's include_* toggles gate on this rather than
  // row_counts, because row_counts reflects the cumulative state of
  // media.db at capture time (every prior run's data for that
  // server is still in the .db) whereas captured_types reflects
  // what THIS run touched. Null on rows captured before the column
  // landed; the frontend falls back to row_counts heuristics then.
  captured_types: string[] | null;
}

// ── Auth (v0.11.0) ───────────────────────────────────────────────────────────
//
// The opt-in JWT auth layer is gated by ``PLEXMIGRATE_AUTH_ENABLED``
// on the backend. The frontend treats it as cosmetic when disabled:
// the App loader hits ``/api/auth/status`` once on mount and either
// jumps straight to the main tabs (auth_enabled === false) or routes
// through setup / login.

// Module-level mutable holder for the access token. App.tsx sets and
// clears this via the setters below; every ``http()`` call below
// reads it just before issuing the request so a logout takes effect
// immediately. Held in a closure rather than React state because
// non-component code (the WebSocket helper, the http() wrapper) needs
// it without round-tripping through React's render cycle - and so
// that ``Main``'s mount-time effects see the post-login token
// synchronously, with no useEffect race in between.
let _accessToken: string | null = null;

// Callback fired when an API call returns 401 - App.tsx subscribes to
// this so it can drop into the login branch from anywhere a 401
// surfaces (which is anywhere, because every panel may call the API).
let _onUnauthorized: (() => void) | null = null;

// ── Token persistence - LoginBugFix1 (no persistence) ───────────────
//
// The token lives ONLY in this module's in-memory closure for the
// lifetime of one mounted <App />. There is no localStorage,
// sessionStorage, or cookie write of the access token anywhere in
// the frontend.
//
// Reasoning (see LoginBugFix1.md): the prior "session" / "persistent"
// modes let the operator's browser sync (Chrome sync, etc.) replicate
// the token across devices, which broke the basic safety property
// that "opening the app on a new device requires logging in." A
// page reload is also a fresh JS context - it correctly triggers
// re-login under this model. The Switch View Mode override is
// React state inside <AuthProvider>, so it resets on reload too -
// that's the intended security feature: dropped views never survive
// a JS-context restart.

/**
 * Push the access token into the api module's closure. ``null`` clears.
 *
 * Synchronous - call this BEFORE setting any React state that
 * triggers a re-render, so child components that mount and call the
 * API in their first effect see the token in the closure immediately.
 */
export function setAccessToken(token: string | null): void {
  _accessToken = token;
}

export function getAccessToken(): string | null {
  return _accessToken;
}

/**
 * v0.12.0 fix - decode a JWT's payload **without verifying the
 * signature**. Returns ``null`` on any malformed input. Used by
 * App.tsx on page reload to recover the signed-in username + role
 * from a persisted token so the topbar's user chip + Log out button
 * stay visible across reloads.
 *
 * Signature verification stays a backend concern: the JWT secret
 * never reaches the browser, and every API request the frontend
 * issues is re-validated server-side. A token whose payload we can
 * decode but that the backend rejects gets cleared via the 401
 * handler at first request.
 */
export function decodeJwtPayload(token: string): Record<string, unknown> | null {
  try {
    const parts = token.split('.');
    if (parts.length !== 3) return null;
    // JWT uses base64url; pad to a multiple of 4 and replace URL-safe
    // chars so the browser's atob accepts it.
    let b64 = parts[1].replace(/-/g, '+').replace(/_/g, '/');
    while (b64.length % 4) b64 += '=';
    const json = atob(b64);
    const obj = JSON.parse(json);
    return obj && typeof obj === 'object' ? (obj as Record<string, unknown>) : null;
  } catch {
    return null;
  }
}

export function onUnauthorized(handler: () => void): void {
  _onUnauthorized = handler;
}

// ── Auth types (PR-A2 / PR-A3) ──────────────────────────────────────────────

/**
 * Five login roles. The added ``admin`` (sudo-root) sits between
 * ``manager`` and ``root_admin``: it has the same permission set as
 * root_admin but cannot modify the root_admin user row.
 *
 * db_admin is NOT a login role - it's a special-purpose credential
 * row managed only via Settings → Accounts → Database Admin Account
 * (admin / root_admin only).
 */
export type Role = 'viewer' | 'operator' | 'manager' | 'admin' | 'root_admin';

export type Permission =
  | 'dashboard.view'
  | 'servers.view' | 'servers.edit'
  | 'jobs.start' | 'jobs.stop'
  | 'schedules.view' | 'schedules.edit'
  | 'logs.view' | 'exports.view'
  | 'settings.edit' | 'settings.tunables'
  | 'users.manage' | 'db_admin.access'
  | 'sync.view' | 'sync.edit';

export interface AuthStatus {
  auth_enabled: boolean;
  setup_needed: boolean;
}

export interface AuthUser {
  username: string;
  role: Role;
  display_name: string | null;
}

export interface AuthSession {
  user: AuthUser;
  access_token: string;
  token_type: string;
  expires_in: number;
}

export interface MeResponse {
  username: string;
  display_name: string | null;
  // Server-side View Mode (Fix 2): real_role is the DB row's role,
  // effective_role is what's currently driving permission gating.
  // They differ when the caller has an active View Mode session.
  real_role: Role;
  effective_role: Role;
  in_view_mode: boolean;
  permissions: Permission[];
  created_at: number | null;
  last_login: number | null;
}

export interface ManagedUser {
  id: number;
  username: string;
  role: Role;
  display_name: string | null;
  last_login: number | null;
  created_at: number;
}

// Access Control — per-user permission grant/revoke layer. Returned
// by GET /api/auth/users/{username}/permissions and the corresponding
// PATCH. ``baseline`` = role's normal permission set; ``extra`` =
// granted on top; ``revoked`` = removed from baseline; ``effective``
// = the final resolved set the user actually has.
export interface UserPermissionsResponse {
  username: string;
  role: Role;
  baseline: Permission[];
  extra: Permission[];
  revoked: Permission[];
  effective: Permission[];
  all_permissions: Permission[];
  // True when this row is root_admin — revokes are ignored by the
  // resolver so the UI can grey out the revoke toggles.
  root_admin_immune_to_revokes?: boolean;
}

// PR-10 - per-server managed users (Plex / Emby / Jellyfin operators
// known to a registered server). Distinct from ``ManagedUser`` above,
// which is the app-login user table. The API never returns plaintext
// credentials; ``has_token`` / ``has_pin`` / ``has_password`` flags
// tell the UI whether something is stored, and the User Management
// panel renders write-only inputs ("- - - - - -" when stored).
export type ManagedUserService = 'plex' | 'emby' | 'jellyfin';
export type ManagedUserKind = 'owner' | 'managed';
// PR-11.1 - tombstone scope. 'none' = visible; 'server' = hidden on
// this server only; 'global' = hidden on every server (username-keyed
// global tombstone). The User Management UI surfaces all three.
export type ManagedUserHiddenScope = 'none' | 'server' | 'global';

export interface ServerManagedUser {
  id: number;
  server_id: string;
  username: string;
  display_name: string | null;
  service_type: ManagedUserService;
  // PR-11 - owner vs. managed distinction. Owner rows are the
  // server's Plex account holder; managed rows are everyone else.
  // JobFormPanel uses this for its picker badge.
  kind: ManagedUserKind;
  machine_identifier: string | null;
  has_token: boolean;
  has_pin: boolean;
  has_password: boolean;
  last_seen: number | null;
  // PR-11.1 - tombstone state. ``tombstoned`` is the per-server flag
  // exactly as stored. ``hidden_scope`` summarises both flags + the
  // global tombstone table into a single value the UI renders against.
  tombstoned: boolean;
  hidden_scope: ManagedUserHiddenScope;
  created_at: number;
  updated_at: number;
}

export interface GlobalTombstone {
  username: string;
  tombstoned_at: number;
}


// ── REST helpers ─────────────────────────────────────────────────────────────
//
// Silent refresh: a single in-flight refresh promise is shared by every
// concurrent 401-retry so a burst of expired-token responses doesn't
// fan out into N refresh calls. The promise resolves to the new access
// token on success or null on failure.
let _refreshInFlight: Promise<string | null> | null = null;

async function _refreshOnce(): Promise<string | null> {
  if (_refreshInFlight) return _refreshInFlight;
  _refreshInFlight = (async () => {
    try {
      const res = await fetch('/api/auth/refresh', {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
      });
      if (!res.ok) return null;
      const body = await res.json();
      const newToken = body && typeof body.access_token === 'string'
        ? body.access_token
        : null;
      if (newToken) _accessToken = newToken;
      return newToken;
    } catch {
      return null;
    } finally {
      // Reset on next tick so simultaneous callers all observe the
      // same resolved value, then a *future* 401 burst gets its own
      // fresh refresh attempt.
      setTimeout(() => { _refreshInFlight = null; }, 0);
    }
  })();
  return _refreshInFlight;
}

async function http<T>(path: string, init?: RequestInit, _isRetry = false): Promise<T> {
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    ...(init?.headers as Record<string, string> | undefined || {}),
  };
  if (_accessToken) {
    headers['Authorization'] = `Bearer ${_accessToken}`;
  }
  const res = await fetch(path, {
    // Same-origin cookies (refresh_token at Path=/api/auth) must
    // accompany requests to that path. Default for same-origin
    // fetch is to include cookies, but pinning it explicitly here
    // protects against future browser changes.
    credentials: 'same-origin',
    ...init,
    headers,
  });
  if (res.status === 401) {
    // Auth endpoints (login, setup, refresh, verify-password) report
    // 401 legitimately - those callers want to see it. Anything else
    // is a mid-session expiry: try a silent refresh once, then
    // transparently retry the original request.
    const isAuthPath = path.startsWith('/api/auth/');
    if (!isAuthPath && !_isRetry) {
      const newToken = await _refreshOnce();
      if (newToken) {
        return http<T>(path, init, true);
      }
      if (_onUnauthorized) _onUnauthorized();
    } else if (!isAuthPath && _isRetry && _onUnauthorized) {
      // Retry also returned 401 - refresh succeeded but the original
      // request was still rejected (role demotion, account deleted).
      // Fall through to the global handler.
      _onUnauthorized();
    }
  }
  if (!res.ok) {
    // v0.9.7 fix: a Response body is a one-shot stream. The previous
    // version called ``res.json()`` first and fell back to ``res.text()``
    // in the catch - but ``res.json()`` consumes the stream even when
    // it throws (e.g. on an HTML / plain-text error body), so the
    // ``res.text()`` fallback then failed with "body stream already
    // read". Read raw text once, then try to parse as JSON to extract
    // ``detail``; either way the original text is available for the
    // error message.
    let detail = '';
    try {
      const raw = await res.text();
      if (raw) {
        try {
          const body = JSON.parse(raw);
          detail = (body && typeof body === 'object' && body.detail) || raw;
        } catch {
          detail = raw;
        }
      }
    } catch {
      // Body couldn't even be read as text. Leave detail empty -
      // the status code + statusText below still tell the user
      // something useful.
    }
    throw new Error(`${res.status} ${res.statusText}${detail ? `: ${detail}` : ''}`);
  }
  if (res.status === 204) return undefined as unknown as T;
  return (await res.json()) as T;
}

// ── Endpoint wrappers ────────────────────────────────────────────────────────

export const api = {
  // Auth (PR-A2). Auth is always on; ``getAuthStatus`` is used to
  // detect first-boot setup (``setup_needed: true``).
  getAuthStatus: () => http<AuthStatus>('/api/auth/status'),
  authSetup: (username: string, password: string, displayName?: string) =>
    http<AuthSession>('/api/auth/setup', {
      method: 'POST',
      body: JSON.stringify({
        username,
        password,
        ...(displayName ? { display_name: displayName } : {}),
      }),
    }),
  authLogin: (username: string, password: string) =>
    http<AuthSession>('/api/auth/login', {
      method: 'POST',
      body: JSON.stringify({ username, password }),
    }),
  authLogout: () =>
    http<{ logged_out: boolean }>('/api/auth/logout', { method: 'POST' }),

  // Silent refresh attempt. Returns the new AuthSession on success or
  // null when the refresh cookie is absent/expired/revoked. Used by
  // App.tsx on mount to detect whether the prior session can be
  // resumed without showing the login form. The http() helper does
  // its own internal refresh-and-retry on mid-session 401s and does
  // NOT need this wrapper - it talks to /api/auth/refresh directly.
  authRefresh: async (): Promise<AuthSession | null> => {
    try {
      const res = await fetch('/api/auth/refresh', {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
      });
      if (!res.ok) return null;
      const body = (await res.json()) as AuthSession;
      if (body && body.access_token) {
        setAccessToken(body.access_token);
        return body;
      }
      return null;
    } catch {
      return null;
    }
  },

  // PR-A2 - identity + live permissions for the current JWT.
  authMe: () => http<MeResponse>('/api/auth/me'),

  // PR-A2 - server-side verify of the caller's password without
  // issuing a new token. Used by the change-password forms to confirm
  // the current password before applying a change. (View Mode no
  // longer uses this endpoint - it has its own dedicated entry/exit
  // routes that bake the password check in.)
  authVerifyPassword: (password: string) =>
    http<{ valid: boolean }>('/api/auth/verify-password', {
      method: 'POST',
      body: JSON.stringify({ password }),
    }),

  // Server-side View Mode (Fix 2). Each call validates the password
  // server-side and updates the View Mode session map keyed by the
  // caller's JWT jti. After a successful call the frontend must
  // refetch /api/auth/me to pull the new effective_role into context.
  viewModeEnter: (target_role: Role, password: string) =>
    http<{ in_view_mode: boolean; real_role: Role; effective_role: Role }>(
      '/api/auth/view-mode/enter',
      { method: 'POST', body: JSON.stringify({ target_role, password }) },
    ),
  viewModeExit: (password: string) =>
    http<{ in_view_mode: boolean; real_role: Role; effective_role: Role }>(
      '/api/auth/view-mode/exit',
      { method: 'POST', body: JSON.stringify({ password }) },
    ),

  // PR-A2 - root_admin user CRUD over the login user table.
  listManagedUsers: () =>
    http<{ users: ManagedUser[] }>('/api/auth/users'),
  createManagedUser: (body: {
    username: string;
    password: string;
    role: 'viewer' | 'operator' | 'manager' | 'admin';
    display_name?: string;
  }) =>
    http<{ username: string; role: Role; display_name: string | null; id: number }>(
      '/api/auth/users',
      { method: 'POST', body: JSON.stringify(body) },
    ),
  updateManagedUser: (
    username: string,
    body: {
      role?: 'viewer' | 'operator' | 'manager' | 'admin';
      display_name?: string;
      clear_display_name?: boolean;
    },
  ) =>
    http<{ username: string; role: Role; display_name: string | null }>(
      `/api/auth/users/${encodeURIComponent(username)}`,
      { method: 'PATCH', body: JSON.stringify(body) },
    ),
  resetManagedUserPassword: (username: string, new_password: string) =>
    http<{ username: string; reset: boolean }>(
      `/api/auth/users/${encodeURIComponent(username)}/reset-password`,
      { method: 'POST', body: JSON.stringify({ new_password }) },
    ),
  deleteManagedUser: (username: string) =>
    http<{ deleted: string }>(
      `/api/auth/users/${encodeURIComponent(username)}`,
      { method: 'DELETE' },
    ),
  // Access Control: per-user permission grant/revoke layer. Root-admin only.
  getUserPermissions: (username: string) =>
    http<UserPermissionsResponse>(
      `/api/auth/users/${encodeURIComponent(username)}/permissions`,
    ),
  setUserPermissions: (
    username: string,
    body: { extra: Permission[]; revoked: Permission[] },
  ) =>
    http<UserPermissionsResponse>(
      `/api/auth/users/${encodeURIComponent(username)}/permissions`,
      { method: 'PATCH', body: JSON.stringify(body) },
    ),
  updateOwnDisplayName: (display_name: string) =>
    http<{ username: string; display_name: string | null }>(
      '/api/auth/users/me/display-name',
      { method: 'POST', body: JSON.stringify({ display_name }) },
    ),
  changeOwnPassword: (current_password: string, new_password: string) =>
    http<{ username: string; changed: boolean }>(
      '/api/auth/users/me/password',
      {
        method: 'POST',
        body: JSON.stringify({ current_password, new_password }),
      },
    ),

  // PR-9.1 - Database Admin Account (role='db_admin'). A SEPARATE row
  // from the application-login admin so the operator can authorise
  // destructive User Management writes (PR-10) with a credential they
  // don't use for everyday login. Always reachable regardless of
  // ``PLEXMIGRATE_AUTH_ENABLED``. Each mutating call carries the
  // current db-admin password in the body as the per-call gate.
  getDbAdminStatus: () =>
    http<{ has_admin: boolean; username: string | null }>('/api/auth/admin/status'),
  setupDbAdmin: (username: string, password: string) =>
    http<{ username: string; role: string }>('/api/auth/admin/setup', {
      method: 'POST',
      body: JSON.stringify({ username, password }),
    }),
  updateDbAdmin: (body: {
    current_password: string;
    new_username?: string;
    new_password?: string;
  }) =>
    http<{ username: string; role: string }>('/api/auth/admin/update', {
      method: 'POST',
      body: JSON.stringify(body),
    }),
  verifyDbAdmin: (username: string, password: string) =>
    http<{ valid: boolean }>('/api/auth/admin/verify', {
      method: 'POST',
      body: JSON.stringify({ username, password }),
    }),

  // PR-9.1 - Application-login admin (role='admin') management. The
  // existing first-boot ``/api/auth/setup`` flow is still the only
  // path that CREATES this row; these endpoints let the operator
  // surface and UPDATE it from Settings → Accounts.
  getLoginAccountStatus: () =>
    http<{ has_admin: boolean; username: string | null }>('/api/auth/login-account/status'),
  updateLoginAccount: (body: {
    current_password: string;
    new_username?: string;
    new_password?: string;
  }) =>
    http<{ username: string; role: string }>('/api/auth/login-account/update', {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  // Settings
  getSettings: () => http<SettingsView>('/api/settings'),
  saveSettings: (patch: Partial<SettingsView & { plex_token: string }>) =>
    http<SettingsView>('/api/settings', { method: 'POST', body: JSON.stringify(patch) }),

  // Audit log on/off. db_admin-gated server-side: the body must carry
  // the separate db_admin username + password (not the JWT). The
  // backend writes a self-documenting "DISABLED by <user>" or
  // "RE-ENABLED by <user>" line in the audit log itself across the
  // transition.
  toggleAuditLog: (body: {
    db_admin_username: string;
    db_admin_password: string;
    enabled: boolean;
  }) =>
    http<{ audit_log_enabled: boolean }>('/api/settings/audit-log-toggle', {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  // Multi-server registry (v0.9.0)
  listServers: () => http<ServerView[]>('/api/servers'),
  createServer: (body: ServerIn) =>
    http<ServerView>('/api/servers', { method: 'POST', body: JSON.stringify(body) }),
  updateServer: (id: string, body: ServerIn) =>
    http<ServerView>(`/api/servers/${encodeURIComponent(id)}`, {
      method: 'PUT',
      body: JSON.stringify(body),
    }),
  deleteServer: (id: string) =>
    http<ServerDeleteSummary>(`/api/servers/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  previewServerCascade: (id: string) =>
    http<ServerCascadePreview>(`/api/servers/${encodeURIComponent(id)}/cascade-preview`),
  testServer: (id: string) =>
    http<ServerView>(`/api/servers/${encodeURIComponent(id)}/test`, { method: 'POST' }),
  // v0.10.0: probe a URL+token without registering. Used by the
  // "Test Connection" button in the add-server form so testing
  // doesn't side-effect the registry. Also flags name mismatch and
  // duplicate-machine-identifier collisions so the UI can warn before
  // the operator clicks Save.
  testUnsavedServer: (body: { name: string; url: string; token: string }) =>
    http<ProbeUnsavedResult>('/api/servers/test-unsaved', {
      method: 'POST',
      body: JSON.stringify(body),
    }),
  // v0.10.0 alias: refreshing the cached library catalogue is the
  // same backend call as the existing /test endpoint, surfaced under
  // a clearer name. The Servers tab uses this for its per-row
  // "Refresh" button.
  refreshServer: (id: string) =>
    http<ServerView>(`/api/servers/${encodeURIComponent(id)}/test`, { method: 'POST' }),
  pingServer: (id: string) =>
    http<PingResult>(`/api/servers/${encodeURIComponent(id)}/ping`, { method: 'POST' }),
  listServerLibraries: (id: string) =>
    http<LibraryDescriptor[]>(`/api/servers/${encodeURIComponent(id)}/libraries`),
  // v0.9.6 Feature 3: list users on a server (owner + managed). Live
  // call to systemAccounts() - one round-trip per visit, no caching.
  listServerUsers: (id: string) =>
    http<ServerUsersResponse>(`/api/servers/${encodeURIComponent(id)}/users`),
  // Set or clear one entry in a server's user_display_names map.
  // Empty display_name clears the mapping (UI falls back to raw id).
  setUserDisplayName: (id: string, plex_id: string, display_name: string) =>
    http<ServerView>(`/api/servers/${encodeURIComponent(id)}/user-display-name`, {
      method: 'PATCH',
      body: JSON.stringify({ plex_id, display_name }),
    }),

  // Legacy single-server library listing - kept for one release.
  listLibraries: () => http<LibraryDescriptor[]>('/api/libraries'),

  // Jobs
  getJob: () => http<{ state: string; dashboard?: DashboardState | null; mode?: string; error?: string }>('/api/job'),
  getJobHistory: () => http<JobPayload[]>('/api/job/history'),
  submitSnapshot: (params: Record<string, unknown>) =>
    http<{ job_id: string; state: string; mode: string }>('/api/job/snapshot', {
      method: 'POST',
      body: JSON.stringify(params),
    }),
  submitRestore: (params: Record<string, unknown>) =>
    http<{ job_id: string; state: string; mode: string }>('/api/job/restore', {
      method: 'POST',
      body: JSON.stringify(params),
    }),
  // Restore-from-snapshot. Body mirrors submitRestore but carries
  // snapshot_id instead of input_files; the server materialises the
  // JSON sidecar for the snapshot before forwarding to the standard
  // import queue.
  submitRestoreFromSnapshot: (params: Record<string, unknown>) =>
    http<{ job_id: string; state: string; mode: string }>('/api/job/restore-from-snapshot', {
      method: 'POST',
      body: JSON.stringify(params),
    }),
  submitDirect: (params: Record<string, unknown>) =>
    http<{ job_id: string; state: string; mode: string }>('/api/job/direct', {
      method: 'POST',
      body: JSON.stringify(params),
    }),
  stopJob: (hard: boolean = false) =>
    http<{ stop_requested: boolean; hard: boolean }>(
      `/api/job/stop${hard ? '?hard=true' : ''}`,
      { method: 'POST' },
    ),

  // Schedules
  listSchedules: () => http<Schedule[]>('/api/schedules'),
  createSchedule: (s: Schedule) =>
    http<Schedule>('/api/schedules', { method: 'POST', body: JSON.stringify(s) }),
  updateSchedule: (id: string, s: Schedule) =>
    http<Schedule>(`/api/schedules/${encodeURIComponent(id)}`, {
      method: 'PUT',
      body: JSON.stringify(s),
    }),
  deleteSchedule: (id: string) =>
    http<{ deleted: string }>(`/api/schedules/${encodeURIComponent(id)}`, { method: 'DELETE' }),

  // Logs
  listLogRuns: () => http<LogRun[]>('/api/logs'),
  listLogFiles: (run: string) => http<LogFile[]>(`/api/logs/${encodeURIComponent(run)}`),
  readLogFile: (run: string, name: string, since: number = 0) =>
    http<LogFileContent>(
      `/api/logs/${encodeURIComponent(run)}/${encodeURIComponent(name)}` +
      (since > 0 ? `?since=${since}` : ''),
    ),
  // PR-13 fix #5 - escape hatch for files bigger than the
  // 16 MB in-browser tail cap. Returns the URL only; callers fetch
  // with the auth bearer and trigger a save dialog (same pattern as
  // ExportsPanel's downloadBlob).
  logFileDownloadUrl: (run: string, name: string) =>
    `/api/logs/${encodeURIComponent(run)}/${encodeURIComponent(name)}/download`,
  // Whole-run download as a zip - bundles every file in the run dir.
  logRunZipUrl: (run: string) =>
    `/api/logs/${encodeURIComponent(run)}/zip`,
  deleteLogRun: (run: string) =>
    http<{ deleted: string; file_count: number; errors: string[] }>(
      `/api/logs/${encodeURIComponent(run)}`,
      { method: 'DELETE' },
    ),
  deleteAllLogRuns: () =>
    http<{ deleted: number; errors: string[] }>(
      '/api/logs',
      { method: 'DELETE' },
    ),

  // Server time / timezone - used by SchedulesPanel to label hour/
  // minute fields with the zone they're interpreted in.
  getServerTime: () => http<ServerTime>('/api/server-time'),

  // Engine + wire-protocol version, surfaced for the Help panel's
  // Troubleshooting page so bug reports auto-stamp the right build.
  getHealth: () =>
    http<{ ok: boolean; api_version: string; app_version: string }>('/api/health'),


  // v0.12.0: media-state database health. Returns schema version,
  // per-table row counts, on-disk size, last-updated timestamps.
  // Used by diagnostic tools and the upcoming Database tab.
  getDbStats: () => http<DbStats>('/api/db/stats'),

  // Rule 2: library walk + prune missing items.
  getLibraryWalkStatus: (serverId: string) =>
    http<LibraryWalkStatus>(`/api/servers/${encodeURIComponent(serverId)}/library-walk`),
  // M11: the walk runs in a background thread on the server; this
  // POST returns immediately with the walk id. Poll
  // getLibraryWalkStatus for progress and completion counts.
  triggerLibraryWalk: (serverId: string) =>
    http<{
      walk_id: number;
      started: number;
      skipped_duplicate?: number;
    }>(
      `/api/servers/${encodeURIComponent(serverId)}/library-walk`,
      { method: 'POST' },
    ),
  prunePreview: (serverId: string, olderThanDays: number) =>
    http<PrunePreview>(
      `/api/servers/${encodeURIComponent(serverId)}/prune-preview` +
      `?older_than_days=${encodeURIComponent(olderThanDays)}`,
    ),
  pruneStaleItems: (
    serverId: string,
    body: {
      db_admin_username: string;
      db_admin_password: string;
      older_than_days: number;
      dry_run?: boolean;
    },
  ) =>
    http<PruneResult>(
      `/api/servers/${encodeURIComponent(serverId)}/prune-stale-items`,
      { method: 'POST', body: JSON.stringify(body) },
    ),

  // Snapshots (PR-13 - registry-backed).
  // Reads return ``{snapshots: Snapshot[]}``; writes are db_admin
  // gated and carry ``{db_admin_username, db_admin_password}`` in
  // the body.
  listSnapshots: () =>
    http<{ snapshots: Snapshot[] }>('/api/snapshots'),
  snapshotDownloadUrl: (id: string) =>
    `/api/snapshots/${encodeURIComponent(id)}/download`,
  // Streams the raw .db file. No render, no cache - canonical
  // artifact straight off disk. Companion to snapshotDownloadUrl
  // (which produces the .plexexport.json sidecar).
  snapshotDbDownloadUrl: (id: string) =>
    `/api/snapshots/${encodeURIComponent(id)}/download-db`,
  deleteSnapshot: (
    id: string,
    // ``keep_json: true`` moves the cached JSON sidecar (or
    // materialises one first if not cached) to the JSON-archive
    // directory before dropping the .db + registry row. The file
    // shows up in the JSON Archives panel afterward.
    body: {
      db_admin_username: string;
      db_admin_password: string;
      keep_json?: boolean;
    },
  ) =>
    http<{
      deleted: string;
      file_removed: boolean;
      json_removed: boolean;
      json_archived: boolean;
      archived_path: string | null;
      errors: string[];
    }>(
      `/api/snapshots/${encodeURIComponent(id)}`,
      { method: 'DELETE', body: JSON.stringify(body) },
    ),
  deleteAllSnapshotsForServer: (
    serverId: string,
    body: { db_admin_username: string; db_admin_password: string },
  ) =>
    http<{ deleted: number; errors: number }>(
      `/api/snapshots/server/${encodeURIComponent(serverId)}`,
      { method: 'DELETE', body: JSON.stringify(body) },
    ),

  // Legacy on-disk ``.plexexport.json`` files relocated to
  // ``snapshots/legacy/`` by the PR-13 migration. Read-only browse
  // + download, db_admin-gated delete.
  listLegacySnapshots: () =>
    http<ExportArchive[]>('/api/snapshots/legacy'),
  legacySnapshotDownloadUrl: (name: string) =>
    `/api/snapshots/legacy/${encodeURIComponent(name)}/download`,
  deleteLegacySnapshot: (
    name: string,
    body: { db_admin_username: string; db_admin_password: string },
  ) =>
    http<{ deleted: string }>(
      `/api/snapshots/legacy/${encodeURIComponent(name)}`,
      { method: 'DELETE', body: JSON.stringify(body) },
    ),
  // Clear every .plexexport.json archive in <output_dir>/legacy/.
  // db_admin gated. Returns counts of files deleted + per-file
  // errors (best-effort sweep, doesn't abort on one failure).
  deleteAllLegacyArchives: (
    body: { db_admin_username: string; db_admin_password: string },
  ) =>
    http<{ deleted: number; errors: string[] }>(
      '/api/snapshots/legacy',
      { method: 'DELETE', body: JSON.stringify(body) },
    ),

  // PR-10 - per-server managed users (the User Management sub-tab
  // under Servers). Reads are JWT-only (operator+); writes carry
  // db_admin credentials in the body and the backend re-validates
  // them on every call.
  // PR-11.1 - the list endpoint accepts ``include_hidden`` so the
  // 'Show hidden' toggle and the ServersPanel diff effect can see
  // tombstoned rows without losing the default-filter behaviour.
  listServerManagedUsers: (serverId: string, includeHidden = false) =>
    http<{ server_id: string; users: ServerManagedUser[] }>(
      `/api/managed-users/${encodeURIComponent(serverId)}` +
      (includeHidden ? '?include_hidden=true' : ''),
    ),
  getServerManagedUser: (serverId: string, username: string) =>
    http<ServerManagedUser>(
      `/api/managed-users/${encodeURIComponent(serverId)}/${encodeURIComponent(username)}`,
    ),
  updateServerManagedUser: (
    serverId: string,
    username: string,
    body: {
      db_admin_username: string;
      db_admin_password: string;
      display_name?: string;
      clear_display_name?: boolean;
      auth_token?: string;
      clear_auth_token?: boolean;
      plex_home_pin?: string;
      clear_plex_home_pin?: boolean;
      service_password?: string;
      clear_service_password?: boolean;
      // PR-11.1 - per-server tombstone. true = hide on this server,
      // false = unhide on this server. Omit to leave alone.
      tombstoned?: boolean;
    },
  ) =>
    http<ServerManagedUser>(
      `/api/managed-users/${encodeURIComponent(serverId)}/${encodeURIComponent(username)}`,
      { method: 'PATCH', body: JSON.stringify(body) },
    ),
  // PR-11.1 - DELETE is now an alias for the per-server hide path.
  // The row stays in the DB with ``tombstoned=1`` and credentials
  // preserved; the response carries ``hidden_scope='server'``.
  hideServerManagedUser: (
    serverId: string,
    username: string,
    body: { db_admin_username: string; db_admin_password: string },
  ) =>
    http<{ hidden: string; server_id: string; hidden_scope: 'server' }>(
      `/api/managed-users/${encodeURIComponent(serverId)}/${encodeURIComponent(username)}`,
      { method: 'DELETE', body: JSON.stringify(body) },
    ),
  syncServerManagedUsers: (serverId: string) =>
    http<{ server_id: string; synced: number; source_error: string | null }>(
      `/api/managed-users/${encodeURIComponent(serverId)}/sync`,
      { method: 'POST' },
    ),
  // PR-11.1 - global tombstones. Username-keyed across every server.
  listGlobalTombstones: () =>
    http<{ usernames: GlobalTombstone[] }>('/api/managed-users/global-tombstones'),
  addGlobalTombstone: (
    body: { db_admin_username: string; db_admin_password: string; username: string },
  ) =>
    http<{ username: string; tombstoned: true }>(
      '/api/managed-users/global-tombstones',
      { method: 'POST', body: JSON.stringify(body) },
    ),
  removeGlobalTombstone: (
    username: string,
    body: { db_admin_username: string; db_admin_password: string },
  ) =>
    http<{ username: string; tombstoned: false }>(
      `/api/managed-users/global-tombstones/${encodeURIComponent(username)}`,
      { method: 'DELETE', body: JSON.stringify(body) },
    ),

  // PR-13 fix #2 - write one credential to every server where this
  // username has a managed-user row. db_admin gated; empty plaintext
  // clears the credential on every match. Returns
  // ``{applied, missing}`` so the UI can surface how many rows took
  // the write.
  setGlobalManagedUserCredential: (
    username: string,
    body: {
      db_admin_username: string;
      db_admin_password: string;
      kind: 'auth_token' | 'plex_home_pin' | 'service_password';
      plaintext: string;
    },
  ) =>
    http<{ username: string; kind: string; applied: number; missing: number }>(
      `/api/managed-users/global-credential/${encodeURIComponent(username)}`,
      { method: 'POST', body: JSON.stringify(body) },
    ),
};

// ── WebSocket wrapper ────────────────────────────────────────────────────────

type SocketListener = (snap: DashboardFrame) => void;

/**
 * Owns the WebSocket connection to the dashboard endpoint.
 *
 * Single instance, lazy-connect on the first .subscribe() call.
 * Automatically reconnects with linear backoff if the socket drops
 * (the user's job may still be running; we don't want to leave them
 * watching a frozen panel).
 */
export class DashboardWsClient {
  private ws: WebSocket | null = null;
  private listeners = new Set<SocketListener>();
  private reconnectTimer: number | null = null;
  private retryDelayMs = 1000;
  private readonly maxDelayMs = 10_000;
  private explicitlyClosed = false;
  // Tracks whether the *next* close-code-4001 should be treated as a
  // hard auth failure rather than a "token might just be expired"
  // event. Set to true after we silently refresh and reconnect in
  // response to a 4001; cleared the moment we receive any successful
  // frame on the new connection. If the post-refresh connection
  // immediately 4001s again, the refresh produced a token the server
  // still rejects - that's a real auth failure and we surrender to
  // the login screen.
  private postRefreshReconnect = false;

  subscribe(fn: SocketListener): () => void {
    this.listeners.add(fn);
    this.ensureConnected();
    return () => {
      this.listeners.delete(fn);
    };
  }

  /** Force-close the socket. Call from React cleanup on full unmount. */
  close() {
    this.explicitlyClosed = true;
    if (this.reconnectTimer !== null) {
      window.clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    this.ws?.close();
    this.ws = null;
  }

  private ensureConnected() {
    if (this.ws && (this.ws.readyState === WebSocket.OPEN || this.ws.readyState === WebSocket.CONNECTING)) {
      return;
    }
    this.explicitlyClosed = false;
    // Same-origin URL - works in dev (Vite proxy) and prod (nginx proxy).
    // v0.11.0: append ``?token=<jwt>`` when an access token is set.
    // Browsers can't send custom headers on a WebSocket handshake, so
    // the token rides on the query string and the backend route
    // handler validates it before accepting the connection.
    const proto = window.location.protocol === 'https:' ? 'wss' : 'ws';
    const qs = _accessToken
      ? `?token=${encodeURIComponent(_accessToken)}`
      : '';
    const url = `${proto}://${window.location.host}/ws/dashboard${qs}`;
    const ws = new WebSocket(url);
    this.ws = ws;

    ws.onmessage = (ev) => {
      try {
        const msg = JSON.parse(ev.data) as DashboardFrame;
        this.listeners.forEach((l) => l(msg));
        // A successful frame resets both the retry backoff and the
        // post-refresh sentinel - the connection is healthy now, so
        // a future 4001 (e.g. another 30 min from now) should once
        // again get the refresh-and-reconnect treatment.
        this.retryDelayMs = 1000;
        this.postRefreshReconnect = false;
      } catch {
        // Ignore malformed frames.
      }
    };

    ws.onclose = (ev) => {
      this.ws = null;
      if (this.explicitlyClosed || this.listeners.size === 0) return;
      // Close code 4001 = "auth required / failed" (set by the
      // backend handshake when the ?token=<jwt> is missing or
      // rejected). With Fix 1's 30-minute access token TTL this is
      // the expected path when an idle session crosses the expiry
      // line - we attempt a silent refresh and reconnect at once,
      // bypassing the normal linear backoff so the operator sees
      // at most a 1-2 second blip instead of waiting for the next
      // poll cycle to invalidate-and-refresh the token.
      if (ev.code === 4001) {
        if (this.postRefreshReconnect) {
          // We already refreshed once for this cycle and the new
          // connection ALSO 4001s. That's a real auth failure -
          // give up and bounce to login.
          this.postRefreshReconnect = false;
          this.explicitlyClosed = true;
          if (_onUnauthorized) _onUnauthorized();
          return;
        }
        // Fire-and-forget the refresh; reconnect when it lands.
        this.postRefreshReconnect = true;
        _refreshOnce().then((newToken) => {
          if (this.explicitlyClosed || this.listeners.size === 0) return;
          if (!newToken) {
            // Refresh cookie is gone or invalid - drop to login.
            this.postRefreshReconnect = false;
            this.explicitlyClosed = true;
            if (_onUnauthorized) _onUnauthorized();
            return;
          }
          // Immediate reconnect with the new token. The new value is
          // already in _accessToken (set inside _refreshOnce), so
          // ensureConnected() will pick it up when it builds the URL.
          this.ensureConnected();
        });
        return;
      }
      // Non-auth close - schedule a reconnect with linear backoff.
      this.reconnectTimer = window.setTimeout(() => {
        this.reconnectTimer = null;
        this.retryDelayMs = Math.min(this.retryDelayMs + 1000, this.maxDelayMs);
        this.ensureConnected();
      }, this.retryDelayMs);
    };

    ws.onerror = () => {
      // The browser will fire onclose right after this - handler there
      // schedules the retry. We don't double-schedule here.
    };
  }
}

export const dashboardWsClient = new DashboardWsClient();
