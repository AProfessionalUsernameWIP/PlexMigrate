// Top-level layout: a sticky topbar, a tab strip, and the active
// tab's panel below it. The WebSocket subscription lives here so
// child components can read the latest snapshot from props without
// each owning its own socket.
//
// State management note: we intentionally use plain useState +
// useEffect instead of a global store. The data flow is one-way
// (server pushes snapshots, components render them) and the surface
// is small. Introducing Redux/Zustand here would be ceremony without
// payoff.

import { useEffect, useRef, useState } from 'react';
import {
  dashboardSocket,
  SnapshotMessage,
  ServerTime,
  api,
} from './api';
import { DashboardPanel } from './components/DashboardPanel';
import { JobFormPanel } from './components/JobFormPanel';
import { SchedulesPanel } from './components/SchedulesPanel';
import { LogsPanel } from './components/LogsPanel';
import { ExportsPanel } from './components/ExportsPanel';
import { SettingsPanel } from './components/SettingsPanel';
import { ServersPanel } from './components/ServersPanel';

type Tab = 'dashboard' | 'run' | 'servers' | 'schedules' | 'logs' | 'exports' | 'settings';

// Connection states surfaced in the topbar dot. "connecting" is the
// initial state before the first WS frame; the dashboard panel uses
// it to render a "Connecting…" placeholder instead of the "no job
// running" empty state, which used to read as "didn't load".
type ConnState = 'connecting' | 'connected' | 'disconnected';

// How long to keep showing the last snapshot's totals after a job
// finishes. Without this the dashboard goes blank the instant the
// engine returns, hiding the final counters the user just earned.
const POST_FINISH_RETAIN_MS = 30_000;

