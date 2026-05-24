// Schedule CRUD extracted from the monolithic ``api`` const during
// Phase 3c. The cross-platform schedule preflight + per-schedule
// resolutions PATCH live with the other preflight endpoints in
// ``./jobs``.

import { http } from './core';
import type { Schedule } from './types';

export const schedulesApi = {
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
};
