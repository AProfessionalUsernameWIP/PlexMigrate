// Type definitions extracted from the legacy ``frontend/src/api.ts``
// during Phase 2f of the decomposition. Pure interface / type / class
// declarations only - no runtime helpers, no fetch calls, no module
// state. The ``PlaylistMgmtStructuredError`` class lives here because
// it is a type-shape class (constructor + fields + name); its
// behaviour is purely value-bearing.
//
// Every legacy import ``from '../api'`` for any of these names keeps
// resolving via the re-exports kept in ``frontend/src/api.ts``.
//
// Mirrors the snapshot shape DashboardState produces in Python
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
  // Run coverage - lets the dashboard show how many
  // users / watched items / playlists / collections / ratings the
  // current run is operating on. Optional for forward-compat with an
  // older backend that hasn't been upgraded yet.
  home_user_count?: number;
  watch_count?: number;
  playlist_count?: number;
  collection_count?: number;
  rating_count?: number;
  // Per-worker "currently processing" rows. Each entry is one
  // active worker thread's in-flight item. Optional for forward-compat.
  current_items?: CurrentItem[];
  threads: Record<string, string>;
  paused: boolean;
  start_time: number;
  log_dir: string;
  now: number;
  // Raw identifier of the user whose data is being
  // processed right now (owner email or managed username). Null when
  // the run has no per-user phase active. Frontend resolves through
  // ``user_display_names`` before rendering.
  current_user?: string | null;
  // Source server's display-name map, copied at
  // run start. Keys: raw Plex identifier. Values: end user-chosen
  // display name. Empty / missing = render raw identifiers as-is.
  user_display_names?: Record<string, string>;
  // HTTP telemetry - status code histogram and
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
  // Per-tier resolver match counts for the active run. Keys are
  // tier names ("guid_db", "guid_api", "filepath", "suffix", "fuzzy").
  // Surfaced live so the Dashboard can warn when Tier 3 (fuzzy) fires
  // during a direct-transfer run.
  tier_counts?: Record<string, number>;
  // Per-container restoration summary built during import.
  // Each playlist / collection contributes one entry once its merge
  // step finishes. Empty on snapshot / direct-transfer runs.
  container_summary?: {
    playlists?: ContainerResult[];
    collections?: ContainerResult[];
  };
  // Per-batch progress for the Process List panel. Each key is a
  // batch label ("watch", "rating", "playlist", "collection") and
  // each value is a {total, completed} pair; ``total`` grows as the
  // engine discovers real work, empty until the first batch tick.
  batch_etrs?: Record<string, {
    total: number;
    completed: number;
  }>;
  // Part B: run-level finalize phase. Set by the job runner during
  // the post-engine close-out (close logs / run-dir finalize /
  // snapshot-DB capture) - the window where every library row already
  // reads "Done" but the job hasn't flipped to COMPLETED. ``null`` /
  // absent = not finalizing; a string is the current sub-step, shown
  // as "Finalizing - <label>".
  finalizing?: string | null;
}

// One restoration row per playlist / collection in an import.
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
  // Short verb describing what this thread is doing on the
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
  // Empty string = unscoped (always shown). A
  // non-empty value tags this entry to a specific server; the WS
  // payload filters these out for non-participants during an active
  // job so the end user only sees the source / destination(s)
  // they're actually running against.
  server_name?: string;
}

export interface JobPayload {
  job_id: string;
  // 'playlist_copy' supports the Playlist Mgmt Deploy refactor (each
  // Deploy becomes a JobRecord on the same queue). The server-side
  // JobRecord uses the same literal strings.
  mode: 'snapshot' | 'restore' | 'direct' | 'playlist_copy';
  // 'stopping' is the intermediate state between user click
  // and engine return - see server/jobs.py :: STATE_STOPPING.
  // 'completed_with_errors' is the partial-success outcome:
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
  // Captured at job completion in the worker-loop finally
  // block. ``tier_counts`` mirrors the live DashboardState field so the
  // Job History view can show post-hoc which tiers matched.
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

// One card per destination in a fan-out job. ``dashboard``
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

// Per-server HTTP telemetry, always present, populated by
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
  // Every active + queued job. The Dashboard tab grows a
  // sub-tab strip when this has more than one entry, one tab per
  // job. Optional for backward compat with older backends.
  jobs?: JobPayload[];
  // Present when a fan-out job is in flight with more than
  // one destination. ``null`` for single-destination jobs so the
  // existing single-card render path is fully unchanged.
  fan_out: FanOutDestState[] | null;
  // Per-server HTTP telemetry, always present.
  servers_network: ServerNetworkState[];
}

