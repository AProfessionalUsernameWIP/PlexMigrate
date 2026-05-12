// Thin REST + WebSocket client for the PlexMigrate server.
//
// All endpoints live under /api/ and the live dashboard stream is at
// /ws/dashboard. Both are same-origin in production (nginx proxies
// them in the frontend container) and same-origin in dev (Vite's
// proxy in vite.config.ts forwards to localhost:8000).
//
// This module exports:
//   * Typed wrappers around every REST endpoint.
//   * A `SnapshotSocket` class that owns the WebSocket lifecycle
//     (connect / reconnect / dispatch). The UI subscribes to it via
//     React state — see App.tsx.

// ── Type definitions ─────────────────────────────────────────────────────────
// These mirror the snapshot shape DashboardState produces in Python
// (services/dashboard.py :: DashboardState.snapshot). We keep them
// narrow — only the fields the UI actually reads — so changes in the
// engine don't force frontend type churn.

export interface DashboardSnapshot {
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
  // v0.9.3: run coverage — added so the dashboard can show how many
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
  // v0.9.6 Feature 2: HTTP telemetry — status code histogram and
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
}

export interface CurrentItem {
  library: string;
  type: string;
  title: string;
  started_at: number;
  // v0.9.3: short verb describing what this thread is doing on the
  // item — "resolving" / "scrobbling" / "rating" / "merging" /
  // "exporting" / "indexing" / "fetching". Optional for forward-compat
  // with older snapshots; missing renders as an em-dash.
  phase?: string;
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
}

export interface JobPayload {
  job_id: string;
  // v0.9.0 added 'direct' alongside the original two modes. The
  // server-side JobRecord uses the same literal strings.
  mode: 'export' | 'import' | 'direct';
  // 'stopping' (v0.9.3) is the intermediate state between user click
  // and engine return — see server/jobs.py :: STATE_STOPPING.
  state: 'idle' | 'queued' | 'running' | 'stopping' | 'completed' | 'failed' | 'cancelled';
  queued_at: number;
  started_at: number | null;
  finished_at: number | null;
  error: string | null;
  run_log_dir: string | null;
  params: Record<string, unknown>;
}

export interface SnapshotMessage {
  type: 'snapshot';
  server_ts: number;
  dashboard: DashboardSnapshot | null;
  job: JobPayload | null;
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
}

export interface LibraryDescriptor {
  name: string;
  type: string;
  key: string | number;
  count: number;
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
  // still present — UI renders "No managed users found" plus a
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
// remove. Lets the UI render a "Deleted Jade.TV (2 schedules, 47
// exports, 12 log dirs cleaned up)" toast instead of a blank success.
// Best-effort: a non-empty ``errors`` array lists per-file failures
// that did not block the rest of the sweep.
export interface ServerDeleteSummary {
  deleted: boolean;
  id: string;
  name: string;
  slug: string;
  schedules: number;
  exports: number;
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
  exports: number;
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

export interface ExportFile {
  name: string;
  size: number;
  mtime: number;
  library: string | null;
  exported_at: string | null;
  // v0.9.3: friendly name + URL of the server that produced this
  // backup. ``null`` for backups exported before this field was added.
  source_server?: string | null;
  source_server_url?: string | null;
  // v0.9.5: how the run was initiated. "manual" for a GUI submission,
  // "schedule" for a scheduler fire (``schedule_name`` carries the
  // schedule's display name). ``null`` on backups exported before
  // these fields existed.
  trigger?: string | null;
  schedule_name?: string | null;
}

// ── REST helpers ─────────────────────────────────────────────────────────────

async function http<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json', ...(init?.headers || {}) },
    ...init,
  });
  if (!res.ok) {
    let detail = '';
    try {
      const body = await res.json();
      detail = body?.detail || JSON.stringify(body);
    } catch {
      detail = await res.text();
    }
    throw new Error(`${res.status} ${res.statusText}: ${detail}`);
  }
  if (res.status === 204) return undefined as unknown as T;
  return (await res.json()) as T;
}

// ── Endpoint wrappers ────────────────────────────────────────────────────────

