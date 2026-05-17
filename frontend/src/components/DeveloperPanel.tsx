// Developer tab (Feature 3 phase 3.4).
//
// In-app unit test runner. Visible only when the backend's /api/health
// reports debug_mode=true (PLEXMIGRATE_DEBUG_MODE env var). The tab
// itself is gated at the App.tsx tab-strip level; this component
// trusts that gate but also re-checks before firing to catch the
// rare race where the end user disabled debug mode mid-session.
//
// Three modes (synthetic/structural/live) + an optional pytest -k
// filter for single-test or single-class runs. Live mode requires a
// typed confirmation phrase to acknowledge that the run will touch a
// real Plex server.

import { useEffect, useState } from 'react';
import { api, DevTestRunSummary } from '../api';

type Mode = 'synthetic' | 'structural' | 'live';

interface ModeDef {
  key: Mode;
  label: string;
  description: string;
}

const MODES: ModeDef[] = [
  {
    key: 'synthetic',
    label: 'Synthetic',
    description:
      'Run the standard backend unit suite (tests_backend/). Safe at any time; no live data is touched.',
  },
  {
    key: 'structural',
    label: 'Structural',
    description:
      'Copy real artefacts (media.db etc.) to a temp dir and assert schema / shape invariants against the copies. Originals are never opened in write mode.',
  },
  {
    key: 'live',
    label: 'Live',
    description:
      'Run read-only operations against a configured live Plex server. Requires explicit confirmation. Never restores or writes; only reads.',
  },
];

const LIVE_CONFIRM_PHRASE = 'RUN LIVE';

export function DeveloperPanel() {
  const [mode, setMode] = useState<Mode>('synthetic');
  const [filter, setFilter] = useState<string>('');
  const [liveConfirm, setLiveConfirm] = useState<string>('');
  const [running, setRunning] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const [lastRun, setLastRun] = useState<DevTestRunSummary | null>(null);
  const [history, setHistory] = useState<DevTestRunSummary[]>([]);
  const [loadingHistory, setLoadingHistory] = useState<boolean>(false);

  const refreshHistory = async () => {
    setLoadingHistory(true);
    try {
      const resp = await api.listDevTestRuns(25);
      setHistory(resp.runs);
    } catch (e) {
      // Surface but don't block the main flow; the history list is
      // a convenience, not the primary action.
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoadingHistory(false);
    }
  };

  useEffect(() => {
    refreshHistory();
  }, []);

  const liveConfirmed = liveConfirm.trim() === LIVE_CONFIRM_PHRASE;
  const canRun = !running && (mode !== 'live' || liveConfirmed);

  const handleRun = async () => {
    if (!canRun) return;
    setRunning(true);
    setError(null);
    try {
      const summary = await api.runDevTests({
        mode,
        pytest_filter: filter.trim() || null,
        confirm_live: mode === 'live' ? liveConfirmed : false,
      });
      setLastRun(summary);
      // Reset the live confirmation so a follow-up live run requires
      // the end user to type the phrase again. This is deliberate
      // friction.
      if (mode === 'live') setLiveConfirm('');
      refreshHistory();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setRunning(false);
    }
  };

  return (
    <div style={{ padding: 16 }}>
      <h2 style={{ marginTop: 0 }}>Developer Tools</h2>
      <p style={{ fontSize: 12, color: 'var(--text-dim)' }}>
        Unit test runner. Visible only when PLEXMIGRATE_DEBUG_MODE is set
        on the backend. Logs land at <code>server_data/logs/test_runs/</code>.
      </p>

      <ModeSelector mode={mode} onChange={setMode} disabled={running} />

      <FilterInput value={filter} onChange={setFilter} disabled={running} />

      {mode === 'live' && (
        <LiveConfirm
          value={liveConfirm}
          onChange={setLiveConfirm}
          phrase={LIVE_CONFIRM_PHRASE}
          confirmed={liveConfirmed}
          disabled={running}
        />
      )}

      <div style={{ marginTop: 12 }}>
        <button
          onClick={handleRun}
          disabled={!canRun}
          style={{
            fontSize: 13,
            padding: '6px 14px',
            background: mode === 'live' ? 'var(--accent-warn)' : 'var(--accent)',
            color: 'white',
            border: 'none',
            borderRadius: 4,
            cursor: canRun ? 'pointer' : 'not-allowed',
          }}
        >
          {running ? 'Running…' : `Run ${MODES.find((m) => m.key === mode)?.label} Tests`}
        </button>
      </div>

      {error && (
        <div className="banner error" style={{ marginTop: 12, fontSize: 12 }}>
          {error}
        </div>
      )}

      {lastRun && (
        <div style={{ marginTop: 16 }}>
          <h3 style={{ fontSize: 13, margin: '0 0 6px' }}>Most recent run</h3>
          <RunSummaryCard run={lastRun} />
        </div>
      )}

      <div style={{ marginTop: 16 }}>
        <div
          style={{
            display: 'flex',
            justifyContent: 'space-between',
            alignItems: 'center',
            marginBottom: 6,
          }}
        >
          <h3 style={{ fontSize: 13, margin: 0 }}>Run history</h3>
          <button
            onClick={refreshHistory}
            disabled={loadingHistory}
            style={{ fontSize: 12 }}
          >
            {loadingHistory ? 'Loading…' : 'Refresh'}
          </button>
        </div>
        {history.length === 0 && !loadingHistory && (
          <div className="empty" style={{ fontSize: 12 }}>
            No prior runs recorded yet.
          </div>
        )}
        {history.length > 0 && <HistoryTable runs={history} />}
      </div>
    </div>
  );
}