// Cross-server identity-map row shape.
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
  // Snapshot retention. ``global`` is the ceiling; the per-
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
  // ETR colour multiplier for the dashboard stall thresholds.
  // Clamped to [0.5, 2.0] at the read boundary. Default 1.0.
  etr_color_multiplier?: number;
  // Sudo-style elevation cache TTL in seconds. Floored 60,
  // ceiled 3600 by the backend. Default 600 (10 minutes).
  elevation_ttl_seconds?: number;
  // Global tooltip toggle. When false the InfoTip component
  // renders no popover affordance; the full body lives in the Help
  // tab only. Default true; root_admin to write.
  tooltips_enabled?: boolean;
  // Opt-in token rotation on Refresh.
  auto_rotate_tokens_on_refresh?: boolean;
  // Opt-in username-string fallback for PIN migration.
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
  // Library-walk job. ``enabled`` toggles the background
  // scheduler; ``interval_seconds`` is the cadence (floored at 3600
  // on the backend); ``stale_threshold_days`` is the default value
  // the Prune Missing Items slider opens at.
  library_walk?: {
    enabled?: boolean;
    interval_seconds?: number;
    stale_threshold_days?: number;
  };
  // Restore-side run defaults (Merge / Replace). Per-run +
  // per-server overrides take precedence; this is the bottom-of-chain
  // fallback. Defaults: {"mode": "merge", "auto_capture_before_replace": true}.
  restore_defaults?: {
    mode?: 'merge' | 'replace';
    auto_capture_before_replace?: boolean;
    // Merge sub-strategy for watch-count math. "higher"
    // (default) = destination ends at max(stored, current); "sum" =
    // current + stored. End user opt-in. Ignored when mode=='replace'.
    merge_watch_strategy?: 'higher' | 'sum';
  };
  // Library-level concurrency cap for file-mediated restore.
  // Default 3; end users that see Plex 429s during
  // multi-library restores lower this (1 = serial). Direct transfer
  // is serial - this knob does not apply there.
  restore_library_workers?: number;
  // Per-fan-out destination concurrency cap. 0 (default)
  // means no cap - one thread per destination. A
  // positive integer caps the destination pool. Independent of
  // restore_library_workers (the two axes are orthogonal).
  fan_out_destination_workers?: number;
  // Library-level concurrency cap for snapshot. 0 (default)
  // inherits ``workers`` (coupled behavior); a positive value caps
  // libraries-in-parallel without
  // affecting the per-library HTTP worker count.
  snapshot_library_workers?: number;
}

// Walk + prune types.
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

// Per-libtype leaf counts. Named keys (not a loose
// Record<string, number>) so a backend key rename surfaces as a
// type error instead of silently rendering nothing. Each leaf is
// optional - an older Refresh that pre-dates the expansion only
// populates the original two keys; the UI omits missing entries.
// Keys vary by libtype:
//   - show:    { seasons?, episodes? }
//   - artist:  { albums?, tracks? }
//   - movie:   { collections? }
// ``playlists`` (any libtype) is surfaced when the playlist cache
// has been warmed (playlist_cache.db's primary_library_id index);
// visible only on the /api/library-mapping/sides response.
export interface LeafCounts {
  episodes?: number;
  tracks?: number;
  seasons?: number;
  albums?: number;
  collections?: number;
  playlists?: number;
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
  // Optional because older registry rows don't have it; the timing
  // estimator treats absence as
  // "fall back to the top-level count, mark accuracy degraded."
  leaf_counts?: LeafCounts;
}

