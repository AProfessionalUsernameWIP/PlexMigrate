// Per-server managed-users REST helpers extracted from the monolithic
// ``api`` const during Phase 3c. Owns reads + db_admin-gated writes
// against /api/managed-users/{server_id}/* and the global tombstone /
// global-credential edges.

import { http } from './core';
import type { GlobalTombstone, ServerManagedUser } from './types';

export const managedUsersApi = {
  // Per-server managed users (the User Management sub-tab
  // under Servers). Reads are JWT-only (end user+); writes carry
  // db_admin credentials in the body and the backend re-validates
  // them on every call.
  // The list endpoint accepts ``include_hidden`` so the
  // 'Show hidden' toggle and the ServersPanel diff effect can see
  // tombstoned rows without losing the default-filter behaviour.
  listServerManagedUsers: (serverId: string, includeHidden = false) =>
    http<{ server_id: string; users: ServerManagedUser[] }>(
      `/api/managed-users/${encodeURIComponent(serverId)}` +
      (includeHidden ? '?include_hidden=true' : ''),
    ),
  getServerManagedUser: (serverId: string, username: string) =>
    http<ServerManagedUser>(
      `/api/managed-users/${encodeURIComponent(serverId)}/${encodeURIComponent(username)}`,
    ),
  updateServerManagedUser: (
    serverId: string,
    username: string,
    body: {
      db_admin_username: string;
      db_admin_password: string;
      display_name?: string;
      clear_display_name?: boolean;
      auth_token?: string;
      clear_auth_token?: boolean;
      plex_home_pin?: string;
      clear_plex_home_pin?: boolean;
      // Migration v16: per-backend PIN columns.
      emby_easy_pin?: string;
      clear_emby_easy_pin?: boolean;
      jellyfin_easy_pin?: string;
      clear_jellyfin_easy_pin?: boolean;
      service_password?: string;
      clear_service_password?: boolean;
      // When true, PIN writes in this PATCH also propagate
      // to cross-backend identity_map links. Same-backend propagation
      // always fires for PIN kinds; this flag only governs cross-
      // backend fan-out. The User Management "Save & apply across
      // backends" button passes true; the standard Save button omits
      // or passes false.
      cross_backend_pin?: boolean;
      // Per-server tombstone. true = hide on this server,
      // false = unhide on this server. Omit to leave alone.
      tombstoned?: boolean;
    },
  ) =>
    http<ServerManagedUser>(
      `/api/managed-users/${encodeURIComponent(serverId)}/${encodeURIComponent(username)}`,
      { method: 'PATCH', body: JSON.stringify(body) },
    ),
  // DELETE is an alias for the per-server hide path.
  // The row stays in the DB with ``tombstoned=1`` and credentials
  // preserved; the response carries ``hidden_scope='server'``.
  hideServerManagedUser: (
    serverId: string,
    username: string,
    body: { db_admin_username: string; db_admin_password: string },
  ) =>
    http<{ hidden: string; server_id: string; hidden_scope: 'server' }>(
      `/api/managed-users/${encodeURIComponent(serverId)}/${encodeURIComponent(username)}`,
      { method: 'DELETE', body: JSON.stringify(body) },
    ),
  syncServerManagedUsers: (serverId: string) =>
    http<{ server_id: string; synced: number; source_error: string | null }>(
      `/api/managed-users/${encodeURIComponent(serverId)}/sync`,
      { method: 'POST' },
    ),
  // Per-user "Sync this user" affordance
  // on the User Management user-detail panel. Captures one user's
  // auth_token via AuthenticateByName (Emby/Jellyfin) or the PIN-aware
  // flow (Plex) without touching any other row on the server.
  captureUserToken: (serverId: string, username: string) =>
    http<{
      server_id: string;
      username: string;
      token_capture: {
        captured: number;
        skipped_existing: number;
        throttled: boolean;
        errors: string[];
      };
    }>(
      `/api/managed-users/${encodeURIComponent(serverId)}/${encodeURIComponent(username)}/capture-token`,
      { method: 'POST' },
    ),
  // Root-admin-only credential reveal.
  // Three gates server-side (require_role("root_admin") + the signed-in
  // root admin's own password re-check + standard db_admin verify).
  // Any failure returns 401 with an identical detail string so an
  // attacker can't probe which gate rejected them. Successful reveals
  // emit a dedicated db_access_log audit event.
  revealManagedUserCredentials: (
    serverId: string,
    username: string,
    body: {
      db_admin_username: string;
      db_admin_password: string;
      root_admin_password: string;
    },
  ) =>
    http<{
      auth_token: string | null;
      plex_home_pin: string | null;
      emby_easy_pin: string | null;
      jellyfin_easy_pin: string | null;
    }>(
      `/api/managed-users/${encodeURIComponent(serverId)}/${encodeURIComponent(username)}/reveal-credentials`,
      { method: 'POST', body: JSON.stringify(body) },
    ),
  // Global tombstones. Username-keyed across every server.
  listGlobalTombstones: () =>
    http<{ usernames: GlobalTombstone[] }>('/api/managed-users/global-tombstones'),
  addGlobalTombstone: (
    body: { db_admin_username: string; db_admin_password: string; username: string },
  ) =>
    http<{ username: string; tombstoned: true }>(
      '/api/managed-users/global-tombstones',
      { method: 'POST', body: JSON.stringify(body) },
    ),
  removeGlobalTombstone: (
    username: string,
    body: { db_admin_username: string; db_admin_password: string },
  ) =>
    http<{ username: string; tombstoned: false }>(
      `/api/managed-users/global-tombstones/${encodeURIComponent(username)}`,
      { method: 'DELETE', body: JSON.stringify(body) },
    ),

  // Write one credential to every server where this
  // username has a managed-user row. db_admin gated; empty plaintext
  // clears the credential on every match. Returns
  // ``{applied, missing}`` so the UI can surface how many rows took
  // the write.
  setGlobalManagedUserCredential: (
    username: string,
    body: {
      db_admin_username: string;
      db_admin_password: string;
      kind:
        | 'auth_token'
        | 'plex_home_pin'
        | 'emby_easy_pin'
        | 'jellyfin_easy_pin'
        | 'service_password';
      plaintext: string;
      // Forwarded to set_managed_user_credential downstream. PIN kinds
      // only; ignored for auth_token / service_password.
      cross_backend_pin?: boolean;
      // Optional backend filter on the sweep. UI's
      // default "Save on every server" passes the origin row's
      // service_type; the "Include other backends" override omits
      // the field (sweep walks every backend).
      service_type_filter?: 'plex' | 'emby' | 'jellyfin';
    },
  ) =>
    http<{ username: string; kind: string; applied: number; missing: number }>(
      `/api/managed-users/global-credential/${encodeURIComponent(username)}`,
      { method: 'POST', body: JSON.stringify(body) },
    ),
};
