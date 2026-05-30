// Multi-server registry + per-server config helpers extracted from the
// monolithic ``api`` const during Phase 3c. Owns CRUD over /api/servers,
// the test/refresh/ping/library/user-listing edges, PIN migration
// suggestions, the per-server tombstone/auth-counter toggles, and the
// per-managed-user token rotation + Plex-home-token write paths that
// scope to a single server.

import { http } from './core';
import type {
  LibraryDescriptor,
  PinMigrationSuggestion,
  PingResult,
  ProbeUnsavedResult,
  ServerCascadePreview,
  ServerDeleteSummary,
  ServerIn,
  ServerManagedUser,
  ServerUsersResponse,
  ServerView,
} from './types';

export const serversApi = {
  // Cross-server PIN migration suggestions + apply.
  pinMigrationSuggestions: (serverId: string) =>
    http<{ suggestions: PinMigrationSuggestion[] }>(
      `/api/servers/${encodeURIComponent(serverId)}/pin-migration-suggestions`,
    ),
  pinMigrationApply: (
    serverId: string,
    suggestions: PinMigrationSuggestion[],
  ) =>
    http<{ applied: string[]; skipped: string[]; errors: string[] }>(
      `/api/servers/${encodeURIComponent(serverId)}/pin-migration/apply`,
      { method: 'POST', body: JSON.stringify({ suggestions }) },
    ),

  // Per-user manual token rotation. Bypasses the throttle
  // and the additive-only contract for one user. Requires sudo-style
  // elevation on the backend.
  rotateManagedUserToken: (serverId: string, username: string) =>
    http<{ captured: number; skipped_existing: number; throttled: boolean; errors: string[] }>(
      `/api/servers/${encodeURIComponent(serverId)}/managed-users/${encodeURIComponent(username)}/rotate-token`,
      { method: 'POST' },
    ),

  // Persist a Plex Home user's X-Plex-Token so
  // the Playlist Management `per_user_token` auth mode can create
  // playlists under that user instead of the destination admin.
  // Body re-authenticates db_admin per call (same gate as the broader
  // managed-users PATCH). When clear_auth_token=true, auth_token is
  // ignored and the row's token is wiped. Plex-only endpoint; non-Plex
  // destinations 400 out.
  setPlexHomeToken: (
    serverId: string,
    username: string,
    body: {
      db_admin_username: string;
      db_admin_password: string;
      auth_token: string | null;
      clear_auth_token: boolean;
      display_name?: string | null;
    },
  ) =>
    http<ServerManagedUser>(
      `/api/managed-users/${encodeURIComponent(serverId)}/${encodeURIComponent(username)}/plex-home-token`,
      { method: 'POST', body: JSON.stringify(body) },
    ),

  // Multi-server registry
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
  // Probe a URL+token without registering. Used by the
  // "Test Connection" button in the add-server form so testing
  // doesn't side-effect the registry. Also flags name mismatch and
  // duplicate-machine-identifier collisions so the UI can warn before
  // the end user clicks Save.
  testUnsavedServer: (body: {
    name: string;
    url: string;
    token: string;
    // When omitted, the backend defaults to 'plex'.
    // The Add Server form sets this explicitly based on the radio
    // picker so the probe goes through the right adapter.
    service_type?: 'plex' | 'jellyfin' | 'emby';
  }) =>
    http<ProbeUnsavedResult>('/api/servers/test-unsaved', {
      method: 'POST',
      body: JSON.stringify(body),
    }),
  // Alias: refreshing the cached library catalogue is the
  // same backend call as the /test endpoint, surfaced under
  // a clearer name. The Servers tab uses this for its per-row
  // "Refresh" button.
  refreshServer: (id: string) =>
    http<ServerView>(`/api/servers/${encodeURIComponent(id)}/test`, { method: 'POST' }),
  // Retry the pending token stashed at Add-time (the end user's
  // original token that failed with 401 but was kept aside for later
  // retry once Plex.tv finished propagating the new server's
  // authorization). On success, the backend swaps the pending into
  // the active slot and clears every pending_* field. The response
  // carries the outcome so the panel can refresh in place.
  retryPendingToken: (id: string) =>
    http<{
      ok: boolean;
      swapped: boolean;
      status: string;
      detail: string;
    }>(`/api/servers/${encodeURIComponent(id)}/retry-pending-token`, {
      method: 'POST',
    }),
  pingServer: (id: string) =>
    http<PingResult>(`/api/servers/${encodeURIComponent(id)}/ping`, { method: 'POST' }),
  // Defaults to the cached library list (last_libraries
  // on the registry row, instant). Pass ``refresh: true`` to force a
  // live re-probe (~3-15s depending on backend). The Run Job form
  // uses the cached path on every server change; a dedicated
  // "Refresh libraries" affordance can pass refresh=true.
  listServerLibraries: (id: string, refresh = false) =>
    http<LibraryDescriptor[]>(
      `/api/servers/${encodeURIComponent(id)}/libraries`
      + (refresh ? '?refresh=1' : ''),
    ),
  // List users on a server (owner + managed). Live
  // call to systemAccounts() - one round-trip per visit, no caching.
  listServerUsers: (id: string) =>
    http<ServerUsersResponse>(`/api/servers/${encodeURIComponent(id)}/users`),
  // Set or clear one entry in a server's user_display_names map.
  // Empty display_name clears the mapping (UI falls back to raw id).
  setUserDisplayName: (id: string, plex_id: string, display_name: string) =>
    http<ServerView>(`/api/servers/${encodeURIComponent(id)}/user-display-name`, {
      method: 'PATCH',
      body: JSON.stringify({ plex_id, display_name }),
    }),

  // Per-server
  // auto-tombstone opt-in toggle. Operator-gated. Defaults off; the
  // sweeper still probes + records signals regardless, but only
  // converts them into tombstones when this is enabled AND the
  // matching per-trigger toggle (auth_error / unreachable) is on
  // AND the global sweeper master switch is on.
  setServerAutoTombstone: (id: string, enabled: boolean) =>
    http<ServerView>(`/api/servers/${encodeURIComponent(id)}/auto-tombstone`, {
      method: 'PATCH',
      body: JSON.stringify({ enabled }),
    }),

  // Operator action: clear a managed user's auth-failure counter
  // without un-tombstoning anyone. Sets last_auth_status='unknown'
  // and consecutive_auth_failures=0. Used by the per-user "Reset
  // counter" affordance under Server Syncing > User Mapping.
  resetUserAuthCounter: (server_id: string, username: string) =>
    http<{
      ok: boolean; server_id: string; username: string;
      last_auth_status: string; consecutive_auth_failures: number;
    }>(
      `/api/managed-users/${encodeURIComponent(server_id)}/`
      + `${encodeURIComponent(username)}/reset-auth-counter`,
      { method: 'POST', body: JSON.stringify({}) },
    ),

  // Legacy single-server library listing - kept for one release.
  listLibraries: () => http<LibraryDescriptor[]>('/api/libraries'),
};
