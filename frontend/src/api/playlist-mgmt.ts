// Playlist Management + Smart Playlist Migration + per-request network
// timeline REST helpers extracted from the monolithic ``api`` const
// during Phase 3c.

import { http } from './core';
import type {
  PlaylistCacheRefreshResult,
  PlaylistCopyBatchIn,
  PlaylistCopyIn,
  PlaylistCopyJob,
  PlaylistCopyResult,
  PlaylistDetail,
  PlaylistMgmtBulkRefreshResponse,
  PlaylistMgmtCacheStatusResponse,
  PlaylistMgmtPlaylistsResponse,
  PlaylistMgmtUsersResponse,
  SmartMigrateItem,
  SmartMigrationRecord,
  SmartPlaylistPreview,
} from './types';

export const playlistMgmtApi = {
  // ── Playlist Management endpoints ─────────────────────────────────
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
  // Async path. Submits the playlist
  // copy as a job in the existing queue and returns the job_id
  // immediately. The active-deploys panel then polls
  // /api/playlist-mgmt/copy-jobs for state + result.
  submitPlaylistCopyJob: (body: PlaylistCopyIn) =>
    http<{ job_id: string; state: string }>(
      '/api/playlist-mgmt/copy-job',
      { method: 'POST', body: JSON.stringify(body) },
    ),
  // One POST submits N
  // PlaylistCopyIn items as ONE batch JobRecord. Internal
  // ThreadPoolExecutor inside services.playlist_copy.copy_playlist_batch
  // runs the per-item copies in parallel; the queue's serial worker
  // pump still runs ONE batch at a time. Response is the same
  // {job_id, state} shape as the single-copy submit.
  submitPlaylistCopyBatchJob: (body: PlaylistCopyBatchIn) =>
    http<{ job_id: string; state: string }>(
      '/api/playlist-mgmt/copy-batch-job',
      { method: 'POST', body: JSON.stringify(body) },
    ),
  // Cancel ONE item
  // inside a running batch (per-item Cancel button on the
  // active-deploys panel). The whole-batch Stop continues to flow
  // through the existing /api/jobs Stop button.
  cancelPlaylistBatchItem: (jobId: string, itemIndex: number) =>
    http<{ status: string; job_id: string; item_index: number }>(
      `/api/playlist-mgmt/copy-batch-job/${encodeURIComponent(jobId)}/cancel-item`,
      { method: 'POST', body: JSON.stringify({ item_index: itemIndex }) },
    ),
  // Trigger background
  // path-index build for the destination server. Idempotent; the
  // panel fires this after a tunable debounce delay (default 3s)
  // when the operator picks a destination, so the build happens
  // in the background while they're still selecting playlists.
  prewarmPlaylistMgmtServer: (serverId: string) =>
    http<{ status: string; server_id: string; scopes?: string[] }>(
      `/api/playlist-mgmt/prewarm/${encodeURIComponent(serverId)}`,
      { method: 'POST' },
    ),
  // ── Smart Playlist Migration ──────────────────────────────────────
  // Smart playlists are browsed via the per-user playlist listing
  // (playlistMgmtListPlaylists) inside Playlist Transfer's Smart
  // Playlist mode; the methods below cover decode, submit + results.
  //
  // Decode one source smart playlist's filter into the portable,
  // server-agnostic form for the inspector + preflight display.
  previewSmartPlaylist: (serverId: string, playlistId: string) =>
    http<SmartPlaylistPreview>(
      `/api/playlist-mgmt/smart-playlist-preview?server_id=${encodeURIComponent(serverId)}&playlist_id=${encodeURIComponent(playlistId)}`,
    ),
  // Submit a Smart Playlist Migration job (N items). Returns
  // {job_id, state} immediately; the panel polls the job and tails
  // the dedicated smart-playlist log for live feedback.
  submitSmartPlaylistMigrateJob: (
    body: { items: SmartMigrateItem[]; label?: string | null },
  ) =>
    http<{ job_id: string; state: string }>(
      '/api/playlist-mgmt/smart-migrate-job',
      { method: 'POST', body: JSON.stringify(body) },
    ),
  // Recent Smart Playlist Migration records, newest first. Filtered
  // to a single ``jobId`` when given (the results view after a run).
  listSmartMigrations: (jobId?: string, limit = 100) => {
    const params = new URLSearchParams();
    if (jobId) params.set('job_id', jobId);
    params.set('limit', String(limit));
    return http<{ migrations: SmartMigrationRecord[] }>(
      `/api/playlist-mgmt/smart-migrations?${params.toString()}`,
    );
  },
  // Per-request
  // HTTP timeline captured by the shared session response hook. Each
  // entry carries URL + method + status + elapsed_ms + host + job_id;
  // optional filters narrow to a specific job or host.
  listRecentNetworkRequests: (
    opts: { limit?: number; jobId?: string; host?: string } = {},
  ) => {
    const params = new URLSearchParams();
    if (opts.limit !== undefined) params.set('limit', String(opts.limit));
    if (opts.jobId) params.set('job_id', opts.jobId);
    if (opts.host) params.set('host', opts.host);
    const qs = params.toString();
    const url = `/api/network/recent-requests${qs ? '?' + qs : ''}`;
    return http<{
      requests: Array<{
        timestamp: number;
        host: string;
        method: string;
        url: string;
        status_code: number;
        elapsed_ms: number;
        job_id: string | null;
      }>;
      count: number;
    }>(url);
  },
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
};