// Multi-server registry types.
// ``last_response_ms`` backs the live ping indicator.
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
  // Backend discriminator. Mirror of the persisted
  // ``service_type`` column. Drives:
  //  - which authentication label the new-server form shows
  //    ("Plex Token" vs "API Key" vs ...)
  //  - which adapter the engine uses (Plex via plexapi, Jellyfin / Emby
  //    via the HTTP adapter)
  //  - which icon / badge the Servers panel renders alongside the row
  // Rows that pre-date the picker default to 'plex'.
  service_type?: 'plex' | 'jellyfin' | 'emby';
  has_token: boolean;
  // Pending-token state. When the end user's typed
  // token failed at Add-server time and a borrowed token was used
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
  // End user-chosen friendly names for users on
  // this server. Keys: raw Plex identifier (owner email or managed
  // username). Empty / missing = no custom names assigned. The
  // Servers tab uses this to populate the inline owner-display-name
  // input and to render display names alongside managed users.
  user_display_names?: Record<string, string>;
  // Identity captured from the Plex server itself. Used by
  // the Servers tab to surface "this row is pointing at server X" and
  // by the backend to reject re-registering the same physical Plex.
  // Empty string when the row pre-dates this field and hasn't been
  // refreshed yet.
  machine_identifier?: string;
  friendly_name?: string;
  // Plex Media Server software version (e.g. "1.32.5.7349").
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
  // Present only on responses from
  // POST /api/servers/{id}/test (the Refresh button). Reports what
  // the additive token-capture sweep did. Absent on other endpoints
  // that return ServerView.
  token_capture?: TokenCaptureSummary;
  // Per-server opt-in
  // for the auto-tombstone sweeper. Default false on every existing
  // row (the registry's flat-dict pattern means missing keys read
  // as the _DEFAULT_SERVER value). Read-write via
  // PATCH /api/servers/{id}/auto-tombstone.
  auto_tombstone_inactive_users_enabled?: boolean;
  // Per-server cursor written by the sweeper after each cycle.
  // Drives the "Last sweep: …" status indicator in the User
  // Mapping panel + Servers > Logs view.
  user_activity_last_sweep_at?: number | null;
  user_activity_last_sweep_summary?: {
    started_at?: number;
    finished_at?: number;
    probed?: number;
    tombstoned?: number;
    errors?: number;
  } | null;
}

export interface TokenCaptureSummary {
  captured: number;
  skipped_existing: number;
  throttled: boolean;
  errors: string[];
}

// One row in the cross-server PIN migration suggestion list.
export interface PinMigrationSuggestion {
  target_username: string;
  source_server_id: string;
  source_server_name: string;
  source_username: string;
  match_kind: 'machine_id' | 'username';
}

// Auto-fallback offer: when the end user's typed token
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

// Result of probing a URL+token without registering it.
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

// One row in the per-server Users panel.
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

// Response shape of POST /api/servers/{id}/ping.
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
  // Backend discriminator. Omitted on legacy callers ->
  // backend defaults to 'plex'. The new-server form sets this
  // explicitly based on the radio picker so the registry knows which
  // adapter to wrap the connection with.
  service_type?: 'plex' | 'jellyfin' | 'emby';
  // Auto-fallback save. When set, the backend registers
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

// Returned by ``DELETE /api/servers/{id}`` after a cascading
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
  // Id-keyed source / destinations.
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
  // Four-flag data-type filter on schedules. All
  // four default true server-side; older schedules saved before this
  // field landed read as undefined here and the UI defaults them on.
  include_watch_history?: boolean;
  include_ratings?: boolean;
  include_playlists?: boolean;
  include_collections?: boolean;
  prebuild_json_sidecar?: boolean;
  // Per-Run Settings on schedules. Mirrors the same per-job
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
  // Per-schedule user filter. List of Plex identifiers (owner
  // email + managed usernames). null / undefined = capture every user
  // the source server reports.
  user_filter?: string[] | null;
  // Schedule mode + restore/direct fields. Older schedules read as
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
  // Per-library
  // metric filter. Authoritative when set; the engine consults this
  // per library. Forward-typed via the shared LibraryMetricsMap.
  // ``null`` (or undefined) on legacy schedule rows; the backend
  // ScheduleIn validator expands global include_* flags into this
  // map at save time so post-save schedules always have a populated
  // map matching their library list.
  library_metrics?: Record<string, { watch_history: boolean; ratings: boolean; playlists: boolean; collections: boolean }> | null;
  // Alignment additions that bring the schedule row up to Run Job
  // parity so the editor can mirror the Run Job form 1:1.
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
  // Per-run overrides for
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
  // Per-destination cross-platform preflight resolutions
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
  // Byte offset to pass back as ?since= on the next poll.
  // Always equal to the file's current size after a successful read.
  next_offset: number;
}

