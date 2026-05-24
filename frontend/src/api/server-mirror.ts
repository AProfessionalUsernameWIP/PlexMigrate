// Server Mirror DB REST helpers extracted from the monolithic
// ``api`` const during Phase 3c.

import { http } from './core';

export const serverMirrorApi = {
  // ── Server Mirror DB ──────────────────────────────────────────────
  // Five endpoints under /api/server-mirror/* back the per-server
  // mirror UI: state badge in Servers panel, Drift History tab,
  // first-run sync dialog, manual sync, invalidate, mode override.
  getServerMirrorState: () =>
    http<{
      servers: Array<{
        server_id: string;
        backend: string;
        first_sync_at: number | null;
        last_full_sync_at: number | null;
        last_drift_check_at: number | null;
        mode_override: 'auto' | 'always-live' | null;
        url_fingerprint: string | null;
        token_hash: string | null;
        item_count: number;
        last_drift_event_at: number | null;
      }>;
      db_size_bytes: number;
    }>('/api/server-mirror/state'),

  getServerMirrorDriftEvents: (
    opts: { serverId?: string; since?: number; limit?: number } = {},
  ) => {
    const params = new URLSearchParams();
    if (opts.serverId) params.set('server_id', opts.serverId);
    if (opts.since !== undefined) params.set('since', String(opts.since));
    if (opts.limit !== undefined) params.set('limit', String(opts.limit));
    const qs = params.toString();
    const url = `/api/server-mirror/drift-events${qs ? '?' + qs : ''}`;
    return http<{
      events: Array<{
        id: number;
        server_id: string;
        section_id: string;
        section_name: string;
        detected_at: number;
        mirror_total_size: number | null;
        live_total_size: number | null;
        mirror_updated_at: number | null;
        live_updated_at: number | null;
        detected_by_job_id: string | null;
      }>;
      count: number;
    }>(url);
  },

  syncServerMirror: (serverId: string) =>
    http<{ status: string; server_id: string }>(
      `/api/server-mirror/sync/${encodeURIComponent(serverId)}`,
      { method: 'POST' },
    ),

  // Response shape carries per-server status so the
  // operator UI can surface a per-server failure list (a server that
  // can't connect shouldn't quietly disappear). Each result row
  // carries one of: 'started' (walker launched), 'in_progress' (a
  // walker was already running for this server), or 'error' with a
  // reason (connect failed / server not registered / network).
  syncAllServerMirrors: () =>
    http<{
      status: string;
      results: Array<{
        server_id: string;
        status: 'started' | 'in_progress' | 'error';
        error?: string;
      }>;
    }>(`/api/server-mirror/sync-all`, { method: 'POST' }),

  invalidateServerMirror: (serverId: string) =>
    http<{ deleted: number; scope: string; server_id?: string }>(
      `/api/server-mirror/invalidate/${encodeURIComponent(serverId)}`,
      { method: 'POST' },
    ),

  setServerMirrorMode: (
    serverId: string,
    mode: 'auto' | 'always-live' | null,
  ) =>
    http<{
      server_id: string;
      mode_override: 'auto' | 'always-live' | null;
      effective_mode: 'auto' | 'always-live';
    }>(`/api/server-mirror/mode/${encodeURIComponent(serverId)}`, {
      method: 'PATCH',
      body: JSON.stringify({ mode }),
    }),
};