interface ModeSelectorProps {
  mode: Mode;
  onChange: (m: Mode) => void;
  disabled: boolean;
}

function ModeSelector({ mode, onChange, disabled }: ModeSelectorProps) {
  return (
    <div style={{ display: 'flex', gap: 8, marginTop: 12, flexWrap: 'wrap' }}>
      {MODES.map((m) => {
        const selected = m.key === mode;
        return (
          <label
            key={m.key}
            style={{
              flex: '1 1 250px',
              padding: 8,
              border: '1px solid var(--border)',
              borderRadius: 4,
              cursor: disabled ? 'not-allowed' : 'pointer',
              background: selected ? 'var(--bg-row-hover)' : undefined,
              opacity: disabled ? 0.6 : 1,
            }}
          >
            <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
              <input
                type="radio"
                checked={selected}
                disabled={disabled}
                onChange={() => onChange(m.key)}
              />
              <strong style={{ fontSize: 12 }}>{m.label}</strong>
            </div>
            <div style={{ fontSize: 11, color: 'var(--text-dim)', marginTop: 4 }}>
              {m.description}
            </div>
          </label>
        );
      })}
    </div>
  );
}

interface FilterInputProps {
  value: string;
  onChange: (v: string) => void;
  disabled: boolean;
}

function FilterInput({ value, onChange, disabled }: FilterInputProps) {
  return (
    <div style={{ marginTop: 12 }}>
      <label style={{ fontSize: 12, fontWeight: 600, display: 'block' }}>
        pytest -k filter (optional)
      </label>
      <input
        type="text"
        value={value}
        onChange={(ev) => onChange(ev.target.value)}
        disabled={disabled}
        placeholder="e.g. TestSmartMode or test_round_trip_identity"
        style={{ width: 380, padding: 4, fontSize: 12, marginTop: 4 }}
      />
      <div style={{ fontSize: 11, color: 'var(--text-dim)', marginTop: 4 }}>
        Empty runs every test in the selected mode. Allowed characters:
        alphanumerics, dot, colon, underscore, hyphen, brackets, space.
      </div>
    </div>
  );
}

interface LiveConfirmProps {
  value: string;
  onChange: (v: string) => void;
  phrase: string;
  confirmed: boolean;
  disabled: boolean;
}

