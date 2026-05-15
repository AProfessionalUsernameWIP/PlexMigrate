// PR-A3 - AuthContext + permission map + role descriptions.
//
// Single source of truth for the four login roles, the permission set
// each one carries, and the human-readable descriptions surfaced by
// the User Accounts explorer (PR-A5). Mirrors the backend's
// ROLE_PERMISSIONS in ``server/auth_router.py`` exactly - keep them in
// sync if you change either side. The backend is authoritative;
// changes here only affect UI gating.

import { createContext, useContext, useMemo, type ReactNode } from 'react';
import type { Permission, Role } from '../api';


export const ALL_PERMISSIONS: Permission[] = [
  'dashboard.view',
  'servers.view', 'servers.edit',
  'jobs.start', 'jobs.stop',
  'schedules.view', 'schedules.edit',
  'logs.view', 'exports.view',
  'settings.edit', 'settings.tunables',
  'users.manage', 'db_admin.access',
  'sync.view', 'sync.edit',
];

// Permission bundle for ``admin`` — everything except settings.tunables.
// Mirrors the backend's ``_ADMIN_PERMS`` in server/auth_router.py:
// admin can edit normal settings but cannot touch the System Tunables
// page (HTTP timeouts, JWT TTL, SQLite busy timeouts, etc.) because
// wrong values there can lock every user out.
const ADMIN_PERMISSIONS: Permission[] = ALL_PERMISSIONS.filter(
  (p) => p !== 'settings.tunables',
);


/**
 * Permission set per role. Viewer is intentionally narrow - three
 * tabs only (Dashboard, Servers read-only, Settings → Account). No
 * logs.view, no exports.view, no schedules.view.
 *
 * db_admin is not a login role and doesn't appear here.
 */
export const ROLE_PERMISSIONS: Record<Role, Permission[]> = {
  viewer:     ['dashboard.view', 'servers.view'],
  operator:   ['dashboard.view', 'servers.view',
               'logs.view', 'exports.view',
               'jobs.start', 'schedules.view'],
  manager:    ['dashboard.view', 'servers.view',
               'logs.view', 'exports.view',
               'jobs.start', 'jobs.stop',
               'schedules.view', 'schedules.edit',
               'sync.view'],
  // ``admin`` carries every permission EXCEPT settings.tunables.
  // Mirrors backend ROLE_PERMISSIONS in server/auth_router.py exactly.
  // Per-row guards on the User Accounts explorer prevent admin from
  // modifying the root_admin row.
  admin:      ADMIN_PERMISSIONS,
  root_admin: ALL_PERMISSIONS,
};


/**
 * Role ranking for "minimum role" comparisons. Higher = more
 * privilege. db_admin is intentionally absent - it's not a login
 * role and never participates in hierarchy comparisons.
 */
export const ROLE_RANK: Record<Role, number> = {
  viewer: 0,
  operator: 1,
  manager: 2,
  admin: 3,
  root_admin: 4,
};


export function roleAtLeast(actual: Role, minimum: Role): boolean {
  return ROLE_RANK[actual] >= ROLE_RANK[minimum];
}


// ── Human-readable role descriptions (PR-A5 User Accounts explorer) ─────────
//
// The detail view shows what a role can / cannot do as a bulleted
// list of plain-English statements, not raw permission strings. The
// "can" list is built from ROLE_PERMISSIONS via PERMISSION_LABELS;
// the "cannot" list is the complement.

export const PERMISSION_LABELS: Record<Permission, string> = {
  'dashboard.view':  'View the dashboard and live job status',
  'servers.view':    'View the Servers tab',
  'servers.edit':    'Add, edit, and remove registered servers',
  'jobs.start':      'Start new snapshot, import, and direct-transfer jobs',
  'jobs.stop':       'Stop running jobs',
  'schedules.view':  'View scheduled snapshots',
  'schedules.edit':  'Create, edit, and delete scheduled snapshots',
  'logs.view':       'Browse run logs',
  'exports.view':    'Browse and download export files',
  'settings.edit':   'Edit system settings (paths, workers, defaults)',
  'settings.tunables': 'Edit infrastructure tunables (HTTP timeouts, JWT TTL, SQLite busy timeouts, etc.) — root admin only',
  'users.manage':    'Create, edit, and delete other user accounts',
  'db_admin.access': 'Access the Database Admin Account credential',
  'sync.view':       'View the Sync tab',
  'sync.edit':       'Configure sync links and resolve conflicts',
};


