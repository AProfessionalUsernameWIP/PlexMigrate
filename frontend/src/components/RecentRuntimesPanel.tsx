// Servers ▸ Recent Runtimes.
//
// Reads the per-RUN history rows captured by services/jobs.py at
// finalisation (see server/run_timings_db.run_history). One row per
// completed job - snapshot, restore, direct transfer, fan-out -
// with the bits an end user scans for first: timestamp, run type,
// libraries touched, users affected, duration, and deep-links to
// the run-settings.log + restoration.log files.
//
// Layout mirrors LibraryCataloguesPanel / ServerUsersPanel: the
// outer nav is a per-server selector; inside, an "All libraries"
// option plus one per-library inner tab narrows the table. The
// "All servers" first entry is the unfiltered view across the
// instance for end users auditing the whole rig.

import { useEffect, useMemo, useState } from 'react';
import { api, RecentRunRow, ServerView } from '../api';
import { UserCountChip } from './UserCountChip';
import { formatTimestamp } from '../utils/format';
import { useResourceQuery } from '../hooks/useResourceQuery';


function _fmtDuration(ms: number): string {
  if (!Number.isFinite(ms) || ms <= 0) return '-';
  const s = Math.round(ms / 1000);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  const rem = s - m * 60;
  if (m < 60) return `${m}m ${rem.toString().padStart(2, '0')}s`;
  const h = Math.floor(m / 60);
  const mRem = m - h * 60;
  return `${h}h ${mRem.toString().padStart(2, '0')}m`;
}




function _stateBadge(state: string): { bg: string; fg: string; label: string } {
  switch (state) {
    case 'completed':
      return { bg: 'rgba(34,197,94,0.12)', fg: '#22c55e', label: 'completed' };
    case 'completed_with_errors':
      return { bg: 'rgba(217,119,6,0.14)', fg: '#d97706', label: 'completed (errors)' };
    case 'failed':
      return { bg: 'rgba(239,68,68,0.14)', fg: '#ef4444', label: 'failed' };
    case 'cancelled':
      return { bg: 'rgba(148,163,184,0.18)', fg: '#94a3b8', label: 'cancelled' };
    default:
      return { bg: 'rgba(148,163,184,0.12)', fg: 'var(--text-dim)', label: state || 'unknown' };
  }
}


// "All servers" sentinel id so the outer nav has a "show everything"
// entry that's distinguishable from a real server_id.
const ALL_SERVERS = '__all__';
const ALL_LIBRARIES = '__all_libs__';


