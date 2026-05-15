// Live network activity for the current run.
//
// Reworked from per-library to per-server:
//
//   * Snapshot mode               → one card for the source server only.
//   * Direct transfer / fan-out   → tab toggle across source + every
//                                   destination so the operator can
//                                   compare both sides while the
//                                   transfer is in flight.
//
// The per-library bucket toggle was removed because the collector
// doesn't track per-library time series for the inline chart - the
// library attribution lived on the (now removed) bucketed dashboard
// fields. Per-server data is what's actually informative when
// debugging throttle or latency, and it already lives on
// ``DashboardFrame.servers_network`` (process-lifetime, server-keyed
// collector introduced in v0.12.0).
//
// Chart.js status histogram stays - it's small, useful, and the per-
// server data shape carries everything we need.
//
// The standalone Networking tab (NetworkingPanel.tsx) still shows
// every registered server; this inline panel is scoped to the active
// job's source/dest servers only so the Dashboard view stays focused.

import { useEffect, useState } from 'react';
import {
  BarElement,
  CategoryScale,
  Chart as ChartJS,
  Filler,
  Legend,
  LinearScale,
  LineElement,
  PointElement,
  Tooltip,
  Title as ChartTitle,
} from 'chart.js';
import { Bar, Line } from 'react-chartjs-2';

import { api, DashboardFrame, JobPayload, ServerNetworkState } from '../api';


ChartJS.register(
  BarElement,
  CategoryScale,
  Filler,
  Legend,
  LinearScale,
  LineElement,
  PointElement,
  ChartTitle,
  Tooltip,
);

// Width of the "instantaneous" window the four tiles compute over.
// 3 seconds = three of the per-second buckets in ``snap.series``.
// Short enough to feel live (values drop within seconds when traffic
// stops) but wide enough that a single network hiccup doesn't blank
// the tile.
const TILE_LIVE_WINDOW_SECONDS = 3;

// Ping cadence for the in-panel poller. Matches the existing
// ServersPanel / JobFormPanel pollers so the per-server collector's
// last_ping_ms field stays warm regardless of which UI tab the
// operator is sitting on. 30s is the same value those panels use.
const PING_INTERVAL_MS = 30_000;


const STATUS_MEANINGS: Record<number, string> = {
  200: '200 OK',
  201: '201 Created',
  204: '204 No Content',
  206: '206 Partial Content',
  301: '301 Moved Permanently',
  302: '302 Found',
  304: '304 Not Modified',
  400: '400 Bad Request',
  401: '401 Unauthorized',
  403: '403 Forbidden',
  404: '404 Not Found',
  408: '408 Request Timeout',
  429: '429 Too Many Requests',
  500: '500 Internal Server Error',
  502: '502 Bad Gateway',
  503: '503 Service Unavailable',
  504: '504 Gateway Timeout',
};

function statusLabel(code: number): string {
  return STATUS_MEANINGS[code] ?? `HTTP ${code}`;
}

function statusColor(code: number): string {
  if (code >= 200 && code < 300) return 'rgba(102, 187, 106, 0.85)';   // pale green
  if (code >= 300 && code < 400) return 'rgba(255, 167, 38, 0.85)';    // amber
  if (code >= 400 && code < 500) return 'rgba(239, 108, 0, 0.85)';     // red-orange
  if (code >= 500) return 'rgba(229, 57, 53, 0.85)';                   // red
  return 'rgba(189, 189, 189, 0.85)';                                  // grey
}


// ── Relevance filter ─────────────────────────────────────────────────────────
//
// "Relevant servers" for the panel = source + destination(s) of the
// current job. Snapshot has one (source only); import has destinations;
// direct has both. The toggle is hidden when only one is relevant so
// snapshot mode reads as a single card with no tab strip.

interface RelevantServer {
  role: 'source' | 'destination';
  name: string;
  state: ServerNetworkState | null;  // null until the collector sees traffic
}