// Media-state database health snapshot returned by
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

// Developer-tool unit test runner.
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

// Runtime breakdown panel.
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

// Per-RUN row in the
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
// ``snapshots/legacy/`` by the snapshot-registry migration. Read-only
// browse + download + db_admin-gated delete.
export interface ExportArchive {
  name: string;
  size: number;
  mtime: number;
  library: string | null;
  captured_at: string | null;
  // Friendly name + URL of the server that produced this
  // export. ``null`` for exports created before this field was added.
  source_server?: string | null;
  source_server_url?: string | null;
  // How the run was initiated. "manual" for a GUI submission,
  // "schedule" for a scheduler fire (``schedule_name`` carries the
  // schedule's display name). ``null`` on exports captured before
  // these fields existed.
  trigger?: string | null;
  schedule_name?: string | null;
}

// Registry row backing one captured snapshot. The Exports
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
  // ``user_count`` is the full roster captured
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
  // A short
  // human-readable one-line summary stamped at capture time. Lists
  // the server, user count, library set, and metric set. Null on
  // older rows (the Exports panel renders an em-dash placeholder
  // for null).
  description: string | null;
}

// Per-library metric
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

// ── Auth types ──────────────────────────────────────────────────────────────

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
  // Server-side View Mode: real_role is the DB row's role,
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

// Per-server managed users (Plex / Emby / Jellyfin end users
// known to a registered server). Distinct from ``ManagedUser`` above,
// which is the app-login user table. The API never returns plaintext
// credentials; ``has_token`` / ``has_pin`` / ``has_password`` flags
// tell the UI whether something is stored, and the User Management
// panel renders write-only inputs ("- - - - - -" when stored).
export type ManagedUserService = 'plex' | 'emby' | 'jellyfin';
export type ManagedUserKind = 'owner' | 'managed';
// Tombstone scope. 'none' = visible; 'server' = hidden on
// this server only; 'global' = hidden on every server (username-keyed
// global tombstone). The User Management UI surfaces all three.
export type ManagedUserHiddenScope = 'none' | 'server' | 'global';

