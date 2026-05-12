// Live network activity for the current run (v0.9.6, Feature 2).
//
// Three sub-panels, all driven by the same WebSocket dashboard
// snapshot:
//
//   2A — HTTP status code bar chart. One bar per observed status,
//        grouped by range (2xx / 3xx / 4xx / 5xx) and coloured by
//        severity.
//   2B — Rolling 60-second requests/sec + average-latency line
//        graphs. Series scrolls left as new buckets arrive.
//   2C — Cumulative rate-limit status block + dedicated event feed.
//        Lives here (not in the activity feed) so a 429 burst can't
//        push useful engine events out of the 8-entry activity deque.
//
// A single Cumulative / Per-library toggle (shared by 2A and 2B)
// switches both charts between the run-wide ``__all__`` bucket and
// the library selected from the dropdown.
//
// Chart.js (via react-chartjs-2) handles rendering. Each tick of the
// 4 Hz WS broadcast rebuilds the chart data props; Chart.js diffs
// internally and animates the change.

import { useEffect, useMemo, useRef, useState } from 'react';
import {
  CategoryScale,
  Chart as ChartJS,
  Filler,
  Legend,
  LinearScale,
  LineElement,
  PointElement,
  Tooltip,
  BarElement,
  Title as ChartTitle,
} from 'chart.js';
import { Bar, Line } from 'react-chartjs-2';

import { DashboardSnapshot } from '../api';

// Register the Chart.js pieces we use, once at module load. Chart.js
// is tree-shakeable; registering only what's needed keeps the bundle
// lean.
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

// ── Status code tooltip table ────────────────────────────────────────────────
// Hardcoded so the user sees the standard meaning, not just the number.
// Common Plex-relevant codes only — anything else falls through to
// "HTTP <code>".
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
  return 'rgba(189, 189, 189, 0.85)';                                  // grey (1xx and odd)
}

// ── Props ────────────────────────────────────────────────────────────────────

interface Props {
  snapshot: DashboardSnapshot | null;
  // Library names queued for this run (already known from job.params.libraries
  // upstream; the parent passes them in so the dropdown can render before any
  // library has actually fired a request).
  queuedLibraries: string[];
  // v0.9.7 Item 2: current job id, used as the reset trigger for the
  // chart Y-axis high-water marks. When the id changes (or goes from
  // null to a value), the marks snap back to their seed minimums so
  // the next run starts with a fresh scale.
  jobId: string | null;
}

// v0.9.7 Item 2: minimum ceiling so the Y-axis never collapses to a
// flat line at very small values. RPS chart can never start below
// 0–10, latency chart can never start below 0–200ms. The marks only
// grow from there.
const RPS_SEED = 10;
const LAT_SEED = 200;

// ── Main component ──────────────────────────────────────────────────────────

export function NetworkPanel({ snapshot, queuedLibraries, jobId }: Props) {
  // Cumulative / per-library toggle, shared by 2A and 2B.
  const [scope, setScope] = useState<'cumulative' | 'library'>('cumulative');
  const [selectedLib, setSelectedLib] = useState<string>('');

  // v0.9.7 Item 2: high-water marks for the line chart's Y-axes.
  // Stored in refs because the chart re-renders every WS tick (4 Hz);
  // useState would force an extra render per tick for no benefit.
  // The marks only ever increase within a run; ``jobId`` change is
  // the reset trigger (cleanest "new run" signal — more reliable than
  // watching state transitions).
  const rpsMaxRef = useRef<number>(RPS_SEED);
  const latMaxRef = useRef<number>(LAT_SEED);
  useEffect(() => {
    rpsMaxRef.current = RPS_SEED;
    latMaxRef.current = LAT_SEED;
  }, [jobId]);

  // Per-tick walk: update each mark if the snapshot's max exceeds it.
  // Scope-independent (Q4 confirmed: one water mark per axis for the
  // whole run, never reset on toggle).
  const series = snapshot?.http_latency_series;
  if (series) {
    for (const lib of Object.keys(series)) {
      for (const s of series[lib]) {
        if (s.rps > rpsMaxRef.current) rpsMaxRef.current = s.rps;
        if (s.avg_ms !== null && s.avg_ms > latMaxRef.current) {
          latMaxRef.current = s.avg_ms;
        }
      }
    }
  }

  // If the operator picks a library and that library later disappears
  // from the queued list (rare — only on a re-run with a different
  // selection), fall back to cumulative so the charts don't go blank.
  useEffect(() => {
    if (scope === 'library' && selectedLib && !queuedLibraries.includes(selectedLib)) {
      setSelectedLib('');
      setScope('cumulative');
    }
  }, [queuedLibraries, scope, selectedLib]);

  const bucketKey =
    scope === 'library' && selectedLib ? selectedLib : '__all__';

  return (
    <div className="panel">
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8 }}>
        <h2 style={{ margin: 0 }}>Network Activity</h2>
        <ScopeToggle
          scope={scope}
          selectedLib={selectedLib}
          queuedLibraries={queuedLibraries}
          onScope={setScope}
          onLib={setSelectedLib}
        />
      </div>
      <SectionHint>
        Live HTTP traffic between this server and Plex. Cumulative covers the whole run;
        per-library narrows to one of the queued libraries.
      </SectionHint>

      <StatusCodeChart snapshot={snapshot} bucketKey={bucketKey} />
      <LatencyAndRateChart
        snapshot={snapshot}
        bucketKey={bucketKey}
        rpsMax={rpsMaxRef.current}
        latMax={latMaxRef.current}
      />
      <RateLimitBlock snapshot={snapshot} />
    </div>
  );
}

