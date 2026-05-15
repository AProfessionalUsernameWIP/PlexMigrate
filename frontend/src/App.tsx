// Top-level layout: a sticky topbar, a tab strip, and the active
// tab's panel below it. The WebSocket subscription lives here so
// child components can read the latest snapshot from props without
// each owning its own socket.
//
// v0.11.0 - opt-in JWT auth wraps the whole app. On mount we hit
// /api/auth/status once to decide what to render:
//   * auth_enabled = false               → render <Main /> straight away
//   * auth_enabled, setup_needed         → render <SetupPage />
//   * auth_enabled, no token in memory   → render <LoginPage />
//   * auth_enabled, token present        → render <Main /> with the
//                                          token attached to every
//                                          REST + WebSocket call
//
// The access token lives in component state only - never in
// localStorage, sessionStorage, or cookies. Closing the tab logs the
// operator out. This is the deliberate trade-off documented in
// roadmapplan4.md: a locally-hosted tool exposed beyond localhost
// should optimise for "no persisted creds in the browser" over
// "stay-signed-in" UX.

import { useEffect, useRef, useState } from 'react';
import {
  AuthStatus,
  MeResponse,
  Role,
  api,
  dashboardWsClient,
  onUnauthorized,
  setAccessToken,
  ServerTime,
  DashboardFrame,
} from './api';
import { AuthProvider, ROLE_RANK, useAuthContext } from './contexts/AuthContext';
import { ClockProvider, useClockDisplay } from './contexts/ClockContext';
import { usePermission } from './hooks/usePermission';
import { DashboardPanel } from './components/DashboardPanel';
import { JobFormPanel } from './components/JobFormPanel';
import { SchedulesPanel } from './components/SchedulesPanel';
import { LogsPanel } from './components/LogsPanel';
import { ExportsPanel } from './components/ExportsPanel';
import { SettingsPanel } from './components/SettingsPanel';
import { AccountsPanel } from './components/AccountsPanel';
import { AccountSettingsPanel } from './components/AccountSettingsPanel';
import { UserAccountsExplorer } from './components/UserAccountsExplorer';
import { ServersPanel } from './components/ServersPanel';
import { NetworkingPanel } from './components/NetworkingPanel';
import { ServerAdvancedSettingsPanel } from './components/ServerAdvancedSettingsPanel';
import { RunDefaultsPanel } from './components/RunDefaultsPanel';
import { TunablesPanel } from './components/TunablesPanel';
import { AccessControlPanel } from './components/AccessControlPanel';
import { UserManagementPanel } from './components/UserManagementPanel';
import { LoginPage } from './components/LoginPage';
import { SetupPage } from './components/SetupPage';
import { InfoTip } from './components/InfoTip';
import { HelpPanel } from './components/HelpPanel';

type Tab = 'dashboard' | 'run' | 'servers' | 'account' | 'settings';
// Sub-tabs nested under Run Job. Persists across navigation.
type RunSubTab = 'run' | 'schedules';
// Sub-tabs nested under Servers. PR-7 removed 'logs' and 'snapshots'
// from here and moved them under Settings; PR-10 added 'users' for
// the User Management panel (operator+ only).
type ServersSubTab = 'servers' | 'networking' | 'users' | 'run_defaults' | 'advanced' | 'exports';
// Sub-tabs nested under Account. ``account`` is every operator's own
// self-service surface (display name, password, clock). ``account_management``
// is admin/root_admin only and contains the Database Admin Account
// page + the User Accounts explorer, switched between via the
// AccountMgmtPage inner nav. The whole sub-strip is suppressed for
// roles that can only see one of the two (i.e. anyone without
// ``users.manage``).
type AccountSubTab = 'account' | 'account_management';
// Sub-tabs nested under Settings. After the Account-promotion, the
// Settings tab houses system-level operator views only: system
// preferences, logs, exports, plus a flat Help reference page.
type SettingsSubTab = 'settings' | 'tunables' | 'logs' | 'help';

// Inner nav inside the Account Management sub-tab. Three pages:
//   db_admin       - Database Admin Account credential (db_admin.access)
//   user_accounts  - User Accounts explorer (users.manage)
//   access_control - Access Control (per-user permission grants/revokes;
//                    root_admin only - gated by canManageAccessControl)
type AccountMgmtPage = 'db_admin' | 'user_accounts' | 'access_control';

// Connection states surfaced in the topbar dot. "connecting" is the
// initial state before the first WS frame; the dashboard panel uses
// it to render a "Connecting…" placeholder instead of the "no job
// running" empty state, which used to read as "didn't load".
type ConnState = 'connecting' | 'connected' | 'disconnected';

// How long to keep showing the last snapshot's totals after a job
// finishes. Without this the dashboard goes blank the instant the
// engine returns, hiding the final counters the user just earned.
const POST_FINISH_RETAIN_MS = 30_000;

// ── Outer App: auth gating ───────────────────────────────────────────────────

