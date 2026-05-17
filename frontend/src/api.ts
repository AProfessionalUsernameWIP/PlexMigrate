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
  // run start. Keys: raw Plex identifier. Values: end user-chosen
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
  // the live in-flight ETR, projected from a rolling window of real
  // throughput samples. ``null`` means the tracker doesn't have enough
  // samples yet.
  rolling_etr_seconds?: number | null;
  // Gap-B of the post-cutover review: the new ETA training engine's
  // pre-run prediction, computed at run start and decayed by elapsed
  // wall-clock. Used as the dashboard's "Estimated remaining" value
  // when ``rolling_etr_seconds`` is null (the live tracker is still
  // warming up). ``null`` when the engine has no history for this
  // job shape AND fell through to its tier-5 default + zero samples.
  predicted_etr_seconds?: number | null;
  // Which source the dashboard should label the rendered value with.
  // "live" = rolling_etr_seconds; "predicted" = predicted_etr_seconds;
  // null = no estimate yet (renders as "Calculating...").
  etr_source?: 'live' | 'predicted' | null;
  // Bugfix 2026-05-16: backend-computed gate for the "Almost done"
  // copy. True when the run is in the post-engine finalize phase OR
  // when cumulative library completion is past the end user-tunable
  // eta_almost_done_progress_threshold (Danger tab). The frontend
  // refuses to render "Almost done" unless this is true, even when
  // the numeric ETR drops below 5 seconds; otherwise a too-small
  // cold-start prediction could falsely claim a 6-minute run was
  // about to finish.
  is_finishing?: boolean;
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
  // as "Finalizing - <label>".
  finalizing?: string | null;
}

// Rule 4: one restoration row per playlist / collection in an import.
// ``restored / total`` drives the "15 / 20 items restored" badge;
// ``skipped_items`` carries the per-member miss list (capped at 25 -
// the backend leaves the full set in the run log). ``smart=true``
// flags a smart playlist that was skipped entirely; ``reason`` is
// the end user-facing one-liner when restored < total or the whole
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
  // job so the end user only sees the source / destination(s)
  // they're actually running against.
  server_name?: string;
}

