// Runtime breakdown panel.
//
// Reads the per-run timing rows recorded by services.run_timer +
// server.run_timings_db. The dashboard surface ONLY shows the most
// recent run - never accumulates history - so an idle dashboard
// returns to "no run yet" between jobs. The long-term historical
// view lives at Servers > Recent Runtimes.
//
// Clicking the single row drills into the per-operation breakdown
// for that run. The whole panel is collapsible via a Verbose toggle
// stored in localStorage: per decision D3, default ON (visible) but
// end user-collapsible.
//
// This panel is deliberately small and unopinionated. It does not
// render a per-phase tree or aggregate by library on the client side;
// the backend's get_run_entries returns entries in chronological
// order and we display them as a flat table with scope + library +
// user columns the end user can scan visually. A future iteration
// can add grouping once we know which views end users actually use.

import { useEffect, useRef, useState } from 'react';
import { api, RuntimeRunSummary, RuntimeEntry } from '../api';
import { errorText, formatTimestamp } from '../utils/format';

// Job-state shape the dashboard hands down so the panel can detect
// the running -> terminal transition and refetch automatically.
// Imported via the existing JobPayload type would be cleaner but
// requires touching more imports; the narrow inline shape below is
// enough for the auto-refresh trigger.
interface RuntimeBreakdownPanelProps {
  job?: {
    job_id?: string;
    state?: string;
    run_log_dir?: string | null;
  } | null;
}

// Terminal job states the worker sets when a job ends. Mirrors the
// list jobs.py uses to decide when to write a run_history row.
const TERMINAL_STATES = new Set([
  'completed', 'completed_with_errors', 'failed', 'cancelled',
]);

const LS_KEY = 'plexmigrate.runtime_panel_visible';

function readVisiblePreference(): boolean {
  // localStorage may be unavailable (private browsing, iframe). Treat
  // any failure as "use the default" rather than crashing the panel.
  try {
    const raw = window.localStorage.getItem(LS_KEY);
    if (raw === null) return true; // D3: default ON
    return raw === '1';
  } catch {
    return true;
  }
}

function writeVisiblePreference(visible: boolean): void {
  try {
    window.localStorage.setItem(LS_KEY, visible ? '1' : '0');
  } catch {
    /* non-fatal */
  }
}

function formatDuration(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) return '-';
  if (seconds < 1) return `${(seconds * 1000).toFixed(0)} ms`;
  if (seconds < 60) return `${seconds.toFixed(2)} s`;
  const m = Math.floor(seconds / 60);
  const s = Math.round(seconds - m * 60);
  return `${m}m ${s.toString().padStart(2, '0')}s`;
}


function formatExtra(extra: Record<string, unknown>): string {
  const keys = Object.keys(extra || {});
  if (keys.length === 0) return '';
  return keys.map((k) => `${k}=${String(extra[k])}`).join(' · ');
}

