// Settings REST helpers extracted from the monolithic ``api`` const
// during Phase 3c.

import { http } from './core';
import type { SettingsView } from './types';

export const settingsApi = {
  getSettings: () => http<SettingsView>('/api/settings'),
  saveSettings: (patch: Partial<SettingsView & { plex_token: string }>) =>
    http<SettingsView>('/api/settings', { method: 'POST', body: JSON.stringify(patch) }),

  // Audit log on/off. db_admin-gated server-side: the body must carry
  // the separate db_admin username + password (not the JWT). The
  // backend writes a self-documenting "DISABLED by <user>" or
  // "RE-ENABLED by <user>" line in the audit log itself across the
  // transition.
  toggleAuditLog: (body: {
    db_admin_username: string;
    db_admin_password: string;
    enabled: boolean;
  }) =>
    http<{ audit_log_enabled: boolean }>('/api/settings/audit-log-toggle', {
      method: 'POST',
      body: JSON.stringify(body),
    }),
};