export interface JobPayload {
  job_id: string;
  // v0.9.0 added 'direct' alongside the original two modes. 2026-05-17
  // added 'playlist_copy' for the Playlist Mgmt Deploy refactor (each
  // Deploy becomes a JobRecord on the same queue). The server-side
  // JobRecord uses the same literal strings.
  mode: 'snapshot' | 'restore' | 'direct' | 'playlist_copy';
  // 'stopping' (v0.9.3) is the intermediate state between user click
  // and engine return - see server/jobs.py :: STATE_STOPPING.
  // 'completed_with_errors' (v0.13.x) is the partial-success outcome:
  // the engine work succeeded (primary data is in media.db, the
  // migration ran end-to-end) but a non-fatal post-engine step (e.g.
  // snapshot artifact capture) failed. ``error`` carries the message;
  // re-running is safe. Surface this as amber/yellow in the UI.
  state: 'idle' | 'queued' | 'running' | 'stopping' | 'completed' | 'completed_with_errors' | 'failed' | 'cancelled';
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
  // ``cumulative`` runs from process start so the end user can spot
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

// Plan[ETA-TRAINING] PR-D. The job-form preview ships a body in this
// shape; the response carries a whole-job rollup plus a per-library
// breakdown so the UI can show which library dominates the runtime.
// Plan[RUN-JOB-UI] follow-up: cross-server identity-map row shape.
// Mirrors the columns on media.db's user_identity_map table; used by
// the standalone mapping panel + the engine's future cross-server
// user-filter resolver.
export interface UserIdentityMap {
  id: number;
  // v12 canonical pair (app_user_uuid). Each end may resolve to a
  // (server_id, user_handle) tuple via the backend's row lookup,
  // surfaced in the legacy *_id / *_handle fields below for
  // compatibility with existing UI surfaces. Either side can be
  // null when the UUID no longer resolves to a live row (server
  // removed, user deleted) - the panel can render an "unresolved"
  // badge in that case.
  user_a_uuid: string;
  user_b_uuid: string;
  server_a_id: string | null;
  user_a_handle: string | null;
  server_b_id: string | null;
  user_b_handle: string | null;
  source: 'manual' | 'auto_copy';
  created_at: number;
}


export interface EtaPredictRequestLibrary {
  name: string;
  library_type: string;
  items_count: number | null;
}
export interface EtaPredictRequest {
  mode: 'snapshot' | 'restore' | 'direct';
  source_server_id: string;
  libraries: EtaPredictRequestLibrary[];
  metrics_enabled: Record<string, boolean>;
  user_count: number;
  workers: number;
  bulk_strategy: 'smart' | 'force_bulk' | 'force_server_side';
}
export interface EtaPredictionLibraryRow {
  name: string;
  library_type: string;
  items_count: number | null;
  point: number;
  low: number;
  high: number;
  std: number;
  samples: number;
  tier: number;
  // Latency-offset multiplier applied to the regression prediction.
  // 1.0 = no offset (feature disabled, ping data unavailable, or no
  // drift from training-time ping). Above 1.0 = worse current ping;
  // below 1.0 = better. Bounded by the inflation cap + deflation floor.
  latency_multiplier?: number;
  display: string;
}
export interface EtaPrediction {
  mode: string;
  point: number;
  low: number;
  high: number;
  std: number;
  samples: number;
  tier: number;
  confidence_z: number;
  latency_multiplier?: number;
  display: string;
  per_library: EtaPredictionLibraryRow[];
}


export interface EtaTrainingBucket {
  label: string;
  library_type: string;
  bulk_strategy: string;
  samples: number;
  tier_one_ready: boolean;
  anchor_ready: boolean;
  ping_ema_ms: number | null;
  last_observed_at: number;
  predicted_at_xbar_seconds: number;
}
export interface EtaTrainingServerStatus {
  server_id: string;
  buckets: EtaTrainingBucket[];
  summary: {
    total_buckets: number;
    tier_one_count: number;
    anchor_count: number;
    min_samples_for_tier_one: number;
  };
}
export interface EtaTrainingStatus {
  by_server: EtaTrainingServerStatus[];
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
  // interpretation - currently plex_connect_timeout_seconds and
  // viewcount_increment_cap. Map: server_id → {key: value}.
  tunables_per_server?: Record<string, Record<string, number>>;
  // ETR colour multiplier for the dashboard stall thresholds (Phase 4).
  // Clamped to [0.5, 2.0] at the read boundary. Default 1.0.
  etr_color_multiplier?: number;
  // Item 1: sudo-style elevation cache TTL in seconds. Floored 60,
  // ceiled 3600 by the backend. Default 600 (10 minutes).
  elevation_ttl_seconds?: number;
  // Item 1: which onboarding model has run. 1 = legacy single-account,
  // 2 = v2 two-step setup or upgrade-split completed.
  auth_setup_version?: number;
  // Item 5: global tooltip toggle. When false the InfoTip component
  // renders no popover affordance; the full body lives in the Help
  // tab only. Default true; root_admin to write.
  tooltips_enabled?: boolean;
  // Item 2 follow-up: opt-in token rotation on Refresh.
  auto_rotate_tokens_on_refresh?: boolean;
  // Item 3 follow-up: opt-in username-string fallback for PIN migration.
  pin_migration_allow_username_fallback?: boolean;
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
  // when the end user removes a server from the registry.
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
  // v0.13.x - Restore-side run defaults (Merge / Replace). Per-run +
  // per-server overrides take precedence; this is the bottom-of-chain
  // fallback. Defaults: {"mode": "merge", "auto_capture_before_replace": true}.
  restore_defaults?: {
    mode?: 'merge' | 'replace';
    auto_capture_before_replace?: boolean;
    // v0.13.x: Merge sub-strategy for watch-count math. "higher"
    // (default) = destination ends at max(stored, current); "sum" =
    // current + stored. End user opt-in. Ignored when mode=='replace'.
    merge_watch_strategy?: 'higher' | 'sum';
  };
  // v0.13.x: library-level concurrency cap for file-mediated restore.
  // Replaces the legacy hardcoded ``min(3, libraries)``. Default 3
  // preserves today's behavior; end users that see Plex 429s during
  // multi-library restores lower this (1 = serial). Direct transfer
  // is still serial in this release - this knob does not apply there.
  restore_library_workers?: number;
  // v0.13.x: per-fan-out destination concurrency cap. 0 (default)
  // means no cap - one thread per destination, today's behavior. A
  // positive integer caps the destination pool. Independent of
  // restore_library_workers (the two axes are orthogonal).
  fan_out_destination_workers?: number;
  // v0.13.x: library-level concurrency cap for snapshot. 0 (default)
  // inherits ``workers`` (today's coupled behavior - preserved on
  // upgrade); a positive value caps libraries-in-parallel without
  // affecting the per-library HTTP worker count.
  snapshot_library_workers?: number;
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
  // Backend kind: "plex" today, "emby" / "jellyfin" reserved for future
  // adapters. The Snapshots panel uses (name, url, service) as the
  // composite identity key so two registry rows describing the same
  // server (same backend reachable at the same URL) merge into one
  // tab, while same-URL-different-backend (a Plex and an Emby both on
  // the same host) remain separate.
  service?: string;
  // PR-Backends backend discriminator. Mirror of the persisted
  // ``service_type`` column. Drives:
  //  - which authentication label the new-server form shows
  //    ("Plex Token" vs "API Key" vs ...)
  //  - which adapter the engine uses (Plex via plexapi, Jellyfin / Emby
  //    via the HTTP adapter)
  //  - which icon / badge the Servers panel renders alongside the row
  // Rows that pre-date the picker default to 'plex'.
  service_type?: 'plex' | 'jellyfin' | 'emby';
  has_token: boolean;
  // Pending-token state (2026-05-15). When the end user's typed
  // token failed at Add-server time and we used a borrowed token
  // instead, the typed one is stashed under ``pending_token`` (the
  // ciphertext never leaves the backend). The frontend gets only
  // ``has_pending_token`` plus the timestamps so it can render a
  // chip on the Servers row + a Retry button. The retry endpoint
  // ``POST /api/servers/{id}/retry-pending-token`` probes the
  // stashed token; on success it replaces the active token and
  // clears every pending_* field.
  has_pending_token?: boolean;
  pending_token_first_seen_at?: number;
  pending_token_last_probed_at?: number;
  pending_token_source?: string;
  last_status: 'ok' | 'unreachable' | 'auth_error' | 'unknown';
  last_status_detail: string;
  last_checked_at: number;
  last_libraries: LibraryDescriptor[];
  owner_name: string;
  // Latency of the last lightweight ping in milliseconds. ``null``
  // if no ping has ever been recorded for this server.
  last_response_ms: number | null;
  // v0.9.6 Feature 3: end user-chosen friendly names for users on
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
  // v0.14 - Plex Media Server software version (e.g. "1.32.5.7349").
  // Captured at probe / refresh time. Used by the JobForm + Schedule
  // forms to gate Fast Collection Detection (which requires Plex ≥1.32).
  // Empty string when the row pre-dates this field or hasn't been
  // refreshed yet - treat unknown as "unsupported" so we never enable
  // a feature against an unverified server.
  plex_version?: string;
  // Timing-spec inputs. Server-wide counts the ETR estimator needs.
  // ``null`` (or missing) means we never captured them; the next
  // Refresh / Test populates them. The frontend renders unknown
  // counts as `?` rather than `0` so the end user can tell stale
  // metadata from genuine empties.
  playlist_count?: number | null;
  collection_count?: number | null;
  counts_refreshed_at?: number | null;
  // Item 2 (admin-management plan): present only on responses from
  // POST /api/servers/{id}/test (the Refresh button). Reports what
  // the additive token-capture sweep did. Absent on other endpoints
  // that return ServerView.
  token_capture?: TokenCaptureSummary;
}

export interface TokenCaptureSummary {
  captured: number;
  skipped_existing: number;
  throttled: boolean;
  errors: string[];
}

// Item 3: one row in the cross-server PIN migration suggestion list.
export interface PinMigrationSuggestion {
  target_username: string;
  source_server_id: string;
  source_server_name: string;
  source_username: string;
  match_kind: 'machine_id' | 'username';
}

// Auto-fallback offer (2026-05-15): when the end user's typed token
// returns 401 but one of their existing registered servers' tokens
// connects against the same URL, the backend includes this in the
// probe response. Frontend renders a "Try fallback token" button on
// the auth_error banner; clicking it sets use_fallback_from_server_id
// on the create payload so the borrowed working token becomes the
// active token and the typed one is stashed as a pending retry.
export interface ProbeFallbackOffer {
  borrowed_from_server_id: string;
  borrowed_from_server_name: string;
  friendly_name: string;
  machine_identifier: string;
  owner_name: string;
  libraries: LibraryDescriptor[];
  response_ms: number | null;
}

// v0.10.0: result of probing a URL+token without registering it.
// Returned by /api/servers/test-unsaved. ``name_mismatch`` flags
// "you typed X but the server identifies as Y"; ``duplicate_of``
// carries the friendly name of the existing registered server that
// shares this server's machine_identifier (null when no collision).
export interface ProbeUnsavedResult {
  ok: boolean;
  // ``auth_error`` covers 401 (token rejected) and 403 (token's
  // account has no permission); the panel renders a token-finder
  // help link distinctly from generic unreachable. ``ssl_error`` and
  // ``timeout`` are split out so the end user sees the right action
  // (trust the cert / switch protocol vs check the server health).
  status: 'ok' | 'unreachable' | 'auth_error' | 'ssl_error' | 'timeout' | 'unknown';
  detail: string;
  friendly_name: string;
  machine_identifier: string;
  owner_name: string;
  libraries: LibraryDescriptor[];
  response_ms: number | null;
  name_mismatch: boolean;
  duplicate_of: string | null;
  // Timing-spec counts surfaced from the unsaved probe so the
  // end user can see "this would register a server with 3955 movies,
  // 12483 episodes, etc." before clicking Save.
  playlist_count?: number | null;
  collection_count?: number | null;
  counts_refreshed_at?: number | null;
  // Auto-fallback. Only present when status==='auth_error' AND one
  // of the end user's existing registered tokens connected against
  // the same URL. ``null`` / undefined means no fallback is on
  // offer (no other registered servers, or none of their tokens
  // worked).
  fallback?: ProbeFallbackOffer | null;
}

// v0.9.6 Feature 3: one row in the per-server Users panel.
// ``raw_name`` is the canonical identifier (email for owner, username
// for managed). ``display_name`` is the end user's chosen friendly
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
  // PR-Backends backend discriminator. Omitted on legacy callers ->
  // backend defaults to 'plex'. The new-server form sets this
  // explicitly based on the radio picker so the registry knows which
  // adapter to wrap the connection with.
  service_type?: 'plex' | 'jellyfin' | 'emby';
  // Auto-fallback save (2026-05-15). When set, the backend registers
  // the new server using the BORROWED token from this existing
  // server's stored credential, and stashes the end user's typed
  // token (in ``token`` above) as a retry-able pending_token.
  // Set by the Add Server form after the end user clicks
  // "Try fallback token" on the auth_error banner.
  // Only valid for Plex backends; the backend rejects this combined
  // with service_type other than 'plex' because Jellyfin / Emby
  // tokens are server-local and not shareable across servers.
  use_fallback_from_server_id?: string;
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
  // 2026-05-16 (developer Emby fix): id-keyed source / destinations.
  // Backend (ScheduleIn) prefers ID; the editor sets both fields so
  // the routing chooses ID. Names stay for back-compat.
  source_server_id?: string | null;
  dest_server_ids?: string[] | null;
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
  // knobs the Run Job form exposes - when set, each scheduled fire
  // forwards them to the snapshot job request as overrides on the
  // global / per-server defaults.
  workers?: number | null;
  scrobble_workers?: number | null;
  verbose?: boolean;
  log_dir?: string | null;
  skip_playlist_prebuild?: boolean;
  fast_collection_detection?: boolean;
  watch_ratings_filter_strategy?: 'smart' | 'force_bulk' | 'force_server_side' | '';
  // v0.14 - per-schedule user filter. List of Plex identifiers (owner
  // email + managed usernames). null / undefined = capture every user
  // the source server reports.
  user_filter?: string[] | null;
  // Task 2 (admin-management plan follow-up, 2026-05-15): schedule
  // mode + restore/direct fields. Pre-Task-2 schedules read as
  // mode='snapshot' (backend default).
  mode?: 'snapshot' | 'restore' | 'direct';
  dest_server_names?: string[] | null;
  input_files?: string[] | null;
  restore_mode?: 'merge' | 'replace' | null;
  auto_capture_before_replace?: boolean | null;
  confirm_replace?: boolean;
  merge_watch_strategy?: 'higher' | 'sum' | null;
  // Required true to save a schedule with merge_watch_strategy='sum'.
  // End user confirmation that each fire ADDS stored counts on top
  // of the destination's current counts (compounds across fires).
  confirm_additive_merge?: boolean;
  remap_old?: string | null;
  remap_new?: string | null;
  // Per-schedule strict-match override (restore + direct modes).
  // None / undefined = inherit Run Defaults at fire time.
  strict_match?: boolean | null;
  // Phase C (admin-management follow-up, 2026-05-15): per-library
  // metric filter. Authoritative when set; the engine consults this
  // per library. Forward-typed via the shared LibraryMetricsMap.
  // ``null`` (or undefined) on legacy schedule rows; the backend
  // ScheduleIn validator expands global include_* flags into this
  // map at save time so post-save schedules always have a populated
  // map matching their library list.
  library_metrics?: Record<string, { watch_history: boolean; ratings: boolean; playlists: boolean; collections: boolean }> | null;
  // 2026-05-16 alignment additions (Plan[SCHEDULES-ALIGNMENT]):
  // bring the schedule row up to Run Job parity so the editor can
  // mirror the Run Job form 1:1.
  include_managed_users?: boolean;
  rate_mode?: 'default' | 'tunable' | 'numeric_only' | null;
  rate_threshold?: number | null;
  user_create_specs?: Array<{
    source_user_handle: string;
    target_username: string;
    temp_password: string;
    target_user_policy?: Record<string, unknown> | null;
  }> | null;
  // PIN-preflight acknowledgement persisted at save time. Cleared
  // automatically by the editor when source_server_name changes.
  pin_preflight_ack?: boolean;
  // Per-Run Settings parity: overwrite_playlists is a no-op flag
  // in the engine today but Run Job exposes it, so Schedules adopt
  // it for the PerRunSettingsPanel prop surface to match.
  overwrite_playlists?: boolean | null;
  // Plan[MIXED-MEDIA-PLAYLISTS]-2026-05-16: per-run overrides for
  // the mixed-media playlist strategy. All optional; null/undefined
  // inherits the corresponding global tunable from
  // Tunables ▸ Playlist ▸ Mixed-media. Same field set lives on
  // SnapshotJobIn / RestoreJobIn / DirectTransferIn server-side, so
  // the editor wires identically across modes.
  mixed_media_behavior?: 'skip' | 'dominant' | 'split' | null;
  mixed_media_dominance_threshold?: number | null;
  mixed_media_video_routing?: 'library_agnostic' | 'library_dominant' | null;
  mixed_media_logging?: 'full' | 'decisions_only' | 'off' | null;
  mixed_media_collision_handling?: 'duplicate' | 'suffix' | 'skip' | null;
  // Phase C: per-destination cross-platform preflight resolutions
  // persisted on the schedule row. Key is destination_server_id.
  // Read by the scheduler at fire time + by the resolution editor
  // UI for re-editing. Backend computes resolutions_status from
  // this against the destination's current user roster.
  cross_platform_resolutions?: Record<string, unknown> | null;
  resolutions_status?: 'ok' | 'auto_fallback' | 'needs_review';
}