// ── Scope toggle ─────────────────────────────────────────────────────────────

function ScopeToggle({
  scope,
  selectedLib,
  queuedLibraries,
  onScope,
  onLib,
}: {
  scope: 'cumulative' | 'library';
  selectedLib: string;
  queuedLibraries: string[];
  onScope: (s: 'cumulative' | 'library') => void;
  onLib: (s: string) => void;
}) {
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 12 }}>
      <button
        onClick={() => onScope('cumulative')}
        className={scope === 'cumulative' ? 'primary' : ''}
        style={{ padding: '4px 10px' }}
      >
        Cumulative
      </button>
      <button
        onClick={() => onScope('library')}
        className={scope === 'library' ? 'primary' : ''}
        style={{ padding: '4px 10px' }}
        disabled={queuedLibraries.length === 0}
      >
        Per library
      </button>
      {scope === 'library' && (
        <select
          value={selectedLib}
          onChange={(e) => onLib(e.target.value)}
          style={{ marginLeft: 4 }}
        >
          <option value="">— pick library —</option>
          {queuedLibraries.map((lib) => (
            <option key={lib} value={lib}>{lib}</option>
          ))}
        </select>
      )}
    </div>
  );
}

// ── 2A: status code bar chart ────────────────────────────────────────────────

function StatusCodeChart({
  snapshot,
  bucketKey,
}: {
  snapshot: DashboardSnapshot | null;
  bucketKey: string;
}) {
  const counts = snapshot?.http_status_counts?.[bucketKey] ?? {};
  const codes = useMemo(
    () => Object.keys(counts).map((k) => Number(k)).sort((a, b) => a - b),
    [counts],
  );

  if (codes.length === 0) {
    return (
      <div style={{ marginTop: 12 }}>
        <h3 style={{ fontSize: 13, margin: '8px 0 4px' }}>HTTP responses by status code</h3>
        <div className="empty" style={{ fontSize: 12 }}>No traffic recorded for this scope yet.</div>
      </div>
    );
  }

  const data = {
    labels: codes.map(statusLabel),
    datasets: [
      {
        label: 'Responses',
        data: codes.map((c) => counts[String(c)] ?? 0),
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

  return (
    <div style={{ marginTop: 12 }}>
      <h3 style={{ fontSize: 13, margin: '8px 0 4px' }}>HTTP responses by status code</h3>
      <div style={{ height: 180 }}>
        <Bar data={data} options={options} />
      </div>
    </div>
  );
}

// ── 2B: latency + rate line chart ────────────────────────────────────────────

function LatencyAndRateChart({
  snapshot,
  bucketKey,
  rpsMax,
  latMax,
}: {
  snapshot: DashboardSnapshot | null;
  bucketKey: string;
  rpsMax: number;
  latMax: number;
}) {
  const series = snapshot?.http_latency_series?.[bucketKey] ?? [];

  if (series.length === 0) {
    return (
      <div style={{ marginTop: 12 }}>
        <h3 style={{ fontSize: 13, margin: '8px 0 4px' }}>Requests / sec & average latency (last 60 s)</h3>
        <div className="empty" style={{ fontSize: 12 }}>No traffic recorded for this scope yet.</div>
      </div>
    );
  }

  // Shift the time axis to "seconds ago" so the rightmost label is
  // always 0 (now) and older buckets march to the left.
  const now = snapshot?.now ?? series[series.length - 1]?.t ?? Date.now() / 1000;
  const labels = series.map((s) => `${Math.round(s.t - now)}s`);

  const data = {
    labels,
    datasets: [
      {
        label: 'Requests / sec',
        data: series.map((s) => s.rps),
        borderColor: 'rgba(102, 187, 106, 0.95)',
        backgroundColor: 'rgba(102, 187, 106, 0.15)',
        yAxisID: 'y',
        fill: true,
        tension: 0.25,
      },
      {
        label: 'Avg latency (ms)',
        // v0.9.7 Item 1: avg_ms is null for buckets with no traffic
        // (backend leaves that field null rather than emitting 0).
        // ``spanGaps: true`` below tells Chart.js to skip those
        // points and connect across them — without it the line
        // would dive to zero between events.
        data: series.map((s) => s.avg_ms),
        borderColor: 'rgba(66, 165, 245, 0.95)',
        backgroundColor: 'rgba(66, 165, 245, 0.15)',
        yAxisID: 'y1',
        fill: false,
        tension: 0.25,
        spanGaps: true,
      },
    ],
  };

  const options = {
    responsive: true,
    maintainAspectRatio: false,
    interaction: { mode: 'index' as const, intersect: false },
    plugins: { legend: { position: 'top' as const, labels: { boxWidth: 12 } } },
    scales: {
      x: {
        ticks: {
          autoSkip: true,
          maxTicksLimit: 8,
        },
      },
      y: {
        type: 'linear' as const,
        position: 'left' as const,
        beginAtZero: true,
        // v0.9.7 Item 2: high-water mark for the run, never shrinks.
        // Cosmetic padding (10%) above the mark so peak points
        // aren't pinned to the top edge of the chart.
        max: Math.ceil(rpsMax * 1.1),
        ticks: { precision: 0 },
        title: { display: true, text: 'req/s' },
      },
      y1: {
        type: 'linear' as const,
        position: 'right' as const,
        beginAtZero: true,
        max: Math.ceil(latMax * 1.1),
        grid: { drawOnChartArea: false },
        ticks: { precision: 0 },
        title: { display: true, text: 'ms' },
      },
    },
  };

  return (
    <div style={{ marginTop: 12 }}>
      <h3 style={{ fontSize: 13, margin: '8px 0 4px' }}>Requests / sec & average latency (last 60 s)</h3>
      <div style={{ height: 200 }}>
        <Line data={data} options={options} />
      </div>
    </div>
  );
}

// ── 2C: rate-limit block + dedicated event feed ──────────────────────────────

function RateLimitBlock({ snapshot }: { snapshot: DashboardSnapshot | null }) {
  const rl = snapshot?.http_rate_limits ?? { count: 0, retries: 0, backing_off: false };
  const events = snapshot?.rate_limit_events ?? [];
  // Most recent first.
  const ordered = [...events].reverse();

  return (
    <div style={{ marginTop: 16, paddingTop: 12, borderTop: '1px solid var(--border, #2a3146)' }}>
      <h3 style={{ fontSize: 13, margin: '0 0 6px' }}>Rate-limit status</h3>
      <div className="kv" style={{ marginBottom: 10 }}>
        <div className="k">Total 429 responses</div>
        <div className="v">{rl.count.toLocaleString()}</div>
        <div className="k">Total retries</div>
        <div className="v">{rl.retries.toLocaleString()}</div>
        <div className="k">Backing off right now</div>
        <div className="v">
          {rl.backing_off ? (
            <span className="tag rate_limit">YES — waiting on Retry-After</span>
          ) : (
            <span style={{ color: 'var(--text-dim)' }}>No</span>
          )}
        </div>
      </div>

      <h3 style={{ fontSize: 13, margin: '8px 0 4px' }}>Rate-limit feed</h3>
      {ordered.length === 0 ? (
        <div className="empty" style={{ fontSize: 12 }}>No throttle events yet.</div>
      ) : (
        <div className="feed" style={{ maxHeight: 160, overflowY: 'auto' }}>
          {ordered.map((e, i) => (
            <div className="row" key={i}>
              <span style={{ color: 'var(--text-dim)' }}>{e.timestamp}</span>
              <span className="tag rate_limit">{statusLabel(e.status_code)}</span>
              <span style={{ color: 'var(--text-dim)' }}>{e.library}</span>
              <span style={{ fontSize: 12 }}>
                {e.retry_after_seconds !== null
                  ? `Retry-After: ${e.retry_after_seconds.toFixed(1)} s`
                  : 'no Retry-After header'}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// Tiny local helper mirroring the one in DashboardPanel so this
// component doesn't have to import it (and stays independently
// movable). Same styling rules.
function SectionHint({ children }: { children: React.ReactNode }) {
  return (
    <div style={{ marginTop: -4, marginBottom: 10, fontSize: 12, color: 'var(--text-dim)' }}>
      {children}
    </div>
  );
}
