// Settings > Databases sub-tab (root-admin only).
//
// Plan[DATABASES-VIEWER]-2026-05-16. Read-only browser for every
// SQLite database the app creates: auth.db, media.db, snapshots.db,
// run_timings.db, playlist_cache.db, plus the per-capture snapshot
// .db files. Schema + paginated data view; encrypted columns and
// other sensitive cells are pre-redacted server-side.
//
// Component tree:
//
//   DatabasesPanel
//     ├── top-level tab strip (one tab per DatabaseTypeSummary)
//     └── DatabaseTypeView (renders the selected type's body)
//          ├── InstancePicker (when cardinality === 'many')
//          └── DatabaseInstanceView
//               ├── metadata header
//               ├── SchemaPane (per-table column lists + DDL)
//               └── DataBrowser
//                    └── RowCell (per-cell formatter)

import { useEffect, useMemo, useState } from 'react';
import {
  api,
  DatabaseCell,
  DatabaseInstance,
  DatabaseInstanceMetadata,
  DatabaseInstanceSchema,
  DatabaseTable,
  DatabaseTableRowsPage,
  DatabaseTypeSummary,
} from '../api';


export function DatabasesPanel() {
  const [types, setTypes] = useState<DatabaseTypeSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [activeKey, setActiveKey] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    api.dbBrowserListDatabases()
      .then((r) => {
        if (cancelled) return;
        setTypes(r.databases);
        if (r.databases.length > 0 && activeKey === null) {
          setActiveKey(r.databases[0].key);
        }
      })
      .catch((e) => { if (!cancelled) setError(String(e)); });
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  if (error) {
    return (
      <div className="panel">
        <div className="banner error">Could not load databases: {error}</div>
      </div>
    );
  }
  if (types === null) {
    return <div className="panel"><div className="empty">Loading databases…</div></div>;
  }

  const active = types.find((t) => t.key === activeKey) || null;

  return (
    <>
      <div className="panel">
        <h2 style={{ marginTop: 0 }}>Databases</h2>
        <span className="help" style={{ display: 'block', color: 'var(--text-dim)', fontSize: 12 }}>
          Read-only view of every SQLite database the app creates.
          Sensitive columns (encrypted tokens, bcrypt hashes, refresh
          token ids) are pre-redacted before they reach this page.
        </span>
        <nav className="tabs sub-tabs" style={{ marginTop: 12, flexWrap: 'wrap' }}>
          {types.map((t) => (
            <button
              key={t.key}
              className={t.key === activeKey ? 'active' : ''}
              onClick={() => setActiveKey(t.key)}
              title={t.description}
            >
              {t.display_name}
              <span
                style={{
                  marginLeft: 6,
                  fontSize: 9,
                  padding: '1px 5px',
                  borderRadius: 2,
                  background: t.sensitivity === 'high'
                    ? 'rgba(217, 119, 6, 0.18)'
                    : t.sensitivity === 'medium'
                    ? 'rgba(80, 150, 200, 0.15)'
                    : 'rgba(127, 127, 127, 0.12)',
                }}
              >
                {t.sensitivity}
              </span>
            </button>
          ))}
        </nav>
      </div>

      {active && <DatabaseTypeView key={active.key} type={active} />}
    </>
  );
}


// ── DatabaseTypeView: instance picker (when many) + viewer ──────────────────

function DatabaseTypeView({ type }: { type: DatabaseTypeSummary }) {
  const [instances, setInstances] = useState<DatabaseInstance[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setInstances(null);
    setError(null);
    setSelectedId(null);
    api.dbBrowserListInstances(type.key)
      .then((r) => {
        if (cancelled) return;
        setInstances(r.instances);
        if (r.instances.length > 0) {
          setSelectedId(r.instances[0].instance_id);
        }
      })
      .catch((e) => { if (!cancelled) setError(String(e)); });
    return () => { cancelled = true; };
  }, [type.key]);

  if (error) {
    return (
      <div className="panel">
        <div className="banner error">Could not load instances: {error}</div>
      </div>
    );
  }
  if (instances === null) {
    return <div className="panel"><div className="empty">Loading instances…</div></div>;
  }
  if (instances.length === 0) {
    return (
      <div className="panel">
        <div className="empty">
          No instances yet. {type.cardinality === 'single'
            ? 'The database file has not been created on this install.'
            : 'No snapshot files have been captured yet.'}
        </div>
      </div>
    );
  }

  const selected = instances.find((i) => i.instance_id === selectedId) || instances[0];

  return (
    <>
      {type.cardinality === 'many' && (
        <InstancePicker
          instances={instances}
          selectedId={selected.instance_id}
          onSelect={setSelectedId}
        />
      )}
      <DatabaseInstanceView
        key={`${type.key}/${selected.instance_id}`}
        dbType={type.key}
        instance={selected}
      />
    </>
  );
}