export interface LogRun {
  name: string;
  mtime: number;
  size: number;
  passed: boolean | null;
  file_count: number;
  // Per-server identity surfaced by the backend (Servers > Logs).
  // Backend extracts ``server_slug`` from the run-dir name and
  // reverse-maps to a registered server via safe_server_name. Either
  // is null when the run-dir doesn't conform to the expected format
  // or the server has since been removed from the registry.
  server_slug?: string | null;
  server_id?: string | null;
  server_name?: string | null;
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

// Feature 3 phase 3.3 / 3.4: developer-tool unit test runner.
//
// ``DevTestRunSummary`` is the shape POST /api/dev/run-tests returns
// and GET /api/dev/test-runs returns one of per run in its ``runs``
// array. Mirrors server.dev_test_harness.RunResult.to_summary_dict.
// The frontend's Developer tab renders this both for the most-recent
// run (POST response) and for the history list (GET response).
export interface DevTestFailedTest {
  nodeid: string;
  duration_seconds: number;
  error_excerpt: string;
}

export interface DevTestXfailTest {
  nodeid: string;
  reason: string;
}

export interface DevTestRunSummary {
  run_id: string;
  mode: 'synthetic' | 'structural' | 'live' | string;
  started_at: number;
  ended_at: number;
  duration_seconds: number;
  test_target: string;
  filter: string | null;
  exit_code: number;
  operator: string | null;
  totals: {
    collected: number;
    passed_normal: number;
    passed_xfail: number;
    failed: number;
    errors: number;
    skipped: number;
    unexpected_pass: number;
  };
  failed_tests: DevTestFailedTest[];
  xfail_tests: DevTestXfailTest[];
  raises_tests: string[];
  log_path: string;
  _summary_filename?: string;
}

// Feature 1 phase 1.5: runtime breakdown panel.
//
// ``RuntimeRunSummary`` is one row in the Recent Runs list returned
// by GET /api/run-timings/runs. ``RuntimeEntry`` is one row in the
// per-run drilldown returned by GET /api/run-timings/runs/<id>. The
// shapes mirror server.run_timings_db's read helpers; see that
// module for the schema details.
export interface RuntimeRunSummary {
  run_id: string;
  started_at: number;
  ended_at: number;
  duration_seconds: number;
  entry_count: number;
  total_items_processed: number;
}

export interface RuntimeEntry {
  id: number;
  run_id: string;
  scope: 'run' | 'library' | 'user' | 'batch' | 'operation' | string;
  label: string;
  server_id: string | null;
  library: string | null;
  user_handle: string | null;
  started_at: number;
  ended_at: number;
  duration_seconds: number;
  items_processed: number | null;
  etr_at_start: number | null;
  recorded_at: number;
  extra: Record<string, unknown>;
}

// Phase 4 of the dashboard / log reorg: per-RUN row in the
// run_history sibling table. One row per completed job; drives
// Servers > Recent Runtimes. has_settings_log / has_restoration_log
// gate the deep-link buttons in the row's "Logs" cell.
export interface RecentRunRow {
  run_id: string;
  started_at: number;
  finished_at: number;
  job_type: string;
  server_id: string | null;
  server_name: string | null;
  libraries: string[];
  users_affected: number;
  users_affected_list: string[];
  state: string;
  duration_ms: number;
  run_log_dir: string | null;
  has_settings_log: boolean;
  has_restoration_log: boolean;
  error_summary: string | null;
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
  // Phase E (2026-05-16): ``user_count`` is the full roster captured
  // (owner + every managed user the engine attempted), and
  // ``user_count_with_data`` is the subset that contributed rows to
  // at least one metric table. The Exports panel renders
  // "N of M users" when the two differ.
  user_count: number | null;
  user_count_with_data: number | null;
  row_counts: Record<string, number>;
  file_size: number | null;
  prebuilt_json_path: string | null;
  // True when the .db at file_path is on disk. False rows are
  // rendered with a "File missing" badge + disabled Download +
  // a Remove-entry button so the end user can clean dead rows.
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
  // accordingly so the end user can compare .db vs JSON size at
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
  // Phase D (admin-management follow-up, 2026-05-15): a short
  // human-readable one-line summary stamped at capture time. Lists
  // the server, user count, library set, and metric set. Null on
  // pre-Phase-D rows (the Exports panel renders an em-dash placeholder
  // for null).
  description: string | null;
}

// Phase C (admin-management follow-up, 2026-05-15): per-library metric
// filter. Keys are library names; values describe which metric tables
// to capture for THAT library. The legacy global include_* flags are
// expanded into this shape at request-parse time so the engine only
// ever consults the map.
export interface LibraryMetricsRow {
  watch_history: boolean;
  ratings: boolean;
  playlists: boolean;
  collections: boolean;
}
export type LibraryMetricsMap = Record<string, LibraryMetricsRow>;

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

// Phase 6 of the dashboard / log reorg: callback fired when an API
// call returns 403 with the elevation-required detail. App.tsx
// registers a handler that opens the ElevateModal, prompts for the
// password, calls /api/auth/elevate, and resolves the promise true on
// success / false on cancel. The http<T> helper waits on that promise
// and transparently retries the original request when it resolves
// true. This pattern lets any caller benefit from the auto-retry
// without changing its signature.
//
// The detail string the backend returns is the marker; matching it
// exactly keeps unrelated 403s (genuine permission failures) flowing
// through the normal error path instead of triggering the modal.
let _onElevationRequired:
  | (() => Promise<boolean>)
  | null = null;

/**
 * Substring the backend's elevation gate puts in its 403 detail.
 * Kept as a const so tests and the modal share the canonical token.
 */
export const ELEVATION_REQUIRED_DETAIL_MARKER = 'recent password re-confirmation';

// ── Token persistence - LoginBugFix1 (no persistence) ───────────────
//
// The token lives ONLY in this module's in-memory closure for the
// lifetime of one mounted <App />. There is no localStorage,
// sessionStorage, or cookie write of the access token anywhere in
// the frontend.
//
// Reasoning (see LoginBugFix1.md): the prior "session" / "persistent"
// modes let the end user's browser sync (Chrome sync, etc.) replicate
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

/**
 * Phase 6 - register the elevation handler. App.tsx supplies a
 * function that opens the ElevateModal and resolves the returned
 * promise true after a successful POST /api/auth/elevate, or false
 * when the user cancels. Subsequent 403s carrying the elevation
 * marker call this handler and retry the original request on
 * resolved-true.
 */
export function onElevationRequired(
  handler: (() => Promise<boolean>) | null,
): void {
  _onElevationRequired = handler;
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
  // Item 1 (admin-management plan): which onboarding model has run.
  // 1 = legacy single-account install (pre-Item-1). 2 = v2 two-step
  // setup OR an upgrade-split completed. When setup_needed=false and
  // setup_version=1, the frontend forces the upgrade-split modal on
  // first login after upgrade.
  setup_version?: number;
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

// Access Control - per-user permission grant/revoke layer. Returned
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
  // True when this row is root_admin - revokes are ignored by the
  // resolver so the UI can grey out the revoke toggles.
  root_admin_immune_to_revokes?: boolean;
}

// PR-10 - per-server managed users (Plex / Emby / Jellyfin end users
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
  // 2026-05-15 share-state cross-reference. ``active_share`` is true
  // when Plex.tv's /api/servers/{mid}/shared_servers confirms the user
  // currently has an active share on this server; false when they did
  // before but no longer. ``is_pin_protected`` reflects Plex.tv's
  // /api/home/users ``protected`` flag. ``shared_state_refreshed_at``
  // is the unix timestamp of the last successful refresh (null when
  // the row hasn't been refreshed since the migration). UI consumers
  // grey out stale rows in User Management, hide them in the picker,
  // and surface a PIN badge when ``is_pin_protected`` is true.
  active_share: boolean;
  is_pin_protected: boolean;
  shared_state_refreshed_at: number | null;
  // 2026-05-15 follow-up #3 (migration v10). Canonical per-user
  // identifier - the Plex.tv numeric userID for Plex servers, the
  // equivalent backend-native id for Jellyfin / Emby once those
  // adapters land. Null on rows that pre-date v10 or that haven't
  // yet been resolved by the share-state refresh (which backfills
  // on first successful alias match). The matcher uses this as the
  // primary key for active_share decisions, making the gate immune
  // to display-name drift, Unicode quirks, and duplicate names.
  backend_user_id: string | null;
  // USER-MGMT-IDENTITY-AUDIT (migration v12). App-generated stable
  // user identifier in canonical form
  // ``<Service>-<HostNameSlug>-<server_uid>-<userkey>``. Used as
  // the cross-server validation handle in user_identity_map and
  // surfaced on the User Management detail view's identity-links
  // panel. Null on rows the boot-time backfill has not yet
  // processed - new rows added through the standard upsert path
  // always carry one.
  app_user_uuid: string | null;
}