export function App() {
  // PR-A2: auth is always on. The status probe is now used only to
  // detect first-boot setup (``setup_needed: true``); the
  // ``auth_enabled`` field is preserved on the wire for backward-
  // compat but ignored by the frontend.
  const [authStatus, setAuthStatus] = useState<AuthStatus | null>(null);
  const [authError, setAuthError] = useState<string | null>(null);
  // Access token lives only in React state for the lifetime of this
  // <App /> (LoginBugFix1: no localStorage / sessionStorage). What
  // survives across page refreshes is the HttpOnly refresh-token
  // cookie set by the backend at Path=/api/auth - the boot effect
  // below silently POSTs to /api/auth/refresh and adopts the new
  // access token if the cookie is still valid.
  const [token, setToken] = useState<string | null>(null);
  // ``refreshAttempted`` flips to true once the boot-time silent
  // refresh either succeeds or fails. Before that we render a neutral
  // loading splash - NOT the login form - so a returning user with a
  // valid refresh cookie doesn't see a login flash on every page load.
  const [refreshAttempted, setRefreshAttempted] = useState(false);
  // PR-A3: the authoritative identity comes from ``/api/auth/me``
  // (fetched whenever ``token`` changes). ``null`` while the fetch is
  // in flight - the App renders a splash until it lands so role-
  // gated children never see a half-formed AuthContext.
  const [me, setMe] = useState<MeResponse | null>(null);
  const [meError, setMeError] = useState<string | null>(null);

  // Register the 401 handler once. With refresh tokens in play, this
  // fires only AFTER api.ts's internal silent-refresh-and-retry has
  // already failed - i.e. the refresh cookie is gone or revoked. We
  // drop the in-memory state and the render gate below sends the
  // operator to the login screen.
  useEffect(() => {
    onUnauthorized(() => {
      setAccessToken(null);
      setToken(null);
      setMe(null);
    });
  }, []);

  // First-mount: probe /api/auth/status (setup vs login) and attempt
  // a silent refresh in parallel. Both must resolve before we render
  // anything other than the splash - otherwise a returning user with
  // a valid refresh cookie would briefly see the login form between
  // mount and the refresh resolving.
  useEffect(() => {
    let cancelled = false;
    const statusP = api.getAuthStatus()
      .then((s) => { if (!cancelled) setAuthStatus(s); })
      .catch((e) => { if (!cancelled) setAuthError(String(e)); });
    const refreshP = api.authRefresh()
      .then((session) => {
        if (cancelled || !session) return;
        // Adopt the freshly-issued access token. setAccessToken in
        // api.ts is called inside authRefresh already; mirror the
        // value into React state so the render gate below allows
        // <Main /> to mount.
        setToken(session.access_token);
      })
      .catch(() => { /* network error - treated as "not signed in" */ });
    Promise.allSettled([statusP, refreshP]).then(() => {
      if (!cancelled) setRefreshAttempted(true);
    });
    return () => { cancelled = true; };
  }, []);

  // Whenever we have a token, fetch the authoritative identity. This
  // also runs after login/setup because the acceptSession helper
  // sets ``token`` which triggers this effect.
  useEffect(() => {
    if (!token) {
      setMe(null);
      return;
    }
    let cancelled = false;
    setMeError(null);
    api.authMe()
      .then((r) => { if (!cancelled) setMe(r); })
      .catch((e) => {
        if (cancelled) return;
        // 401 is handled by the global onUnauthorized handler above.
        // Surface anything else as a visible error in the splash.
        setMeError(String(e));
      });
    return () => { cancelled = true; };
  }, [token]);

  // Session-acceptance helper used by both LoginPage and SetupPage.
  // Critical: push the token into the api module's closure SYNCHRONOUSLY
  // BEFORE setting React state - otherwise the next render's effects
  // (WebSocket connect, /me fetch) read a null token from the closure
  // and 401 immediately, clearing the freshly-issued token.
  //
  // LoginBugFix1 - no ``remember`` flag, no storage write. Token is
  // in-memory only for the lifetime of this <App />.
  const acceptSession = (session: { access_token: string }) => {
    setAccessToken(session.access_token);
    setToken(session.access_token);
  };

  // Refresh /me into context - used by AccountSettingsPanel (PR-A5)
  // after a self-display-name edit so the topbar chip updates without
  // a page reload.
  const refreshMe = async () => {
    try {
      const r = await api.authMe();
      setMe(r);
    } catch {
      /* 401 handler covers stale-token case */
    }
  };

  // Loading splash - shown until BOTH boot probes finish: the
  // /auth/status check (setup vs login) AND the silent /refresh
  // attempt. Holding the splash through the refresh prevents a
  // returning user from seeing the login form flash before their
  // session is silently restored.
  if (authStatus === null || !refreshAttempted) {
    return (
      <div className="app">
        <header className="topbar">
          <div className="brand">PlexMigrate</div>
        </header>
        <main className="main">
          <div className="panel">
            {authError
              ? <div className="banner error">Could not reach server: {authError}</div>
              : <div className="empty">Loading…</div>}
          </div>
        </main>
      </div>
    );
  }

  // Setup branch - only ever rendered once per fresh install. The
  // freshly-created root admin is signed in immediately (the setup
  // endpoint returns a token alongside the user row).
  if (authStatus.setup_needed) {
    return (
      <SetupPage
        onSetupComplete={(session) => {
          acceptSession(session);
          setAuthStatus({ ...authStatus, setup_needed: false });
        }}
      />
    );
  }

  // Login branch - until we have a token. LoginBugFix1: this is the
  // hard render gate. Nothing protected mounts above this branch.
  if (!token) {
    return (
      <LoginPage
        onLogin={(session) => acceptSession(session)}
      />
    );
  }

  // Token present but /me not yet resolved - short splash. Most loads
  // resolve sub-100ms so this is rarely visible.
  if (me === null) {
    return (
      <div className="app">
        <header className="topbar">
          <div className="brand">PlexMigrate</div>
        </header>
        <main className="main">
          <div className="panel">
            {meError
              ? <div className="banner error">Could not load profile: {meError}</div>
              : <div className="empty">Loading profile…</div>}
          </div>
        </main>
      </div>
    );
  }

  // Authenticated and profile loaded - wrap Main in AuthProvider so
  // every gated component can read role + permissions via context.
  return (
    <ClockProvider>
      <AuthProvider
        username={me.username}
        displayName={me.display_name}
        realRole={me.real_role}
        effectiveRole={me.effective_role}
        inViewMode={me.in_view_mode}
        permissions={me.permissions}
        lastLogin={me.last_login}
        createdAt={me.created_at}
        refreshMe={refreshMe}
      >
        <Main
          onLogout={() => {
            api.authLogout().catch(() => { /* no-op */ });
            dashboardWsClient.close();
            setAccessToken(null);
            setToken(null);
            setMe(null);
          }}
        />
      </AuthProvider>
    </ClockProvider>
  );
}