export interface PermissionSummary {
  can: string[];
  cannot: string[];
}

export function permissionSummaryForRole(role: Role): PermissionSummary {
  const granted = new Set(ROLE_PERMISSIONS[role]);
  const can: string[] = [];
  const cannot: string[] = [];
  for (const p of ALL_PERMISSIONS) {
    if (granted.has(p)) can.push(PERMISSION_LABELS[p]);
    else cannot.push(PERMISSION_LABELS[p]);
  }
  return { can, cannot };
}


// ── Context shape ──────────────────────────────────────────────────────────

export interface AuthContextValue {
  username: string;
  displayName: string | null;
  // ``role`` is an alias for ``effectiveRole`` kept for compatibility
  // with the many panels that already read ``role`` for UI gating.
  // New code should prefer the explicit ``effectiveRole`` /
  // ``realRole`` names.
  role: Role;
  realRole: Role;
  effectiveRole: Role;
  // True when the user has an active server-side View Mode session.
  // Mirrors the backend's /me ``in_view_mode`` field.
  inViewMode: boolean;
  permissions: Permission[];
  // Profile metadata for Account Settings. ``lastLogin`` is the
  // unix timestamp (seconds) of the most recent login, written by
  // ``/api/auth/login`` and ``/api/auth/setup``. ``createdAt`` is
  // the account's creation timestamp. Both can be null (e.g. a
  // user that has never logged in won't have last_login set).
  lastLogin: number | null;
  createdAt: number | null;
  effectivePermissions: Permission[];
  // Convenience: refresh /me into the context. Used after a
  // self-display-name edit AND after every View Mode enter/exit so
  // the topbar + tab gating reflect the new effective role without
  // a page reload.
  refreshMe?: () => Promise<void>;
}

const AuthContext = createContext<AuthContextValue | null>(null);


export interface AuthProviderProps {
  username: string;
  displayName: string | null;
  // The backend's /me response feeds these props directly. App.tsx is
  // the only caller; it maps real_role/effective_role/in_view_mode
  // from the MeResponse into the props below.
  realRole: Role;
  effectiveRole: Role;
  inViewMode: boolean;
  permissions: Permission[];
  lastLogin: number | null;
  createdAt: number | null;
  refreshMe?: () => Promise<void>;
  children: ReactNode;
}

export function AuthProvider(props: AuthProviderProps) {
  const value = useMemo<AuthContextValue>(() => {
    // Backend-provided effective permissions take precedence — they
    // already incorporate per-user grants and revokes from the new
    // Access Control feature. Fall back to the role baseline only
    // when /me returns an empty list (e.g. an older backend that
    // doesn't ship the effective_permissions resolver yet).
    const effectivePermissions = (props.permissions && props.permissions.length > 0)
      ? props.permissions
      : (ROLE_PERMISSIONS[props.effectiveRole] ?? []);
    return {
      username: props.username,
      displayName: props.displayName,
      role: props.effectiveRole,
      realRole: props.realRole,
      effectiveRole: props.effectiveRole,
      inViewMode: props.inViewMode,
      permissions: props.permissions,
      lastLogin: props.lastLogin,
      createdAt: props.createdAt,
      effectivePermissions,
      refreshMe: props.refreshMe,
    };
  }, [props.username, props.displayName, props.realRole, props.effectiveRole,
      props.inViewMode, props.permissions,
      props.lastLogin, props.createdAt, props.refreshMe]);

  return <AuthContext.Provider value={value}>{props.children}</AuthContext.Provider>;
}


export function useAuthContext(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (ctx === null) {
    throw new Error(
      'useAuthContext must be used inside <AuthProvider>. ' +
      'This indicates a render path that bypassed the auth gate.'
    );
  }
  return ctx;
}
