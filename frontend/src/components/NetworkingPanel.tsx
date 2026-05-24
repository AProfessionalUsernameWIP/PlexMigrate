// The Networking tab - process-lifetime, server-keyed HTTP telemetry
// for every registered server.
//
// Why a dedicated tab?
// --------------------
// Network telemetry tied to a per-job DashboardState would disappear
// in two valid scenarios:
//
//   * Idle: no job → no DashboardState → no network surface, even
//     though the registered servers are still reachable and pingable.
//   * Fan-out: every destination has its own DashboardState; there's
//     no single "the" dashboard to embed network details on, so a
//     fan-out layout would omit the panel entirely.
//
// The Networking tab reads from the process-lifetime collector keyed
// by URL host. It stays correct in every UI state - idle, single-job,
// fan-out - because telemetry does not ride on the per-job
// abstraction. One card per registered server, each with rolling 60s
// RPS / latency, window + cumulative status histograms, and the
// rate-limit feed for 429s and 503s.
//
// The Dashboard's inline NetworkPanel is unchanged for single-dest
// jobs; this tab is an addition, not a replacement.

import { useCallback, useEffect, useState } from 'react';
import { api, DashboardFrame, ServerNetworkState } from '../api';
import { errorText } from '../utils/format';
import { pausableInterval } from '../utils/pausableInterval';

interface Props {
  snapshot: DashboardFrame | null;
}

export function NetworkingPanel({ snapshot }: Props) {
  const servers = snapshot?.servers_network ?? [];
  if (servers.length === 0) {
    return (
      <>
        <div className="panel">
          <h2>Networking</h2>
          <div className="empty">
            No registered servers yet. Add one from the <strong>Servers</strong> tab and
            the per-server live telemetry will appear here automatically.
          </div>
        </div>
        <RecentRequestsPanel />
      </>
    );
  }
  return (
    <>
      <div className="panel">
        <h2>Networking</h2>
        <p style={{ color: 'var(--text-dim)', fontSize: 13, marginTop: 0 }}>
          Live HTTP health for every registered server. The 60-second window combines
          engine traffic (when a job is running) with the 30-second reachability ping
          (always running). One card per server - fan-out jobs show one card per
          destination side-by-side here, in addition to the per-destination cards on
          the Dashboard tab.
        </p>
      </div>
      {servers.map((s) => (
        <ServerNetworkCard key={s.server_id || s.host} snap={s} />
      ))}
      <RecentRequestsPanel />
    </>
  );
}


// ── Recent requests ──
//
// Per-request HTTP timeline. The shared session response hook in
// services.auth captures every Plex API call's URL + method + status
// + elapsed_ms + host + job_id and pushes them onto a ring buffer
// (5000 entries process-lifetime). This panel polls
// /api/network/recent-requests every 3s while visible so the operator
// can see the actual requests fired during direct / fan-out / batch
// runs. Includes filters for job_id + host so a busy timeline can be
// narrowed to one specific run.

interface RecentRequest {
  timestamp: number;
  host: string;
  method: string;
  url: string;
  status_code: number;
  elapsed_ms: number;
  job_id: string | null;
}

