// Job submission + cross-platform preflight helpers extracted from the
// monolithic ``api`` const during Phase 3c.

import { http } from './core';
import type {
  DashboardState,
  InlineCreateUserBody,
  InlineCreateUserResponse,
  JobPayload,
  PreflightResponse,
  Schedule,
  ScheduleResolutionsPatch,
} from './types';

export const jobsApi = {
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

  // PIN preflight. Returns the at-risk managed-user list (empty
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

  // ── Cross-platform preflight ──────────────────────────────────────────
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
};