// ── InstancePicker (server-first grouping for snapshot files) ───────────────

function InstancePicker({
  instances,
  selectedId,
  onSelect,
}: {
  instances: DatabaseInstance[];
  selectedId: string;
  onSelect: (id: string) => void;
}) {
  // Group by server_name for snapshot files; the end user can scan
  // by server and pick the capture they want. Falls back to a flat
  // list when none of the instances carry server metadata.
  const grouped = useMemo(() => {
    const buckets = new Map<string, DatabaseInstance[]>();
    for (const inst of instances) {
      const key = inst.server_name || '(unknown server)';
      if (!buckets.has(key)) buckets.set(key, []);
      buckets.get(key)!.push(inst);
    }
    return Array.from(buckets.entries()).sort(([a], [b]) => a.localeCompare(b));
  }, [instances]);

  return (
    <div className="panel" style={{ marginTop: 8 }}>
      <div style={{ fontSize: 12, color: 'var(--text-dim)', marginBottom: 6 }}>
        Pick a capture to view ({instances.length} total):
      </div>
      <div style={{ maxHeight: 280, overflowY: 'auto', display: 'flex', flexDirection: 'column', gap: 6 }}>
        {grouped.map(([serverName, group]) => (
          <div key={serverName}>
            <div style={{ fontSize: 11, fontWeight: 'bold', color: 'var(--text-dim)', marginBottom: 4 }}>
              {serverName} ({group.length})
            </div>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 2, marginLeft: 8 }}>
              {group.map((inst) => (
                <button
                  key={inst.instance_id}
                  className={inst.instance_id === selectedId ? 'active' : ''}
                  onClick={() => onSelect(inst.instance_id)}
                  style={{
                    textAlign: 'left',
                    padding: '4px 8px',
                    fontSize: 12,
                    background: inst.instance_id === selectedId
                      ? 'rgba(80, 150, 200, 0.18)'
                      : 'transparent',
                  }}
                >
                  {inst.label} <span style={{ color: 'var(--text-dim)', fontSize: 11 }}>
                    ({formatBytes(inst.size_bytes)})
                  </span>
                </button>
              ))}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}


// ── DatabaseInstanceView: metadata + schema + data browser ──────────────────

function DatabaseInstanceView({
  dbType,
  instance,
}: {
  dbType: string;
  instance: DatabaseInstance;
}) {
  const [metadata, setMetadata] = useState<DatabaseInstanceMetadata | null>(null);
  const [schema, setSchema] = useState<DatabaseInstanceSchema | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [activeTable, setActiveTable] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setError(null);
    setMetadata(null);
    setSchema(null);
    setActiveTable(null);
    Promise.all([
      api.dbBrowserMetadata(dbType, instance.instance_id),
      api.dbBrowserSchema(dbType, instance.instance_id),
    ])
      .then(([m, s]) => {
        if (cancelled) return;
        setMetadata(m);
        setSchema(s);
        if (s.tables.length > 0) {
          setActiveTable(s.tables[0].name);
        }
      })
      .catch((e) => { if (!cancelled) setError(String(e)); });
    return () => { cancelled = true; };
  }, [dbType, instance.instance_id]);

  if (error) {
    return (
      <div className="panel">
        <div className="banner error">Could not load database: {error}</div>
      </div>
    );
  }
  if (metadata === null || schema === null) {
    return <div className="panel"><div className="empty">Loading…</div></div>;
  }

  return (
    <>
      <div className="panel" style={{ marginTop: 8 }}>
        <h3 style={{ marginTop: 0, fontSize: 14 }}>{instance.label}</h3>
        <table className="table" style={{ fontSize: 12 }}>
          <tbody>
            <tr><td style={{ color: 'var(--text-dim)' }}>File path</td>
                <td><code style={{ fontSize: 11 }}>{metadata.file_path}</code></td></tr>
            <tr><td style={{ color: 'var(--text-dim)' }}>Size</td>
                <td>{formatBytes(metadata.size_bytes)}</td></tr>
            <tr><td style={{ color: 'var(--text-dim)' }}>Schema version</td>
                <td>{metadata.schema_version ?? <em style={{ color: 'var(--text-dim)' }}>n/a</em>}</td></tr>
            <tr><td style={{ color: 'var(--text-dim)' }}>Last modified</td>
                <td>{new Date(metadata.modified_at * 1000).toISOString()}</td></tr>
            <tr><td style={{ color: 'var(--text-dim)' }}>WAL sidecars</td>
                <td>{metadata.wal_present ? 'yes' : 'no'}</td></tr>
          </tbody>
        </table>
      </div>

      <SchemaPane
        tables={schema.tables}
        activeTable={activeTable}
        onSelect={setActiveTable}
      />

      {activeTable && (
        <DataBrowser
          key={`${dbType}/${instance.instance_id}/${activeTable}`}
          dbType={dbType}
          instanceId={instance.instance_id}
          table={schema.tables.find((t) => t.name === activeTable)!}
        />
      )}
    </>
  );
}


