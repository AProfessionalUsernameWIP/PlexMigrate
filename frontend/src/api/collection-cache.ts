// Collection cache + bulk playlist cache warm REST helpers extracted
// from the monolithic ``api`` const during Phase 3c. The
// ``warmAllPlaylistCaches`` endpoint sits in this module because its
// route lives under /api/playlist-mgmt but the bulk cache UI control
// keeps it next to its collection-cache twin.

import { http } from './core';

export const collectionCacheApi = {
  // ── Collection-children cache ─────────────────────────────────────
  // Operator surface mirrors the playlist cache: invalidate per-server
  // (or "_all_") + read per-server status. The next snapshot of a
  // server with an empty cache pays the full /children fetch cost;
  // subsequent snapshots skip unchanged collections.
  getCollectionCacheStatus: () =>
    http<{
      servers: Array<{
        server_id: string;
        collections: number;
        items: number;
        last_fetched_at: number;
        oldest_fetched_at: number;
      }>;
      // Per-(server, owner_user_id) breakdown. `_owner`
      // is the sentinel for library-wide / auto-generated; any other
      // string is a managed user's librarySectionUserID.
      servers_per_owner: Array<{
        server_id: string;
        owner_user_id: string;
        collections: number;
        items: number;
        last_fetched_at: number;
        oldest_fetched_at: number;
      }>;
      totals: { collections: number; items: number };
    }>('/api/collection-cache/status'),

  invalidateCollectionCache: (serverId: string) =>
    http<{ deleted: number; scope: string; server_id?: string }>(
      `/api/collection-cache/invalidate/${encodeURIComponent(serverId)}`,
      { method: 'POST' },
    ),

  // Populate the collection cache for one server WITHOUT running a
  // full snapshot. Walks every library + every collection + writes
  // results to cache. Wall-clock cost equals one snapshot's collection
  // phase; subsequent runs hit cache instead of re-fetching.
  warmCollectionCache: (serverId: string) =>
    http<{
      server_id: string;
      total_collections: number;
      total_items: number;
      elapsed_seconds: number;
      libraries: Array<{
        name: string;
        collections: number;
        items: number;
        skipped: number;
        errors: number;
      }>;
      error: string | null;
    }>(
      `/api/collection-cache/warm/${encodeURIComponent(serverId)}`,
      { method: 'POST' },
    ),

  // Warm the collection cache for every registered server. Used by
  // the "Build caches" header button. One server's failure does
  // NOT abort the others.
  warmAllCollectionCaches: () =>
    http<{
      servers_warmed: number;
      servers_failed: number;
      total_collections: number;
      total_items: number;
      elapsed_seconds: number;
      results: Array<{
        server_id: string;
        total_collections?: number;
        total_items?: number;
        error?: string | null;
      }>;
    }>(`/api/collection-cache/warm-all`, { method: 'POST' }),

  // Warm the playlist cache for every registered server. Symmetric
  // with warmAllCollectionCaches; both feed the bulk-cache UI control
  // whose checkboxes let the operator pick collections, playlists,
  // or both. Per-server failure reasons land in ``results[*].error``.
  warmAllPlaylistCaches: (source: string = 'servers-bulk') =>
    http<{
      servers_warmed: number;
      servers_failed: number;
      total_playlists: number;
      total_items: number;
      elapsed_seconds: number;
      results: Array<{
        server_id: string;
        error?: string | null;
        playlists_count?: number;
        items_count?: number;
        per_user_errors?: number;
      }>;
    }>(
      `/api/playlist-mgmt/cache/refresh-all?source=${encodeURIComponent(source)}`,
      { method: 'POST' },
    ),
};