function RecentRequestsPanel() {
  const [requests, setRequests] = useState<RecentRequest[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [hostFilter, setHostFilter] = useState('');
  const [jobIdFilter, setJobIdFilter] = useState('');
  const [autoRefresh, setAutoRefresh] = useState(true);

  const fetchRequests = useCallback(async () => {
    setRefreshing(true);
    try {
      const r = await api.listRecentNetworkRequests({
        limit: 500,
        jobId: jobIdFilter.trim() || undefined,
        host: hostFilter.trim() || undefined,
      });
      setRequests(r.requests || []);
      setError(null);
    } catch (e) {
      setError(errorText(e));
    } finally {
      setRefreshing(false);
    }
  }, [hostFilter, jobIdFilter]);

  useEffect(() => {
    void fetchRequests();
  }, [fetchRequests]);

  useEffect(() => {
    if (!autoRefresh) return;
    return pausableInterval(() => { void fetchRequests(); }, 3000);
  }, [autoRefresh, fetchRequests]);

  return (
    <div className="panel" style={{ marginTop: 12 }}>
      <div style={{ display: 'flex', alignItems: 'baseline', justifyContent: 'space-between', gap: 12, flexWrap: 'wrap' }}>
        <h2 style={{ margin: 0 }}>
          Recent requests
          <span style={{ color: 'var(--text-dim)', fontSize: 12, fontWeight: 400, marginLeft: 8 }}>
            {requests.length} entries (newest first)
          </span>
        </h2>
        <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'baseline' }}>
          <label style={{ fontSize: 12 }}>
            Filter by host:
            <input
              type="text"
              value={hostFilter}
              onChange={(e) => setHostFilter(e.target.value)}
              placeholder="plex.local:32400"
              style={{ marginLeft: 4, width: 160, fontSize: 11 }}
            />
          </label>
          <label style={{ fontSize: 12 }}>
            Filter by job_id:
            <input
              type="text"
              value={jobIdFilter}
              onChange={(e) => setJobIdFilter(e.target.value)}
              placeholder="(job UUID)"
              style={{ marginLeft: 4, width: 260, fontSize: 11 }}
            />
          </label>
          <label style={{ fontSize: 12 }}>
            <input
              type="checkbox"
              checked={autoRefresh}
              onChange={(e) => setAutoRefresh(e.target.checked)}
            />
            {' '}Auto-refresh (3s)
          </label>
          <button
            type="button"
            onClick={() => void fetchRequests()}
            disabled={refreshing}
            style={{ fontSize: 11 }}
          >
            {refreshing ? 'Refreshing…' : 'Refresh'}
          </button>
        </div>
      </div>
      <p style={{ color: 'var(--text-dim)', fontSize: 12, marginTop: 6 }}>
        Per-request HTTP timeline captured by the shared session response hook.
        Tags include the originating job ID so you can correlate a request to a
        specific snapshot / restore / direct transfer / playlist batch.
      </p>
      {error && (
        <div className="banner error" style={{ fontSize: 12, marginBottom: 8 }}>
          Could not load requests: {error}
        </div>
      )}
      {requests.length === 0 ? (
        <div className="empty" style={{ fontSize: 12 }}>
          No requests captured yet. Run a job to populate the timeline.
        </div>
      ) : (
        <div style={{ maxHeight: 420, overflowY: 'auto', border: '1px solid rgba(128,128,128,0.2)', borderRadius: 3 }}>
          <table style={{ width: '100%', fontSize: 11, borderCollapse: 'collapse' }}>
            <thead style={{ position: 'sticky', top: 0, background: 'var(--bg, #1a1a1a)' }}>
              <tr>
                <th style={{ textAlign: 'left', padding: '4px 8px' }}>When</th>
                <th style={{ textAlign: 'left', padding: '4px 8px' }}>Method</th>
                <th style={{ textAlign: 'left', padding: '4px 8px' }}>Status</th>
                <th style={{ textAlign: 'right', padding: '4px 8px' }}>ms</th>
                <th style={{ textAlign: 'left', padding: '4px 8px' }}>Host</th>
                <th style={{ textAlign: 'left', padding: '4px 8px' }}>URL</th>
                <th style={{ textAlign: 'left', padding: '4px 8px' }}>Job</th>
              </tr>
            </thead>
            <tbody>
              {requests.map((r, i) => {
                const statusColor =
                  r.status_code >= 500 ? 'var(--bad, #ef4444)'
                  : r.status_code >= 400 ? 'var(--warn, #f5a623)'
                  : r.status_code >= 300 ? 'var(--text-dim)'
                  : 'var(--success, #16a34a)';
                const elapsedColor =
                  r.elapsed_ms > 2000 ? 'var(--bad, #ef4444)'
                  : r.elapsed_ms > 500 ? 'var(--warn, #f5a623)'
                  : undefined;
                const when = new Date(r.timestamp * 1000).toLocaleTimeString();
                return (
                  <tr
                    key={`${r.timestamp}-${i}`}
                    style={{ borderBottom: '1px dotted rgba(128,128,128,0.15)' }}
                  >
                    <td style={{ padding: '2px 8px', whiteSpace: 'nowrap' }}>{when}</td>
                    <td style={{ padding: '2px 8px', fontFamily: 'monospace' }}>{r.method}</td>
                    <td style={{ padding: '2px 8px', color: statusColor, fontWeight: 600 }}>{r.status_code}</td>
                    <td style={{ padding: '2px 8px', textAlign: 'right', color: elapsedColor }}>{r.elapsed_ms.toFixed(0)}</td>
                    <td style={{ padding: '2px 8px', fontFamily: 'monospace' }}>{r.host}</td>
                    <td style={{ padding: '2px 8px', fontFamily: 'monospace', wordBreak: 'break-all' }}>
                      {/* Truncate by code point (Array.from splits on
                          full code points) so a surrogate pair is never
                          cut in half. */}
                      {Array.from(r.url).length > 100
                        ? Array.from(r.url).slice(0, 100).join('') + '…'
                        : r.url}
                    </td>
                    <td style={{ padding: '2px 8px', fontFamily: 'monospace', color: r.job_id ? undefined : 'var(--text-dim)' }}>
                      {r.job_id ? r.job_id.slice(0, 8) : '-'}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}


function ServerNetworkCard({ snap }: { snap: ServerNetworkState }) {
  const pingDot = snap.last_ping_ms === null
    ? 'amber'
    : snap.last_ping_ok ? 'green' : 'red';
  const pingLabel = snap.last_ping_ms === null
    ? '-'
    : `${snap.last_ping_ms.toFixed(0)} ms`;
  const avgLabel = snap.avg_ms === null
    ? '-'
    : `${snap.avg_ms.toFixed(0)} ms`;
  const rpsLabel = snap.rps.toFixed(2);

  // Status-code histogram for the trailing 60s window. Sorted by
  // count desc so the loudest codes lead. Cumulative codes appear
  // dimmed underneath so chronic problems stay visible even when the
  // window is currently quiet.
  const windowCodes = Object.entries(snap.window_status_counts)
    .sort((a, b) => b[1] - a[1]);
  const cumCodes = Object.entries(snap.cumulative_status_counts)
    .sort((a, b) => b[1] - a[1]);

  return (
    <div className="panel">
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 12, flexWrap: 'wrap' }}>
        <h2 style={{ margin: 0 }}>{snap.server_name || snap.host}</h2>
        <span style={{ color: 'var(--text-dim)', fontSize: 12 }}>{snap.host}</span>
        <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6, marginLeft: 'auto' }}>
          <span className={`dot ${pingDot}`} />
          <span style={{ fontSize: 12, color: 'var(--text-dim)' }}>
            Ping: {pingLabel}
          </span>
        </span>
      </div>

      <div className="grid-4" style={{ marginTop: 10 }}>
        <div className="stat">
          <div className="label">Requests / sec</div>
          <div className="value">{rpsLabel}</div>
        </div>
        <div className="stat">
          <div className="label">Avg latency (60s)</div>
          <div className="value">{avgLabel}</div>
        </div>
        <div className="stat">
          <div className="label">Samples in window</div>
          <div className="value">{snap.sample_count_in_window}</div>
        </div>
        <div className={`stat${snap.rate_limit_events.length > 0 ? ' warn' : ''}`}>
          <div className="label">429 / 503 in window</div>
          <div className="value">{snap.rate_limit_events.length}</div>
        </div>
      </div>

      {/* Status code histogram. Window codes lead in bold; cumulative
          show below at lower contrast so chronic patterns (lots of
          429s over the run's lifetime) stay visible even after the
          throttle eases. */}
      <div style={{ marginTop: 12 }}>
        <div className="label" style={{ marginBottom: 4 }}>Status codes (last 60s)</div>
        {windowCodes.length === 0 ? (
          <div className="empty" style={{ padding: '6px 0' }}>No traffic in window.</div>
        ) : (
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
            {windowCodes.map(([code, n]) => (
              <span key={code} className={`tag ${statusTagClass(code)}`}>
                <strong>{code}</strong> · {n}
              </span>
            ))}
          </div>
        )}
        {cumCodes.length > 0 && (
          <>
            <div className="label" style={{ marginTop: 8, marginBottom: 4 }}>
              Status codes (since boot)
            </div>
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, opacity: 0.7 }}>
              {cumCodes.map(([code, n]) => (
                <span key={code} className={`tag ${statusTagClass(code)}`}>
                  {code} · {n}
                </span>
              ))}
            </div>
          </>
        )}
      </div>

      {/* Rate-limit feed - 429 / 503 events with Retry-After when the
          server provided one. Anything older than 60s ages out so the
          feed clears once Plex stops throttling. */}
      {snap.rate_limit_events.length > 0 && (
        <div style={{ marginTop: 12 }}>
          <div className="label" style={{ marginBottom: 4 }}>Rate-limit events</div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
            {snap.rate_limit_events
              .slice().reverse()  // newest first
              .map((ev, i) => (
                <div key={`${ev.timestamp}-${i}`} style={{ fontSize: 12, color: 'var(--text-dim)' }}>
                  <span className="mono">
                    {new Date(ev.timestamp * 1000).toLocaleTimeString()}
                  </span>
                  <span style={{ margin: '0 6px' }}>·</span>
                  <strong>HTTP {ev.status_code}</strong>
                  {ev.retry_after_seconds !== null && (
                    <span style={{ marginLeft: 6 }}>
                      Retry-After: {ev.retry_after_seconds.toFixed(1)}s
                    </span>
                  )}
                </div>
              ))}
          </div>
        </div>
      )}
    </div>
  );
}


/**
 * Pick a tag-style class for a status code based on the family.
 * Maps onto the existing tag colour classes in styles.css so we
 * don't have to introduce a new colour vocabulary.
 */
function statusTagClass(code: string): string {
  const n = parseInt(code, 10);
  if (!Number.isFinite(n)) return 'phase';
  if (n >= 200 && n < 300) return 'created';   // green-ish for OK
  if (n >= 300 && n < 400) return 'phase';
  if (n === 429 || n === 503) return 'skipped'; // amber-ish for throttle
  if (n >= 400 && n < 500) return 'rated';     // distinct for client errors
  if (n >= 500) return 'merged';               // distinct for server errors
  return 'phase';
}