export interface ServerManagedUser {
  id: number;
  server_id: string;
  username: string;
  display_name: string | null;
  service_type: ManagedUserService;
  // Owner vs. managed distinction. Owner rows are the
  // server's Plex account holder; managed rows are everyone else.
  // JobFormPanel uses this for its picker badge.
  kind: ManagedUserKind;
  machine_identifier: string | null;
  has_token: boolean;
  has_pin: boolean;
  // Migration v16: per-backend PIN-equivalent columns. Plex rows use
  // ``has_pin`` (Plex Home PIN); Emby rows use ``has_emby_pin``
  // (Emby EasyPassword); Jellyfin rows use ``has_jellyfin_pin``
  // (Jellyfin EasyPassword). A given row only ever populates the one
  // matching its own ``service_type``; the others stay false.
  has_emby_pin: boolean;
  has_jellyfin_pin: boolean;
  has_password: boolean;
  last_seen: number | null;
  // Tombstone state. ``tombstoned`` is the per-server flag
  // exactly as stored. ``hidden_scope`` summarises both flags + the
  // global tombstone table into a single value the UI renders against.
  tombstoned: boolean;
  hidden_scope: ManagedUserHiddenScope;
  created_at: number;
  updated_at: number;
  // Share-state cross-reference. ``active_share`` is true
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
  // Canonical per-user
  // identifier - the Plex.tv numeric userID for Plex servers, the
  // equivalent backend-native id for Jellyfin / Emby once those
  // adapters land. Null on rows that pre-date migration v10 or that haven't
  // yet been resolved by the share-state refresh (which backfills
  // on first successful alias match). The matcher uses this as the
  // primary key for active_share decisions, making the gate immune
  // to display-name drift, Unicode quirks, and duplicate names.
  backend_user_id: string | null;
  // App-generated stable
  // user identifier (migration v12) in canonical form
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


// ── Databases viewer ─────────────────────────────────────────────────────
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

// ── Cross-platform preflight types ──────────────────────────────────────────
//
// Mirror of the Pydantic models in server/models.py:2260-2432
// (CrossPlatformPreflightReport + dependents). The backend owns those
// shapes; this block stays in sync.

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

// ── Playlist Management types ───────────────────────────────────────────────
//
// These mirror the backend's Pydantic models for the Playlist Mgmt
// foundation 1:1.

export interface PlaylistSpec {
  playlist_id: string;
  name: string;
  item_count: number;
  is_smart: boolean;
  // Library-attribution fields. Empty string / null when
  // the adapter couldn't determine them - the UI falls back to a
  // "(no library)" bucket or to playlist_type when grouping.
  playlist_type?: string;
  primary_library_id?: string | null;
  primary_library_name?: string | null;
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
  // True when copy_playlist short-
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
// Mirrors the backend's Pydantic PlaylistCacheRefreshResult
// at server/models.py:2802.
export interface PlaylistCacheRefreshResult {
  server_id: string;
  user_id: string | null;
  refreshed_at: number;
  playlists_count: number;
  items_count: number;
  duration_ms: number;
  error: string | null;
}

// ── Playlist Mgmt endpoint response envelopes ───────────────────────────────
//
// These endpoints wrap the actual payloads in small
// envelopes carrying query parameters back to the caller. The
// frontend unwraps them at the API method boundary.

// User shape returned by /api/playlist-mgmt/users - uniform across
// all 3 backends. Different from the legacy `ServerUser` shape
// (which keys by plex_id / raw_name / kind).
export interface PlaylistMgmtUser {
  backend_user_id: string;
  username: string;
  role: 'owner' | 'admin' | 'managed';
  // True when service_type=plex AND a
  // managed_users row exists for (server_id, username) AND its
  // auth_token_enc is non-null. Always false on Jellyfin/Emby; the
  // per-user-token affordance only matters for Plex Home routes.
  has_token: boolean;
  // Canonical app-generated
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

// Job-tracked deploy. Each playlist
// copy submitted via /api/playlist-mgmt/copy-job becomes a JobRecord
// in the existing queue; this is the end user-facing serialization
// returned by /api/playlist-mgmt/copy-jobs.
export interface PlaylistCopyJob {
  job_id: string;
  // The backend serialises both legacy single-copy jobs AND new batch
  // jobs through the same /copy-jobs endpoint. The mode discriminator
  // lets the UI pick the single-row vs batch-collapsed render shape.
  // Optional for back-compat: older job rows may omit it; treat
  // absent as 'playlist_copy'.
  mode?: 'playlist_copy' | 'playlist_copy_batch';
  // Matches JobRecord.state - typically 'queued' | 'running' |
  // 'completed' | 'completed_with_errors' | 'failed' | 'cancelled'.
  state: string;
  queued_at: number;
  started_at: number | null;
  finished_at: number | null;
  // Non-null when the worker stamped an error on rec.error. Distinct
  // from the per-item misses inside result.errors - this is a
  // single-line summary surfaced when the job state is 'failed' or
  // 'completed_with_errors'.
  error: string | null;
  // The PlaylistCopyResult shape from copy_playlist. Null while the
  // job is queued; populated as soon as the worker stamps a summary.
  // Only present on mode='playlist_copy' rows.
  result: PlaylistCopyResult | null;
  // Batch summary. Only present on mode='playlist_copy_batch' rows.
  batch?: PlaylistCopyBatchResult | null;
  // The original PlaylistCopyIn submission, surfaced so the Clone
  // Deploy button can re-submit without local memory. For batch
  // rows this is a thin echo of the batch-submit params (label +
  // parallelism); the per-item ``items`` list is stripped out to
  // keep the payload light (the per-item drill-down lives inside
  // ``batch.results``).
  params: {
    source_server_id?: string;
    source_user_id?: string;
    source_playlist_id?: string;
    dest_server_id?: string;
    dest_user_id?: string;
    dest_playlist_name?: string | null;
    // Batch-only echoed fields:
    label?: string | null;
    parallelism?: number | null;
  };
}

// ── Playlist Transfer batch ─────────────────────────────────────────────────
//
// One submission carries N PlaylistCopyIn items + optional parallelism
// override + optional label. The backend runs the N copies through an
// internal ThreadPoolExecutor (Approach A); per-item structured errors
// land in the result row without aborting the batch.

export interface PlaylistCopyBatchIn {
  items: PlaylistCopyIn[];
  // Per-submit override for the worker pool size. Absent -> server
  // falls back to ``playlist_mgmt_batch_workers`` tunable. Clamped to
  // [1, playlist_mgmt_batch_max_size] server-side.
  parallelism?: number | null;
  // Operator-supplied label shown in the activity feed + active-
  // deploys panel header. Absent -> backend uses "batch of N
  // playlist(s)".
  label?: string | null;
}

export interface PlaylistCopyBatchItemResult {
  // 1:1 with the input items list, ordered by input index.
  index: number;
  success: boolean;
  cancelled: boolean;
  error_code: string | null;
  new_playlist_id: string | null;
  items_written: number;
  items_skipped_no_match: number;
  items_failed: number;
  errors: string[];
  elapsed_seconds: number;
  skipped: boolean;
  skip_reason: string | null;
}

export interface PlaylistCopyBatchResult {
  // job_id present when the result is read off a job row; the synchronous
  // POST /copy-batch-job route returns {job_id, state} instead of the
  // full result (the job runs asynchronously).
  job_id?: string;
  total: number;
  succeeded: number;
  failed: number;
  skipped: number;
  cancelled: number;
  elapsed_seconds: number;
  parallelism: number;
  label: string | null;
  results: PlaylistCopyBatchItemResult[];
}

// Structured-error shape returned by /api/playlist-mgmt/copy on 4xx/5xx.
// Documented codes: SMART_PLAYLIST_NOT_PORTABLE (422),
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

// ── Smart Playlist Migration ────────────────────────────────────────────────
//
// A first-class job type: migrate a Plex SMART playlist (a saved
// filter, not a fixed item list) to another server. The source filter
// is read, its server-specific tag ids are translated to names, then
// re-applied: a true smart playlist on a Plex destination, a static
// materialisation of the current contents on Jellyfin / Emby (which
// have no smart concept). Shapes mirror services/smart_playlist.py
// to_dict() + server/smart_playlist_db.py list_migrations().

// One leaf clause of a decoded smart filter. ``values`` carries tag
// NAMES (server-agnostic) when value_kind is 'tag', literal strings
// otherwise. ``unresolved_ids`` are source tag ids that had no name.
export interface SmartFilterClause {
  kind: 'clause';
  field: string;
  operator: string;
  values: string[];
  value_kind: 'tag' | 'literal';
  unresolved_ids: string[];
}

// An AND / OR group of clauses and/or nested groups.
export interface SmartFilterGroup {
  kind: 'group';
  match: 'and' | 'or';
  children: SmartFilterNode[];
}

export type SmartFilterNode = SmartFilterClause | SmartFilterGroup;

// The portable, server-agnostic filter (services.smart_playlist
// to_dict). ``description`` is the pre-rendered plain-language line.
export interface SmartFilterDict {
  library_type: string;
  library_name: string;
  libtype: string;
  root: SmartFilterNode | null;
  sort: string[];
  limit: number | null;
  unresolved_source_ids: string[];
  description: string;
}

// GET /smart-playlist-preview response.
export interface SmartPlaylistPreview {
  playlist_name: string;
  filter: SmartFilterDict;
}

// One item of a Smart Playlist Migration submission. A PlaylistCopyIn
// plus the per-playlist mode:
//   hard_copy=false -> re-apply the FILTER (true smart playlist on a
//                      Plex destination, static snapshot on J/E).
//   hard_copy=true  -> transfer the current matched items as a normal
//                      static playlist, on any destination backend.
export interface SmartMigrateItem extends PlaylistCopyIn {
  hard_copy: boolean;
}

// One row from server/smart_playlist_db.py list_migrations(). The
// ``portable_filter`` / ``unresolved`` fields are the parsed JSON;
// the raw ``*_json`` columns also ride along but the UI ignores them.
export interface SmartMigrationRecord {
  id: number;
  job_id: string;
  migrated_at: number;
  source_server_id: string;
  source_playlist_id: string;
  source_playlist_name: string;
  dest_server_id: string;
  dest_backend: string;
  dest_playlist_id: string | null;
  mode: string; // 'smart' | 'static'
  portable_filter: SmartFilterDict | null;
  status: string; // 'success' | 'partial' | 'failed'
  coverage: number | null;
  unresolved: string[] | null;
  warning: string;
}

// ── Server Commands developer console ───────────────────────────────────────
// Shapes mirror server/dev_console_db.py + services/dev_console.py.
// Root-admin only; every endpoint 404s when dev_console_enabled is off.
// Reads come from the per-server mirror database, not the live server.

export interface DevConsoleServer {
  server_id: string;
  name: string;
  service_type: string;
  url: string;
  last_status: string;
}

export interface DevConsoleLibrary {
  library_id: string;
  name: string;
  type: string;
  item_count: number | null;
  last_synced_at?: number | null;
}

export interface DevConsoleUser {
  backend_user_id: string;
  username: string;
  display_name: string;
  role: string;
  is_admin: boolean;
}

export interface DevConsolePerUserState {
  username: string;
  display_name: string;
  view_count: number;
  last_viewed_at: number | null;
  view_offset_ms: number;
  user_rating: number | null;
  is_favorite: boolean;
}

export interface DevConsoleItem {
  backend_item_id: string;
  library_id: string;
  title: string;
  type: string;
  year: number | null;
  guids: string[];
  view_count: number;
  last_viewed_at: number | null;
  view_offset_ms: number;
  user_rating: number | null;
  is_favorite: boolean;
  show_title: string;
  season_index: number | null;
  episode_index: number | null;
  artist: string;
  album: string;
  staged: boolean;
  in_any_playlist: boolean;
  // Present only in the all-users explorer view: the users that
  // actually have state on this item.
  per_user?: DevConsolePerUserState[];
}

export interface DevConsoleServerDetail {
  server_id: string;
  name: string;
  service_type: string;
  url: string;
  last_status: string;
  synced: boolean;
  last_full_sync_at: number | null;
  last_sync_error: string;
  pending_staged: number;
  libraries: DevConsoleLibrary[];
  users: DevConsoleUser[];
  library_count: number;
  user_count: number;
}

export interface DevConsoleItemsResponse {
  server_id: string;
  library_id: string;
  user: string;
  all_users: boolean;
  items: DevConsoleItem[];
  total: number;
  offset: number;
  page_size: number;
  user_synced: boolean;
  mirror_cold: boolean;
}

export interface DevConsoleRelation {
  playlist_id?: string;
  collection_id?: string;
  name: string;
}

export interface DevConsoleItemDetail {
  server_id: string;
  user: string;
  item: DevConsoleItem;
  playlists: DevConsoleRelation[];
  collections: DevConsoleRelation[];
}

export interface DevConsoleCommandResult {
  op: string;
  server_id: string;
  item_id?: string;
  user?: string;
  staged: boolean;
  staged_id?: number;
  success: boolean;
  unsupported?: boolean;
  detail: string;
  new_state?: Record<string, unknown>;
  [k: string]: unknown;
}

export interface DevConsoleStagedChange {
  id: number;
  created_at: number;
  library_id: string;
  backend_item_id: string;
  item_title: string;
  username: string;
  op: string;
  payload: Record<string, unknown>;
  status: string;
  result_detail: string;
}

export interface DevConsoleStagedResponse {
  server_id: string;
  pending: DevConsoleStagedChange[];
  history: DevConsoleStagedChange[];
}

export interface DevConsoleSyncStatus {
  server_id: string;
  synced: boolean;
  last_full_sync_at: number | null;
  last_sync_error: string;
  libraries: Array<{ library_id: string; name: string; last_synced_at: number | null }>;
  synced_usernames: string[];
  pending_staged: number;
}

export interface DevConsolePlaylist {
  playlist_id: string;
  name: string;
  is_smart: boolean;
  item_count: number;
  playlist_type: string;
}

export interface DevConsoleCollection {
  collection_id: string;
  name: string;
  library_id: string | null;
  item_count: number;
}

export interface DevConsoleEvent {
  type: string;
  server_ts: number;
  [k: string]: unknown;
}

export interface DevConsoleGroup {
  key: string;
  label: string;
  count: number;       // leaf items (tracks / episodes) under this node
  sub_count: number;   // sub-groups (albums per artist, seasons per show)
}

export interface DevConsoleGroupsResponse {
  server_id: string;
  library_id: string;
  level: string;
  parent: string;
  groups: DevConsoleGroup[];
  // playlist level: false when the viewed user's playlists are not
  // mirrored yet (managed users sync on demand).
  user_synced: boolean;
}

export interface DevConsoleSmartFilterField {
  field: string;
  title: string;
  type: string;
  operators: Array<{ key: string; title: string }>;
}

export interface DevConsoleSmartFieldsResponse {
  server_id: string;
  library_id: string;
  libtype: string;
  fields: DevConsoleSmartFilterField[];
}

export interface DevConsoleItemFilters {
  user?: string;
  all_users?: boolean;
  categories?: string[];
  min_plays?: number;
  has_rating?: boolean;
  in_playlist?: string;
  in_collection?: string;
  artist?: string;
  album?: string;
  show_title?: string;
  season_index?: number;
  search?: string;
  sort?: string;
  offset?: number;
  page_size?: number;
}

// ── Library mapping types ────────────────────────────────────────────────────

export interface LibraryMappingLib {
  server_id: string;
  library_id: string;
  library_name: string;
  library_type: string;
  item_count: number;
}

export interface LibraryMappingCandidate {
  dest_library: LibraryMappingLib;
  confidence: number;
  tier: string;
  src_size: number;
  dst_size: number;
  overlap_size: number;
  auto_apply: boolean;
}

export interface LibraryMappingRow {
  source_server_id: string;
  source_library_id: string;
  source_library_name: string;
  dest_server_id: string;
  dest_library_id: string;
  dest_library_name: string;
  confidence: number;
  source: 'auto' | 'operator';
  tier: string;
  last_computed_at: number;
  last_confirmed_at: number | null;
  notes: string | null;
  bidirectional: boolean;
}

// ── Sync subscriptions ───────────────────────────────────────────────────────
export type SyncType =
  | 'watch_counts'
  | 'ratings'
  | 'favorites'
  | 'last_watched'
  | 'playlists';

export type ConflictPolicy =
  | 'max'
  | 'sum'
  | 'latest_wins'
  | 'source_of_truth';

export interface SyncSubscription {
  id: number;
  source_server_id: string;
  source_library_id: string;
  source_library_name: string;
  dest_server_id: string;
  dest_library_id: string;
  dest_library_name: string;
  sync_type: SyncType;
  conflict_policy: ConflictPolicy;
  enabled: boolean;
  dry_run: boolean;
  bidirectional: boolean;
  user_scope: 'owner' | 'all' | 'specific';
  user_filter: string[] | null;
  poll_interval_seconds: number;
  auto_sync_new_playlists: boolean;
  last_polled_at: number | null;
  last_synced_at: number | null;
  last_status: Record<string, unknown> | null;
  created_at: number;
  created_by: string | null;
  scope: 'library' | 'server';
}

export interface SyncWriteRow {
  id: number;
  subscription_id: number;
  poll_cycle_id: string;
  target_server_id: string;
  target_library_id: string;
  target_item_rating_key: string;
  target_user_id: string;
  sync_type: SyncType;
  before_value: number | null;
  after_value: number | null;
  issued: boolean;
  error: string | null;
  written_at: number;
}

export interface SyncPlaylistSelection {
  subscription_id: number;
  source_playlist_id: string;
  source_playlist_name: string;
  added_by: 'operator' | 'auto';
  enabled: boolean;
  added_at: number;
}