export interface GlobalTombstone {
  username: string;
  tombstoned_at: number;
}


// ── Databases viewer (Plan[DATABASES-VIEWER]-2026-05-16) ─────────────────
// Mirrors server/db_browser.py's response shapes.

export type DatabaseCardinality = 'single' | 'many';
export type DatabaseSensitivity = 'high' | 'medium' | 'low';

export interface DatabaseTypeSummary {
  key: string;
  display_name: string;
  description: string;
  cardinality: DatabaseCardinality;
  sensitivity: DatabaseSensitivity;
}

export interface DatabaseInstance {
  instance_id: string;
  label: string;
  file_path: string;
  size_bytes: number;
  exists?: boolean;
  // snapshot_file-only fields (populated when the parent type is 'snapshot_file')
  server_id?: string | null;
  server_name?: string | null;
  service_type?: string | null;
  snapshot_name?: string | null;
  captured_at?: number | null;
}

export interface DatabaseInstanceMetadata {
  db_type: string;
  instance_id: string;
  file_path: string;
  size_bytes: number;
  modified_at: number;
  schema_version: number | null;
  wal_present: boolean;
}

export interface DatabaseColumn {
  name: string;
  type: string;
  notnull: boolean;
  default: string | null;
  is_primary_key: boolean;
}

export interface DatabaseIndex {
  name: string;
  unique: boolean;
  columns: string[];
}

export interface DatabaseTable {
  name: string;
  row_count: number;
  columns: DatabaseColumn[];
  indexes: DatabaseIndex[];
  ddl: string;
}

export interface DatabaseInstanceSchema {
  db_type: string;
  instance_id: string;
  tables: DatabaseTable[];
}

// Cell shape: the server pre-formats every value with security-aware
// substitutions. ``display`` is always present (the human-readable
// string). Optional fields:
//
//   * ``raw``           - the underlying value when safe to surface
//                         (e.g. full text behind a truncation, raw
//                         epoch behind an ISO timestamp display).
//   * ``raw_present``   - true when the underlying value is held
//                         back for security (encrypted columns,
//                         bcrypt hashes, redacted refresh tokens).
//                         The UI can offer no expand affordance for
//                         these; they are deliberately one-way.
//   * ``truncated``     - true for long TEXT clipped to the first
//                         200 chars; the UI offers an "expand"
//                         affordance to reveal ``raw``.
export interface DatabaseCell {
  display: string | number | boolean | null;
  raw?: string | number | boolean | null;
  raw_present?: boolean;
  truncated?: boolean;
}

export interface DatabaseTableRowsPage {
  db_type: string;
  instance_id: string;
  table: string;
  columns: string[];
  rows: DatabaseCell[][];
  total_rows: number;
  limit: number;
  offset: number;
  has_more: boolean;
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
    // ``detail`` is whatever FastAPI puts in the ``detail`` slot of
    // the JSON body. Some routes return a plain string, some return
    // a structured ``{code, message}`` object (developer's playlist-mgmt
    // routes do this for SMART_PLAYLIST_NOT_PORTABLE, PLAYLIST_NOT_FOUND,
    // DEST_USER_TOKEN_MISSING, etc.). We keep the raw value here so the
    // downstream Error message can format both shapes correctly. Without
    // this, structured detail ended up as ``[object Object]`` in the
    // thrown message because string-template coerces an object via
    // ``Object.prototype.toString``.
    let detail: string | Record<string, unknown> = '';
    try {
      const raw = await res.text();
      if (raw) {
        try {
          const body = JSON.parse(raw);
          if (body && typeof body === 'object' && 'detail' in body) {
            const inner = (body as { detail: unknown }).detail;
            if (typeof inner === 'string') {
              detail = inner;
            } else if (inner && typeof inner === 'object') {
              detail = inner as Record<string, unknown>;
            } else {
              detail = raw;
            }
          } else {
            detail = raw;
          }
        } catch {
          detail = raw;
        }
      }
    } catch {
      // Body couldn't even be read as text. Leave detail empty -
      // the status code + statusText below still tell the user
      // something useful.
    }
    // Phase 6: 403s carrying the elevation-required marker are
    // intercepted here. The handler opens the modal, captures the
    // password, calls /api/auth/elevate, and resolves true on
    // success. We then retry the original request once. A user
    // cancel (resolved false) falls through to the normal throw so
    // the caller sees a clean 403.
    //
    // The retry is gated on _isRetry so a misbehaving elevate flow
    // (where the second call still returns 403) cannot infinite-loop.
    // _isRetry is also already used by the 401-then-refresh path.
    if (
      res.status === 403
      && !_isRetry
      && _onElevationRequired
      && typeof detail === 'string'
      && detail.includes(ELEVATION_REQUIRED_DETAIL_MARKER)
    ) {
      try {
        const ok = await _onElevationRequired();
        if (ok) {
          return http<T>(path, init, true);
        }
      } catch {
        // Handler threw - fall through to the normal error throw
        // below so the caller sees the original 403 and the modal
        // closes via its own cancel path.
      }
    }
    // Format the message. Structured ``{code, message}`` detail (returned
    // by several /api/playlist-mgmt routes) is rendered as ``CODE: message``
    // so the end user-actionable string is readable in toasts and result
    // panels. When the detail carries both fields we additionally throw
    // a ``PlaylistMgmtStructuredError`` so callers can ``instanceof``-
    // dispatch on the code (e.g. handle DEST_USER_NOT_FOUND or
    // DEST_USER_TOKEN_MISSING specifically). Plain-text details fall
    // through to a regular ``Error`` so existing callers keep working.
    let detailText = '';
    let structuredCode: string | null = null;
    let structuredMessage: string | null = null;
    if (typeof detail === 'string') {
      detailText = detail;
    } else if (detail && typeof detail === 'object') {
      const code = typeof (detail as { code?: unknown }).code === 'string'
        ? (detail as { code: string }).code
        : null;
      const message = typeof (detail as { message?: unknown }).message === 'string'
        ? (detail as { message: string }).message
        : null;
      structuredCode = code;
      structuredMessage = message;
      if (code && message) detailText = `${code}: ${message}`;
      else if (message) detailText = message;
      else if (code) detailText = code;
      else {
        try { detailText = JSON.stringify(detail); }
        catch { detailText = '(unserializable detail)'; }
      }
    }
    if (structuredCode && structuredMessage) {
      throw new PlaylistMgmtStructuredError(
        res.status, structuredCode, structuredMessage,
      );
    }
    throw new Error(`${res.status} ${res.statusText}${detailText ? `: ${detailText}` : ''}`);
  }
  if (res.status === 204) return undefined as unknown as T;
  return (await res.json()) as T;
}

