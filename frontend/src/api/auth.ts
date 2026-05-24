// Auth + login-account + db-admin + managed-login-user REST helpers.
// Phase 3c extracted these out of the monolithic ``api`` const in
// ``frontend/src/api.ts``.

import { http, setAccessToken } from './core';
import type {
  AuthSession,
  AuthStatus,
  ManagedUser,
  MeResponse,
  Permission,
  Role,
  UserPermissionsResponse,
} from './types';

export const authApi = {
  // Auth. Auth is always on; ``getAuthStatus`` is used to
  // detect first-boot setup (``setup_needed: true``).
  getAuthStatus: () => http<AuthStatus>('/api/auth/status'),

  authSetup: (username: string, password: string, displayName?: string) =>
    http<AuthSession>('/api/auth/setup', {
      method: 'POST',
      body: JSON.stringify({
        username,
        password,
        ...(displayName ? { display_name: displayName } : {}),
      }),
    }),

  // Atomic two-step first-boot setup. Creates an admin account
  // AND a separate root_admin account in a single request; signs the
  // end user in as the admin. The frontend collects both credentials
  // in its wizard and submits them together so a half-complete setup
  // cannot leave the install with only one account.
  authSetupV2: (input: {
    admin_username: string;
    admin_password: string;
    admin_display_name?: string;
    root_username: string;
    root_password: string;
    root_display_name?: string;
  }) =>
    http<AuthSession>('/api/auth/setup-v2', {
      method: 'POST',
      body: JSON.stringify(input),
    }),

  // Sudo-style elevation. Caller proves their own password and
  // the backend stamps an elevation flag on the JWT's session for the
  // configured TTL (default 10 min). Elevated sessions can hit
  // require_elevation-gated endpoints (root_admin destructive writes).
  authElevate: (password: string) =>
    http<{ elevated_until: number }>('/api/auth/elevate', {
      method: 'POST',
      body: JSON.stringify({ password }),
    }),

  authElevationStatus: () =>
    http<{ elevated: boolean; elevated_until: number | null; ttl_seconds: number }>(
      '/api/auth/elevation/status',
    ),

  authElevationClear: () =>
    http<{ ok: boolean }>('/api/auth/elevation/clear', { method: 'POST' }),

  authGrantRoot: (username: string) =>
    http<{ username: string; role: string; noop: boolean }>(
      `/api/auth/users/${encodeURIComponent(username)}/grant-root`,
      { method: 'POST' },
    ),

  authRevokeRoot: (username: string, newRole: string = 'admin') =>
    http<{ username: string; role: string }>(
      `/api/auth/users/${encodeURIComponent(username)}/revoke-root`,
      { method: 'POST', body: JSON.stringify({ new_role: newRole }) },
    ),

  authLogin: (username: string, password: string) =>
    http<AuthSession>('/api/auth/login', {
      method: 'POST',
      body: JSON.stringify({ username, password }),
    }),
  authLogout: () =>
    http<{ logged_out: boolean }>('/api/auth/logout', { method: 'POST' }),

  // Silent refresh attempt. Returns the new AuthSession on success or
  // null when the refresh cookie is absent/expired/revoked. Used by
  // App.tsx on mount to detect whether the prior session can be
  // resumed without showing the login form. The http() helper does
  // its own internal refresh-and-retry on mid-session 401s and does
  // NOT need this wrapper - it talks to /api/auth/refresh directly.
  authRefresh: async (): Promise<AuthSession | null> => {
    try {
      const res = await fetch('/api/auth/refresh', {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
      });
      if (!res.ok) return null;
      const body = (await res.json()) as AuthSession;
      if (body && body.access_token) {
        setAccessToken(body.access_token);
        return body;
      }
      return null;
    } catch {
      return null;
    }
  },

  // Identity + live permissions for the current JWT.
  authMe: () => http<MeResponse>('/api/auth/me'),

  // Server-side verify of the caller's password without
  // issuing a new token. Used by the change-password forms to confirm
  // the current password before applying a change. (View Mode no
  // longer uses this endpoint - it has its own dedicated entry/exit
  // routes that bake the password check in.)
  authVerifyPassword: (password: string) =>
    http<{ valid: boolean }>('/api/auth/verify-password', {
      method: 'POST',
      body: JSON.stringify({ password }),
    }),

  // Server-side View Mode. Each call validates the password
  // server-side and updates the View Mode session map keyed by the
  // caller's JWT jti. After a successful call the frontend must
  // refetch /api/auth/me to pull the new effective_role into context.
  viewModeEnter: (target_role: Role, password: string) =>
    http<{ in_view_mode: boolean; real_role: Role; effective_role: Role }>(
      '/api/auth/view-mode/enter',
      { method: 'POST', body: JSON.stringify({ target_role, password }) },
    ),
  viewModeExit: (password: string) =>
    http<{ in_view_mode: boolean; real_role: Role; effective_role: Role }>(
      '/api/auth/view-mode/exit',
      { method: 'POST', body: JSON.stringify({ password }) },
    ),

  // Root_admin user CRUD over the login user table.
  listManagedUsers: () =>
    http<{ users: ManagedUser[] }>('/api/auth/users'),
  createManagedUser: (body: {
    username: string;
    password: string;
    role: 'viewer' | 'operator' | 'manager' | 'admin';
    display_name?: string;
  }) =>
    http<{ username: string; role: Role; display_name: string | null; id: number }>(
      '/api/auth/users',
      { method: 'POST', body: JSON.stringify(body) },
    ),
  updateManagedUser: (
    username: string,
    body: {
      role?: 'viewer' | 'operator' | 'manager' | 'admin';
      display_name?: string;
      clear_display_name?: boolean;
    },
  ) =>
    http<{ username: string; role: Role; display_name: string | null }>(
      `/api/auth/users/${encodeURIComponent(username)}`,
      { method: 'PATCH', body: JSON.stringify(body) },
    ),
  resetManagedUserPassword: (username: string, new_password: string) =>
    http<{ username: string; reset: boolean }>(
      `/api/auth/users/${encodeURIComponent(username)}/reset-password`,
      { method: 'POST', body: JSON.stringify({ new_password }) },
    ),
  deleteManagedUser: (username: string) =>
    http<{ deleted: string }>(
      `/api/auth/users/${encodeURIComponent(username)}`,
      { method: 'DELETE' },
    ),
  // Access Control: per-user permission grant/revoke layer. Root-admin only.
  getUserPermissions: (username: string) =>
    http<UserPermissionsResponse>(
      `/api/auth/users/${encodeURIComponent(username)}/permissions`,
    ),
  setUserPermissions: (
    username: string,
    body: { extra: Permission[]; revoked: Permission[] },
  ) =>
    http<UserPermissionsResponse>(
      `/api/auth/users/${encodeURIComponent(username)}/permissions`,
      { method: 'PATCH', body: JSON.stringify(body) },
    ),
  updateOwnDisplayName: (display_name: string) =>
    http<{ username: string; display_name: string | null }>(
      '/api/auth/users/me/display-name',
      { method: 'POST', body: JSON.stringify({ display_name }) },
    ),
  changeOwnPassword: (current_password: string, new_password: string) =>
    http<{ username: string; changed: boolean }>(
      '/api/auth/users/me/password',
      {
        method: 'POST',
        body: JSON.stringify({ current_password, new_password }),
      },
    ),

  // Database Admin Account (role='db_admin'). A SEPARATE row
  // from the application-login admin so the end user can authorise
  // destructive User Management writes with a credential they
  // don't use for everyday login. Always reachable regardless of
  // ``PLEXMIGRATE_AUTH_ENABLED``. Each mutating call carries the
  // current db-admin password in the body as the per-call gate.
  getDbAdminStatus: () =>
    http<{ has_admin: boolean; username: string | null }>('/api/auth/admin/status'),
  setupDbAdmin: (username: string, password: string) =>
    http<{ username: string; role: string }>('/api/auth/admin/setup', {
      method: 'POST',
      body: JSON.stringify({ username, password }),
    }),
  updateDbAdmin: (body: {
    current_password: string;
    new_username?: string;
    new_password?: string;
  }) =>
    http<{ username: string; role: string }>('/api/auth/admin/update', {
      method: 'POST',
      body: JSON.stringify(body),
    }),
  verifyDbAdmin: (username: string, password: string) =>
    http<{ valid: boolean }>('/api/auth/admin/verify', {
      method: 'POST',
      body: JSON.stringify({ username, password }),
    }),

  // Application-login admin (role='admin') management. The
  // first-boot ``/api/auth/setup`` flow is the only
  // path that CREATES this row; these endpoints let the end user
  // surface and UPDATE it from Settings → Accounts.
  getLoginAccountStatus: () =>
    http<{ has_admin: boolean; username: string | null }>('/api/auth/login-account/status'),
  updateLoginAccount: (body: {
    current_password: string;
    new_username?: string;
    new_password?: string;
  }) =>
    http<{ username: string; role: string }>('/api/auth/login-account/update', {
      method: 'POST',
      body: JSON.stringify(body),
    }),
};