function LiveConfirm({
  value,
  onChange,
  phrase,
  confirmed,
  disabled,
}: LiveConfirmProps) {
  return (
    <div
      style={{
        marginTop: 12,
        padding: 8,
        border: '1px solid var(--accent-warn)',
        borderRadius: 4,
        background: 'rgba(255, 165, 0, 0.05)',
      }}
    >
      <div style={{ fontSize: 12, fontWeight: 600 }}>
        Live mode confirmation required
      </div>
      <div style={{ fontSize: 11, color: 'var(--text-dim)', marginTop: 4 }}>
        Live tests connect to a real Plex server using stored
        credentials. They are READ-ONLY by contract, but a misbehaving
        test can still hit your real install. Type <code>{phrase}</code> below
        to enable the Run button.
      </div>
      <input
        type="text"
        value={value}
        onChange={(ev) => onChange(ev.target.value)}
        disabled={disabled}
        placeholder={phrase}
        style={{ width: 240, padding: 4, fontSize: 12, marginTop: 6 }}
      />
      <span
        style={{
          fontSize: 11,
          marginLeft: 8,
          color: confirmed ? 'var(--accent-ok)' : 'var(--text-dim)',
        }}
      >
        {confirmed ? '✓ confirmed' : 'not yet confirmed'}
      </span>
    </div>
  );
}

function formatDuration(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) return '-';
  if (seconds < 1) return `${(seconds * 1000).toFixed(0)} ms`;
  return `${seconds.toFixed(2)} s`;
}

function formatTimestamp(epochSeconds: number): string {
  if (!Number.isFinite(epochSeconds) || epochSeconds <= 0) return '-';
  return new Date(epochSeconds * 1000).toLocaleString();
}

function statusFromRun(run: DevTestRunSummary): {
  label: string;
  color: string;
} {
  if (run.totals.failed > 0 || run.totals.errors > 0) {
    return { label: 'FAILED', color: 'var(--accent-err)' };
  }
  if (run.totals.unexpected_pass > 0) {
    return { label: 'XPASS', color: 'var(--accent-warn)' };
  }
  if (run.exit_code !== 0) {
    return { label: `EXIT ${run.exit_code}`, color: 'var(--accent-err)' };
  }
  return { label: 'PASSED', color: 'var(--accent-ok)' };
}