// ── Cross-platform preflight types (Phase C, 2026-05-16) ────────────────────
//
// Mirror of the Pydantic models in server/models.py:2260-2432
// (CrossPlatformPreflightReport + dependents). developer ships and
// owns the backend shapes; this block stays in sync.
//
// Contract sources:
//   * Plan[UI-FOR-PREFLIGHT]-2026-05-16.md (data shape spec)
//   * developer ACK on 2026-05-16 (PreflightResponse wrapper +
//     InlineCreateUserResponse refinements)

export type CppProposedResolution =
  | 'identity_map'
  | 'direct_match'
  | 'single_admin_fallback'
  | 'role_flip_ack'
  | 'tombstone_blocked'
  | 'zero_row_skip'
  | 'no_match'
  | 'multi_admin_collapse';

export type CppOverallVerdict = 'ok' | 'ack_required' | 'blocked';
export type CppSourceRole = 'owner' | 'admin' | 'managed';
export type CppDestRole = 'owner' | 'admin' | 'managed';
export type CppResolutionAction = 'map' | 'create' | 'drop' | 'accept_proposed';
export type CppResolutionsStatus = 'ok' | 'auto_fallback' | 'needs_review';

export interface CppUserRowCounts {
  watch_history: number;
  ratings: number;
  playlists: number;
  collections: number;
}

export interface CppDestUserOption {
  backend_user_id: string;
  username: string;
  role: CppDestRole;
  is_tombstoned: boolean;
}

export interface CppUserResolution {
  source_username: string;
  source_role: CppSourceRole;
  source_row_counts: CppUserRowCounts;
  proposed_resolution: CppProposedResolution;
  proposed_dest_user_id: string | null;
  proposed_dest_username: string | null;
  proposed_dest_role: CppDestRole | null;
  needs_ack: boolean;
  blocks_submit: boolean;
  warnings: string[];
  available_dest_users: CppDestUserOption[];
}

export interface CppLibraryTypeNote {
  source_library: string;
  source_type: string;
  dest_type_used: string;
  message: string;
}

export interface CppTombstoneNote {
  dest_username: string;
  reason: string;
}

export interface CppZeroRowSkip {
  source_username: string;
  empty_signals: string[];
  filter_flags_in_effect: string[];
  message: string;
}

export interface CrossPlatformPreflightReport {
  source_kind: string;
  dest_kind: string;
  source_server_id: string;
  dest_server_id: string;
  is_cross_platform: boolean;
  source_admin_count: number;
  dest_admin_count: number;
  resolutions: CppUserResolution[];
  smart_playlists_skipped: number;
  smart_playlist_names: string[];
  library_type_notes: CppLibraryTypeNote[];
  tombstoned_users_excluded: CppTombstoneNote[];
  zero_row_skipped: CppZeroRowSkip[];
  overall_verdict: CppOverallVerdict;
  blocking_reasons: string[];
}

// Uniform wrapper used by both preflight endpoints. Single-destination
// jobs return one entry in ``reports``; fan-out / schedule responses
// return N entries keyed by destination_server_id. ``aggregate_verdict``
// is the worst-of across reports - the Submit gate reads this one
// boolean.
export interface PreflightResponse {
  reports: Record<string, CrossPlatformPreflightReport>;
  aggregate_verdict: CppOverallVerdict;
}

export interface CppUserResolutionDecision {
  source_username: string;
  action: CppResolutionAction;
  // Populated when action='map'
  dest_user_id?: string;
  final_role?: 'admin' | 'managed';
  // Populated when action='create'
  create_username?: string;
  create_role?: 'admin' | 'managed';
  create_password?: string;
  admin_acknowledgement?: boolean;
}

export interface CrossPlatformPreflightAck {
  source_server_id: string;
  dest_server_id: string;
  resolutions: CppUserResolutionDecision[];
  apply_col_scope_prefix: boolean;
  persist_as_identity_map: boolean;
}

export interface InlineCreateUserBody {
  destination_server_id: string;
  username: string;
  role: 'admin' | 'managed';
  initial_password?: string;
  acknowledgement: boolean;
}

export interface InlineCreateUserResponse {
  user: CppDestUserOption;
  was_newly_created: boolean;
}

// PATCH /api/schedules/{id}/resolutions body shape.
export interface ScheduleResolutionsPatch {
  resolutions: Record<string, CrossPlatformPreflightAck>;
}

// ── Playlist Management types (Plan[PLAYLIST-MANAGEMENT] section 5) ─────────
//
// Scaffolded against the documented shapes per developer's
// coordination note (2026-05-16). developer's Pydantic models commit
// with the Playlist Mgmt foundation next session; when they land
// these types should match 1:1 with no structural drift.

export interface PlaylistSpec {
  playlist_id: string;
  name: string;
  item_count: number;
  is_smart: boolean;
}

export interface PlaylistItem {
  title: string;
  guids: string[];
  type: string; // 'movie' | 'episode' | 'audio' | ...
  duration_ms: number | null;
}

export interface PlaylistDetail {
  playlist_id: string;
  name: string;
  is_smart: boolean;
  items: PlaylistItem[];
  fetched_at: number; // epoch seconds
  from_cache: boolean;
}

export interface PlaylistCopyIn {
  source_server_id: string; // prefixed UID
  source_user_id: string;
  source_playlist_id: string;
  dest_server_id: string;
  dest_user_id: string;
  dest_playlist_name?: string;
}

export interface PlaylistCopyResult {
  success: boolean;
  new_playlist_id: string | null;
  items_written: number;
  items_skipped_no_match: number;
  items_failed: number;
  errors: string[];
  elapsed_seconds: number;
  // 2026-05-17 (same-user-skip): true when copy_playlist short-
  // circuited because source + dest resolved to the same logical
  // user. The "Written / Skipped / Failed" counter row is hidden
  // when true; ActiveDeploysPanel surfaces ``skip_reason`` instead.
  skipped?: boolean;
  skip_reason?: string | null;
}

export interface PlaylistCacheStatus {
  server_id: string;
  user_id: string;
  last_refreshed_at: number; // epoch seconds
  playlists_count: number;
  age_seconds: number;
  is_stale: boolean; // age > snapshot_threshold
}

// Returned by POST /api/playlist-mgmt/cache/refresh (per-user) and
// /api/playlist-mgmt/cache/refresh-server (per-server bulk). Bulk
// results surface `user_id = null` on the aggregate row.
// Reconciled against developer's Pydantic PlaylistCacheRefreshResult
// at server/models.py:2802 (2026-05-16).
export interface PlaylistCacheRefreshResult {
  server_id: string;
  user_id: string | null;
  refreshed_at: number;
  playlists_count: number;
  items_count: number;
  duration_ms: number;
  error: string | null;
}

// ── Playlist Mgmt endpoint response envelopes (developer shipped 2026-05-16) ──
//
// developer's shipped endpoints wrap the actual payloads in small
// envelopes carrying query parameters back to the caller. The
// frontend unwraps them at the API method boundary.

// User shape returned by /api/playlist-mgmt/users — uniform across
// all 3 backends. Different from the legacy `ServerUser` shape
// (which keys by plex_id / raw_name / kind).
export interface PlaylistMgmtUser {
  backend_user_id: string;
  username: string;
  role: 'owner' | 'admin' | 'managed';
  // 2026-05-16 (developer): true when service_type=plex AND a
  // managed_users row exists for (server_id, username) AND its
  // auth_token_enc is non-null. Always false on Jellyfin/Emby; the
  // per-user-token affordance only matters for Plex Home routes.
  has_token: boolean;
  // 2026-05-16 (developer schema-v12 follow-up): canonical app-generated
  // identifier from media_db.managed_users. Guaranteed unique per
  // (server, user) when present. May be null for live-only users that
  // haven't been written to managed_users yet; UI keying logic falls
  // back to backend_user_id / username in that case.
  app_user_uuid: string | null;
}

export interface PlaylistMgmtUsersResponse {
  server_id: string;
  service_type: 'plex' | 'jellyfin' | 'emby' | string;
  users: PlaylistMgmtUser[];
}

// 2026-05-17 (end user request): job-tracked deploy. Each playlist
// copy submitted via /api/playlist-mgmt/copy-job becomes a JobRecord
// in the existing queue; this is the end user-facing serialization
// returned by /api/playlist-mgmt/copy-jobs.
export interface PlaylistCopyJob {
  job_id: string;
  // Matches JobRecord.state — typically 'queued' | 'running' |
  // 'completed' | 'completed_with_errors' | 'failed' | 'cancelled'.
  state: string;
  queued_at: number;
  started_at: number | null;
  finished_at: number | null;
  // Non-null when the worker stamped an error on rec.error. Distinct
  // from the per-item misses inside result.errors — this is a
  // single-line summary surfaced when the job state is 'failed' or
  // 'completed_with_errors'.
  error: string | null;
  // The PlaylistCopyResult shape from copy_playlist. Null while the
  // job is queued; populated as soon as the worker stamps a summary.
  result: PlaylistCopyResult | null;
  // The original PlaylistCopyIn submission, surfaced so the Clone
  // Deploy button can re-submit without local memory.
  params: {
    source_server_id?: string;
    source_user_id?: string;
    source_playlist_id?: string;
    dest_server_id?: string;
    dest_user_id?: string;
    dest_playlist_name?: string | null;
  };
}