function relevantServers(
  job: JobPayload | null,
  serversNetwork: ServerNetworkState[],
  scopedDest: string | null,
): RelevantServer[] {
  if (!job) return [];
  const params = job.params || {};
  const out: RelevantServer[] = [];
  const seen = new Set<string>();

  const push = (role: 'source' | 'destination', name: string) => {
    if (!name || seen.has(name)) return;
    seen.add(name);
    const state = serversNetwork.find((s) => s.server_name === name) ?? null;
    out.push({ role, name, state });
  };

  const allDestinations = (): string[] => {
    const list = (params.dest_server_names as string[] | undefined) || [];
    if (list.length > 0) return list;
    const single = String(params.dest_server_name || '');
    return single ? [single] : [];
  };

  if (job.mode === 'snapshot') {
    push('source', String(params.source_server_name || ''));
  } else if (job.mode === 'restore') {
    const dests = allDestinations();
    const filtered = scopedDest ? dests.filter((n) => n === scopedDest) : dests;
    for (const n of filtered) push('destination', n);
  } else if (job.mode === 'direct') {
    push('source', String(params.source_server_name || ''));
    const dests = allDestinations();
    const filtered = scopedDest ? dests.filter((n) => n === scopedDest) : dests;
    for (const n of filtered) push('destination', n);
  }
  return out;
}


// ── Main component ──────────────────────────────────────────────────────────

interface Props {
  // Whole frame so we can pull servers_network alongside the job. The
  // job tells us which servers are participating in this run; the
  // server_network entries carry the per-server telemetry the cards
  // render.
  frame: DashboardFrame | null;
  // When the inline panel is rendered inside a fan-out per-destination
  // sub-tab, narrow the toggle to just (source +) that one destination
  // so each tab shows only its own server. Top-level Dashboard render
  // passes null and the full source/dest set is shown.
  scopedDest?: string | null;
}

export function NetworkPanel({ frame, scopedDest = null }: Props) {
  const job = frame?.job ?? null;
  const serversNetwork = frame?.servers_network ?? [];
  const relevant = relevantServers(job, serversNetwork, scopedDest);

  // Selected tab. Defaults to the first relevant entry (the source for
  // snapshot/direct, the first destination for import). Reset when the
  // relevant-server list changes.
  const [activeName, setActiveName] = useState<string>(() => relevant[0]?.name ?? '');
  useEffect(() => {
    if (!relevant.some((r) => r.name === activeName)) {
      setActiveName(relevant[0]?.name ?? '');
    }
  }, [relevant, activeName]);

  // Ping poller. Fires ``/api/servers/{id}/ping`` for every relevant
  // server every PING_INTERVAL_MS while this panel is mounted. Pre-fix
  // the Dashboard had no ping cadence at all (only the Servers tab and
  // JobForm fired pings on a 30s interval); the "Last ping" tile read
  // ``-`` because the per-server collector had no fresh ping data
  // while the operator was sitting on the Dashboard. The poll fires
  // an immediate kick on mount so the first value lands within a tick
  // of the WS broadcast rather than after 30s.
  //
  // Only servers we actually have a ``server_id`` for can be pinged
  // (the ping route is registry-id-keyed). Servers with no state
  // entry yet have no id - we skip them; the standalone Networking
  // tab's listing picks them up on its own poll loop.
  //
  // Dependency list uses the joined relevant-server-id list so adding
  // / removing destinations during a fan-out re-arms the interval.
  const relevantIdsKey = relevant
    .map((r) => r.state?.server_id || '')
    .filter(Boolean)
    .join(',');
  useEffect(() => {
    if (!relevantIdsKey) return;
    const ids = relevantIdsKey.split(',');
    const tick = () => {
      for (const id of ids) {
        api.pingServer(id).catch(() => { /* poll noise; ignore */ });
      }
    };
    tick();
    const handle = window.setInterval(tick, PING_INTERVAL_MS);
    return () => window.clearInterval(handle);
  }, [relevantIdsKey]);

  if (!job) {
    // Idle: nothing to show. The standalone Networking tab covers
    // idle telemetry across every registered server.
    return null;
  }
  if (relevant.length === 0) {
    return (
      <div className="panel">
        <h2>Network Activity</h2>
        <SectionHint>
          No source or destination server identified for this job yet.
        </SectionHint>
      </div>
    );
  }

  const active = relevant.find((r) => r.name === activeName) ?? relevant[0];

  return (
    <div className="panel">
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8, gap: 12, flexWrap: 'wrap' }}>
        <h2 style={{ margin: 0 }}>Network Activity</h2>
        {relevant.length > 1 && (
          <ServerToggle
            servers={relevant}
            activeName={activeName}
            onSelect={setActiveName}
          />
        )}
      </div>
      <SectionHint>
        {job.mode === 'snapshot'
          ? 'Live HTTP traffic between this job and the source server.'
          : job.mode === 'restore'
            ? 'Live HTTP traffic between this job and the destination server(s).'
            : 'Live HTTP traffic between this job and the source / destination servers.'}
      </SectionHint>

      <ServerView slot={active} />
    </div>
  );
}