export function RuntimeBreakdownPanel({ job }: RuntimeBreakdownPanelProps = {}) {
  const [visible, setVisibleState] = useState<boolean>(readVisiblePreference);
  const [runs, setRuns] = useState<RuntimeRunSummary[]>([]);
  const [loadingRuns, setLoadingRuns] = useState<boolean>(false);
  const [runsError, setRunsError] = useState<string | null>(null);

  const [expandedRunId, setExpandedRunId] = useState<string | null>(null);
  const [entries, setEntries] = useState<RuntimeEntry[]>([]);
  const [loadingEntries, setLoadingEntries] = useState<boolean>(false);
  const [entriesError, setEntriesError] = useState<string | null>(null);

  // Auto-refresh the panel when a job transitions from 'running' to
  // a terminal state. Without this the panel only fetches on mount +
  // on manual Refresh click, so after a restore job finishes the
  // user keeps seeing the previous job's row until they click
  // Refresh. We track the previous job state in a ref and
  // fire refreshRuns() on the running -> terminal edge.
  const prevJobStateRef = useRef<string | null>(null);

  const setVisible = (v: boolean) => {
    setVisibleState(v);
    writeVisiblePreference(v);
  };

  const refreshRuns = async () => {
    setLoadingRuns(true);
    setRunsError(null);
    try {
      // The dashboard ONLY shows the most recent
      // run. Asking for limit=1 prevents the panel from ever
      // accumulating a list of older runs on the live dashboard.
      // Historical browsing lives at Servers > Recent Runtimes.
      const resp = await api.listRuntimeRuns(1);
      setRuns(resp.runs);
    } catch (e) {
      setRunsError(errorText(e));
    } finally {
      setLoadingRuns(false);
    }
  };

  // Initial fetch on mount; we don't poll on a timer because the
  // dashboard's existing WebSocket already drives the live-run UI.
  // End users who want fresh data hit the Refresh button. Fetching
  // is skipped when the panel is hidden so the request volume reflects
  // end user interest.
  useEffect(() => {
    if (!visible) return;
    refreshRuns();
  }, [visible]);

  // Watch the job state transition. When the dashboard reports a job
  // just transitioned from 'running' (or 'queued') to a terminal
  // state, refetch the run summary so the end user sees the
  // just-completed run's row instead of the previous one. Also fires
  // when run_log_dir changes between renders (defensive: covers a
  // job that finished while the WS reconnected and skipped frames).
  useEffect(() => {
    if (!visible) return;
    const currentState = job?.state ?? null;
    const prev = prevJobStateRef.current;
    prevJobStateRef.current = currentState;
    if (!currentState) return;
    if (prev && prev !== currentState && TERMINAL_STATES.has(currentState)) {
      // Small debounce so the run_history row has landed by the time
      // we re-fetch. The worker writes run_history in its central
      // finally block; on a heavily loaded machine the WS state can
      // arrive a few hundred ms before the disk write commits.
      const t = setTimeout(refreshRuns, 250);
      return () => clearTimeout(t);
    }
  }, [job?.state, job?.run_log_dir, visible]);

  const expandRun = async (runId: string) => {
    // Clicking the same row twice collapses it; this matches the
    // common expand-collapse pattern from other panels.
    if (expandedRunId === runId) {
      setExpandedRunId(null);
      setEntries([]);
      return;
    }
    setExpandedRunId(runId);
    setEntries([]);
    setLoadingEntries(true);
    setEntriesError(null);
    try {
      const resp = await api.getRuntimeRunDetail(runId);
      setEntries(resp.entries);
    } catch (e) {
      setEntriesError(errorText(e));
    } finally {
      setLoadingEntries(false);
    }
  };

  return (
    <div
      className="panel"
      style={{ marginTop: 12, padding: 12, border: '1px solid var(--border)' }}
    >
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          gap: 8,
          marginBottom: visible ? 8 : 0,
        }}
      >
        <h3 style={{ margin: 0, fontSize: 13 }}>
          Runtime breakdown
          <span
            style={{ marginLeft: 6, fontSize: 11, color: 'var(--text-dim)' }}
            title="The dashboard only shows the most recent run. For historical runs, see Servers > Recent Runtimes."
          >
            (last run)
          </span>
        </h3>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          {visible && (
            <button
              onClick={refreshRuns}
              disabled={loadingRuns}
              style={{ fontSize: 12 }}
            >
              {loadingRuns ? 'Loading…' : 'Refresh'}
            </button>
          )}
          <label
            style={{ fontSize: 12, display: 'inline-flex', alignItems: 'center', gap: 4 }}
            title="Toggle visibility of the Runtime breakdown panel. Preference is stored per-browser."
          >
            <input
              type="checkbox"
              checked={visible}
              onChange={(ev) => setVisible(ev.target.checked)}
            />
            Verbose
          </label>
        </div>
      </div>

      {!visible ? null : (
        <>
          {runsError && (
            <div className="banner error" style={{ fontSize: 12, marginBottom: 6 }}>
              Could not load recent runs: {runsError}
            </div>
          )}

          {!runsError && runs.length === 0 && !loadingRuns && (
            <div className="empty" style={{ fontSize: 12 }}>
              No runs recorded yet. The first snapshot or restore will populate this list.
            </div>
          )}

          {runs.length > 0 && (
            <table style={{ width: '100%', fontSize: 12, borderCollapse: 'collapse' }}>
              <thead>
                <tr style={{ textAlign: 'left', color: 'var(--text-dim)' }}>
                  <th style={{ padding: '4px 6px' }}>Started</th>
                  <th style={{ padding: '4px 6px' }}>Duration</th>
                  <th style={{ padding: '4px 6px' }}>Entries</th>
                  <th style={{ padding: '4px 6px' }}>Items</th>
                  <th style={{ padding: '4px 6px' }}>Run ID</th>
                </tr>
              </thead>
              <tbody>
                {runs.map((r) => {
                  const isExpanded = expandedRunId === r.run_id;
                  return (
                    <RuntimeRunRow
                      key={r.run_id}
                      run={r}
                      isExpanded={isExpanded}
                      onToggle={() => expandRun(r.run_id)}
                      entries={isExpanded ? entries : []}
                      loadingEntries={isExpanded && loadingEntries}
                      entriesError={isExpanded ? entriesError : null}
                    />
                  );
                })}
              </tbody>
            </table>
          )}
        </>
      )}
    </div>
  );
}

