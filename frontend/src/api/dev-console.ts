// Server Commands developer console REST helpers.
// Phase 3c extracted these out of the monolithic ``api`` const in
// ``frontend/src/api.ts``. The ``dcQuery`` helper used to be a
// file-private ``_dcQuery`` in the legacy module - it now lives here
// alongside its only callers.

import { http } from './core';
import type {
  DevConsoleCollection,
  DevConsoleCommandResult,
  DevConsoleGroupsResponse,
  DevConsoleItemDetail,
  DevConsoleItemFilters,
  DevConsoleItemsResponse,
  DevConsoleLibrary,
  DevConsolePlaylist,
  DevConsoleServer,
  DevConsoleServerDetail,
  DevConsoleSmartFieldsResponse,
  DevConsoleStagedResponse,
  DevConsoleSyncStatus,
  DevConsoleUser,
} from './types';

function dcQuery(filters: DevConsoleItemFilters): string {
  const p = new URLSearchParams();
  if (filters.user) p.set('user', filters.user);
  if (filters.all_users) p.set('all_users', 'true');
  if (filters.categories && filters.categories.length) {
    p.set('categories', filters.categories.join(','));
  }
  if (filters.min_plays != null) p.set('min_plays', String(filters.min_plays));
  if (filters.has_rating) p.set('has_rating', 'true');
  if (filters.in_playlist) p.set('in_playlist', filters.in_playlist);
  if (filters.in_collection) p.set('in_collection', filters.in_collection);
  if (filters.artist != null) p.set('artist', filters.artist);
  if (filters.album != null) p.set('album', filters.album);
  if (filters.show_title != null) p.set('show_title', filters.show_title);
  if (filters.season_index != null) p.set('season_index', String(filters.season_index));
  if (filters.search) p.set('search', filters.search);
  if (filters.sort) p.set('sort', filters.sort);
  if (filters.offset != null) p.set('offset', String(filters.offset));
  if (filters.page_size != null) p.set('page_size', String(filters.page_size));
  const s = p.toString();
  return s ? `?${s}` : '';
}

