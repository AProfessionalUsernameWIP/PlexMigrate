// Misc system + diagnostics REST helpers extracted from the monolithic
// ``api`` const during Phase 3c. Covers health, server-time, DB stats,
// runtime breakdown, recent run history, and the dev-tests entrypoints.

import { http } from './core';
import type {
  DbStats,
  DevTestRunSummary,
  RecentRunRow,
  RuntimeEntry,
  RuntimeRunSummary,
  ServerTime,
} from './types';

export const systemApi = {
  // Server time / timezone - used by SchedulesPanel to label hour/
  // minute fields with the zone they're interpreted in.
  getServerTime: () => http<ServerTime>('/api/server-time'),

  // Engine + wire-protocol version, surfaced for the Help panel's
  // Troubleshooting page so bug reports auto-stamp the right build.
  getHealth: () =>
    http<{
      ok: boolean;
      api_version: string;
      app_version: string;
      debug_mode?: boolean;
    }>('/api/health'),


  // Media-state database health. Returns schema version,
  // per-table row counts, on-disk size, last-updated timestamps.
  // Used by diagnostic tools and the upcoming Database tab.
  getDbStats: () => http<DbStats>('/api/db/stats'),

  // Runtime breakdown.
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

  // List per-run history rows.
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

  // Developer tool unit test runner. Both endpoints
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
};