// Schema-pane bounded row count threshold. Mirrors
// server/db_browser.py:ROW_COUNT_THRESHOLD. Counts at or above this
// value are reported as ">=N" rather than an exact number; the
// bounded-count query stops scanning at N+1 rows so very large
// tables don't pay a 100ms+ COUNT(*) scan on every schema load.
const DB_ROW_COUNT_THRESHOLD = 10000;


// ── SchemaPane: per-table picker + column / index / DDL view ───────────────

function SchemaPane({
  tables,
  activeTable,
  onSelect,
}: {
  tables: DatabaseTable[];
  activeTable: string | null;
  onSelect: (table: string) => void;
}) {
  const active = tables.find((t) => t.name === activeTable);
  return (
    <div className="panel" style={{ marginTop: 8 }}>
      <h3 style={{ marginTop: 0, fontSize: 14 }}>Schema</h3>
      <nav className="tabs sub-tabs" style={{ flexWrap: 'wrap', marginBottom: 8 }}>
        {tables.map((t) => (
          <button
            key={t.name}
            className={t.name === activeTable ? 'active' : ''}
            onClick={() => onSelect(t.name)}
            style={{ fontSize: 12 }}
          >
            {t.name}
            <span style={{ color: 'var(--text-dim)', marginLeft: 4, fontSize: 10 }}>
              ({t.row_count < 0 ? '?' : t.row_count >= DB_ROW_COUNT_THRESHOLD ? `≥${DB_ROW_COUNT_THRESHOLD}` : t.row_count})
            </span>
          </button>
        ))}
      </nav>

      {active && (
        <details open>
          <summary style={{ cursor: 'pointer', fontSize: 12, marginBottom: 6 }}>
            <strong>Columns</strong> ({active.columns.length})
          </summary>
          <table className="table" style={{ fontSize: 12 }}>
            <thead>
              <tr>
                <th style={{ textAlign: 'left' }}>Name</th>
                <th style={{ textAlign: 'left' }}>Type</th>
                <th style={{ textAlign: 'left' }}>Constraints</th>
                <th style={{ textAlign: 'left' }}>Default</th>
              </tr>
            </thead>
            <tbody>
              {active.columns.map((c) => (
                <tr key={c.name}>
                  <td>
                    {c.name}
                    {c.is_primary_key && (
                      <span style={{ marginLeft: 4, fontSize: 9, color: 'var(--text-dim)' }}>PK</span>
                    )}
                  </td>
                  <td><code style={{ fontSize: 11 }}>{c.type || ''}</code></td>
                  <td style={{ fontSize: 11, color: 'var(--text-dim)' }}>
                    {c.notnull ? 'NOT NULL' : ''}
                  </td>
                  <td><code style={{ fontSize: 11 }}>{c.default ?? ''}</code></td>
                </tr>
              ))}
            </tbody>
          </table>
        </details>
      )}

      {active && active.indexes.length > 0 && (
        <details style={{ marginTop: 8 }}>
          <summary style={{ cursor: 'pointer', fontSize: 12, marginBottom: 6 }}>
            <strong>Indexes</strong> ({active.indexes.length})
          </summary>
          <ul style={{ fontSize: 12, marginLeft: 16 }}>
            {active.indexes.map((i) => (
              <li key={i.name}>
                <code style={{ fontSize: 11 }}>{i.name}</code>
                {i.unique && <span style={{ marginLeft: 4, fontSize: 9, color: 'var(--text-dim)' }}>UNIQUE</span>}
                <span style={{ marginLeft: 6, color: 'var(--text-dim)' }}>
                  ({i.columns.join(', ')})
                </span>
              </li>
            ))}
          </ul>
        </details>
      )}

      {active && active.ddl && (
        <details style={{ marginTop: 8 }}>
          <summary style={{ cursor: 'pointer', fontSize: 12, marginBottom: 6 }}>
            <strong>CREATE TABLE DDL</strong>
          </summary>
          <pre style={{
            fontSize: 11,
            padding: 8,
            background: 'var(--code-bg, rgba(127,127,127,0.10))',
            borderRadius: 3,
            overflowX: 'auto',
          }}>{active.ddl}</pre>
        </details>
      )}
    </div>
  );
}