interface RuntimeRunRowProps {
  run: RuntimeRunSummary;
  isExpanded: boolean;
  onToggle: () => void;
  entries: RuntimeEntry[];
  loadingEntries: boolean;
  entriesError: string | null;
}

function RuntimeRunRow({
  run,
  isExpanded,
  onToggle,
  entries,
  loadingEntries,
  entriesError,
}: RuntimeRunRowProps) {
  return (
    <>
      <tr
        onClick={onToggle}
        style={{
          cursor: 'pointer',
          borderTop: '1px solid var(--border)',
          background: isExpanded ? 'var(--bg-row-hover)' : undefined,
        }}
      >
        <td style={{ padding: '4px 6px' }}>{formatTimestamp(run.started_at)}</td>
        <td style={{ padding: '4px 6px' }}>{formatDuration(run.duration_seconds)}</td>
        <td style={{ padding: '4px 6px' }}>{run.entry_count}</td>
        <td style={{ padding: '4px 6px' }}>{run.total_items_processed}</td>
        <td
          style={{
            padding: '4px 6px',
            fontFamily: 'monospace',
            color: 'var(--text-dim)',
          }}
        >
          {isExpanded ? '▼ ' : '▶ '}
          {run.run_id}
        </td>
      </tr>
      {isExpanded && (
        <tr>
          <td colSpan={5} style={{ padding: 6 }}>
            {entriesError && (
              <div className="banner error" style={{ fontSize: 12 }}>
                Could not load entries: {entriesError}
              </div>
            )}
            {loadingEntries && (
              <div className="empty" style={{ fontSize: 12 }}>
                Loading entries…
              </div>
            )}
            {!entriesError && !loadingEntries && entries.length === 0 && (
              <div className="empty" style={{ fontSize: 12 }}>
                No entries recorded for this run.
              </div>
            )}
            {entries.length > 0 && <RuntimeEntriesTable entries={entries} />}
          </td>
        </tr>
      )}
    </>
  );
}

function RuntimeEntriesTable({ entries }: { entries: RuntimeEntry[] }) {
  return (
    <table
      style={{
        width: '100%',
        fontSize: 11,
        borderCollapse: 'collapse',
        background: 'var(--bg-nested)',
      }}
    >
      <thead>
        <tr style={{ textAlign: 'left', color: 'var(--text-dim)' }}>
          <th style={{ padding: '3px 6px' }}>Scope</th>
          <th style={{ padding: '3px 6px' }}>Label</th>
          <th style={{ padding: '3px 6px' }}>Library</th>
          <th style={{ padding: '3px 6px' }}>User</th>
          <th style={{ padding: '3px 6px', textAlign: 'right' }}>Duration</th>
          <th style={{ padding: '3px 6px', textAlign: 'right' }}>Items</th>
          <th style={{ padding: '3px 6px' }}>Extra</th>
        </tr>
      </thead>
      <tbody>
        {entries.map((e) => (
          <tr key={e.id} style={{ borderTop: '1px solid var(--border)' }}>
            <td style={{ padding: '3px 6px' }}>
              <span
                style={{
                  fontSize: 10,
                  padding: '1px 4px',
                  borderRadius: 3,
                  background: 'var(--bg-chip)',
                  textTransform: 'uppercase',
                }}
              >
                {e.scope}
              </span>
            </td>
            <td style={{ padding: '3px 6px', fontFamily: 'monospace' }}>
              {e.label}
            </td>
            <td style={{ padding: '3px 6px' }}>{e.library || '-'}</td>
            <td style={{ padding: '3px 6px' }}>{e.user_handle || '-'}</td>
            <td style={{ padding: '3px 6px', textAlign: 'right', fontVariantNumeric: 'tabular-nums' }}>
              {formatDuration(e.duration_seconds)}
            </td>
            <td style={{ padding: '3px 6px', textAlign: 'right' }}>
              {e.items_processed ?? '-'}
            </td>
            <td
              style={{
                padding: '3px 6px',
                color: 'var(--text-dim)',
                fontFamily: 'monospace',
                fontSize: 10,
              }}
            >
              {formatExtra(e.extra)}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