export function RecentRuntimesPanel({ servers }: { servers: ServerView[] }) {
  const [selectedServerId, setSelectedServerId] = useState<string>(ALL_SERVERS);
  const [selectedLibrary, setSelectedLibrary] = useState<string>(ALL_LIBRARIES);
  // Refetches whenever the server / library filter changes. Initial
  // mount fires through the same path because both selections start
  // as their sentinel values, so the first request hits the unfiltered
  // endpoint and the end user sees the whole instance's history.
  const { data: rows, loading, error, reload } = useResourceQuery<RecentRunRow[]>(
    () => api.listRecentRunHistory({
      limit: 200,
      serverId: selectedServerId === ALL_SERVERS ? undefined : selectedServerId,
      library: selectedLibrary === ALL_LIBRARIES ? undefined : selectedLibrary,
    }).then((resp) => resp.runs),
    [selectedServerId, selectedLibrary],
    [],
  );

  // Library list comes from the rows themselves (union of every
  // library every captured row touched). This auto-grows as new
  // libraries are captured without requiring a separate API.
  const libraryOptions = useMemo(() => {
    const set = new Set<string>();
    for (const r of rows) {
      for (const lib of r.libraries || []) set.add(lib);
    }
    return Array.from(set).sort((a, b) => a.localeCompare(b));
  }, [rows]);

  // If the end user switches servers and the previously-selected
  // library doesn't exist on the new server's rows, drop the filter.
  useEffect(() => {
    if (
      selectedLibrary !== ALL_LIBRARIES
      && !libraryOptions.includes(selectedLibrary)
    ) {
      setSelectedLibrary(ALL_LIBRARIES);
    }
  }, [libraryOptions, selectedLibrary]);

  return (
    <div className="panel">
      <h2>Recent Runtimes</h2>
      <p style={{ color: 'var(--text-dim)', fontSize: 12, marginTop: 0 }}>
        Historical job runs - snapshots, restores, direct transfers, fan-outs - newest
        first. Click <strong>logs</strong> on a row to jump into that run's settings
        snapshot or restoration breakdown. Retained: last 200 runs (configurable).
      </p>

      {/* Per-server outer nav. ALL_SERVERS is a synthetic id that
          asks for the unfiltered list. */}
      <nav className="tabs sub-tabs" style={{ marginBottom: 8 }}>
        <button
          className={selectedServerId === ALL_SERVERS ? 'active' : ''}
          onClick={() => setSelectedServerId(ALL_SERVERS)}
        >
          All servers
        </button>
        {servers.map((s) => (
          <button
            key={s.id}
            className={selectedServerId === s.id ? 'active' : ''}
            onClick={() => setSelectedServerId(s.id)}
          >
            {s.name}
          </button>
        ))}
      </nav>

      {/* Per-library inner nav: auto-derived from the loaded rows.
          Hidden when no rows are visible to avoid an empty nav strip. */}
      {libraryOptions.length > 0 && (
        <nav className="tabs sub-tabs" style={{ marginBottom: 12 }}>
          <button
            className={selectedLibrary === ALL_LIBRARIES ? 'active' : ''}
            onClick={() => setSelectedLibrary(ALL_LIBRARIES)}
          >
            All libraries
          </button>
          {libraryOptions.map((lib) => (
            <button
              key={lib}
              className={selectedLibrary === lib ? 'active' : ''}
              onClick={() => setSelectedLibrary(lib)}
            >
              {lib}
            </button>
          ))}
        </nav>
      )}

      <div style={{ display: 'flex', justifyContent: 'flex-end', marginBottom: 6 }}>
        <button
          onClick={reload}
          disabled={loading}
          style={{ fontSize: 12 }}
        >
          {loading ? 'Loading…' : 'Refresh'}
        </button>
      </div>

      {error && (
        <div className="banner error" style={{ fontSize: 12, marginBottom: 6 }}>
          Could not load Recent Runtimes: {error}
        </div>
      )}

      {!error && rows.length === 0 && !loading && (
        <div className="empty" style={{ fontSize: 12 }}>
          No runs recorded yet for this filter. The next snapshot or restore will populate this list.
        </div>
      )}

      {rows.length > 0 && (
        <div style={{ overflowX: 'auto' }}>
          <table data-testid="recent-runtimes-table" className="list" style={{ width: '100%', fontSize: 12, borderCollapse: 'collapse' }}>
            <thead>
              <tr>
                <th style={{ textAlign: 'left' }}>Started</th>
                <th style={{ textAlign: 'left' }}>Run type</th>
                <th style={{ textAlign: 'left' }}>Server</th>
                <th style={{ textAlign: 'left' }}>Libraries</th>
                <th style={{ textAlign: 'right' }}>Users affected</th>
                <th style={{ textAlign: 'right' }}>Duration</th>
                <th style={{ textAlign: 'left' }}>State</th>
                <th style={{ textAlign: 'left' }}>Logs</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => {
                const badge = _stateBadge(r.state);
                return (
                  <tr key={r.run_id} data-testid={`recent-runtime-row-${r.run_id}`}>
                    <td>{formatTimestamp(r.started_at)}</td>
                    <td><code style={{ fontSize: 11 }}>{r.job_type}</code></td>
                    <td>{r.server_name || '-'}</td>
                    <td data-testid="recent-runtime-libraries">
                      {(r.libraries || []).length === 0 ? '-' : (r.libraries || []).join(', ')}
                    </td>
                    <td style={{ textAlign: 'right' }}>
                      <UserCountChip
                        count={r.users_affected}
                        users={r.users_affected_list || []}
                        ariaLabel="Users affected by this run"
                      />
                    </td>
                    <td data-testid="recent-runtime-duration" style={{ textAlign: 'right' }}>{_fmtDuration(r.duration_ms)}</td>
                    <td data-testid="recent-runtime-state">
                      <span
                        style={{
                          padding: '2px 6px',
                          borderRadius: 3,
                          background: badge.bg,
                          color: badge.fg,
                          fontSize: 11,
                        }}
                      >
                        {badge.label}
                      </span>
                      {r.error_summary && (
                        <div
                          style={{ fontSize: 10, color: 'var(--warn, #d97706)', marginTop: 2 }}
                          title={r.error_summary}
                        >
                          {r.error_summary.length > 60
                            ? r.error_summary.slice(0, 57) + '…'
                            : r.error_summary}
                        </div>
                      )}
                    </td>
                    <td style={{ fontSize: 11 }}>
                      {r.run_log_dir ? (
                        <>
                          <button
                            type="button"
                            onClick={() => {
                              // Best-effort: dispatch a global event so a
                              // future Logs panel listener can deep-link.
                              // Keeps this panel free of cross-tab routing
                              // primitives.
                              window.dispatchEvent(new CustomEvent('plexmigrate.open-run-log', {
                                detail: { run_log_dir: r.run_log_dir },
                              }));
                            }}
                            style={{ fontSize: 10, padding: '2px 6px' }}
                          >
                            run
                          </button>
                          {r.has_settings_log && (
                            <button
                              type="button"
                              onClick={() => {
                                window.dispatchEvent(new CustomEvent('plexmigrate.open-run-log', {
                                  detail: { run_log_dir: r.run_log_dir, file: 'run-settings.log' },
                                }));
                              }}
                              style={{ fontSize: 10, padding: '2px 6px', marginLeft: 4 }}
                            >
                              settings
                            </button>
                          )}
                          {r.has_restoration_log && (
                            <button
                              type="button"
                              onClick={() => {
                                window.dispatchEvent(new CustomEvent('plexmigrate.open-run-log', {
                                  detail: { run_log_dir: r.run_log_dir, file: 'restoration.log' },
                                }));
                              }}
                              style={{ fontSize: 10, padding: '2px 6px', marginLeft: 4 }}
                            >
                              restoration
                            </button>
                          )}
                        </>
                      ) : (
                        <span style={{ color: 'var(--text-dim)' }}>none</span>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          <div style={{ marginTop: 8, fontSize: 11, color: 'var(--text-dim)' }}>
            {rows.length} run{rows.length === 1 ? '' : 's'} shown
            {selectedServerId !== ALL_SERVERS && ' (server filter active)'}
            {selectedLibrary !== ALL_LIBRARIES && ' (library filter active)'}
          </div>
        </div>
      )}
    </div>
  );
}
