// Library walk + prune REST helpers extracted from the monolithic
// ``api`` const during Phase 3c.

import { http } from './core';
import type { LibraryWalkStatus, PrunePreview, PruneResult } from './types';

export const libraryWalkApi = {
  // Library walk + prune missing items.
  getLibraryWalkStatus: (serverId: string) =>
    http<LibraryWalkStatus>(`/api/servers/${encodeURIComponent(serverId)}/library-walk`),
  // The walk runs in a background thread on the server; this
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
};