export function App() {
  // The single source of truth for live state, pushed in by the WebSocket.
  const [snapshot, setSnapshot] = useState<SnapshotMessage | null>(null);
  const [conn, setConn] = useState<ConnState>('connecting');
  const [tab, setTab] = useState<Tab>('dashboard');
  // Server-side clock surfaced in the topbar. We fetch the timezone +
  // an initial wallclock from /api/server-time, then tick locally using
  // a skew offset (server_now - browser_now). One periodic re-fetch
  // every 5 min keeps DST transitions and host clock drift in line
  // without polling once per second.
  const [serverTime, setServerTime] = useState<ServerTime | null>(null);
  const [, setClockTick] = useState(0);
  const clockSkewRef = useRef<number>(0);
  // Keep the last seen "running"/"stopping" snapshot around for a
  // brief grace period after the job finishes so the user can read
  // the final totals before the panel goes blank.
  const lastRunningRef = useRef<{ snap: SnapshotMessage; at: number } | null>(null);

  // ── WebSocket lifecycle ────────────────────────────────────────────────
  useEffect(() => {
    // The socket reconnects itself; the connected indicator just
    // tracks whether we've received any message in the last 5 s.
    let lastMsg = 0;
    const unsubscribe = dashboardSocket.subscribe((msg) => {
      lastMsg = Date.now();
      // Capture in-flight snapshots so we can keep them visible for
      // ~30s after the engine exits. Stopping counts as running for
      // this purpose (the snapshot is still meaningful while the
      // wind-down finishes).
      const state = msg.job?.state;
      if (state === 'running' || state === 'stopping' || msg.dashboard) {
        lastRunningRef.current = { snap: msg, at: lastMsg };
      }
      setSnapshot(msg);
      setConn('connected');
    });
    const tick = window.setInterval(() => {
      if (Date.now() - lastMsg > 5000) setConn('disconnected');
    }, 1000);
    return () => {
      unsubscribe();
      window.clearInterval(tick);
    };
  }, []);

  // Server-clock lifecycle: one initial fetch + a 5-minute refresh,
  // plus a 1 Hz local tick so the rendered HH:MM:SS advances smoothly.
  useEffect(() => {
    const load = () => {
      api.getServerTime()
        .then((st) => {
          setServerTime(st);
          clockSkewRef.current = st.now * 1000 - Date.now();
        })
        .catch(() => { /* keep prior value; render falls back to '—' */ });
    };
    load();
    const refresh = window.setInterval(load, 5 * 60 * 1000);
    const tick = window.setInterval(() => setClockTick((x) => x + 1), 1000);
    return () => {
      window.clearInterval(refresh);
      window.clearInterval(tick);
    };
  }, []);

  // First-load fallback: if the WS hasn't arrived yet, fetch a one-shot
  // snapshot via REST so the dashboard isn't blank for the first second.
  useEffect(() => {
    if (snapshot !== null) return;
    let cancelled = false;
    (async () => {
      try {
        const j = await api.getJob();
        if (cancelled) return;
        setSnapshot({
          type: 'snapshot',
          server_ts: Date.now() / 1000,
          dashboard: j.dashboard || null,
          job:
            j.state === 'idle'
              ? null
              : {
                  job_id: '',
                  mode: (j.mode as 'export' | 'import') || 'export',
                  state: j.state as JobPayloadState,
                  queued_at: 0,
                  started_at: null,
                  finished_at: null,
                  error: j.error || null,
                  run_log_dir: null,
                  params: {},
                },
        });
      } catch {
        // Server unreachable on first paint — the WS reconnect loop
        // will fill in the data once the backend is up.
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [snapshot]);

  // Choose the snapshot to render. If the live job state is idle (or
  // there's no live snapshot at all) but we saw a running snapshot in
  // the last POST_FINISH_RETAIN_MS, show that instead so the user can
  // read the final totals after the engine returns.
  const displaySnapshot = (() => {
    if (snapshot && snapshot.dashboard) return snapshot;
    const retained = lastRunningRef.current;
    if (retained && Date.now() - retained.at < POST_FINISH_RETAIN_MS) {
      return retained.snap;
    }
    return snapshot;
  })();

  const dotClass = conn === 'connected' ? 'green' : conn === 'connecting' ? 'amber' : 'red';
  const connLabel = conn === 'connected' ? 'Live' : conn === 'connecting' ? 'Connecting…' : 'Disconnected';

  // Format the topbar clock in the *server's* timezone. We feed the
  // skew-corrected wallclock into Intl.DateTimeFormat with the IANA
  // zone the backend reported. If the backend only returned a short
  // abbreviation (no $TZ set), Intl will reject it — fall back to the
  // browser's local zone so the topbar still renders.
  const clockLabel = (() => {
    if (!serverTime) return null;
    const d = new Date(Date.now() + clockSkewRef.current);
    const fmtOpts: Intl.DateTimeFormatOptions = {
      hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
    };
    try {
      return new Intl.DateTimeFormat(undefined, { ...fmtOpts, timeZone: serverTime.tz }).format(d);
    } catch {
      return new Intl.DateTimeFormat(undefined, fmtOpts).format(d);
    }
  })();
  const clockTzLabel = serverTime?.tz_abbrev || serverTime?.tz || '';

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">PlexMigrate</div>
        <div className="conn" style={{ display: 'flex', alignItems: 'center', gap: 14 }}>
          {clockLabel && (
            <span
              className="mono"
              title={serverTime ? `Server time (${serverTime.tz})` : ''}
              style={{ color: 'var(--text-dim)', fontSize: 13 }}
            >
              {clockLabel}
              {clockTzLabel && (
                <span style={{ marginLeft: 6, opacity: 0.75 }}>{clockTzLabel}</span>
              )}
            </span>
          )}
          <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
            <span className={`dot ${dotClass}`} />
            {connLabel}
          </span>
        </div>
      </header>

      <nav className="tabs">
        <button className={tab === 'dashboard' ? 'active' : ''} onClick={() => setTab('dashboard')}>Dashboard</button>
        <button className={tab === 'run' ? 'active' : ''} onClick={() => setTab('run')}>Run Job</button>
        <button className={tab === 'servers' ? 'active' : ''} onClick={() => setTab('servers')}>Servers</button>
        <button className={tab === 'schedules' ? 'active' : ''} onClick={() => setTab('schedules')}>Schedules</button>
        <button className={tab === 'logs' ? 'active' : ''} onClick={() => setTab('logs')}>Logs</button>
        <button className={tab === 'exports' ? 'active' : ''} onClick={() => setTab('exports')}>Exports</button>
        <button className={tab === 'settings' ? 'active' : ''} onClick={() => setTab('settings')}>Settings</button>
      </nav>

      <main className="main">
        {tab === 'dashboard' && <DashboardPanel snapshot={displaySnapshot} connState={conn} />}
        {tab === 'run' && <JobFormPanel snapshot={snapshot} />}
        {tab === 'servers' && <ServersPanel />}
        {tab === 'schedules' && <SchedulesPanel />}
        {tab === 'logs' && <LogsPanel />}
        {tab === 'exports' && <ExportsPanel />}
        {tab === 'settings' && <SettingsPanel />}
      </main>
    </div>
  );
}

// String-literal alias used in one place only.
type JobPayloadState = 'idle' | 'queued' | 'running' | 'completed' | 'failed' | 'cancelled';