export const api = {
  // Settings
  getSettings: () => http<SettingsView>('/api/settings'),
  saveSettings: (patch: Partial<SettingsView & { plex_token: string }>) =>
    http<SettingsView>('/api/settings', { method: 'POST', body: JSON.stringify(patch) }),

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
  pingServer: (id: string) =>
    http<PingResult>(`/api/servers/${encodeURIComponent(id)}/ping`, { method: 'POST' }),
  listServerLibraries: (id: string) =>
    http<LibraryDescriptor[]>(`/api/servers/${encodeURIComponent(id)}/libraries`),
  // v0.9.6 Feature 3: list users on a server (owner + managed). Live
  // call to systemAccounts() — one round-trip per visit, no caching.
  listServerUsers: (id: string) =>
    http<ServerUsersResponse>(`/api/servers/${encodeURIComponent(id)}/users`),
  // Set or clear one entry in a server's user_display_names map.
  // Empty display_name clears the mapping (UI falls back to raw id).
  setUserDisplayName: (id: string, plex_id: string, display_name: string) =>
    http<ServerView>(`/api/servers/${encodeURIComponent(id)}/user-display-name`, {
      method: 'PATCH',
      body: JSON.stringify({ plex_id, display_name }),
    }),

  // Legacy single-server library listing — kept for one release.
  listLibraries: () => http<LibraryDescriptor[]>('/api/libraries'),

  // Jobs
  getJob: () => http<{ state: string; dashboard?: DashboardSnapshot | null; mode?: string; error?: string }>('/api/job'),
  getJobHistory: () => http<JobPayload[]>('/api/job/history'),
  submitExport: (params: Record<string, unknown>) =>
    http<{ job_id: string; state: string; mode: string }>('/api/job/export', {
      method: 'POST',
      body: JSON.stringify(params),
    }),
  submitImport: (params: Record<string, unknown>) =>
    http<{ job_id: string; state: string; mode: string }>('/api/job/import', {
      method: 'POST',
      body: JSON.stringify(params),
    }),
  submitDirect: (params: Record<string, unknown>) =>
    http<{ job_id: string; state: string; mode: string }>('/api/job/direct', {
      method: 'POST',
      body: JSON.stringify(params),
    }),
  stopJob: () => http<{ stop_requested: boolean }>('/api/job/stop', { method: 'POST' }),

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

  // Server time / timezone — used by SchedulesPanel to label hour/
  // minute fields with the zone they're interpreted in.
  getServerTime: () => http<ServerTime>('/api/server-time'),

  // Exports
  listExports: () => http<ExportFile[]>('/api/exports'),
  exportDownloadUrl: (name: string) => `/api/exports/${encodeURIComponent(name)}`,
  deleteExport: (name: string) =>
    http<{ deleted: string }>(`/api/exports/${encodeURIComponent(name)}`, { method: 'DELETE' }),
};

// ── WebSocket wrapper ────────────────────────────────────────────────────────

type SocketListener = (snap: SnapshotMessage) => void;

/**
 * Owns the WebSocket connection to the dashboard endpoint.
 *
 * Single instance, lazy-connect on the first .subscribe() call.
 * Automatically reconnects with linear backoff if the socket drops
 * (the user's job may still be running; we don't want to leave them
 * watching a frozen panel).
 */
export class SnapshotSocket {
  private ws: WebSocket | null = null;
  private listeners = new Set<SocketListener>();
  private reconnectTimer: number | null = null;
  private retryDelayMs = 1000;
  private readonly maxDelayMs = 10_000;
  private explicitlyClosed = false;

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
    // Same-origin URL — works in dev (Vite proxy) and prod (nginx proxy).
    const proto = window.location.protocol === 'https:' ? 'wss' : 'ws';
    const url = `${proto}://${window.location.host}/ws/dashboard`;
    const ws = new WebSocket(url);
    this.ws = ws;

    ws.onmessage = (ev) => {
      try {
        const msg = JSON.parse(ev.data) as SnapshotMessage;
        this.listeners.forEach((l) => l(msg));
        // A successful message resets the retry backoff.
        this.retryDelayMs = 1000;
      } catch {
        // Ignore malformed frames.
      }
    };

    ws.onclose = () => {
      this.ws = null;
      if (this.explicitlyClosed || this.listeners.size === 0) return;
      // Schedule a reconnect; linear backoff with a hard cap.
      this.reconnectTimer = window.setTimeout(() => {
        this.reconnectTimer = null;
        this.retryDelayMs = Math.min(this.retryDelayMs + 1000, this.maxDelayMs);
        this.ensureConnected();
      }, this.retryDelayMs);
    };

    ws.onerror = () => {
      // The browser will fire onclose right after this — handler there
      // schedules the retry. We don't double-schedule here.
    };
  }
}

export const dashboardSocket = new SnapshotSocket();