function RunSummaryCard({ run }: { run: DevTestRunSummary }) {
  const status = statusFromRun(run);
  return (
    <div
      style={{
        padding: 10,
        border: '1px solid var(--border)',
        borderRadius: 4,
        fontSize: 12,
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
        <span
          style={{
            padding: '2px 6px',
            background: status.color,
            color: 'white',
            borderRadius: 3,
            fontSize: 11,
            fontWeight: 600,
          }}
        >
          {status.label}
        </span>
        <span style={{ fontFamily: 'monospace' }}>{run.run_id}</span>
        <span style={{ color: 'var(--text-dim)' }}>{run.mode}</span>
        <span style={{ color: 'var(--text-dim)' }}>
          {formatDuration(run.duration_seconds)}
        </span>
      </div>

      <div style={{ marginTop: 6, fontSize: 11, color: 'var(--text-dim)' }}>
        Started {formatTimestamp(run.started_at)} · Target {run.test_target}
        {run.filter ? ` · Filter -k "${run.filter}"` : ''}
      </div>

      <div style={{ marginTop: 8, fontSize: 11 }}>
        <strong>Totals:</strong> {run.totals.collected} collected,{' '}
        {run.totals.passed_normal} passed,{' '}
        {run.totals.passed_xfail > 0 && (
          <>
            {run.totals.passed_xfail} xfail-as-expected,{' '}
          </>
        )}
        {run.totals.failed > 0 && (
          <>
            <span style={{ color: 'var(--accent-err)' }}>
              {run.totals.failed} failed
            </span>
            ,{' '}
          </>
        )}
        {run.totals.errors > 0 && (
          <>
            <span style={{ color: 'var(--accent-err)' }}>
              {run.totals.errors} errors
            </span>
            ,{' '}
          </>
        )}
        {run.totals.unexpected_pass > 0 && (
          <>
            <span style={{ color: 'var(--accent-warn)' }}>
              {run.totals.unexpected_pass} unexpected pass
            </span>
            ,{' '}
          </>
        )}
        {run.totals.skipped} skipped
      </div>

      {run.failed_tests.length > 0 && (
        <details style={{ marginTop: 8 }}>
          <summary style={{ cursor: 'pointer', fontSize: 11 }}>
            Failed tests ({run.failed_tests.length})
          </summary>
          <ul style={{ marginTop: 4, paddingLeft: 18 }}>
            {run.failed_tests.map((ft) => (
              <li key={ft.nodeid} style={{ fontSize: 11, fontFamily: 'monospace' }}>
                {ft.nodeid}
                <div
                  style={{
                    fontSize: 10,
                    color: 'var(--text-dim)',
                    whiteSpace: 'pre-wrap',
                    marginTop: 2,
                  }}
                >
                  {ft.error_excerpt}
                </div>
              </li>
            ))}
          </ul>
        </details>
      )}

      {run.xfail_tests.length > 0 && (
        <details style={{ marginTop: 6 }}>
          <summary style={{ cursor: 'pointer', fontSize: 11 }}>
            Passed by expected failure / xfail ({run.xfail_tests.length})
          </summary>
          <ul style={{ marginTop: 4, paddingLeft: 18 }}>
            {run.xfail_tests.map((xt) => (
              <li key={xt.nodeid} style={{ fontSize: 11, fontFamily: 'monospace' }}>
                {xt.nodeid}
                {xt.reason && (
                  <span style={{ color: 'var(--text-dim)' }}> · {xt.reason}</span>
                )}
              </li>
            ))}
          </ul>
        </details>
      )}

      {run.raises_tests.length > 0 && (
        <details style={{ marginTop: 6 }}>
          <summary style={{ cursor: 'pointer', fontSize: 11 }}>
            Tests passing via pytest.raises ({run.raises_tests.length})
          </summary>
          <ul style={{ marginTop: 4, paddingLeft: 18 }}>
            {run.raises_tests.map((nid) => (
              <li key={nid} style={{ fontSize: 11, fontFamily: 'monospace' }}>
                {nid}
              </li>
            ))}
          </ul>
        </details>
      )}

      <div style={{ marginTop: 8, fontSize: 11, color: 'var(--text-dim)' }}>
        Raw log: <code>{run.log_path}</code>
      </div>
    </div>
  );
}

function HistoryTable({ runs }: { runs: DevTestRunSummary[] }) {
  return (
    <table style={{ width: '100%', fontSize: 12, borderCollapse: 'collapse' }}>
      <thead>
        <tr style={{ textAlign: 'left', color: 'var(--text-dim)' }}>
          <th style={{ padding: '4px 6px' }}>Status</th>
          <th style={{ padding: '4px 6px' }}>Mode</th>
          <th style={{ padding: '4px 6px' }}>Started</th>
          <th style={{ padding: '4px 6px' }}>Duration</th>
          <th style={{ padding: '4px 6px' }}>Totals</th>
          <th style={{ padding: '4px 6px' }}>Run ID</th>
        </tr>
      </thead>
      <tbody>
        {runs.map((r) => {
          const s = statusFromRun(r);
          return (
            <tr key={r.run_id} style={{ borderTop: '1px solid var(--border)' }}>
              <td style={{ padding: '4px 6px' }}>
                <span
                  style={{
                    padding: '1px 5px',
                    background: s.color,
                    color: 'white',
                    borderRadius: 3,
                    fontSize: 10,
                    fontWeight: 600,
                  }}
                >
                  {s.label}
                </span>
              </td>
              <td style={{ padding: '4px 6px' }}>{r.mode}</td>
              <td style={{ padding: '4px 6px' }}>
                {formatTimestamp(r.started_at)}
              </td>
              <td style={{ padding: '4px 6px' }}>
                {formatDuration(r.duration_seconds)}
              </td>
              <td style={{ padding: '4px 6px' }}>
                {r.totals.passed_normal}p
                {r.totals.failed > 0 && (
                  <span style={{ color: 'var(--accent-err)' }}>
                    {' '}/ {r.totals.failed}f
                  </span>
                )}
                {r.totals.errors > 0 && (
                  <span style={{ color: 'var(--accent-err)' }}>
                    {' '}/ {r.totals.errors}e
                  </span>
                )}
              </td>
              <td
                style={{
                  padding: '4px 6px',
                  fontFamily: 'monospace',
                  color: 'var(--text-dim)',
                }}
              >
                {r.run_id}
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}
