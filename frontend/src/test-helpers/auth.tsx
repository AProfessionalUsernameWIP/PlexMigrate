// Reusable helper: render any UI inside a configurable AuthProvider
// so tests can flip between roles without rebuilding the context.
//
// Most components in the app read role + permissions from
// `useAuthContext`; rendering them outside an AuthProvider throws.
// This helper supplies sensible defaults (root_admin, all permissions)
// and lets per-test overrides flow through.

import type { ReactElement } from 'react';
import { render, type RenderResult } from '@testing-library/react';
import {
  AuthProvider,
  ALL_PERMISSIONS,
} from '../contexts/AuthContext';
import { ConfirmProvider } from '../components/ConfirmModal';
import type { Permission, Role } from '../api';

export interface RenderWithAuthOptions {
  role?: Role;
  realRole?: Role;
  permissions?: Permission[];
  inViewMode?: boolean;
  username?: string;
  displayName?: string | null;
}

export function renderWithAuth(
  ui: ReactElement,
  opts: RenderWithAuthOptions = {},
): RenderResult {
  const realRole = opts.realRole ?? opts.role ?? 'root_admin';
  const effectiveRole = opts.role ?? realRole;
  const permissions = opts.permissions ?? [...ALL_PERMISSIONS];
  return render(
    <AuthProvider
      username={opts.username ?? 'test_root'}
      displayName={opts.displayName ?? null}
      realRole={realRole}
      effectiveRole={effectiveRole}
      inViewMode={opts.inViewMode ?? false}
      permissions={permissions}
      lastLogin={null}
      createdAt={null}
    >
      {/* Components migrated to useConfirm() need this provider in the
          tree; bundling it here keeps per-component tests provider-free. */}
      <ConfirmProvider>{ui}</ConfirmProvider>
    </AuthProvider>,
  );
}