function ServerToggle({
  servers,
  activeName,
  onSelect,
}: {
  servers: RelevantServer[];
  activeName: string;
  onSelect: (name: string) => void;
}) {
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 12 }}>
      {servers.map((s) => (
        <button
          key={s.name}
          onClick={() => onSelect(s.name)}
          className={s.name === activeName ? 'primary' : ''}
          style={{ padding: '4px 10px' }}
          title={s.role === 'source' ? 'Source server' : 'Destination server'}
        >
          {s.role === 'source' ? 'Source: ' : 'Destination: '}{s.name}
        </button>
      ))}
    </div>
  );
}


function ServerView({ slot }: { slot: RelevantServer }) {
  if (!slot.state) {
    return (
      <div className="empty" style={{ fontSize: 12 }}>
        No traffic recorded yet for <strong>{slot.name}</strong>.
        Telemetry will appear here once the engine reaches this server.
      </div>
    );
  }
  return (
    <>
      <Stats snap={slot.state} />
      <LiveSeriesChart snap={slot.state} />
      <StatusHistograms snap={slot.state} />
      <RateLimitEvents snap={slot.state} />
    </>
  );
}


function Stats({ snap }: { snap: ServerNetworkState }) {
  const pingDot = snap.last_ping_ms === null
    ? 'amber'
    : snap.last_ping_ok ? 'green' : 'red';
  const pingLabel = snap.last_ping_ms === null
    ? '-'
    : `${snap.last_ping_ms.toFixed(0)} ms`;

  // Instantaneous (option C): the four tiles compute over the last
  // TILE_LIVE_WINDOW_SECONDS of the series rather than the 60s
  // rolling window the backend pre-computes. Drops to zero within
  // seconds of traffic stopping; spikes immediately when it
  // resumes. The line chart below keeps the full 60s view.
  const recent = (snap.series ?? []).slice(-TILE_LIVE_WINDOW_SECONDS);
  const samplesLive = recent.reduce((acc, b) => acc + (b.rps || 0), 0);
  const rpsLive = samplesLive / TILE_LIVE_WINDOW_SECONDS;
  const latencyBuckets = recent.filter((b) => b.avg_ms !== null && b.rps > 0);
  // Weighted by sample count so a 1-sample bucket doesn't out-vote a
  // 50-sample bucket in the same window.
  let avgMsLive: number | null = null;
  if (latencyBuckets.length > 0) {
    const totalMs = latencyBuckets.reduce(
      (acc, b) => acc + (b.avg_ms as number) * b.rps, 0,
    );
    const totalSamples = latencyBuckets.reduce((acc, b) => acc + b.rps, 0);
    avgMsLive = totalSamples > 0 ? totalMs / totalSamples : null;
  }
  const avgLabel = avgMsLive === null ? '-' : `${avgMsLive.toFixed(0)} ms`;

  return (
    <div className="grid-4" style={{ marginTop: 4 }}>
      <div className="stat">
        <div className="label">Requests / sec</div>
        <div className="value" title={`Samples in last ${TILE_LIVE_WINDOW_SECONDS}s / ${TILE_LIVE_WINDOW_SECONDS}`}>
          {rpsLive.toFixed(2)}
        </div>
      </div>
      <div className="stat">
        <div className="label">Avg latency ({TILE_LIVE_WINDOW_SECONDS}s)</div>
        <div className="value">{avgLabel}</div>
      </div>
      <div className="stat">
        <div className="label">Samples ({TILE_LIVE_WINDOW_SECONDS}s)</div>
        <div className="value">{samplesLive}</div>
      </div>
      <div className="stat">
        <div className="label">Last ping</div>
        <div className="value" style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
          <span className={`dot ${pingDot}`} />
          {pingLabel}
        </div>
      </div>
    </div>
  );
}


