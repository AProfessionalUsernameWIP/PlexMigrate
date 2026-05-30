// Database export/import + per-server browser + user identity map
// helpers extracted from the monolithic ``api`` const during Phase 3c.
//
// Owns:
//   * /api/database/* run-history table export + import + per-server
//     pivot.
//   * /api/users/* identity map CRUD + rerun-auto-link.
//   * /api/db-browser/* read-only schema + rows viewer.

import { http } from './core';
import type {
  DatabaseInstance,
  DatabaseInstanceMetadata,
  DatabaseInstanceSchema,
  DatabaseTableRowsPage,
  DatabaseTypeSummary,
  UserIdentityMap,
} from './types';

export const databaseApi = {
  // Settings Run History database export / import. Per-table JSON
  // dumps for backup + interop; replace-only import behind typed
  // REPLACE confirmation. Per-server pivot for inspecting one
  // server's identity-related rows in isolation.
  listDatabaseTables: () =>
    http<{ tables: Array<{
      table_id: string;
      db_file: string;
      table_name: string;
      row_count: number | null;
      available: boolean;
      per_server: boolean;
    }> }>('/api/database/tables'),

  exportDatabaseTable: (table_id: string) =>
    http<Record<string, unknown>>(
      `/api/database/export/${encodeURIComponent(table_id)}`,
    ),

  exportDatabaseArchive: () =>
    http<Record<string, unknown>>('/api/database/export-all'),

  importDatabaseTable: (table_id: string, payload: Record<string, unknown>) =>
    http<{ table_id: string; deleted: number; inserted: number }>(
      `/api/database/import/${encodeURIComponent(table_id)}`,
      {
        method: 'POST',
        body: JSON.stringify({ confirm: 'REPLACE', payload }),
      },
    ),

  importDatabaseArchive: (payload: Record<string, unknown>) =>
    http<{
      per_table: Record<string, { deleted?: number; inserted?: number; error?: string }>;
      total_deleted: number;
      total_inserted: number;
      errors: string[];
    }>('/api/database/import-archive', {
      method: 'POST',
      body: JSON.stringify({ confirm: 'REPLACE', payload }),
    }),

  listDatabasePerServer: (server_id: string) =>
    http<{ server_id: string; tables: Array<{
      table_id: string;
      db_file: string;
      table_name: string;
      row_count: number;
    }> }>(`/api/database/per-server/${encodeURIComponent(server_id)}`),

  exportDatabasePerServer: (server_id: string) =>
    http<Record<string, unknown>>(
      `/api/database/export-per-server/${encodeURIComponent(server_id)}`,
    ),

  // Ad-hoc single-user copy +
  // cross-server identity mapping.
  copyUserToDestination: (body: {
    source_server_id: string;
    target_server_id: string;
    source_user_handle: string;
    target_username: string;
    temp_password: string;
    target_user_policy?: Record<string, unknown> | null;
  }) =>
    http<{
      backend_user_id: string;
      target_username: string;
      source_user_handle: string;
    }>('/api/users/copy_to_destination', {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  addUserIdentityMap: (body: {
    server_a_id: string;
    user_a_handle: string;
    server_b_id: string;
    user_b_handle: string;
    source?: 'manual' | 'auto_copy';
  }) =>
    http<{ id: number | null; duplicate: boolean }>(
      '/api/users/identity_map',
      { method: 'POST', body: JSON.stringify({ source: 'manual', ...body }) },
    ),

  listUserIdentityMaps: () =>
    http<{ maps: UserIdentityMap[] }>('/api/users/identity_map'),

  deleteUserIdentityMap: (map_id: number) =>
    http<{ removed: boolean; id: number }>(
      `/api/users/identity_map/${map_id}`,
      { method: 'DELETE' },
    ),

  // End user-triggered rerun of
  // the backend_user_id auto-link helper. The helper also fires
  // automatically after every managed-users sync; this button is
  // for end users who just added a manual mapping or registered a
  // new server and want immediate cross-server detection without
  // waiting for the next sync.
  rerunAutoLinkIdentityMap: () =>
    http<{ pairs_written: number; pairs_skipped_duplicate: number; groups_seen: number }>(
      '/api/users/identity_map/rerun-auto-link',
      { method: 'POST' },
    ),

  // ── Databases viewer ──────────────────────────────────────────────
  //
  // Root-admin only at the route layer. The frontend mirrors the
  // server's read-only contract: no edit endpoints exist and none
  // are exposed here. Cell values are pre-formatted server-side so
  // the UI just renders ``display``.

  dbBrowserListDatabases: () =>
    http<{ databases: DatabaseTypeSummary[] }>('/api/db-browser/databases'),

  dbBrowserListInstances: (db_type: string) =>
    http<{ instances: DatabaseInstance[] }>(
      `/api/db-browser/${encodeURIComponent(db_type)}/instances`,
    ),

  dbBrowserMetadata: (db_type: string, instance_id: string) =>
    http<DatabaseInstanceMetadata>(
      `/api/db-browser/${encodeURIComponent(db_type)}/${encodeURIComponent(instance_id)}/metadata`,
    ),

  dbBrowserSchema: (db_type: string, instance_id: string) =>
    http<DatabaseInstanceSchema>(
      `/api/db-browser/${encodeURIComponent(db_type)}/${encodeURIComponent(instance_id)}/schema`,
    ),

  dbBrowserRows: (
    db_type: string,
    instance_id: string,
    table: string,
    opts: {
      limit?: number;
      offset?: number;
      filter_column?: string;
      filter_value?: string;
    } = {},
  ) => {
    const params = new URLSearchParams();
    if (opts.limit !== undefined) params.set('limit', String(opts.limit));
    if (opts.offset !== undefined) params.set('offset', String(opts.offset));
    if (opts.filter_column) params.set('filter_column', opts.filter_column);
    if (opts.filter_value !== undefined) params.set('filter_value', opts.filter_value);
    const qs = params.toString();
    return http<DatabaseTableRowsPage>(
      `/api/db-browser/${encodeURIComponent(db_type)}/${encodeURIComponent(instance_id)}/tables/${encodeURIComponent(table)}/rows${qs ? `?${qs}` : ''}`,
    );
  },
};