export const devConsoleApi = {
  devConsoleServers: () =>
    http<{ servers: DevConsoleServer[] }>('/api/dev-console/servers'),
  devConsoleServerDetail: (sid: string) =>
    http<DevConsoleServerDetail>(
      `/api/dev-console/servers/${encodeURIComponent(sid)}`,
    ),
  devConsoleLibraries: (sid: string) =>
    http<{ libraries: DevConsoleLibrary[] }>(
      `/api/dev-console/servers/${encodeURIComponent(sid)}/libraries`,
    ),
  devConsoleUsers: (sid: string) =>
    http<{ users: DevConsoleUser[] }>(
      `/api/dev-console/servers/${encodeURIComponent(sid)}/users`,
    ),
  devConsoleSyncStatus: (sid: string) =>
    http<DevConsoleSyncStatus>(
      `/api/dev-console/servers/${encodeURIComponent(sid)}/sync-status`,
    ),
  devConsoleRequestSync: (sid: string, users?: string[]) =>
    http<{ server_id: string; queued: boolean }>(
      `/api/dev-console/servers/${encodeURIComponent(sid)}/sync`,
      { method: 'POST', body: JSON.stringify({ users: users || null }) },
    ),
  devConsoleResetMirror: (sid: string) =>
    http<{ server_id: string; cleared: boolean; queued: boolean }>(
      `/api/dev-console/servers/${encodeURIComponent(sid)}/reset`,
      { method: 'POST' },
    ),
  devConsoleSyncUserPlaylists: (sid: string, user?: string) =>
    http<{ server_id: string; ok: boolean; phase: string; detail: string }>(
      `/api/dev-console/servers/${encodeURIComponent(sid)}/sync-user-playlists`,
      { method: 'POST', body: JSON.stringify({ user: user || null }) },
    ),
  devConsoleItems: (
    sid: string, libraryId: string, filters: DevConsoleItemFilters = {},
  ) =>
    http<DevConsoleItemsResponse>(
      `/api/dev-console/servers/${encodeURIComponent(sid)}` +
      `/libraries/${encodeURIComponent(libraryId)}/items${dcQuery(filters)}`,
    ),
  devConsoleItemDetail: (sid: string, itemId: string, user?: string) =>
    http<DevConsoleItemDetail>(
      `/api/dev-console/servers/${encodeURIComponent(sid)}` +
      `/items/${encodeURIComponent(itemId)}` +
      (user ? `?user=${encodeURIComponent(user)}` : ''),
    ),
  devConsoleGroups: (
    sid: string, libraryId: string, level: string, parent?: string,
    opts?: { categories?: string[]; user?: string; allUsers?: boolean },
  ) => {
    const p = new URLSearchParams({ level });
    if (parent) p.set('parent', parent);
    if (opts?.categories && opts.categories.length) {
      p.set('categories', opts.categories.join(','));
    }
    if (opts?.user) p.set('user', opts.user);
    if (opts?.allUsers) p.set('all_users', 'true');
    return http<DevConsoleGroupsResponse>(
      `/api/dev-console/servers/${encodeURIComponent(sid)}` +
      `/libraries/${encodeURIComponent(libraryId)}/groups?${p.toString()}`,
    );
  },
  devConsoleCommand: (
    sid: string, libraryId: string, itemId: string,
    body: {
      op: string; user?: string; stage: boolean;
      payload: Record<string, unknown>;
    },
  ) =>
    http<DevConsoleCommandResult>(
      `/api/dev-console/servers/${encodeURIComponent(sid)}` +
      `/libraries/${encodeURIComponent(libraryId)}` +
      `/items/${encodeURIComponent(itemId)}/command`,
      { method: 'POST', body: JSON.stringify(body) },
    ),
  devConsoleRawCall: (
    sid: string, itemId: string,
    body: { op: string; user?: string; params?: Record<string, unknown> },
  ) =>
    http<Record<string, unknown>>(
      `/api/dev-console/servers/${encodeURIComponent(sid)}` +
      `/items/${encodeURIComponent(itemId)}/raw`,
      { method: 'POST', body: JSON.stringify(body) },
    ),
  devConsoleStaged: (sid: string) =>
    http<DevConsoleStagedResponse>(
      `/api/dev-console/servers/${encodeURIComponent(sid)}/staged`,
    ),
  devConsoleSendStaged: (sid: string) =>
    http<{ server_id: string; sent: number; failed: number;
           results: Array<{ id: number; ok: boolean; detail: string }> }>(
      `/api/dev-console/servers/${encodeURIComponent(sid)}/staged/send`,
      { method: 'POST', body: JSON.stringify({}) },
    ),
  devConsoleDiscardStaged: (sid: string, stagedId?: number) =>
    http<{ server_id: string; discarded: number }>(
      `/api/dev-console/servers/${encodeURIComponent(sid)}/staged/discard`,
      { method: 'POST', body: JSON.stringify({ staged_id: stagedId ?? null }) },
    ),
  devConsolePlaylists: (sid: string) =>
    http<{ playlists: DevConsolePlaylist[] }>(
      `/api/dev-console/servers/${encodeURIComponent(sid)}/playlists`,
    ),
  devConsoleCreatePlaylist: (
    sid: string,
    body: { name: string; item_ids?: string[]; library_id?: string; user?: string },
  ) =>
    http<DevConsoleCommandResult>(
      `/api/dev-console/servers/${encodeURIComponent(sid)}/playlists`,
      { method: 'POST', body: JSON.stringify(body) },
    ),
  devConsoleSmartPlaylistFields: (
    sid: string, libraryId: string, libtype?: string, user?: string,
  ) => {
    const p = new URLSearchParams();
    if (libtype) p.set('libtype', libtype);
    if (user) p.set('user', user);
    const qs = p.toString();
    return http<DevConsoleSmartFieldsResponse>(
      `/api/dev-console/servers/${encodeURIComponent(sid)}` +
      `/libraries/${encodeURIComponent(libraryId)}/smart-playlist-fields` +
      (qs ? `?${qs}` : ''),
    );
  },
  devConsoleCreateSmartPlaylist: (
    sid: string,
    body: {
      name: string; library_id: string; libtype?: string;
      match?: string;
      rows: Array<{ field: string; op: string; value: string }>;
      sort?: string[]; limit?: number; user?: string;
    },
  ) =>
    http<DevConsoleCommandResult>(
      `/api/dev-console/servers/${encodeURIComponent(sid)}/smart-playlists`,
      { method: 'POST', body: JSON.stringify(body) },
    ),
  devConsoleCollections: (sid: string) =>
    http<{ collections: DevConsoleCollection[] }>(
      `/api/dev-console/servers/${encodeURIComponent(sid)}/collections`,
    ),
  devConsolePlaylistMembers: (
    sid: string, playlistId: string,
    body: {
      add_item_ids?: string[]; remove_item_ids?: string[];
      library_id?: string; user?: string;
    },
  ) =>
    http<DevConsoleCommandResult>(
      `/api/dev-console/servers/${encodeURIComponent(sid)}` +
      `/playlists/${encodeURIComponent(playlistId)}/members`,
      { method: 'POST', body: JSON.stringify(body) },
    ),
  devConsoleCollectionMembers: (
    sid: string, collectionId: string,
    body: {
      add_item_ids?: string[]; remove_item_ids?: string[];
      library_id?: string;
    },
  ) =>
    http<DevConsoleCommandResult>(
      `/api/dev-console/servers/${encodeURIComponent(sid)}` +
      `/collections/${encodeURIComponent(collectionId)}/members`,
      { method: 'POST', body: JSON.stringify(body) },
    ),
};