// ── Per-server line chart ────────────────────────────────────────────────────
// Two y-axes (left = req/s, right = ms). 60 x-axis ticks (one per
// second). RPS area fills under the line for quick burst visibility;
// latency is a thin line on the right axis. The chart re-renders
// every WS tick (4 Hz); Chart.js diffs internally.

function LiveSeriesChart({ snap }: { snap: ServerNetworkState }) {
  const series = snap.series ?? [];
  const hasAny = series.some((b) => b.rps > 0);
  if (!hasAny) {
    return (
      <div style={{ marginTop: 12 }}>
        <h3 style={{ fontSize: 13, margin: '8px 0 4px' }}>Requests / sec &amp; average latency (last 60 s)</h3>
        <div className="empty" style={{ fontSize: 12 }}>No traffic recorded in the rolling window.</div>
      </div>
    );
  }
  // Right-edge of the chart is "now"; bucket t values count back.
  const lastT = series[series.length - 1]?.t ?? 0;
  const labels = series.map((b) => `${b.t - lastT}s`);
  const data = {
    labels,
    datasets: [
      {
        label: 'Requests / sec',
        data: series.map((b) => b.rps),
        borderColor: 'rgba(102, 187, 106, 0.95)',
        backgroundColor: 'rgba(102, 187, 106, 0.15)',
        yAxisID: 'y',
        fill: true,
        tension: 0.25,
        pointRadius: 0,
      },
      {
        label: 'Avg latency (ms)',
        // ``spanGaps`` keeps the line continuous across seconds that
        // had no traffic (avg_ms is null then). Without it the line
        // would dive to zero between events.
        data: series.map((b) => b.avg_ms),
        borderColor: 'rgba(66, 165, 245, 0.95)',
        backgroundColor: 'rgba(66, 165, 245, 0.15)',
        yAxisID: 'y1',
        fill: false,
        tension: 0.25,
        spanGaps: true,
        pointRadius: 0,
      },
    ],
  };
  const options = {
    responsive: true,
    maintainAspectRatio: false,
    interaction: { mode: 'index' as const, intersect: false },
    plugins: { legend: { position: 'top' as const, labels: { boxWidth: 12 } } },
    scales: {
      x: { ticks: { autoSkip: true, maxTicksLimit: 8 } },
      y: {
        type: 'linear' as const,
        position: 'left' as const,
        beginAtZero: true,
        ticks: { precision: 0 },
        title: { display: true, text: 'req/s' },
      },
      y1: {
        type: 'linear' as const,
        position: 'right' as const,
        beginAtZero: true,
        grid: { drawOnChartArea: false },
        ticks: { precision: 0 },
        title: { display: true, text: 'ms' },
      },
    },
  };
  return (
    <div style={{ marginTop: 12 }}>
      <h3 style={{ fontSize: 13, margin: '8px 0 4px' }}>Requests / sec &amp; average latency (last 60 s)</h3>
      <div style={{ height: 200 }}>
        <Line data={data} options={options} />
      </div>
    </div>
  );
}