// ── Inner Main: the existing tab UI ──────────────────────────────────────────

function Main({
  onLogout,
}: {
  onLogout: (() => void) | null;
}) {
  // PR-A3 - identity now reads from AuthContext. The previous
  // ``currentUser`` prop is gone; Main is always rendered inside an
  // ``<AuthProvider>``.
  const auth = useAuthContext();
  const currentUser = { username: auth.username, role: auth.role };
  // The single source of truth for live state, pushed in by the WebSocket.
  const [snapshot, setSnapshot] = useState<DashboardFrame | null>(null);
  const [conn, setConn] = useState<ConnState>('connecting');
  const [tab, setTab] = useState<Tab>('dashboard');
  // Both sub-tab states persist independently - navigating away and back
  // always restores the last active sub-tab rather than resetting.
  const [runSubTab, setRunSubTab] = useState<RunSubTab>('run');
  const [serversSubTab, setServersSubTab] = useState<ServersSubTab>('servers');
  // Account tab inner state. Defaults to the operator's own account
  // surface; the Account Management sub-tab is only visible for roles
  // with ``users.manage`` (admin / root_admin).
  const [accountSubTab, setAccountSubTab] = useState<AccountSubTab>('account');
  // Settings tab inner state. Defaults to system settings; the snap-back
  // effect below picks the first sub-tab the operator actually has
  // permission for when they land on the Settings tab.
  const [settingsSubTab, setSettingsSubTab] = useState<SettingsSubTab>('settings');
  // Inner page within the Account Management sub-tab. Persists
  // separately so the operator's last-viewed inner page survives
  // navigation around the rest of the app.
  const [accountMgmtPage, setAccountMgmtPage] = useState<AccountMgmtPage>('db_admin');

  // PR-A4 - permission flags for the tab strip + sub-tab strips. Use
  // the same hook every gated component uses so Switch View Mode (when
  // root_admin temporarily drops to a lesser role) flips the visible
  // tabs along with everything else.
  const canStartJobs = usePermission('jobs.start');
  const canViewSchedules = usePermission('schedules.view');
  const canEditSettings = usePermission('settings.edit');
  // ``settings.tunables`` is root_admin-only and gates the System
  // Tunables sub-tab under Settings (infrastructure-level knobs that
  // could lock users out if set wrong).
  const canManageTunables = usePermission('settings.tunables');
  const canManageUsers = usePermission('users.manage');
  // The Database Admin Account inner page sits behind its own
  // permission so the inner-nav button hides for any role that can
  // reach Account Management but shouldn't see the db_admin gate
  // credential. In practice that's just admin + root_admin - the
  // same roles that have ``users.manage`` today - but keeping the
  // gate explicit means a future role with users.manage but not
  // db_admin.access (e.g. a "team manager") wouldn't accidentally
  // inherit it.
  const canAccessDbAdmin = usePermission('db_admin.access');
  const canViewLogs = usePermission('logs.view');
  const canViewExports = usePermission('exports.view');
  // Access Control is the most sensitive admin surface in the app -
  // it edits every other user's permission set. Gated to root_admin
  // only at both the UI (here) and backend (require_role("root_admin")
  // on the /api/auth/users/{u}/permissions routes). View-mode-aware:
  // a root_admin dropped into viewer mode loses the button just like
  // any other privileged surface.
  const canManageAccessControl = auth.effectiveRole === 'root_admin';

  // PR-A5 - Switch View Mode modal visibility. Only root_admin ever
  // sees the trigger button (rendered conditionally below).
  const [showSwitchViewModal, setShowSwitchViewModal] = useState(false);

  // The Settings tab carries operator surfaces plus the always-on
  // Help reference page; if the effective role has none of the
  // operator surfaces we still keep the tab visible because Help is
  // available to everyone, but that means the tab is effectively
  // unconditional. Kept as a constant for symmetry with the snap-back
  // and tab-strip logic below.
  const canSeeSettingsTab = true;

  // Snap the active tab back to a permitted one when the effective
  // role changes (Switch View Mode drop) and the current tab is no
  // longer visible.
  useEffect(() => {
    if (tab === 'run' && !canStartJobs) setTab('dashboard');
    if (tab === 'settings' && !canSeeSettingsTab) setTab('dashboard');
  }, [tab, canStartJobs, canSeeSettingsTab]);
  // Servers sub-tab snap-back. ``users`` (User Management, PR-10) is
  // operator+ only; ``exports`` is gated by canViewExports. If a
  // Switch View Mode drop strands the caller on either, bounce back
  // to the plain Servers list.
  useEffect(() => {
    if (tab === 'servers' && serversSubTab === 'users' && !canStartJobs) {
      setServersSubTab('servers');
    }
    if (tab === 'servers' && serversSubTab === 'exports' && !canViewExports) {
      setServersSubTab('servers');
    }
  }, [tab, serversSubTab, canStartJobs, canViewExports]);
  // Account sub-tab snap-back. Only ``account_management`` can become
  // forbidden via a Switch View Mode drop - the personal ``account``
  // page is always available to every role.
  useEffect(() => {
    if (tab === 'account' && accountSubTab === 'account_management' && !canManageUsers) {
      setAccountSubTab('account');
    }
  }, [tab, accountSubTab, canManageUsers]);
  // Settings sub-tab snap-back. Picks the first permitted face when the
  // current one is no longer visible (e.g. ``settings`` after a drop
  // out of canEditSettings). ``help`` is unconditional, so it's the
  // ultimate fallback when no operator surface is available.
  // ``exports`` moved to the Servers tab in v0.13.0 and is no longer
  // a Settings sub-tab.
  useEffect(() => {
    if (tab !== 'settings') return;
    const valid =
      (settingsSubTab === 'settings' && canEditSettings) ||
      (settingsSubTab === 'tunables' && canManageTunables) ||
      (settingsSubTab === 'logs' && canViewLogs) ||
      settingsSubTab === 'help';
    if (valid) return;
    if (canEditSettings) setSettingsSubTab('settings');
    else if (canManageTunables) setSettingsSubTab('tunables');
    else if (canViewLogs) setSettingsSubTab('logs');
    else setSettingsSubTab('help');
  }, [tab, settingsSubTab, canEditSettings, canManageTunables, canViewLogs]);
  // If the operator was viewing the Database Admin inner page and a
  // Switch View Mode drop removes ``db_admin.access``, fall back to
  // the User Accounts inner page so the surrounding nav doesn't show
  // an active-but-hidden button with a blank pane below it.
  useEffect(() => {
    if (
      tab === 'account' &&
      accountSubTab === 'account_management' &&
      accountMgmtPage === 'db_admin' &&
      !canAccessDbAdmin
    ) {
      setAccountMgmtPage('user_accounts');
    }
  }, [tab, accountSubTab, accountMgmtPage, canAccessDbAdmin]);
  // Same snap-back for Access Control: if the operator was on this
  // page and lost root_admin (e.g. View Mode drop), bounce them to
  // User Accounts so the page doesn't render under the wrong identity.
  useEffect(() => {
    if (
      tab === 'account' &&
      accountSubTab === 'account_management' &&
      accountMgmtPage === 'access_control' &&
      !canManageAccessControl
    ) {
      setAccountMgmtPage('user_accounts');
    }
  }, [tab, accountSubTab, accountMgmtPage, canManageAccessControl]);
  // PR-8 - Dashboard multi-job sub-tab selection. ``null`` means
  // "auto-pick the running job," which is also the only sane choice
  // when there's just one job. The strip renders dynamically from
  // the WS payload's ``jobs`` array, mirroring Run Job / Servers /
  // Settings sub-tab visuals. State lives here (not in DashboardPanel)
  // so the strip can sit at the same layer as the other sub-tab strips.
  const [dashJobId, setDashJobId] = useState<string | null>(null);
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
  const lastRunningRef = useRef<{ snap: DashboardFrame; at: number } | null>(null);

  // ── WebSocket lifecycle ────────────────────────────────────────────────
  useEffect(() => {
    // The socket reconnects itself; the connected indicator just
    // tracks whether we've received any message in the last 5 s.
    let lastMsg = 0;
    const unsubscribe = dashboardWsClient.subscribe((msg) => {
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
        .catch(() => { /* keep prior value; render falls back to '-' */ });
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
          type: 'dashboard_frame',
          server_ts: Date.now() / 1000,
          dashboard: j.dashboard || null,
          // v0.10.0: REST one-shot fallback doesn't know about fan-out
          // - the field is populated on the next WS tick if a fan-out
          // job is in flight. ``null`` here keeps the type intact and
          // gives the DashboardPanel its single-destination render
          // path until the socket catches up.
          fan_out: null,
          // v0.12.0: servers_network ships only via WS; the REST
          // fallback returns an empty array and the Networking tab
          // shows "Waiting for first tick…" until the socket connects.
          servers_network: [],
          job:
            j.state === 'idle'
              ? null
              : {
                  job_id: '',
                  mode: (j.mode as 'snapshot' | 'restore') || 'snapshot',
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
        // Server unreachable on first paint - the WS reconnect loop
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

  // Topbar clock rendering. Default = server's timezone, fed by the
  // skew-corrected wallclock. The per-user clock-display preference
  // (set in Account Settings) can switch to ``local`` (browser's
  // clock) or ``custom`` (server time + a user offset). Always render
  // the same HH:MM:SS / TZ shape so the topbar layout doesn't jitter.
  const clock = useClockDisplay();
  const clockLabel = (() => {
    if (!serverTime) return null;
    const serverNowMs = Date.now() + clockSkewRef.current;
    const d = clock.resolveDisplay(serverNowMs);
    const fmtOpts: Intl.DateTimeFormatOptions = {
      hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
    };
    // ``server`` mode keeps the server timezone. ``local`` mode and
    // ``custom`` mode render in the browser's local timezone - the
    // operator chose to look away from the server clock, so the
    // browser zone is the right context for those modes.
    if (clock.mode === 'server') {
      try {
        return new Intl.DateTimeFormat(undefined, { ...fmtOpts, timeZone: serverTime.tz }).format(d);
      } catch {
        return new Intl.DateTimeFormat(undefined, fmtOpts).format(d);
      }
    }
    return new Intl.DateTimeFormat(undefined, fmtOpts).format(d);
  })();
  const clockTzLabel = (() => {
    if (!serverTime) return '';
    if (clock.mode === 'local') return 'local';
    if (clock.mode === 'custom') return 'custom';
    return serverTime.tz_abbrev || serverTime.tz || '';
  })();

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">PlexMigrate</div>
        {/* Right side stacks vertically: clock + Live on top, user
            info + Log out beneath. The bottom row only renders when
            auth is enabled AND a user is signed in (both ``currentUser``
            and ``onLogout`` are null in the auth-disabled path), so
            single-user / CLI-feel installs see no extra chrome. */}
        <div
          className="conn"
          style={{
            display: 'flex',
            flexDirection: 'column',
            alignItems: 'flex-end',
            gap: 4,
          }}
        >
          <div style={{ display: 'flex', alignItems: 'center', gap: 14 }}>
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
          {/* ``onLogout`` is the truthy gate - App.tsx only passes a
              function when auth is enabled AND a token is in state.
              ``currentUser`` is optional decoration; if the JWT
              payload didn't decode cleanly the button still appears
              so the operator never gets locked into a half-state
              with no way out. */}
          {onLogout && (
            <div style={{ display: 'inline-flex', alignItems: 'center', gap: 8 }}>
              {currentUser && (
                <span style={{ color: 'var(--text-dim)', fontSize: 12 }}>
                  {auth.displayName || currentUser.username}
                  {auth.realRole !== 'viewer' && (
                    <span style={{ marginLeft: 4, opacity: 0.7 }}>
                      ({auth.realRole.replace('_', ' ')})
                    </span>
                  )}
                  {auth.inViewMode && (
                    <span style={{
                      marginLeft: 8,
                      padding: '2px 8px',
                      borderRadius: 999,
                      background: '#5a3a85',
                      color: '#fff',
                      fontSize: 10,
                      fontWeight: 700,
                      letterSpacing: 0.3,
                      textTransform: 'uppercase',
                      verticalAlign: 'middle',
                    }}>
                      Viewing as {auth.effectiveRole.replace('_', ' ')}
                    </span>
                  )}
                </span>
              )}
              {/* Switch View Mode button visibility is keyed off the
                  REAL role, not the effective one - the button must
                  stay reachable while dropped so the operator can
                  always restore. Hidden only for viewer (no drop
                  targets exist for that role). */}
              {auth.realRole !== 'viewer' && (
                <button
                  onClick={() => setShowSwitchViewModal(true)}
                  title="View mode resets on page refresh."
                  style={{ fontSize: 12 }}
                >
                  Switch View Mode
                </button>
              )}
              <button
                onClick={onLogout}
                title="Log out and clear the refresh-token cookie."
                style={{ fontSize: 12 }}
              >
                Log out
              </button>
            </div>
          )}
        </div>
      </header>

      {/* Top-level tabs are permission-gated. ``Run Job`` is hidden
          for viewer (no ``jobs.start``); ``Account`` is visible to
          every role (everyone can manage their own profile);
          ``Settings`` is visible only when the role has at least one
          operator surface (system settings, logs, or exports). */}
      <nav className="tabs">
        <button className={tab === 'dashboard' ? 'active' : ''} onClick={() => setTab('dashboard')}>Dashboard</button>
        {canStartJobs && (
          <button className={tab === 'run' ? 'active' : ''} onClick={() => setTab('run')}>Run Job</button>
        )}
        <button className={tab === 'servers' ? 'active' : ''} onClick={() => setTab('servers')}>Servers</button>
        <button className={tab === 'account' ? 'active' : ''} onClick={() => setTab('account')}>Account</button>
        {canSeeSettingsTab && (
          <button className={tab === 'settings' ? 'active' : ''} onClick={() => setTab('settings')}>Settings</button>
        )}
      </nav>

      {tab === 'run' && canStartJobs && (
        <nav className="tabs sub-tabs">
          <button className={runSubTab === 'run' ? 'active' : ''} onClick={() => setRunSubTab('run')}>Run Job</button>
          {/* Schedules sub-tab requires schedules.view (operator+) -
              viewer never reaches this whole branch anyway because
              Run Jobs is hidden for them. */}
          {canViewSchedules && (
            <button className={runSubTab === 'schedules' ? 'active' : ''} onClick={() => setRunSubTab('schedules')}>Schedules</button>
          )}
        </nav>
      )}

      {tab === 'servers' && (
        <nav className="tabs sub-tabs">
          <button className={serversSubTab === 'servers' ? 'active' : ''} onClick={() => setServersSubTab('servers')}>Overview</button>
          <button className={serversSubTab === 'networking' ? 'active' : ''} onClick={() => setServersSubTab('networking')}>Networking</button>
          {/* PR-10 - User Management. Operator+ only; hidden from
              viewer since they have no jobs to set up credentials for. */}
          {canStartJobs && (
            <button className={serversSubTab === 'users' ? 'active' : ''} onClick={() => setServersSubTab('users')}>User Management</button>
          )}
          {/* Run Defaults - moved here from Settings ▸ General Settings.
              These are the run-level knobs (paths, performance, snapshot
              defaults, transfer resolution, retention ceiling) that
              describe HOW snapshots and direct transfers operate
              against Plex. settings.edit gates write access; the panel
              itself stays read-only without it. */}
          {canEditSettings && (
            <button className={serversSubTab === 'run_defaults' ? 'active' : ''} onClick={() => setServersSubTab('run_defaults')}>Run Defaults</button>
          )}
          {/* Per-server snapshot defaults + retention overrides. Gated
              behind settings.edit since it changes engine behaviour. */}
          {canEditSettings && (
            <button className={serversSubTab === 'advanced' ? 'active' : ''} onClick={() => setServersSubTab('advanced')}>Advanced Settings</button>
          )}
          {/* v0.13.0: Export moved from Settings to Servers since the
              exports list is server-scoped anyway. Same exports.view
              permission gate; rendered last so the existing Servers
              flow (Overview -> Networking -> Users -> Advanced) is
              preserved at the front of the strip. */}
          {canViewExports && (
            <button className={serversSubTab === 'exports' ? 'active' : ''} onClick={() => setServersSubTab('exports')}>Export</button>
          )}
        </nav>
      )}

      {/* Account sub-tabs. The ``Account Management`` face is admin /
          root_admin only - for everyone else the Account tab has just
          one face and we suppress the strip entirely so it doesn't
          look like a dead-end nav. */}
      {tab === 'account' && canManageUsers && (
        <nav className="tabs sub-tabs">
          <button
            className={accountSubTab === 'account' ? 'active' : ''}
            onClick={() => setAccountSubTab('account')}
          >
            Account
          </button>
          <button
            className={accountSubTab === 'account_management' ? 'active' : ''}
            onClick={() => setAccountSubTab('account_management')}
          >
            Account Management
          </button>
        </nav>
      )}

      {/* Settings sub-tabs filtered by role: general settings (admin+),
          logs (operator+). Help is always visible since it's a reference
          page with no destructive controls. v0.13.0 moved Exports to
          the Servers tab; the "Settings" sub-tab is now labeled
          "General Settings" to distinguish it from Servers > Advanced
          Settings (per-server config). */}
      {tab === 'settings' && (
        <nav className="tabs sub-tabs">
          {canEditSettings && (
            <button className={settingsSubTab === 'settings' ? 'active' : ''} onClick={() => setSettingsSubTab('settings')}>General Settings</button>
          )}
          {/* System Tunables - root_admin only. Infrastructure-level
              knobs (HTTP timeouts, JWT TTL, SQLite busy timeout, etc.)
              that used to be hardcoded literals. */}
          {canManageTunables && (
            <button className={settingsSubTab === 'tunables' ? 'active' : ''} onClick={() => setSettingsSubTab('tunables')}>Tunables</button>
          )}
          {canViewLogs && (
            <button className={settingsSubTab === 'logs' ? 'active' : ''} onClick={() => setSettingsSubTab('logs')}>Logs</button>
          )}
          <button className={settingsSubTab === 'help' ? 'active' : ''} onClick={() => setSettingsSubTab('help')}>Help</button>
        </nav>
      )}

      {/* PR-8 - Dashboard multi-job sub-tab strip. Renders only when
          more than one job is active or queued; single-job case is
          unchanged. Lives at the same visual layer as Run Job / Servers
          / Settings sub-tabs so the nesting feels consistent. The
          buttons are built dynamically from the WS payload's ``jobs``
          array - no hardcoded tab count. */}
      {tab === 'dashboard' && (snapshot?.jobs?.length ?? 0) > 1 && (() => {
        const jobs = snapshot!.jobs!;
        // Resolve the effective selection so the active highlight is
        // never blank: the operator's pick (when still in the list),
        // else the running job, else the first job. ``dashJobId`` is
        // not auto-cleared when stale - the operator keeps the
        // affordance to click back to their old pick if it reappears,
        // but the visual always points at a real entry.
        const effective =
          (dashJobId && jobs.some((j) => j.job_id === dashJobId))
            ? dashJobId
            : (snapshot?.job?.job_id ?? jobs[0].job_id);
        return (
          <nav className="tabs sub-tabs">
            {jobs.map((j, idx) => {
              const isRunning =
                !!snapshot?.job && j.job_id === snapshot.job.job_id;
              const mode = j.mode.charAt(0).toUpperCase() + j.mode.slice(1);
              const label = isRunning
                ? `${mode} (running)`
                : j.state === 'queued'
                  ? `${mode} (queue #${idx})`
                  : `${mode} (${j.state})`;
              return (
                <button
                  key={j.job_id}
                  className={j.job_id === effective ? 'active' : ''}
                  onClick={() => setDashJobId(j.job_id)}
                  title={`${j.mode.toUpperCase()} · ${j.state}`}
                >
                  {label}
                </button>
              );
            })}
          </nav>
        );
      })()}

      <main className="main">
        {tab === 'dashboard' && (
          <DashboardPanel
            snapshot={displaySnapshot}
            connState={conn}
            selectedJobId={dashJobId}
          />
        )}
        {tab === 'run' && runSubTab === 'run' && canStartJobs && <JobFormPanel snapshot={snapshot} />}
        {tab === 'run' && runSubTab === 'schedules' && canViewSchedules && <SchedulesPanel />}
        {tab === 'servers' && serversSubTab === 'servers' && <ServersPanel />}
        {tab === 'servers' && serversSubTab === 'networking' && <NetworkingPanel snapshot={snapshot} />}
        {tab === 'servers' && serversSubTab === 'users' && canStartJobs && <UserManagementPanel />}
        {tab === 'servers' && serversSubTab === 'run_defaults' && canEditSettings && <RunDefaultsPanel />}
        {tab === 'servers' && serversSubTab === 'advanced' && canEditSettings && <ServerAdvancedSettingsPanel />}
        {tab === 'servers' && serversSubTab === 'exports' && canViewExports && <ExportsPanel />}
        {tab === 'account' && accountSubTab === 'account' && <AccountSettingsPanel />}
        {tab === 'account' && accountSubTab === 'account_management' && canManageUsers && (
          <>
            {/* Inner sub-nav for the Account Management group. The
                Database Admin Account button is gated by its own
                ``db_admin.access`` permission so it only renders for
                admin / root_admin even if a future role gains
                ``users.manage`` without it. */}
            <nav className="tabs sub-tabs">
              {canAccessDbAdmin && (
                <button
                  className={accountMgmtPage === 'db_admin' ? 'active' : ''}
                  onClick={() => setAccountMgmtPage('db_admin')}
                >
                  Database Admin Account
                </button>
              )}
              <button
                className={accountMgmtPage === 'user_accounts' ? 'active' : ''}
                onClick={() => setAccountMgmtPage('user_accounts')}
              >
                User Accounts
              </button>
              {/* Access Control - root_admin only. Per-user permission
                  grant/revoke layered on top of the role baseline. */}
              {canManageAccessControl && (
                <button
                  className={accountMgmtPage === 'access_control' ? 'active' : ''}
                  onClick={() => setAccountMgmtPage('access_control')}
                >
                  Access Control
                </button>
              )}
            </nav>
            {accountMgmtPage === 'db_admin' && canAccessDbAdmin && <AccountsPanel />}
            {accountMgmtPage === 'user_accounts' && <UserAccountsExplorer />}
            {accountMgmtPage === 'access_control' && canManageAccessControl && <AccessControlPanel />}
          </>
        )}
        {tab === 'settings' && settingsSubTab === 'settings' && canEditSettings && <SettingsPanel />}
        {tab === 'settings' && settingsSubTab === 'tunables' && canManageTunables && <TunablesPanel />}
        {tab === 'settings' && settingsSubTab === 'logs' && canViewLogs && <LogsPanel />}
        {tab === 'settings' && settingsSubTab === 'help' && <HelpPanel />}
      </main>

      {showSwitchViewModal && (
        <SwitchViewModeModal onClose={() => setShowSwitchViewModal(false)} />
      )}
    </div>
  );
}


// Server-side View Mode (Fix 2). The override lives in
// _VIEW_MODE_SESSIONS on the backend, keyed by the caller's JWT jti.
// Every enter/exit hits the server, then we refetch /me to pull the
// new effective_role into context. Password is required for every
// transition (enter, switch between drops, exit).
//
// The dropdown shows the caller's drop targets only - all roles
// strictly below their REAL role. The operator can switch between
// any two of those targets without exiting first; the server
// overwrites the existing entry.

const VIEW_MODE_DROP_TARGETS: Record<Role, Role[]> = {
  viewer: [],
  operator: ['viewer'],
  manager: ['operator', 'viewer'],
  admin: ['manager', 'operator', 'viewer'],
  root_admin: ['manager', 'operator', 'viewer'],
};

const ROLE_DROP_LABEL: Record<Role, string> = {
  viewer:     'Viewer - read-only',
  operator:   'Operator - start jobs',
  manager:    'Manager - start + stop jobs, edit schedules',
  admin:      'Admin - full access, cannot modify root admin',
  root_admin: 'Root admin - full access',
};

function SwitchViewModeModal({ onClose }: { onClose: () => void }) {
  const auth = useAuthContext();
  const dropTargets = VIEW_MODE_DROP_TARGETS[auth.realRole];
  // Initial target: current view when dropped, first drop target otherwise.
  const initialTarget: Role =
    auth.inViewMode && dropTargets.includes(auth.effectiveRole)
      ? auth.effectiveRole
      : (dropTargets[0] ?? auth.realRole);
  const [target, setTarget] = useState<Role>(initialTarget);
  const [password, setPassword] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Direction-aware password rule (matches backend enforcement):
  //   * Apply requires password only when raising the visible role
  //     (target rank > current effective rank). Dropping to a lower
  //     target is free - the operator already has the higher privilege.
  //   * Exit always raises the visible role (effective -> real), so
  //     it always requires a password.
  // The input field is rendered only when one of the visible actions
  // needs it - dropping out of view mode shouldn't show a password box.
  const targetRank = ROLE_RANK[target];
  const effectiveRank = ROLE_RANK[auth.effectiveRole];
  const applyRequiresPassword = targetRank > effectiveRank;
  const exitVisible = auth.inViewMode;
  const showPasswordInput = applyRequiresPassword || exitVisible;

  const applyEnabled =
    !submitting
    && target !== auth.effectiveRole
    && (!applyRequiresPassword || password.length > 0);
  const exitEnabled = !submitting && exitVisible && password.length > 0;

  const applyEnter = async () => {
    setError(null);
    if (!applyEnabled) return;
    setSubmitting(true);
    try {
      // Send the password only when actually needed; the server
      // ignores it otherwise. Empty string keeps the wire payload
      // shape consistent.
      await api.viewModeEnter(target, applyRequiresPassword ? password : '');
      if (auth.refreshMe) await auth.refreshMe();
      onClose();
    } catch (e) {
      setError(String(e));
    } finally {
      setSubmitting(false);
    }
  };

  const applyExit = async () => {
    setError(null);
    if (!exitEnabled) {
      if (!password) setError('Password is required to restore full access.');
      return;
    }
    setSubmitting(true);
    try {
      await api.viewModeExit(password);
      if (auth.refreshMe) await auth.refreshMe();
      onClose();
    } catch (e) {
      setError(String(e));
    } finally {
      setSubmitting(false);
    }
  };

  // Defensive: viewer has no drop targets, so the modal trigger
  // shouldn't even be reachable for them. If it ever is, show a
  // disabled state rather than crashing.
  if (dropTargets.length === 0) {
    return (
      <div
        onClick={onClose}
        style={{
          position: 'fixed', inset: 0,
          background: 'rgba(0,0,0,0.55)',
          zIndex: 1000,
          display: 'flex', alignItems: 'flex-start', justifyContent: 'center',
          paddingTop: '8vh',
        }}
      >
        <div
          onClick={(e) => e.stopPropagation()}
          className="panel"
          style={{ width: 360, maxWidth: '92vw' }}
        >
          <h2 style={{ marginTop: 0 }}>Switch view mode</h2>
          <span className="help">
            Your role has no roles below it to preview as.
          </span>
          <div className="row-buttons" style={{ marginTop: 12 }}>
            <button onClick={onClose}>Close</button>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div
      onClick={onClose}
      style={{
        position: 'fixed', inset: 0,
        background: 'rgba(0,0,0,0.55)',
        zIndex: 1000,
        display: 'flex', alignItems: 'flex-start', justifyContent: 'center',
        paddingTop: '8vh',
      }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        className="panel"
        style={{ width: 480, maxWidth: '92vw' }}
      >
        <h2 style={{ marginTop: 0 }}>
          Switch view mode
          <InfoTip>
            Backend-enforced privilege drop. The server actually denies
            elevated calls while you're dropped, not just the UI. The
            override survives page refresh and is only cleared by Exit
            view mode or logging out. Password is required only when
            raising the visible role.
          </InfoTip>
        </h2>
        <span className="help" style={{ display: 'block', color: 'var(--text-dim)', fontSize: 12, marginBottom: 12 }}>
          Preview the UI as a lesser role. Dropping down is free;
          raising the visible role (or exiting view mode) requires
          your password.
        </span>

        {auth.inViewMode && (
          <div className="banner info" style={{ fontSize: 12, marginBottom: 12 }}>
            Currently viewing as <strong>{auth.effectiveRole.replace('_', ' ')}</strong>.
            Switch to a different role below, or exit to restore full access.
          </div>
        )}

        {error && <div className="banner error">{error}</div>}

        <label className="field">
          <span className="label">View as</span>
          <select value={target} onChange={(e) => setTarget(e.target.value as Role)}>
            {dropTargets.map((r) => (
              <option key={r} value={r}>{ROLE_DROP_LABEL[r]}</option>
            ))}
          </select>
        </label>

        {showPasswordInput && (
          <label className="field">
            <span className="label">
              Confirm with your password
              <InfoTip>
                Required only to raise the visible role - dropping
                further is free. Exit view mode always requires the
                password since it restores full access.
              </InfoTip>
            </span>
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              autoComplete="current-password"
              autoFocus
            />
          </label>
        )}

        <div className="row-buttons" style={{ marginTop: 12 }}>
          <button
            className="primary"
            disabled={!applyEnabled}
            onClick={applyEnter}
          >
            {submitting
              ? 'Switching…'
              : auth.inViewMode
                ? `Switch to ${target.replace('_', ' ')}`
                : `Enter view mode as ${target.replace('_', ' ')}`}
          </button>
          {exitVisible && (
            <button onClick={applyExit} disabled={!exitEnabled}>
              Exit view mode
            </button>
          )}
          <button onClick={onClose} disabled={submitting}>Cancel</button>
        </div>
      </div>
    </div>
  );
}


// String-literal alias used in one place only.
type JobPayloadState = 'idle' | 'queued' | 'running' | 'completed' | 'failed' | 'cancelled';