// Structured-error shape returned by /api/playlist-mgmt/copy on 4xx/5xx.
// Codes developer documents: SMART_PLAYLIST_NOT_PORTABLE (422),
// DEST_USER_TOKEN_MISSING (412), SOURCE_UNREACHABLE /
// DEST_UNREACHABLE (502), PLAYLIST_NOT_FOUND / DEST_USER_NOT_FOUND
// (404), DEST_WRITE_FAILED (502).
export class PlaylistMgmtStructuredError extends Error {
  status: number;
  code: string;
  constructor(status: number, code: string, message: string) {
    super(message);
    this.status = status;
    this.code = code;
    this.name = 'PlaylistMgmtStructuredError';
  }
}

export interface PlaylistMgmtPlaylistsResponse {
  server_id: string;
  user_id: string;
  from_cache: boolean;
  fetched_at: number;
  playlists: PlaylistSpec[];
}

export interface PlaylistMgmtCacheStatusResponse {
  server_id: string;
  rows: PlaylistCacheStatus[];
}

export interface PlaylistMgmtBulkRefreshResponse {
  server_id: string;
  // Per-user rows followed by an aggregate row (`user_id = null`).
  results: PlaylistCacheRefreshResult[];
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

  // Item 1: atomic two-step first-boot setup. Creates an admin account
  // AND a separate root_admin account in a single request; signs the
  // end user in as the admin. The frontend collects both credentials
  // in its wizard and submits them together so a half-complete setup
  // cannot leave the install with only one account.
  authSetupV2: (input: {
    admin_username: string;
    admin_password: string;
    admin_display_name?: string;
    root_username: string;
    root_password: string;
    root_display_name?: string;
  }) =>
    http<AuthSession>('/api/auth/setup-v2', {
      method: 'POST',
      body: JSON.stringify(input),
    }),

  // Item 1: forced legacy-install split. Caller is the legacy
  // root_admin; this creates a new dedicated root_admin and demotes
  // the caller to admin. Caller must re-prove their password.
  authUpgradeSplit: (input: {
    caller_password: string;
    root_username: string;
    root_password: string;
    root_display_name?: string;
  }) =>
    http<{ caller_demoted_to: string; new_root_username: string }>(
      '/api/auth/upgrade-split',
      { method: 'POST', body: JSON.stringify(input) },
    ),

  // Item 1: sudo-style elevation. Caller proves their own password and
  // the backend stamps an elevation flag on the JWT's session for the
  // configured TTL (default 10 min). Elevated sessions can hit
  // require_elevation-gated endpoints (root_admin destructive writes).
  authElevate: (password: string) =>
    http<{ elevated_until: number }>('/api/auth/elevate', {
      method: 'POST',
      body: JSON.stringify({ password }),
    }),

  authElevationStatus: () =>
    http<{ elevated: boolean; elevated_until: number | null; ttl_seconds: number }>(
      '/api/auth/elevation/status',
    ),

  authElevationClear: () =>
    http<{ ok: boolean }>('/api/auth/elevation/clear', { method: 'POST' }),

  authGrantRoot: (username: string) =>
    http<{ username: string; role: string; noop: boolean }>(
      `/api/auth/users/${encodeURIComponent(username)}/grant-root`,
      { method: 'POST' },
    ),

  authRevokeRoot: (username: string, newRole: string = 'admin') =>
    http<{ username: string; role: string }>(
      `/api/auth/users/${encodeURIComponent(username)}/revoke-root`,
      { method: 'POST', body: JSON.stringify({ new_role: newRole }) },
    ),

  // Item 3: cross-server PIN migration suggestions + apply.
  pinMigrationSuggestions: (serverId: string) =>
    http<{ suggestions: PinMigrationSuggestion[] }>(
      `/api/servers/${encodeURIComponent(serverId)}/pin-migration-suggestions`,
    ),
  pinMigrationApply: (
    serverId: string,
    suggestions: PinMigrationSuggestion[],
  ) =>
    http<{ applied: string[]; skipped: string[]; errors: string[] }>(
      `/api/servers/${encodeURIComponent(serverId)}/pin-migration/apply`,
      { method: 'POST', body: JSON.stringify({ suggestions }) },
    ),

  // Item 5: per-user manual token rotation. Bypasses the throttle
  // and the additive-only contract for one user. Requires sudo-style
  // elevation on the backend.
  rotateManagedUserToken: (serverId: string, username: string) =>
    http<{ captured: number; skipped_existing: number; throttled: boolean; errors: string[] }>(
      `/api/servers/${encodeURIComponent(serverId)}/managed-users/${encodeURIComponent(username)}/rotate-token`,
      { method: 'POST' },
    ),

  // 2026-05-16 (developer): persist a Plex Home user's X-Plex-Token so
  // the Playlist Management `per_user_token` auth mode can create
  // playlists under that user instead of the destination admin.
  // Body re-authenticates db_admin per call (same gate as the broader
  // managed-users PATCH). When clear_auth_token=true, auth_token is
  // ignored and the row's token is wiped. Plex-only endpoint; non-Plex
  // destinations 400 out.
  setPlexHomeToken: (
    serverId: string,
    username: string,
    body: {
      db_admin_username: string;
      db_admin_password: string;
      auth_token: string | null;
      clear_auth_token: boolean;
      display_name?: string | null;
    },
  ) =>
    http<ServerManagedUser>(
      `/api/managed-users/${encodeURIComponent(serverId)}/${encodeURIComponent(username)}/plex-home-token`,
      { method: 'POST', body: JSON.stringify(body) },
    ),

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
  // from the application-login admin so the end user can authorise
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
  // path that CREATES this row; these endpoints let the end user
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
  // the end user clicks Save.
  testUnsavedServer: (body: {
    name: string;
    url: string;
    token: string;
    // PR-Backends: when omitted, the backend defaults to 'plex'.
    // The Add Server form sets this explicitly based on the radio
    // picker so the probe goes through the right adapter.
    service_type?: 'plex' | 'jellyfin' | 'emby';
  }) =>
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
  // Retry the pending token stashed at Add-time (the end user's
  // original token that failed with 401 but was kept aside for later
  // retry once Plex.tv finished propagating the new server's
  // authorization). On success, the backend swaps the pending into
  // the active slot and clears every pending_* field. The response
  // carries the outcome so the panel can refresh in place.
  retryPendingToken: (id: string) =>
    http<{
      ok: boolean;
      swapped: boolean;
      status: string;
      detail: string;
    }>(`/api/servers/${encodeURIComponent(id)}/retry-pending-token`, {
      method: 'POST',
    }),
  pingServer: (id: string) =>
    http<PingResult>(`/api/servers/${encodeURIComponent(id)}/ping`, { method: 'POST' }),
  listServerLibraries: (id: string) =>
    http<LibraryDescriptor[]>(`/api/servers/${encodeURIComponent(id)}/libraries`),
  // v0.9.6 Feature 3: list users on a server (owner + managed). Live
  // call to systemAccounts() - one round-trip per visit, no caching.
  listServerUsers: (id: string) =>
    http<ServerUsersResponse>(`/api/servers/${encodeURIComponent(id)}/users`),
  // v0.14 - list users captured inside a snapshot .db. Reads the
  // snapshot_users table. Returns the same ServerUser shape so the
  // Restore form can intersect snapshot users with destination users
  // by ``plex_id`` (managed) and ``kind === "owner"`` (owner).
  listSnapshotUsers: (snapshotId: string) =>
    http<ServerUsersResponse>(`/api/snapshots/${encodeURIComponent(snapshotId)}/users`),
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