function StatusHistograms({ snap }: { snap: ServerNetworkState }) {
  const windowCounts = snap.window_status_counts ?? {};
  const codes = Object.keys(windowCounts)
    .map((k) => Number(k))
    .filter((n) => Number.isFinite(n))
    .sort((a, b) => a - b);

  if (codes.length === 0) {
    return (
      <div style={{ marginTop: 12 }}>
        <h3 style={{ fontSize: 13, margin: '8px 0 4px' }}>HTTP responses by status code (last 60 s)</h3>
        <div className="empty" style={{ fontSize: 12 }}>No traffic in the rolling window.</div>
      </div>
    );
  }

  const data = {
    labels: codes.map(statusLabel),
    datasets: [
      {
        label: 'Responses',
        data: codes.map((c) => windowCounts[String(c)] ?? 0),
        backgroundColor: codes.map(statusColor),
        borderWidth: 0,
      },
    ],
  };

  const options = {
    responsive: true,
    maintainAspectRatio: false,
    plugins: {
      legend: { display: false },
      tooltip: {
        callbacks: {
          title: (items: { label?: string }[]) => items[0]?.label ?? '',
          label: (ctx: { parsed: { y: number | null } }) => {
            const n = ctx.parsed.y ?? 0;
            return `${n.toLocaleString()} response(s)`;
          },
        },
      },
    },
    scales: {
      x: { ticks: { autoSkip: false } },
      y: { beginAtZero: true, ticks: { precision: 0 } },
    },
  };

  // Cumulative (since process start) lives in a dim chip row beneath
  // the chart so chronic problems remain visible even when the
  // window is quiet.
  const cumCodes = Object.entries(snap.cumulative_status_counts ?? {})
    .filter(([k]) => Number.isFinite(Number(k)))
    .sort((a, b) => b[1] - a[1]);

  return (
    <div style={{ marginTop: 12 }}>
      <h3 style={{ fontSize: 13, margin: '8px 0 4px' }}>HTTP responses by status code (last 60 s)</h3>
      <div style={{ height: 180 }}>
        <Bar data={data} options={options} />
      </div>
      {cumCodes.length > 0 && (
        <div style={{ marginTop: 8 }}>
          <div className="label" style={{ marginBottom: 4 }}>Since process start</div>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, opacity: 0.7 }}>
            {cumCodes.map(([code, n]) => (
              <span key={code} className="tag" style={{ fontSize: 11 }}>
                {code} · {n}
              </span>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}


function RateLimitEvents({ snap }: { snap: ServerNetworkState }) {
  const events = snap.rate_limit_events ?? [];
  if (events.length === 0) return null;
  const ordered = [...events].reverse();  // newest first
  return (
    <div style={{ marginTop: 16, paddingTop: 12, borderTop: '1px solid var(--border, #2a3146)' }}>
      <h3 style={{ fontSize: 13, margin: '0 0 6px' }}>Rate-limit events (last 60 s)</h3>
      <div className="feed" style={{ maxHeight: 160, overflowY: 'auto' }}>
        {ordered.map((e, i) => (
          <div className="row" key={`${e.timestamp}-${i}`} style={{ fontSize: 12 }}>
            <span style={{ color: 'var(--text-dim)' }} className="mono">
              {new Date(e.timestamp * 1000).toLocaleTimeString()}
            </span>
            <span className="tag rate_limit" style={{ marginLeft: 8 }}>
              {statusLabel(e.status_code)}
            </span>
            <span style={{ marginLeft: 8, color: 'var(--text-dim)' }}>
              {e.retry_after_seconds !== null
                ? `Retry-After: ${e.retry_after_seconds.toFixed(1)} s`
                : 'no Retry-After header'}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}


function SectionHint({ children }: { children: React.ReactNode }) {
  return (
    <div style={{ marginTop: -4, marginBottom: 10, fontSize: 12, color: 'var(--text-dim)' }}>
      {children}
    </div>
  );
}
