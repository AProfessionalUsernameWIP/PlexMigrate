// Sync subscriptions REST helpers extracted from the monolithic
// ``api`` const during Phase 3c.

import { http } from './core';
import type { SyncPlaylistSelection, SyncSubscription, SyncWriteRow } from './types';

export const syncApi = {
  // ── Sync subscriptions ────────────────────────────────────────────
  // Library-pair / server-pair sync engine. Each subscription
  // declares "for this server pair (or this library pair within the
  // pair), reconcile this sync_type under this conflict policy."

  syncListSubscriptions: (
    sourceServerId?: string | null,
    destServerId?: string | null,
    enabledOnly: boolean = false,
  ) => {
    const params = new URLSearchParams();
    if (sourceServerId) params.set('source_server_id', sourceServerId);
    if (destServerId) params.set('dest_server_id', destServerId);
    if (enabledOnly) params.set('enabled_only', 'true');
    const q = params.toString();
    return http<{ subscriptions: SyncSubscription[] }>(
      `/api/sync/subscriptions${q ? '?' + q : ''}`,
    );
  },

  syncSaveSubscription: (body: {
    source_server_id: string;
    dest_server_id: string;
    sync_type: 'watch_counts' | 'ratings' | 'favorites' | 'last_watched' | 'playlists';
    source_library_id?: string | null;
    source_library_name?: string;
    dest_library_id?: string | null;
    dest_library_name?: string;
    conflict_policy?: 'max' | 'sum' | 'latest_wins' | 'source_of_truth';
    enabled?: boolean;
    dry_run?: boolean;
    bidirectional?: boolean;
    user_scope?: 'owner' | 'all' | 'specific';
    user_filter?: string[];
    poll_interval_seconds?: number;
    auto_sync_new_playlists?: boolean;
  }) =>
    http<{ id: number }>(`/api/sync/subscriptions`, {
      method: 'PUT',
      body: JSON.stringify(body),
    }),

  syncDeleteSubscription: (subId: number) =>
    http<{ deleted: number }>(
      `/api/sync/subscriptions/${subId}`,
      { method: 'DELETE' },
    ),

  syncGetWrites: (subId: number, limit: number = 100) =>
    http<{ subscription_id: number; writes: SyncWriteRow[] }>(
      `/api/sync/subscriptions/${subId}/writes?limit=${limit}`,
    ),

  syncListPlaylistSelections: (
    subId: number, enabledOnly: boolean = false,
  ) =>
    http<{
      subscription_id: number;
      selections: SyncPlaylistSelection[];
    }>(
      `/api/sync/subscriptions/${subId}/playlists${
        enabledOnly ? '?enabled_only=true' : ''
      }`,
    ),

  syncAddPlaylistSelection: (
    subId: number,
    body: {
      source_playlist_id: string;
      source_playlist_name?: string;
      added_by?: 'operator' | 'auto';
      enabled?: boolean;
    },
  ) =>
    http<{ ok: boolean }>(
      `/api/sync/subscriptions/${subId}/playlists`,
      { method: 'PUT', body: JSON.stringify(body) },
    ),

  syncRemovePlaylistSelection: (subId: number, sourcePlaylistId: string) =>
    http<{ deleted: number }>(
      `/api/sync/subscriptions/${subId}/playlists/${encodeURIComponent(sourcePlaylistId)}`,
      { method: 'DELETE' },
    ),

  syncRunNow: (subId: number) =>
    http<{ subscription_id: number; queued: boolean; note: string }>(
      `/api/sync/subscriptions/${subId}/run`,
      { method: 'POST' },
    ),

  syncStats: () =>
    http<{ subscriptions: number; observations: number; writes: number }>(
      `/api/sync/stats`,
    ),
};