// ── DataBrowser: paginated row reader + filter input ───────────────────────

const DATA_BROWSER_PAGE_SIZE = 50;

function DataBrowser({
  dbType,
  instanceId,
  table,
}: {
  dbType: string;
  instanceId: string;
  table: DatabaseTable;
}) {
  const [page, setPage] = useState<DatabaseTableRowsPage | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [offset, setOffset] = useState(0);
  const [filterCol, setFilterCol] = useState<string>('');
  const [filterVal, setFilterVal] = useState<string>('');
  const [appliedFilterCol, setAppliedFilterCol] = useState<string>('');
  const [appliedFilterVal, setAppliedFilterVal] = useState<string>('');

  useEffect(() => {
    let cancelled = false;
    setError(null);
    setPage(null);
    api.dbBrowserRows(dbType, instanceId, table.name, {
      limit: DATA_BROWSER_PAGE_SIZE,
      offset,
      filter_column: appliedFilterCol || undefined,
      filter_value: appliedFilterCol ? appliedFilterVal : undefined,
    })
      .then((r) => { if (!cancelled) setPage(r); })
      .catch((e) => { if (!cancelled) setError(String(e)); });
    return () => { cancelled = true; };
  }, [dbType, instanceId, table.name, offset, appliedFilterCol, appliedFilterVal]);

  // Reset offset to 0 when table changes.
  useEffect(() => { setOffset(0); }, [dbType, instanceId, table.name]);

  const applyFilter = () => {
    setOffset(0);
    setAppliedFilterCol(filterCol);
    setAppliedFilterVal(filterVal);
  };
  const clearFilter = () => {
    setFilterCol('');
    setFilterVal('');
    setAppliedFilterCol('');
    setAppliedFilterVal('');
    setOffset(0);
  };

  return (
    <div className="panel" style={{ marginTop: 8 }}>
      <h3 style={{ marginTop: 0, fontSize: 14 }}>
        Data: <code style={{ fontSize: 12 }}>{table.name}</code>
      </h3>

      <div style={{ display: 'flex', gap: 6, alignItems: 'center', marginBottom: 8 }}>
        <span style={{ fontSize: 12, color: 'var(--text-dim)' }}>Filter:</span>
        <select
          value={filterCol}
          onChange={(e) => setFilterCol(e.target.value)}
          style={{ fontSize: 12 }}
        >
          <option value="">(none)</option>
          {table.columns.map((c) => (
            <option key={c.name} value={c.name}>{c.name}</option>
          ))}
        </select>
        <span style={{ fontSize: 12 }}>=</span>
        <input
          type="text"
          value={filterVal}
          onChange={(e) => setFilterVal(e.target.value)}
          placeholder="(empty matches NULL)"
          style={{ fontSize: 12, maxWidth: 200 }}
          disabled={!filterCol}
        />
        <button onClick={applyFilter} style={{ fontSize: 12 }} disabled={!filterCol}>
          Apply
        </button>
        {appliedFilterCol && (
          <button onClick={clearFilter} style={{ fontSize: 12 }}>
            Clear
          </button>
        )}
      </div>

      {error && (
        <div className="banner error" style={{ fontSize: 12 }}>{error}</div>
      )}
      {page === null && !error && (
        <div className="empty" style={{ fontSize: 12 }}>Loading rows…</div>
      )}

      {page && (
        <>
          <div style={{ fontSize: 11, color: 'var(--text-dim)', marginBottom: 6 }}>
            Rows {page.total_rows === 0 ? 0 : (page.offset + 1)}-
            {Math.min(page.offset + page.rows.length, page.total_rows)}
            {' '}of {page.total_rows}
          </div>

          {page.rows.length > 0 ? (
            <div style={{ overflowX: 'auto' }}>
              <table className="table" style={{ fontSize: 11 }}>
                <thead>
                  <tr>
                    {page.columns.map((col) => (
                      <th key={col} style={{ textAlign: 'left' }}>{col}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {page.rows.map((row, ri) => (
                    <tr key={ri}>
                      {row.map((cell, ci) => (
                        <td key={ci}>
                          <RowCell cell={cell} />
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <div className="empty" style={{ fontSize: 12 }}>
              {appliedFilterCol ? 'No rows match the filter.' : 'Empty table.'}
            </div>
          )}

          <div style={{ display: 'flex', gap: 6, alignItems: 'center', marginTop: 8 }}>
            <button
              onClick={() => setOffset(Math.max(0, offset - page.limit))}
              disabled={offset === 0}
              style={{ fontSize: 12 }}
            >
              ← Previous
            </button>
            <button
              onClick={() => setOffset(offset + page.limit)}
              disabled={!page.has_more}
              style={{ fontSize: 12 }}
            >
              Next →
            </button>
          </div>
        </>
      )}
    </div>
  );
}


// ── RowCell: single-cell formatter ─────────────────────────────────────────

function RowCell({ cell }: { cell: DatabaseCell }) {
  const [expanded, setExpanded] = useState(false);

  if (cell.display === null) {
    return <span style={{ color: 'var(--text-dim)', fontSize: 10 }}>NULL</span>;
  }

  // Truncated long text: show expand toggle.
  if (cell.truncated && typeof cell.raw === 'string') {
    return (
      <>
        <span>{expanded ? cell.raw : String(cell.display)}</span>
        <button
          onClick={() => setExpanded(!expanded)}
          style={{
            marginLeft: 6, fontSize: 10, padding: '0 4px',
            background: 'transparent', border: '1px solid var(--border, rgba(127,127,127,0.3))',
            cursor: 'pointer',
          }}
        >
          {expanded ? 'collapse' : 'expand'}
        </button>
      </>
    );
  }

  // Redacted columns - show the placeholder. No expand affordance
  // (the raw value is deliberately not on the wire).
  if (cell.raw_present) {
    return (
      <span style={{ fontStyle: 'italic', color: 'var(--text-dim)' }}>
        {String(cell.display)}
      </span>
    );
  }

  // Decoded timestamp: show ISO + raw on hover (title attr).
  if (typeof cell.raw === 'number' && typeof cell.display === 'string') {
    return (
      <span title={`raw: ${cell.raw}`}>{cell.display}</span>
    );
  }

  // Boolean / number / plain string.
  return <span>{String(cell.display)}</span>;
}


// ── Helpers ────────────────────────────────────────────────────────────────

function formatBytes(n: number): string {
  if (!Number.isFinite(n) || n < 0) return '?';
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1024 * 1024 * 1024) return `${(n / (1024 * 1024)).toFixed(1)} MB`;
  return `${(n / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}