  // Plan[ETA-TRAINING] PR-D: adaptive ETA prediction. The job-form
  // preview hits this on every change (debounced) so the end user
  // sees a learned "~12m (typically 8-15m)" estimate before submit.
  predictEta: (body: EtaPredictRequest) =>
    http<EtaPrediction>('/api/eta/predict', {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  // Snapshot of the trainer's bucket store. Run Job form's 'Training
  // progress' disclosure consumes this so the end user can confirm
  // each bucket is accumulating samples after every job.
  etaTrainingStatus: (server_id?: string) => {
    const qs = server_id ? `?server_id=${encodeURIComponent(server_id)}` : '';
    return http<EtaTrainingStatus>(`/api/eta/training-status${qs}`);
  },

  // Settings Run History database export / import. Per-table JSON
  // dumps for backup + interop; replace-only import behind typed
  // REPLACE confirmation. Per-server pivot for inspecting one
  // server's identity-related rows in isolation.
  listDatabaseTables: () =>
    http<{ tables: Array<{
      table_id: string;
      db_file: string;
      table_name: string;
      row_count: number | null;
      available: boolean;
      per_server: boolean;
    }> }>('/api/database/tables'),

  exportDatabaseTable: (table_id: string) =>
    http<Record<string, unknown>>(
      `/api/database/export/${encodeURIComponent(table_id)}`,
    ),

  exportDatabaseArchive: () =>
    http<Record<string, unknown>>('/api/database/export-all'),

  importDatabaseTable: (table_id: string, payload: Record<string, unknown>) =>
    http<{ table_id: string; deleted: number; inserted: number }>(
      `/api/database/import/${encodeURIComponent(table_id)}`,
      {
        method: 'POST',
        body: JSON.stringify({ confirm: 'REPLACE', payload }),
      },
    ),

  importDatabaseArchive: (payload: Record<string, unknown>) =>
    http<{
      per_table: Record<string, { deleted?: number; inserted?: number; error?: string }>;
      total_deleted: number;
      total_inserted: number;
      errors: string[];
    }>('/api/database/import-archive', {
      method: 'POST',
      body: JSON.stringify({ confirm: 'REPLACE', payload }),
    }),

  listDatabasePerServer: (server_id: string) =>
    http<{ server_id: string; tables: Array<{
      table_id: string;
      db_file: string;
      table_name: string;
      row_count: number;
    }> }>(`/api/database/per-server/${encodeURIComponent(server_id)}`),

  exportDatabasePerServer: (server_id: string) =>
    http<Record<string, unknown>>(
      `/api/database/export-per-server/${encodeURIComponent(server_id)}`,
    ),

  // Settings Run History "Repair legacy training data" button. Backfills
  // missing server_id on run_timings entries by joining run_history.
  // End user follows this with backfillEtaWeights(true) to rebuild
  // the bucket store from the now-complete history.
  etaRepairServerIds: () =>
    http<{ updated: number; still_orphan: number }>('/api/eta/repair-server-ids', {
      method: 'POST',
    }),

  // Settings ETA Training "Flush all training data" button. Admin-
  // only; behind a typed FLUSH confirmation.
  etaFlushAll: (include_run_timings: boolean) =>
    http<{
      in_memory_buckets_cleared: number;
      eta_buckets_rows_deleted: number;
      run_timings_rows_deleted: number;
    }>('/api/eta/flush', {
      method: 'POST',
      body: JSON.stringify({ include_run_timings, confirm: 'FLUSH' }),
    }),

  // Plan[ETA-TRAINING] D-RESET: end user's "I just upgraded this
  // server's storage; the learned timings are wrong" escape hatch.
  // Behind a typed RESET confirmation in the server panel.
  resetEtaWeights: (server_id: string) =>
    http<{ server_id: string; removed: number }>('/api/eta/reset', {
      method: 'POST',
      body: JSON.stringify({ server_id, confirm: 'RESET' }),
    }),

  // Gap-A of the post-cutover review: warm-start the ETA engine
  // from the historical run_timings table. Admin-only; safe to
  // re-run (additive unless reset_first is true).
  backfillEtaWeights: (reset_first: boolean = false) =>
    http<{ entries_read: number; buckets_touched: number }>(
      '/api/eta/backfill',
      { method: 'POST', body: JSON.stringify({ reset_first }) },
    ),

  // Plan[RUN-JOB-UI] follow-up: ad-hoc single-user copy +
  // cross-server identity mapping.
  copyUserToDestination: (body: {
    source_server_id: string;
    target_server_id: string;
    source_user_handle: string;
    target_username: string;
    temp_password: string;
    target_user_policy?: Record<string, unknown> | null;
  }) =>
    http<{
      backend_user_id: string;
      target_username: string;
      source_user_handle: string;
    }>('/api/users/copy_to_destination', {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  addUserIdentityMap: (body: {
    server_a_id: string;
    user_a_handle: string;
    server_b_id: string;
    user_b_handle: string;
    source?: 'manual' | 'auto_copy';
  }) =>
    http<{ id: number | null; duplicate: boolean }>(
      '/api/users/identity_map',
      { method: 'POST', body: JSON.stringify({ source: 'manual', ...body }) },
    ),

  listUserIdentityMaps: () =>
    http<{ maps: UserIdentityMap[] }>('/api/users/identity_map'),

  deleteUserIdentityMap: (map_id: number) =>
    http<{ removed: boolean; id: number }>(
      `/api/users/identity_map/${map_id}`,
      { method: 'DELETE' },
    ),

  // USER-MGMT-IDENTITY-AUDIT follow-on: end user-triggered rerun of
  // the backend_user_id auto-link helper. The helper also fires
  // automatically after every managed-users sync; this button is
  // for end users who just added a manual mapping or registered a
  // new server and want immediate cross-server detection without
  // waiting for the next sync.
  rerunAutoLinkIdentityMap: () =>
    http<{ pairs_written: number; pairs_skipped_duplicate: number; groups_seen: number }>(
      '/api/users/identity_map/rerun-auto-link',
      { method: 'POST' },
    ),

  // ── Databases viewer (Plan[DATABASES-VIEWER]-2026-05-16) ──────────
  //
  // Root-admin only at the route layer. The frontend mirrors the
  // server's read-only contract: no edit endpoints exist and none
  // are exposed here. Cell values are pre-formatted server-side so
  // the UI just renders ``display``.

  dbBrowserListDatabases: () =>
    http<{ databases: DatabaseTypeSummary[] }>('/api/db-browser/databases'),

  dbBrowserListInstances: (db_type: string) =>
    http<{ instances: DatabaseInstance[] }>(
      `/api/db-browser/${encodeURIComponent(db_type)}/instances`,
    ),

  dbBrowserMetadata: (db_type: string, instance_id: string) =>
    http<DatabaseInstanceMetadata>(
      `/api/db-browser/${encodeURIComponent(db_type)}/${encodeURIComponent(instance_id)}/metadata`,
    ),

  dbBrowserSchema: (db_type: string, instance_id: string) =>
    http<DatabaseInstanceSchema>(
      `/api/db-browser/${encodeURIComponent(db_type)}/${encodeURIComponent(instance_id)}/schema`,
    ),

  dbBrowserRows: (
    db_type: string,
    instance_id: string,
    table: string,
    opts: {
      limit?: number;
      offset?: number;
      filter_column?: string;
      filter_value?: string;
    } = {},
  ) => {
    const params = new URLSearchParams();
    if (opts.limit !== undefined) params.set('limit', String(opts.limit));
    if (opts.offset !== undefined) params.set('offset', String(opts.offset));
    if (opts.filter_column) params.set('filter_column', opts.filter_column);
    if (opts.filter_value !== undefined) params.set('filter_value', opts.filter_value);
    const qs = params.toString();
    return http<DatabaseTableRowsPage>(
      `/api/db-browser/${encodeURIComponent(db_type)}/${encodeURIComponent(instance_id)}/tables/${encodeURIComponent(table)}/rows${qs ? `?${qs}` : ''}`,
    );
  },
  // PR-12 preflight. Returns the at-risk managed-user list (empty
  // when the modal should be skipped: restore mode, or every user is
  // already credentialed). The frontend renders the warning modal
  // when ``checked && at_risk_users.length > 0`` and stamps
  // ``pin_preflight_acknowledged: true`` on the next submit when the
  // end user clicks Continue anyway.
  preflightPinCheck: (params: {
    mode: 'snapshot' | 'restore' | 'direct';
    source_server_name?: string | null;
    dest_server_names?: string[] | null;
    user_filter?: string[] | null;
  }) =>
    http<{
      mode: string;
      checked: boolean;
      at_risk_users: string[];
      servers_checked: string[];
    }>('/api/job/preflight-pin-check', {
      method: 'POST',
      body: JSON.stringify(params),
    }),
  stopJob: (hard: boolean = false) =>
    http<{ stop_requested: boolean; hard: boolean }>(
      `/api/job/stop${hard ? '?hard=true' : ''}`,
      { method: 'POST' },
    ),

  // ── Cross-platform preflight (developer backend, Phase C) ─────────────
  // Body shape matches /api/job submit for jobs preflight, and the
  // schedule create/edit body for schedule preflight. Backend
  // computes a CrossPlatformPreflightReport per destination and
  // wraps them in PreflightResponse with an aggregate verdict.
  jobsCrossPlatformPreflight: (params: Record<string, unknown>) =>
    http<PreflightResponse>('/api/jobs/cross-platform-preflight', {
      method: 'POST',
      body: JSON.stringify(params),
    }),
  schedulesCrossPlatformPreflight: (params: Record<string, unknown>) =>
    http<PreflightResponse>('/api/schedules/cross-platform-preflight', {
      method: 'POST',
      body: JSON.stringify(params),
    }),
  jobsInlineCreateUser: (body: InlineCreateUserBody) =>
    http<InlineCreateUserResponse>('/api/jobs/inline-create-user', {
      method: 'POST',
      body: JSON.stringify(body),
    }),
  schedulesUpdateResolutions: (id: string, body: ScheduleResolutionsPatch) =>
    http<Schedule>(`/api/schedules/${encodeURIComponent(id)}/resolutions`, {
      method: 'PATCH',
      body: JSON.stringify(body),
    }),

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

  // Application-level logs (Settings > Application Logs). Admin-gated.
  //
  // Categories: 'db-access' (db_access.log) is the only category
  // with a real backing log file today; 'auth', 'network', 'debug'
  // are scaffolded placeholders.
  //
  // ``tailBytes`` defaults to 0, which the backend resolves to the
  // end user-tuned log_read_max_bytes (default 16 MB). Pass an
  // explicit non-zero value to read less. ``since`` selects polling
  // mode (since=0 returns the tail; since=<offset> returns bytes
  // from offset to current end). ``backup`` switches the read
  // target from the active file to a specific rotated backup
  // (e.g. "db_access.log.1"); when set, the endpoint ignores
  // ``since`` and always returns the tail of that backup.
  //
  // Response distinguishes two truncation cases:
  //
  //   * head_omitted: initial-tail of a file larger than the read
  //     cap. Older bytes are still in the same file.
  //   * rotated_during_poll: the polling cursor landed past the
  //     file's current end. Logrotate fired; older bytes are now
  //     in the .1 (or higher) backup, listed in ``backups``.
  readAppLog: (
    category: string,
    tailBytes = 0,
    since = 0,
    backup = '',
  ) =>
    http<{
      category: string;
      path: string;
      size_bytes: number;
      next_offset: number;
      head_omitted: boolean;
      rotated_during_poll: boolean;
      content: string;
      backups: Array<{ filename: string; size_bytes: number }>;
      note: string;
    }>(
      `/api/logs/app/${encodeURIComponent(category)}`
        + `?tail_bytes=${encodeURIComponent(tailBytes)}`
        + `&since=${encodeURIComponent(since)}`
        + (backup ? `&backup=${encodeURIComponent(backup)}` : ''),
    ),
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

  // ── Playlist Management endpoints (developer shipped 2026-05-16) ──
  // All endpoints accept query params for server_id + user_id +
  // (optional) force_refresh; responses are wrapped in small
  // envelopes that re-surface the query parameters. The methods
  // below unwrap to the inner payload where the end user-visible
  // data lives.
  playlistMgmtListUsers: (serverId: string, forceRefresh = false) =>
    http<PlaylistMgmtUsersResponse>(
      `/api/playlist-mgmt/users?server_id=${encodeURIComponent(serverId)}${forceRefresh ? '&force_refresh=true' : ''}`,
    ),
  playlistMgmtListPlaylists: (serverId: string, userId: string, forceRefresh = false) =>
    http<PlaylistMgmtPlaylistsResponse>(
      `/api/playlist-mgmt/playlists?server_id=${encodeURIComponent(serverId)}&user_id=${encodeURIComponent(userId)}${forceRefresh ? '&force_refresh=true' : ''}`,
    ),
  playlistMgmtGetDetail: (serverId: string, userId: string, playlistId: string, forceRefresh = false) =>
    http<PlaylistDetail>(
      `/api/playlist-mgmt/playlist-detail?server_id=${encodeURIComponent(serverId)}&user_id=${encodeURIComponent(userId)}&playlist_id=${encodeURIComponent(playlistId)}${forceRefresh ? '&force_refresh=true' : ''}`,
    ),
  // Synchronous copy path. Kept for scripted callers / back-compat.
  // The UI now uses the async copy-job pair below so deploys persist
  // beyond page reload and stack on the active-deploys panel.
  playlistMgmtCopy: (body: PlaylistCopyIn) =>
    http<PlaylistCopyResult>('/api/playlist-mgmt/copy', {
      method: 'POST',
      body: JSON.stringify(body),
    }),
  // 2026-05-17 (end user request): async path. Submits the playlist
  // copy as a job in the existing queue and returns the job_id
  // immediately. The active-deploys panel then polls
  // /api/playlist-mgmt/copy-jobs for state + result.
  submitPlaylistCopyJob: (body: PlaylistCopyIn) =>
    http<{ job_id: string; state: string }>(
      '/api/playlist-mgmt/copy-job',
      { method: 'POST', body: JSON.stringify(body) },
    ),
  listPlaylistCopyJobs: () =>
    http<{ jobs: PlaylistCopyJob[] }>(
      '/api/playlist-mgmt/copy-jobs',
    ),
  playlistMgmtRefreshCache: (serverId: string, userId: string) =>
    http<PlaylistCacheRefreshResult>('/api/playlist-mgmt/cache/refresh', {
      method: 'POST',
      body: JSON.stringify({ server_id: serverId, user_id: userId }),
    }),
  // Per-server bulk refresh (locked answer #4). Body uses
  // `user_id: null` to signal bulk. Response is a list of per-user
  // PlaylistCacheRefreshResult rows + a final aggregate row with
  // `user_id = null`.
  playlistMgmtRefreshServer: (serverId: string, source: string = 'api') =>
    http<PlaylistMgmtBulkRefreshResponse>(
      `/api/playlist-mgmt/cache/refresh-server?source=${encodeURIComponent(source)}`,
      {
        method: 'POST',
        body: JSON.stringify({ server_id: serverId, user_id: null }),
      },
    ),
  playlistMgmtCacheStatus: (serverId: string) =>
    http<PlaylistMgmtCacheStatusResponse>(
      `/api/playlist-mgmt/cache/status?server_id=${encodeURIComponent(serverId)}`,
    ),

  // Engine + wire-protocol version, surfaced for the Help panel's
  // Troubleshooting page so bug reports auto-stamp the right build.
  getHealth: () =>
    http<{
      ok: boolean;
      api_version: string;
      app_version: string;
      debug_mode?: boolean;
    }>('/api/health'),


  // v0.12.0: media-state database health. Returns schema version,
  // per-table row counts, on-disk size, last-updated timestamps.
  // Used by diagnostic tools and the upcoming Database tab.
  getDbStats: () => http<DbStats>('/api/db/stats'),

  // Feature 1 phase 1.5: runtime breakdown.
  // ``limit`` is clamped server-side to [1, 200]; the panel default
  // (25) matches the backend's default so a missing query string
  // returns the same shape either way.
  listRuntimeRuns: (limit = 25) =>
    http<{ runs: RuntimeRunSummary[] }>(
      `/api/run-timings/runs?limit=${encodeURIComponent(limit)}`,
    ),

  // Drill into one run. Returns ``{run_id, entries: []}`` for an
  // unknown run_id (404 would force two empty-state branches in the
  // panel for no actual gain).
  getRuntimeRunDetail: (runId: string) =>
    http<{ run_id: string; entries: RuntimeEntry[] }>(
      `/api/run-timings/runs/${encodeURIComponent(runId)}`,
    ),

  // Phase 4 of the dashboard / log reorg: list per-run history rows.
  // Backend clamps the limit to [1, 500]; default 200 matches the
  // server-side default. Optional filters narrow to one server or
  // one library.
  listRecentRunHistory: (opts: {
    limit?: number;
    serverId?: string;
    library?: string;
  } = {}) => {
    const params = new URLSearchParams();
    if (opts.limit !== undefined) params.set('limit', String(opts.limit));
    if (opts.serverId) params.set('server_id', opts.serverId);
    if (opts.library) params.set('library', opts.library);
    const q = params.toString();
    return http<{ runs: RecentRunRow[] }>(
      `/api/runs/recent${q ? `?${q}` : ''}`,
    );
  },

  // Drill into one run history row by id. 404 when absent.
  getRecentRunDetail: (runId: string) =>
    http<RecentRunRow>(`/api/runs/recent/${encodeURIComponent(runId)}`),

  // Feature 3: developer tool unit test runner. Both endpoints
  // return 403 when PLEXMIGRATE_DEBUG_MODE is unset on the backend.
  // The Developer tab gates its render on the debug_mode field of
  // /api/health; these calls only fire from inside the tab so a 403
  // here indicates a server-side env-var change between page load
  // and submit.
  runDevTests: (body: {
    mode: 'synthetic' | 'structural' | 'live';
    pytest_filter?: string | null;
    confirm_live?: boolean;
  }) =>
    http<DevTestRunSummary>('/api/dev/run-tests', {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  listDevTestRuns: (limit = 25) =>
    http<{ runs: DevTestRunSummary[] }>(
      `/api/dev/test-runs?limit=${encodeURIComponent(limit)}`,
    ),

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

  // Reassign orphan snapshot rows into a target server. Admin-gated;
  // non-destructive (rewrites server_id only, never deletes files).
  // The Exports panel already merges orphan rows at display time, so
  // end users rarely need this; it exists for end users who want the
  // underlying registry to be clean rather than display-side-clean.
  mergeOrphanSnapshots: (serverId: string) =>
    http<{ checked: number; reassigned: number; no_op: number }>(
      `/api/snapshots/server/${encodeURIComponent(serverId)}/merge-orphans`,
      { method: 'POST', body: JSON.stringify({}) },
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
  // under Servers). Reads are JWT-only (end user+); writes carry
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
      // bypassing the normal linear backoff so the end user sees
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
