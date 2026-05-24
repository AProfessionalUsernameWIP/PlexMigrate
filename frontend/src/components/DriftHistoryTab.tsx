// Drift History sub-tab inside the Servers panel. Lists drift_events
// rows newest-first, optionally filtered by server. Each event
// records when the live state diverged from the mirror's recorded
// state (live_total_size or live_updated_at mismatch).
//
// Operators use this to audit cache freshness: a spike of drift
// events suggests an external system is mutating the server outside
// the snapshot cadence.

import { useState } from 'react';
import { api } from '../api';
import { formatTimestamp } from '../utils/format';
import { useResourceQuery } from '../hooks/useResourceQuery';


export interface DriftHistoryTabProps {
  /** Optional pre-filter to one server's drift events. */
  serverIdFilter?: string;
}


interface DriftRow {
  id: number;
  server_id: string;
  section_id: string;
  section_name: string;
  detected_at: number;
  mirror_total_size: number | null;
  live_total_size: number | null;
  mirror_updated_at: number | null;
  live_updated_at: number | null;
  detected_by_job_id: string | null;
}




function sizeDelta(
  mirror: number | null, live: number | null,
): string {
  if (mirror === null || live === null) return '?';
  const diff = live - mirror;
  if (diff === 0) return 'unchanged';
  return diff > 0 ? `+${diff}` : `${diff}`;
}


export function DriftHistoryTab({
  serverIdFilter,
}: DriftHistoryTabProps) {
  const [limit, setLimit] = useState<number>(100);
  const { data: rows, loading, error, reload } = useResourceQuery<DriftRow[]>(
    () => api.getServerMirrorDriftEvents({ serverId: serverIdFilter, limit })
      .then((r) => r.events),
    [serverIdFilter, limit],
    [],
  );

  return (
    <div className="drift-history-tab">
      <div className="toolbar">
        <h3>
          Drift History
          {serverIdFilter ? ` · ${serverIdFilter}` : ''}
        </h3>
        <label>
          Limit:
          <select
            value={limit}
            onChange={(e) => setLimit(parseInt(e.target.value, 10))}
            disabled={loading}
          >
            <option value="50">50</option>
            <option value="100">100</option>
            <option value="500">500</option>
            <option value="1000">1000</option>
          </select>
        </label>
        <button
          type="button"
          onClick={reload}
          disabled={loading}
        >
          {loading ? 'Loading...' : 'Refresh'}
        </button>
      </div>
      {error && (
        <div className="banner error" role="alert">
          {error}
        </div>
      )}
      {rows.length === 0 && !loading && (
        <div className="empty-state">
          No drift events recorded
          {serverIdFilter ? ` for ${serverIdFilter}` : ''} yet. Drift
          is detected when a live probe disagrees with the mirror's
          recorded state.
        </div>
      )}
      {rows.length > 0 && (
        <table className="drift-table">
          <thead>
            <tr>
              <th>Detected</th>
              <th>Server</th>
              <th>Section</th>
              <th>Items (mirror &rarr; live)</th>
              <th>Job</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.id}>
                <td>{formatTimestamp(r.detected_at)}</td>
                <td>{r.server_id}</td>
                <td>{r.section_name}</td>
                <td>
                  {r.mirror_total_size ?? '?'}
                  {' '}&rarr;{' '}
                  {r.live_total_size ?? '?'}
                  {' '}
                  <span className="delta">
                    ({sizeDelta(r.mirror_total_size, r.live_total_size)})
                  </span>
                </td>
                <td>
                  {r.detected_by_job_id ? (
                    <code>{r.detected_by_job_id}</code>
                  ) : (
                    <span className="muted">refresher</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
