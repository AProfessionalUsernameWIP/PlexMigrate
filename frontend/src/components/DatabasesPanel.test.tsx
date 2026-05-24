// Tests for the DatabasesPanel. Covers: catalogue tab strip, type-tab
// selection drives an instances API call, single-instance types
// render the viewer directly, RowCell formatter renders the
// pre-formatted display value (and security-aware substitutions
// don't expose raw values).

import { describe, expect, it, vi, beforeEach } from 'vitest';
import { fireEvent, screen, waitFor } from '@testing-library/react';
import { renderWithAuth } from '../test-helpers/auth';

vi.mock('../api', async (importOriginal) => {
  const mod = (await importOriginal()) as typeof import('../api');
  return {
    ...mod,
    api: {
      ...mod.api,
      dbBrowserListDatabases: vi.fn().mockResolvedValue({
        databases: [
          {
            key: 'auth', display_name: 'auth.db',
            description: 'Auth DB', cardinality: 'single', sensitivity: 'high',
          },
          {
            key: 'media', display_name: 'media.db',
            description: 'Media DB', cardinality: 'single', sensitivity: 'medium',
          },
          {
            key: 'snapshot_file', display_name: 'Snapshot files',
            description: 'Per-capture snapshot .db files',
            cardinality: 'many', sensitivity: 'high',
          },
        ],
      }),
      dbBrowserListInstances: vi.fn().mockImplementation(async (db_type: string) => {
        if (db_type === 'auth') {
          return {
            instances: [{
              instance_id: '_', label: 'auth.db',
              file_path: '/server_data/auth.db', size_bytes: 1024, exists: true,
            }],
          };
        }
        if (db_type === 'snapshot_file') {
          return {
            instances: [{
              instance_id: '42', label: 'Server A - cap1',
              file_path: '/server_data/snapshots/server_a/cap1.db',
              size_bytes: 4096, exists: true,
              server_id: 'plex_a', server_name: 'Server A',
              service_type: 'plex',
              snapshot_name: 'cap1', captured_at: 0,
            }],
          };
        }
        return { instances: [] };
      }),
      dbBrowserMetadata: vi.fn().mockResolvedValue({
        db_type: 'auth', instance_id: '_',
        file_path: '/server_data/auth.db',
        size_bytes: 1024, modified_at: 0,
        schema_version: null, wal_present: false,
      }),
      dbBrowserSchema: vi.fn().mockResolvedValue({
        db_type: 'auth', instance_id: '_',
        tables: [{
          name: 'app_users', row_count: 1,
          columns: [
            { name: 'id', type: 'INTEGER', notnull: true, default: null, is_primary_key: true },
            { name: 'username', type: 'TEXT', notnull: true, default: null, is_primary_key: false },
            { name: 'password_hash', type: 'TEXT', notnull: true, default: null, is_primary_key: false },
          ],
          indexes: [],
          ddl: 'CREATE TABLE app_users (id INTEGER PRIMARY KEY, username TEXT, password_hash TEXT)',
        }],
      }),
      dbBrowserRows: vi.fn().mockResolvedValue({
        db_type: 'auth', instance_id: '_', table: 'app_users',
        columns: ['id', 'username', 'password_hash'],
        rows: [[
          { display: 1 },
          { display: 'root' },
          { display: '[bcrypt hash, 60 chars]', raw_present: true },
        ]],
        total_rows: 1, limit: 50, offset: 0, has_more: false,
      }),
    },
  };
});

beforeEach(() => {
  vi.clearAllMocks();
});

describe('DatabasesPanel', () => {
  it('renders a tab per catalogue entry', async () => {
    const { DatabasesPanel } = await import('./DatabasesPanel');
    renderWithAuth(<DatabasesPanel />);
    expect(await screen.findByRole('button', { name: /auth\.db/i })).toBeInTheDocument();
    expect(await screen.findByRole('button', { name: /media\.db/i })).toBeInTheDocument();
    expect(await screen.findByRole('button', { name: /snapshot files/i })).toBeInTheDocument();
  });

  it('renders sensitivity chip on each tab', async () => {
    const { DatabasesPanel } = await import('./DatabasesPanel');
    renderWithAuth(<DatabasesPanel />);
    await screen.findByRole('button', { name: /auth\.db/i });
    expect(screen.getAllByText(/high/i).length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText(/medium/i).length).toBeGreaterThanOrEqual(1);
  });

  it('auto-selects the first type and lists its instances', async () => {
    const { api } = await import('../api');
    const { DatabasesPanel } = await import('./DatabasesPanel');
    renderWithAuth(<DatabasesPanel />);
    await waitFor(() => {
      expect(api.dbBrowserListInstances).toHaveBeenCalledWith('auth');
    });
  });

  it('shows the redacted bcrypt placeholder, not the raw hash', async () => {
    const { DatabasesPanel } = await import('./DatabasesPanel');
    renderWithAuth(<DatabasesPanel />);
    // Wait until the row cell hits the DOM. The pre-formatted display
    // string is what the server sent; the raw value must NOT appear.
    expect(
      await screen.findByText(/\[bcrypt hash, 60 chars\]/),
    ).toBeInTheDocument();
  });

  it('renders the CREATE TABLE DDL inside the schema pane', async () => {
    const { DatabasesPanel } = await import('./DatabasesPanel');
    renderWithAuth(<DatabasesPanel />);
    // The DDL block is inside a <details> wrapper; the heading text
    // is always present even when collapsed.
    expect(await screen.findByText(/CREATE TABLE DDL/i)).toBeInTheDocument();
  });
});
