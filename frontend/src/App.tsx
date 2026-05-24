// Top-level layout: a sticky topbar, a tab strip, and the active
// tab's panel below it. The WebSocket subscription lives here so
// child components can read the latest snapshot from props without
// each owning its own socket.
//
// Opt-in JWT auth wraps the whole app. On mount we hit
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
// user out. This is a deliberate trade-off: a locally-hosted tool
// exposed beyond localhost should optimise for "no persisted creds
// in the browser" over "stay-signed-in" UX.

import { lazy, Suspense, useCallback, useEffect, useRef, useState } from 'react';
import {
  AuthStatus,
  MeResponse,
  Role,
  api,
  dashboardWsClient,
  onElevationRequired,
  onUnauthorized,
  setAccessToken,
  ServerTime,
  DashboardFrame,
  JobPayload,
} from './api';
import { AuthProvider, ROLE_RANK, useAuthContext } from './contexts/AuthContext';
import { ClockProvider, useClockDisplay } from './contexts/ClockContext';
import { usePermission } from './hooks/usePermission';
import { useNowTick } from './hooks/useNowTick';
import { pausableInterval } from './utils/pausableInterval';
import { ConfirmProvider } from './components/ConfirmModal';
import { DashboardPanel } from './components/DashboardPanel';
import { ElevateModal } from './components/ElevateModal';
import { RuntimeBreakdownPanel } from './components/RuntimeBreakdownPanel';
import { DashboardDownloadLogsPanel } from './components/DashboardDownloadLogsPanel';import { ThemeSwitcher } from './components/ThemeSwitcher';
import { Modal } from './components/Modal';
import { ServerLogsPanel } from './components/ServerLogsPanel';
import { SchedulesPanel } from './components/SchedulesPanel';import { ServerSyncingPage } from './components/ServerSyncingPage';
import { LogsPanel } from './components/LogsPanel';
import { ApplicationLogsPanel } from './components/ApplicationLogsPanel';
import { ExportsPanel } from './components/ExportsPanel';
import { AccountsPanel } from './components/AccountsPanel';
import { AccountSettingsPanel } from './components/AccountSettingsPanel';
import { UserAccountsExplorer } from './components/UserAccountsExplorer';
import { ServerAdvancedSettingsPanel } from './components/ServerAdvancedSettingsPanel';
import { RunDefaultsPanel } from './components/RunDefaultsPanel';import { AccessControlPanel } from './components/AccessControlPanel';import { LoginPage } from './components/LoginPage';
import { SetupPage } from './components/SetupPage';
import { InfoTip } from './components/InfoTip';import { TooltipProvider } from './contexts/TooltipContext';
import { ElevationProvider, useElevation } from './contexts/ElevationContext';
import { BackendTintProvider, useBackendTint } from './contexts/BackendTintContext';

// Heavy / rarely-visited routes are code-split via React.lazy so they
// stay out of the initial bundle. Each panel is a named export, hence
// the `.then` remap to the `default` shape React.lazy expects. The
// Suspense boundary around <main> shows a brief loader on first open.
const DeveloperPanel = lazy(() => import('./components/DeveloperPanel').then((m) => ({ default: m.DeveloperPanel })));
const DevBlogPanel = lazy(() => import('./components/DevBlogPanel').then((m) => ({ default: m.DevBlogPanel })));
const PlaylistManagementPanel = lazy(() => import('./components/PlaylistManagementPanel').then((m) => ({ default: m.PlaylistManagementPanel })));
const NetworkingPanel = lazy(() => import('./components/NetworkingPanel').then((m) => ({ default: m.NetworkingPanel })));
const TunablesPanel = lazy(() => import('./components/TunablesPanel').then((m) => ({ default: m.TunablesPanel })));
const DatabasesPanel = lazy(() => import('./components/DatabasesPanel').then((m) => ({ default: m.DatabasesPanel })));
const UserManagementPanel = lazy(() => import('./components/UserManagementPanel').then((m) => ({ default: m.UserManagementPanel })));
const ServerCommandsPanel = lazy(() => import('./components/ServerCommandsPanel').then((m) => ({ default: m.ServerCommandsPanel })));
const HelpPanel = lazy(() => import('./components/HelpPanel').then((m) => ({ default: m.HelpPanel })));
const JobFormPanel = lazy(() => import('./components/JobFormPanel').then((m) => ({ default: m.JobFormPanel })));
const SettingsPanel = lazy(() => import('./components/SettingsPanel').then((m) => ({ default: m.SettingsPanel })));
const ServersPanel = lazy(() => import('./components/ServersPanel').then((m) => ({ default: m.ServersPanel })));

// 'developer' is appended dynamically only when the backend reports
// debug_mode=true on /api/health. Production builds never see it.
type Tab = 'dashboard' | 'run' | 'servers' | 'account' | 'settings' | 'server_commands' | 'devblog' | 'developer';
// Sub-tabs nested under Jobs. Persists across navigation. ``syncing``
// (Server Syncing) is the fourth sub-tab since it's job-class work
// like the other three.
type RunSubTab = 'run' | 'schedules' | 'playlists' | 'syncing';
// Sub-tabs nested under Servers. 'users' is the User Management
// panel (end user+ only).
type ServersSubTab = 'servers' | 'networking' | 'users' | 'run_defaults' | 'advanced' | 'exports' | 'logs';
// Sub-tabs nested under Account. ``account`` is every end user's own
// self-service surface (display name, password, clock). ``account_management``
// is admin/root_admin only and contains the Database Admin Account
// page + the User Accounts explorer, switched between via the
// AccountMgmtPage inner nav. The whole sub-strip is suppressed for
// roles that can only see one of the two (i.e. anyone without
// ``users.manage``).
type AccountSubTab = 'account' | 'account_management';
// Sub-tabs nested under Settings. The Settings tab houses
// system-level end user views only: system preferences, logs,
// exports, plus a flat Help reference page.
type SettingsSubTab = 'settings' | 'tunables' | 'databases' | 'logs' | 'help';

// Inner nav inside the Account Management sub-tab. Three pages:
//   db_admin       - Database Admin Account credential (db_admin.access)
//   user_accounts  - User Accounts explorer (users.manage)
//   access_control - Access Control (per-user permission grants/revokes;
//                    root_admin only - gated by canManageAccessControl)
type AccountMgmtPage = 'db_admin' | 'user_accounts' | 'access_control';

// Connection states surfaced in the topbar dot. "connecting" is the
// initial state before the first WS frame; the dashboard panel uses
// it to render a "Connecting…" placeholder instead of the "no job
// running" empty state, which would otherwise read as "didn't load".
type ConnState = 'connecting' | 'connected' | 'disconnected';

// How long to keep showing the last snapshot's totals after a job
// finishes. Without this the dashboard goes blank the instant the
// engine returns, hiding the final counters the user just earned.
const POST_FINISH_RETAIN_MS = 30_000;

// localStorage key for the dashboard "keep results until next job"
// toggle. Browser-local; a per-operator view preference.
const DASHBOARD_PERSIST_KEY = 'hm.dashboard.persistUntilNextJob';

// localStorage key for the retained dashboard snapshot itself. The
// last finished job's frame is mirrored here so a "kept" dashboard
// survives a full page reload, not just a same-session navigation.
const DASHBOARD_SNAPSHOT_KEY = 'hm.dashboard.lastSnapshot';

type RetainedSnapshot = { snap: DashboardFrame; at: number };

// Read the persisted finished-job snapshot, if any. Tolerates a
// missing, malformed, or unavailable store by returning null.
function readPersistedSnapshot(): RetainedSnapshot | null {
  try {
    const raw = localStorage.getItem(DASHBOARD_SNAPSHOT_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as RetainedSnapshot;
    if (parsed && typeof parsed.at === 'number' && parsed.snap) return parsed;
    return null;
  } catch {
    return null;
  }
}

function writePersistedSnapshot(entry: RetainedSnapshot): void {
  try {
    localStorage.setItem(DASHBOARD_SNAPSHOT_KEY, JSON.stringify(entry));
  } catch {
    /* quota exceeded / unavailable - the in-memory retain still works */
  }
}

// Drop the persisted snapshot on logout so a finished job's dashboard
// from one operator's session isn't visible to the next.
function clearPersistedSnapshot(): void {
  try {
    localStorage.removeItem(DASHBOARD_SNAPSHOT_KEY);
  } catch {
    /* unavailable - nothing to clear */
  }
}

// ── Outer App: auth gating ───────────────────────────────────────────────────

export function App() {
  // Auth is always on. The status probe is used only to detect
  // first-boot setup (``setup_needed: true``); the ``auth_enabled``
  // field is preserved on the wire for backward-compat but ignored by
  // the frontend.
  const [authStatus, setAuthStatus] = useState<AuthStatus | null>(null);
  const [authError, setAuthError] = useState<string | null>(null);
  // Access token lives only in React state for the lifetime of this
  // <App /> - no localStorage / sessionStorage. What survives across
  // page refreshes is the HttpOnly refresh-token cookie set by the
  // backend at Path=/api/auth - the boot effect below silently POSTs
  // to /api/auth/refresh and adopts the new access token if the
  // cookie is still valid.
  const [token, setToken] = useState<string | null>(null);
  // ``refreshAttempted`` flips to true once the boot-time silent
  // refresh either succeeds or fails. Before that we render a neutral
  // loading splash - NOT the login form - so a returning user with a
  // valid refresh cookie doesn't see a login flash on every page load.
  const [refreshAttempted, setRefreshAttempted] = useState(false);
  // The authoritative identity comes from ``/api/auth/me`` (fetched
  // whenever ``token`` changes). ``null`` while the fetch is in
  // flight - the App renders a splash until it lands so role-gated
  // children never see a half-formed AuthContext.
  const [me, setMe] = useState<MeResponse | null>(null);
  const [meError, setMeError] = useState<string | null>(null);

  // Inline re-auth modal state. Open when an API call returns 403
  // with the elevation marker; ``elevatePromiseRef`` carries the
  // pending resolve callback so the http<T> helper can await the
  // user's confirm / cancel choice before deciding whether to retry
  // the original request.
  const [elevateOpen, setElevateOpen] = useState<boolean>(false);
  const elevatePromiseRef = useRef<((ok: boolean) => void) | null>(null);

  // Register the 401 handler once. With refresh tokens in play, this
  // fires only AFTER api.ts's internal silent-refresh-and-retry has
  // already failed - i.e. the refresh cookie is gone or revoked. We
  // drop the in-memory state and the render gate below sends the
  // user to the login screen.
  useEffect(() => {
    onUnauthorized(() => {
      setAccessToken(null);
      setToken(null);
      setMe(null);
    });
  }, []);

  // Register the elevation handler once. http<T> calls this on any
  // 403 carrying the elevation marker; we stash the resolver on the
  // ref so the modal's close handler can finish the promise. If the
  // modal is already open when another elevation-required call
  // lands, we resolve the previous promise false so the prior call
  // surfaces a normal error rather than waiting forever - the user
  // can only consciously confirm one elevate at a time.
  useEffect(() => {
    onElevationRequired(() => {
      return new Promise<boolean>((resolve) => {
        // If a prior modal is still pending, dismiss its waiter
        // with false. This is rare in practice (modal blocks
        // further user actions) but defensive against parallel
        // background fetches.
        if (elevatePromiseRef.current) {
          try { elevatePromiseRef.current(false); } catch { /* ignore */ }
        }
        elevatePromiseRef.current = resolve;
        setElevateOpen(true);
      });
    });
    return () => onElevationRequired(null);
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
  const acceptSession = useCallback((session: { access_token: string }) => {
    setAccessToken(session.access_token);
    setToken(session.access_token);
  }, []);

  // Refresh /me into context - used by AccountSettingsPanel after a
  // self-display-name edit so the topbar chip updates without a page
  // reload.
  const refreshMe = useCallback(async () => {
    try {
      const r = await api.authMe();
      setMe(r);
    } catch {
      /* 401 handler covers stale-token case */
    }
  }, []);

  // Loading splash - shown until BOTH boot probes finish: the
  // /auth/status check (setup vs login) AND the silent /refresh
  // attempt. Holding the splash through the refresh prevents a
  // returning user from seeing the login form flash before their
  // session is silently restored.
  if (authStatus === null || !refreshAttempted) {
    return (
      <div className="app">
        <header className="topbar">
          <div className="brand" title="Hestia-MediaManager"><span className="wordmark-badge">HM²</span><span className="brand-fullname">Hestia-MediaManager</span></div>
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
          <div className="brand" title="Hestia-MediaManager"><span className="wordmark-badge">HM²</span><span className="brand-fullname">Hestia-MediaManager</span></div>
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
     <TooltipProvider>
      <ElevationProvider>
       <BackendTintProvider>
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
       <ConfirmProvider>
        <Main
          onLogout={() => {
            api.authLogout().catch(() => { /* no-op */ });
            dashboardWsClient.close();
            clearPersistedSnapshot();
            setAccessToken(null);
            setToken(null);
            setMe(null);
          }}
          onSessionSwap={(newToken) => {
            // Login-as-root landed a fresh AuthSession. Adopt the new
            // token + clear ``me`` so the identity-fetch effect re-runs
            // and the new role lands on screen. The dashboard WS is
            // left alone: the new token is attached to subsequent
            // frames automatically since the WS client reads the
            // module-level access token slot.
            setAccessToken(newToken);
            setToken(newToken);
            setMe(null);
          }}
        />
        {/* Inline re-auth modal. Mounted as a sibling to Main so it
            overlays whichever tab the user was on when the
            elevation-required 403 landed. The api.ts http<T> helper
            awaits the user's confirm / cancel choice via the
            elevatePromiseRef-backed resolver and either retries the
            original request silently (on confirm) or throws the 403
            to the caller (on cancel). */}
        <ElevateModal
          open={elevateOpen}
          onClose={(ok) => {
            setElevateOpen(false);
            const resolve = elevatePromiseRef.current;
            elevatePromiseRef.current = null;
            if (resolve) resolve(ok);
          }}
        />
       </ConfirmProvider>
       </AuthProvider>
       </BackendTintProvider>
      </ElevationProvider>
     </TooltipProvider>
    </ClockProvider>
  );
}


// ── Inner Main: the existing tab UI ──────────────────────────────────────────

// Browser-local toggle on the dashboard page: when on, a finished
// job's full dashboard (process list, libraries, thread pool,
// activity feed) stays visible until a new job starts, instead of
// clearing ~30s after the job ends.
function DashboardPersistToggle({
  value,
  onChange,
}: {
  value: boolean;
  onChange: (next: boolean) => void;
}) {
  return (
    <div
      className="panel"
      style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '8px 14px' }}
    >
      <label className="switch" style={{ margin: 0 }}>
        <input
          type="checkbox"
          checked={value}
          onChange={(e) => onChange(e.target.checked)}
        />
        <span>Keep results on screen until the next job starts</span>
      </label>
      <InfoTip>
        On: after a job finishes, the full dashboard - process list,
        libraries, thread pool, and activity feed - stays on screen so
        you can review it, until a new job starts. Off (default): the
        dashboard clears about 30 seconds after the job ends. The
        choice is saved in this browser.
      </InfoTip>
    </div>
  );
}

function Main({
  onLogout,
  onSessionSwap,
}: {
  onLogout: (() => void) | null;
  // Re-auth callback: when "Login as root" lands a fresh AuthSession,
  // App owns the token + me state and must adopt them. Main can't
  // touch those slots directly, so it hands the new token up. The
  // App callback below mirrors the onLogout cleanup but installs the
  // new token instead of clearing.
  onSessionSwap: ((newToken: string) => void) | null;
}) {
  // Identity reads from AuthContext. Main is always rendered inside
  // an ``<AuthProvider>``.
  const auth = useAuthContext();
  const currentUser = { username: auth.username, role: auth.role };
  // The single source of truth for live state, pushed in by the WebSocket.
  const [snapshot, setSnapshot] = useState<DashboardFrame | null>(null);
  const [conn, setConn] = useState<ConnState>('connecting');
  const [tab, setTab] = useState<Tab>('dashboard');
  // Debug-mode probe. Polled once on mount. The Developer tab is only
  // added to the nav when this is true. Production deployments leave
  // PLEXMIGRATE_DEBUG_MODE unset and the tab never renders.
  // False-positive risk: a user who unsets the env var mid-session
  // keeps the tab visible until they reload; the backend endpoint
  // still 403s so no harm done.
  const [debugMode, setDebugMode] = useState<boolean>(false);
  useEffect(() => {
    api.getHealth()
      .then((h) => setDebugMode(Boolean(h.debug_mode)))
      .catch(() => setDebugMode(false));
  }, []);
  // Server Commands developer console gate. The top-level tab renders
  // only for root_admin AND when the ``dev_console_enabled`` tunable
  // is on. The tunable defaults to true on the backend, so an absent
  // key still counts as enabled; only an explicit false hides it.
  const [devConsoleEnabled, setDevConsoleEnabled] = useState<boolean>(false);
  useEffect(() => {
    api.getSettings()
      .then((s) => {
        const v = s.tunables?.dev_console_enabled as unknown;
        setDevConsoleEnabled(
          v !== false && v !== 0 && v !== 'false' && v !== 'False',
        );
      })
      .catch(() => setDevConsoleEnabled(false));
  }, []);
  // Both sub-tab states persist independently - navigating away and back
  // always restores the last active sub-tab rather than resetting.
  const [runSubTab, setRunSubTab] = useState<RunSubTab>('run');
  const [serversSubTab, setServersSubTab] = useState<ServersSubTab>('servers');
  // Account tab inner state. Defaults to the user's own account
  // surface; the Account Management sub-tab is only visible for roles
  // with ``users.manage`` (admin / root_admin).
  const [accountSubTab, setAccountSubTab] = useState<AccountSubTab>('account');
  // Settings tab inner state. Defaults to system settings; the snap-back
  // effect below picks the first sub-tab the user actually has
  // permission for when they land on the Settings tab.
  const [settingsSubTab, setSettingsSubTab] = useState<SettingsSubTab>('settings');
  // Inner page within the Account Management sub-tab. Persists
  // separately so the user's last-viewed inner page survives
  // navigation around the rest of the app.
  const [accountMgmtPage, setAccountMgmtPage] = useState<AccountMgmtPage>('db_admin');

  // Permission flags for the tab strip + sub-tab strips. Use the same
  // hook every gated component uses so Switch View Mode (when
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

  // Switch View Mode modal visibility. Only root_admin ever sees the
  // trigger button (rendered conditionally below).
  const [showSwitchViewModal, setShowSwitchViewModal] = useState(false);
  // Login-as-root re-auth modal visibility. Trigger lives next to
  // Logout in the topbar. The flow runs a fresh /api/auth/login call
  // against root_admin credentials and swaps the in-memory access
  // token on success; the existing refresh-token cookie is replaced
  // by the new login's cookie, so the prior session is overwritten.
  const [showLoginAsRootModal, setShowLoginAsRootModal] = useState(false);

  // The Settings tab carries end user surfaces plus the always-on
  // Help reference page; even if the effective role has none of the
  // end user surfaces the tab stays visible because Help is available
  // to everyone, which makes the tab effectively unconditional. Kept
  // as a constant for symmetry with the snap-back and tab-strip logic
  // below.
  const canSeeSettingsTab = true;

  // Snap the active tab back to a permitted one when the effective
  // role changes (Switch View Mode drop) and the current tab is no
  // longer visible.
  useEffect(() => {
    if (tab === 'run' && !canStartJobs) setTab('dashboard');
    if (tab === 'settings' && !canSeeSettingsTab) setTab('dashboard');
    if (tab === 'developer' && !debugMode) setTab('dashboard');
    if (
      tab === 'server_commands'
      && !(auth.role === 'root_admin' && devConsoleEnabled)
    ) {
      setTab('dashboard');
    }
  }, [tab, canStartJobs, canSeeSettingsTab, debugMode, auth.role, devConsoleEnabled]);
  // Servers sub-tab snap-back. ``users`` (User Management) is end
  // user+ only; ``exports`` is gated by canViewExports. If a Switch
  // View Mode drop strands the caller on either, bounce back to the
  // plain Servers list.
  useEffect(() => {
    if (tab === 'servers' && serversSubTab === 'users' && !canStartJobs) {
      setServersSubTab('servers');
    }
    if (tab === 'servers' && serversSubTab === 'exports' && !canViewExports) {
      setServersSubTab('servers');
    }
    if (tab === 'servers' && serversSubTab === 'logs' && !canViewLogs) {
      setServersSubTab('servers');
    }
  }, [tab, serversSubTab, canStartJobs, canViewExports, canViewLogs]);
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
  // ultimate fallback when no end user surface is available.
  // ``exports`` is a Servers-tab sub-tab, not a Settings sub-tab.
  useEffect(() => {
    if (tab !== 'settings') return;
    const valid =
      (settingsSubTab === 'settings' && canEditSettings) ||
      (settingsSubTab === 'tunables' && canManageTunables) ||
      (settingsSubTab === 'databases' && canManageTunables) ||
      (settingsSubTab === 'logs' && canViewLogs) ||
      settingsSubTab === 'help';
    if (valid) return;
    if (canEditSettings) setSettingsSubTab('settings');
    else if (canManageTunables) setSettingsSubTab('tunables');
    else if (canViewLogs) setSettingsSubTab('logs');
    else setSettingsSubTab('help');
  }, [tab, settingsSubTab, canEditSettings, canManageTunables, canViewLogs]);
  // If the user was viewing the Database Admin inner page and a
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
  // Same snap-back for Access Control: if the user was on this page
  // and lost root_admin (e.g. View Mode drop), bounce them to User
  // Accounts so the page doesn't render under the wrong identity.
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
  // Dashboard multi-job sub-tab selection. ``null`` means "auto-pick
  // the running job," which is also the only sane choice when there's
  // just one job. The strip renders dynamically from the WS payload's
  // ``jobs`` array, mirroring Run Job / Servers / Settings sub-tab
  // visuals. State lives here (not in DashboardPanel) so the strip
  // can sit at the same layer as the other sub-tab strips.
  const [dashJobId, setDashJobId] = useState<string | null>(null);
  // Server-side clock surfaced in the topbar. We fetch the timezone +
  // an initial wallclock from /api/server-time, then tick locally using
  // a skew offset (server_now - browser_now). One periodic re-fetch
  // every 5 min keeps DST transitions and host clock drift in line
  // without polling once per second.
  const [serverTime, setServerTime] = useState<ServerTime | null>(null);
  // Drives the 1 Hz topbar server-clock re-render (pauses on tab-hide).
  useNowTick(1000);
  const clockSkewRef = useRef<number>(0);
  // Last running/finished snapshot, kept so a completed job's
  // dashboard can be retained after it ends. Lazy-initialised from
  // localStorage on first render so a "kept" dashboard (see
  // persistDashboard) survives a full page reload.
  const lastRunningRef = useRef<RetainedSnapshot | null>();
  if (lastRunningRef.current === undefined) {
    lastRunningRef.current = readPersistedSnapshot();
  }

  // Dashboard "keep results until next job" toggle (browser-local).
  // When on, a finished job's full dashboard stays on screen until a
  // NEW job starts instead of clearing after POST_FINISH_RETAIN_MS.
  const [persistDashboard, setPersistDashboard] = useState<boolean>(() => {
    try {
      return localStorage.getItem(DASHBOARD_PERSIST_KEY) === '1';
    } catch {
      return false;
    }
  });
  const setPersistDashboardPref = useCallback((next: boolean) => {
    setPersistDashboard(next);
    try {
      localStorage.setItem(DASHBOARD_PERSIST_KEY, next ? '1' : '0');
    } catch {
      /* localStorage unavailable (private mode) - in-memory only */
    }
  }, []);

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
        const entry: RetainedSnapshot = { snap: msg, at: lastMsg };
        lastRunningRef.current = entry;
        // On the terminal frame, mirror the retained snapshot to
        // localStorage so a "kept" dashboard survives a page reload.
        // Only the finished frame is written - a mid-run reload
        // reconnects the socket and gets live data anyway.
        if (
          state === 'completed' || state === 'completed_with_errors'
          || state === 'failed' || state === 'cancelled'
        ) {
          writePersistedSnapshot(entry);
        }
      }
      setSnapshot(msg);
      setConn('connected');
    });
    const stopStaleCheck = pausableInterval(() => {
      if (Date.now() - lastMsg > 5000) setConn('disconnected');
    }, 1000);
    return () => {
      unsubscribe();
      stopStaleCheck();
    };
  }, []);

  // Server-clock lifecycle: one initial fetch + a 5-minute refresh.
  // The 1 Hz tick that advances the rendered HH:MM:SS is useNowTick.
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
    return pausableInterval(load, 5 * 60 * 1000);
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
          // The REST one-shot fallback doesn't know about fan-out
          // - the field is populated on the next WS tick if a fan-out
          // job is in flight. ``null`` here keeps the type intact and
          // gives the DashboardPanel its single-destination render
          // path until the socket catches up.
          fan_out: null,
          // servers_network ships only via WS; the REST fallback
          // returns an empty array and the Networking tab shows
          // "Waiting for first tick…" until the socket connects.
          servers_network: [],
          job:
            j.state === 'idle'
              ? null
              : {
                  job_id: '',
                  mode: (j.mode as JobPayload['mode']) || 'snapshot',
                  state: j.state as JobPayload['state'],
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
    if (retained && (persistDashboard || Date.now() - retained.at < POST_FINISH_RETAIN_MS)) {
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
    // user chose to look away from the server clock, so the browser
    // zone is the right context for those modes.
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
        <div className="brand" title="Hestia-MediaManager"><span className="wordmark-badge">HM²</span><span className="brand-fullname">Hestia-MediaManager</span></div>
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
            <ThemeSwitcher />
          </div>
          {/* ``onLogout`` is the truthy gate - App.tsx only passes a
              function when auth is enabled AND a token is in state.
              ``currentUser`` is optional decoration; if the JWT
              payload didn't decode cleanly the button still appears
              so the user never gets locked into a half-state with no
              way out. */}
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
                    <span
                      data-testid="topbar-view-mode-chip"
                      style={{
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
                      }}
                    >
                      Viewing as {auth.effectiveRole.replace('_', ' ')}
                    </span>
                  )}
                </span>
              )}
              {/* Elevation chip. Only visible when the caller is
                  currently elevated (sudo-style cache is live). Shows
                  a countdown to expiry and lets the user drop
                  elevation manually. */}
              {auth.realRole === 'root_admin' && <TopbarElevationChip />}
              {/* Switch View Mode button visibility is keyed off the
                  REAL role, not the effective one - the button must
                  stay reachable while dropped so the user can always
                  restore. Hidden only for viewer (no drop targets
                  exist for that role). */}
              {auth.realRole !== 'viewer' && (
                <button
                  onClick={() => setShowSwitchViewModal(true)}
                  title="Change the active permission level for this session. Resets on page refresh."
                  style={{ fontSize: 12 }}
                  data-testid="topbar-switch-permissions"
                >
                  Switch permissions
                </button>
              )}
              <button
                onClick={onLogout}
                title="Log out and clear the refresh-token cookie."
                style={{ fontSize: 12 }}
                data-testid="logout-btn"
              >
                Log out
              </button>
              {/* Login-as-root shortcut. Visible only when the user's
                  REAL role is below root_admin and they are not already in
                  a switched-down view (use Exit view mode from the
                  permissions modal in that case). Single-click path to
                  the same viewModeEnter('root_admin') flow surfaced
                  via the Switch permissions modal; kept as a dedicated
                  button because many sessions need root for a single
                  action and burying it in the modal is friction. */}
              {auth.realRole !== 'root_admin'
                && auth.realRole === auth.role
                && auth.realRole !== 'viewer' && (
                <button
                  onClick={() => setShowLoginAsRootModal(true)}
                  title="Elevate this session to root_admin permissions. Requires the root password."
                  style={{ fontSize: 12 }}
                >
                  Login as root
                </button>
              )}
            </div>
          )}
        </div>
      </header>

      {/* Top-level tabs are permission-gated. ``Run Job`` is hidden
          for viewer (no ``jobs.start``); ``Account`` is visible to
          every role (everyone can manage their own profile);
          ``Settings`` is visible only when the role has at least one
          end user surface (system settings, logs, or exports). */}
      <nav className="tabs" data-testid="tab-strip">
        <button className={tab === 'dashboard' ? 'active' : ''} onClick={() => setTab('dashboard')} data-testid="tab-dashboard">Dashboard</button>
        {canStartJobs && (
          <button className={tab === 'run' ? 'active' : ''} onClick={() => setTab('run')} data-testid="tab-jobs">Jobs</button>
        )}
        <button className={tab === 'servers' ? 'active' : ''} onClick={() => setTab('servers')} data-testid="tab-servers">Servers</button>
        <button className={tab === 'account' ? 'active' : ''} onClick={() => setTab('account')} data-testid="tab-account">Account</button>
        {canSeeSettingsTab && (
          <button className={tab === 'settings' ? 'active' : ''} onClick={() => setTab('settings')} data-testid="tab-settings">Settings</button>
        )}
        {auth.role === 'root_admin' && devConsoleEnabled && (
          <button
            className={tab === 'server_commands' ? 'active' : ''}
            onClick={() => setTab('server_commands')}
            title="Root-admin developer console: live per-item state, raw API calls, membership editing."
            data-testid="tab-dev-console"
          >
            Server Commands
          </button>
        )}
        {/* Dev Blog tab temporarily hidden pre-v0.18.0. The panel,
            iframe wrapper, and the static HTML under
            frontend/public/dev-blog/ are all still present in the
            tree but not shipped. Un-comment this button when the
            blog content is ready for public viewing. */}
        {/* <button
          className={tab === 'devblog' ? 'active' : ''}
          onClick={() => setTab('devblog')}
          title="About the developer and walkthroughs of how this app was built."
        >
          Dev Blog
        </button> */}
        {debugMode && (
          <button
            className={tab === 'developer' ? 'active' : ''}
            onClick={() => setTab('developer')}
            title="Visible only when PLEXMIGRATE_DEBUG_MODE is enabled on the backend."
          >
            Developer
          </button>
        )}
      </nav>

      {tab === 'run' && canStartJobs && (
        <nav className="tabs sub-tabs">
          <button className={runSubTab === 'run' ? 'active' : ''} onClick={() => setRunSubTab('run')}>Run Job</button>
          {/* Schedules sub-tab requires schedules.view (end user+) -
              viewer never reaches this whole branch anyway because
              Run Jobs is hidden for them. */}
          {canViewSchedules && (
            <button className={runSubTab === 'schedules' ? 'active' : ''} onClick={() => setRunSubTab('schedules')}>Schedules</button>
          )}
          {/* Playlist Transfer sub-tab. Same permission gate as Run
              Job; viewers don't see this branch. The feature is
              specifically about transferring a playlist from one user
              to another, which the label makes explicit. Internal
              identifiers + API URLs keep the -mgmt-prefixed names;
              only the operator-facing label differs. */}
          <button className={runSubTab === 'playlists' ? 'active' : ''} onClick={() => setRunSubTab('playlists')}>Playlist Transfer</button>
          {/* Server Syncing. Cross-server library mapping + sync
              subscriptions. Same ``canStartJobs`` gate as the rest of
              this strip, so no inner permission check is needed. */}
          <button
            className={runSubTab === 'syncing' ? 'active' : ''}
            onClick={() => setRunSubTab('syncing')}
            title="Cross-server library mapping + sync subscriptions (watch counts, ratings, playlists)."
          >
            Server Syncing
          </button>
        </nav>
      )}

      {tab === 'servers' && (
        <nav className="tabs sub-tabs">
          <button className={serversSubTab === 'servers' ? 'active' : ''} onClick={() => setServersSubTab('servers')}>Overview</button>
          <button className={serversSubTab === 'networking' ? 'active' : ''} onClick={() => setServersSubTab('networking')}>Networking</button>
          {/* User Management. End user+ only; hidden from viewer
              since they have no jobs to set up credentials for. */}
          {canStartJobs && (
            <button className={serversSubTab === 'users' ? 'active' : ''} onClick={() => setServersSubTab('users')}>User Management</button>
          )}
          {/* Run Defaults. These are the run-level knobs (paths,
              performance, snapshot defaults, transfer resolution,
              retention ceiling) that describe HOW snapshots and
              direct transfers operate against Plex. settings.edit
              gates write access; the panel itself stays read-only
              without it. */}
          {canEditSettings && (
            <button className={serversSubTab === 'run_defaults' ? 'active' : ''} onClick={() => setServersSubTab('run_defaults')}>Run Defaults</button>
          )}
          {/* Per-server snapshot defaults + retention overrides. Gated
              behind settings.edit since it changes engine behaviour. */}
          {canEditSettings && (
            <button className={serversSubTab === 'advanced' ? 'active' : ''} onClick={() => setServersSubTab('advanced')}>Advanced Settings</button>
          )}
          {/* Export. The exports list is server-scoped, so it belongs
              on the Servers tab. Gated by exports.view; rendered last
              so the Servers flow (Overview -> Networking -> Users ->
              Advanced) stays at the front of the strip. */}
          {canViewExports && (
            <button className={serversSubTab === 'exports' ? 'active' : ''} onClick={() => setServersSubTab('exports')}>Export</button>
          )}
          {/* Logs sub-tab. Mirrors the per-server-grouped layout of
              Exports so the user can scope log browsing to one server
              without leaving the Servers tab. The global view of
              every run (including ones whose server has since been
              removed) lives under Settings > Logs. */}
          {canViewLogs && (
            <button className={serversSubTab === 'logs' ? 'active' : ''} onClick={() => setServersSubTab('logs')}>Logs</button>
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
          logs (end user+). Help is always visible since it's a reference
          page with no destructive controls. The "Settings" sub-tab is
          labeled "General Settings" to distinguish it from Servers >
          Advanced Settings (per-server config). */}
      {tab === 'settings' && (
        <nav className="tabs sub-tabs">
          {canEditSettings && (
            <button className={settingsSubTab === 'settings' ? 'active' : ''} onClick={() => setSettingsSubTab('settings')}>General Settings</button>
          )}
          {/* System Tunables - root_admin only. Infrastructure-level
              knobs (HTTP timeouts, JWT TTL, SQLite busy timeout,
              etc.). */}
          {canManageTunables && (
            <button className={settingsSubTab === 'tunables' ? 'active' : ''} onClick={() => setSettingsSubTab('tunables')}>Tunables</button>
          )}
          {/* Databases viewer - root_admin only. Read-only schema +
              row browser for every SQLite database the app creates.
              Same gate as Tunables + Access Control (most sensitive
              end user surface in the app; auth.db is in here). */}
          {canManageTunables && (
            <button
              className={settingsSubTab === 'databases' ? 'active' : ''}
              onClick={() => setSettingsSubTab('databases')}
              data-testid="tab-databases"
            >Databases</button>
          )}
          {canViewLogs && (
            <button
              className={settingsSubTab === 'logs' ? 'active' : ''}
              onClick={() => setSettingsSubTab('logs')}
              data-testid="tab-app-logs"
            >Logs</button>
          )}
          <button className={settingsSubTab === 'help' ? 'active' : ''} onClick={() => setSettingsSubTab('help')}>Help</button>
        </nav>
      )}

      {/* Dashboard multi-job sub-tab strip. Renders only when more
          than one job is active or queued; the single-job case has no
          strip. Lives at the same visual layer as Run Job / Servers
          / Settings sub-tabs so the nesting feels consistent. The
          buttons are built dynamically from the WS payload's ``jobs``
          array - no hardcoded tab count. */}
      {tab === 'dashboard' && (snapshot?.jobs?.length ?? 0) > 1 && (() => {
        const jobs = snapshot!.jobs!;
        // Resolve the effective selection so the active highlight is
        // never blank: the end user's pick (when still in the list),
        // else the running job, else the first job. ``dashJobId`` is
        // not auto-cleared when stale - the end user keeps the
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
              // FECORE-14: queue position is 1-based among ONLY the
              // queued jobs, not the raw array index (index 0 would
              // otherwise render "#0" when no job is running).
              const queuePos =
                jobs.slice(0, idx).filter((q) => q.state === 'queued').length + 1;
              const label = isRunning
                ? `${mode} (running)`
                : j.state === 'queued'
                  ? `${mode} (queue #${queuePos})`
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
        <Suspense fallback={<div className="panel"><div className="empty">Loading…</div></div>}>
        {tab === 'dashboard' && (
          <>
            <DashboardPersistToggle value={persistDashboard} onChange={setPersistDashboardPref} />
            <DashboardPanel
              snapshot={displaySnapshot}
              connState={conn}
              selectedJobId={dashJobId}
            />
            {/* Per-operation runtime data lives in its own panel
                below the live dashboard. The panel reads from
                /api/run-timings/runs and is collapsible via the
                Verbose toggle (default ON). Keeping it as a sibling
                of DashboardPanel rather than embedding inside
                DashboardPanel avoids editing the 2200-line dashboard
                component. The current job is passed in so the panel
                can auto-refresh on the running -> terminal transition;
                without it a just-completed restore wouldn't be
                visible until the user clicked Refresh. */}
            <RuntimeBreakdownPanel job={displaySnapshot?.job ?? null} />
            {/* Download-logs panel for the current / last-completed
                run. Persists between runs (reads from the same
                displaySnapshot.job that DashboardPanel uses, which
                itself freezes on the last job until a new one
                starts). */}
            <DashboardDownloadLogsPanel job={displaySnapshot?.job ?? null} />
          </>
        )}
        {tab === 'run' && runSubTab === 'run' && canStartJobs && <JobFormPanel snapshot={snapshot} />}
        {tab === 'run' && runSubTab === 'schedules' && canViewSchedules && <SchedulesPanel />}
        {tab === 'run' && runSubTab === 'playlists' && canStartJobs && <PlaylistManagementPanel />}
        {tab === 'run' && runSubTab === 'syncing' && canStartJobs && <ServerSyncingPage />}
        {tab === 'servers' && serversSubTab === 'servers' && <ServersPanel />}
        {tab === 'servers' && serversSubTab === 'networking' && <NetworkingPanel snapshot={snapshot} />}
        {tab === 'servers' && serversSubTab === 'users' && canStartJobs && <UserManagementPanel />}
        {tab === 'servers' && serversSubTab === 'run_defaults' && canEditSettings && <RunDefaultsPanel />}
        {tab === 'servers' && serversSubTab === 'advanced' && canEditSettings && <ServerAdvancedSettingsPanel />}
        {tab === 'servers' && serversSubTab === 'exports' && canViewExports && <ExportsPanel />}
        {tab === 'servers' && serversSubTab === 'logs' && canViewLogs && <ServerLogsPanel />}
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
        {tab === 'settings' && settingsSubTab === 'databases' && canManageTunables && <DatabasesPanel />}
        {/* Settings > Logs hosts APP-LEVEL logs only; per-run job
            logs live under the ServersPanel > Logs sub-tab grouped by
            server. ``LogsPanel`` still exists in the codebase but is
            no longer mounted; deletion is a follow-up cleanup once
            nothing else references it. */}
        {tab === 'settings' && settingsSubTab === 'logs' && canViewLogs && <ApplicationLogsPanel />}
        {tab === 'settings' && settingsSubTab === 'help' && <HelpPanel />}
        {tab === 'devblog' && <DevBlogPanel />}
        {tab === 'developer' && debugMode && <DeveloperPanel />}
        {tab === 'server_commands' && auth.role === 'root_admin' && devConsoleEnabled && (
          <ServerCommandsPanel />
        )}
        </Suspense>
      </main>

      {showLoginAsRootModal && (
        <LoginAsRootModal
          onClose={() => setShowLoginAsRootModal(false)}
          onSuccess={(newToken) => {
            if (onSessionSwap) onSessionSwap(newToken);
            setShowLoginAsRootModal(false);
          }}
        />
      )}
      {showSwitchViewModal && (
        <SwitchViewModeModal onClose={() => setShowSwitchViewModal(false)} />
      )}
    </div>
  );
}


// Server-side View Mode. The override lives in _VIEW_MODE_SESSIONS on
// the backend, keyed by the caller's JWT jti. Every enter/exit hits
// the server, then we refetch /me to pull the new effective_role into
// context. Password is required for every transition (enter, switch
// between drops, exit).
//
// The dropdown shows the caller's drop targets only - all roles
// strictly below their REAL role. The user can switch between any two
// of those targets without exiting first; the server overwrites the
// existing entry.

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
  //     target is free - the end user already has the higher privilege.
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
      <Modal onClose={onClose} title="Switch view mode" width={360}>
        <span className="help">
          Your role has no roles below it to preview as.
        </span>
        <div className="row-buttons" style={{ marginTop: 12 }}>
          <button onClick={onClose}>Close</button>
        </div>
      </Modal>
    );
  }

  return (
    <Modal onClose={onClose} width={480}>
        <h2 style={{ marginTop: 0 }} data-testid="view-mode-modal">
          Switch permissions
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
          <select
            value={target}
            onChange={(e) => setTarget(e.target.value as Role)}
            data-testid="view-mode-select"
          >
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
            data-testid="view-mode-confirm"
          >
            {submitting
              ? 'Switching…'
              : auth.inViewMode
                ? `Switch to ${target.replace('_', ' ')}`
                : `Enter view mode as ${target.replace('_', ' ')}`}
          </button>
          {exitVisible && (
            <button
              onClick={applyExit}
              disabled={!exitEnabled}
              data-testid="view-mode-exit"
            >
              Exit view mode
            </button>
          )}
          <button onClick={onClose} disabled={submitting}>Cancel</button>
        </div>
    </Modal>
  );
}


// ── Login-as-Root modal ─────────────────────────────────────────────────────
//
// Re-auth flow. Triggered from the topbar button placed under Logout.
// Calls /api/auth/login with the root account credentials, the
// backend rotates the refresh-token cookie and returns a fresh
// access token, and App.tsx's onSessionSwap callback adopts both.
// The prior admin session is replaced; logging out from the new
// root session does not bring the admin session back (intentional:
// the end user chose to swap, not stack).
//
// The existing /api/auth/login endpoint already implements every
// security control needed (rate limit, audit log, password hash
// comparison) so no new backend code is required.

function LoginAsRootModal({
  onClose,
  onSuccess,
}: {
  onClose: () => void;
  onSuccess: (newToken: string) => void;
}) {
  const [username, setUsername] = useState('root');
  const [password, setPassword] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const canSubmit = !submitting && username.length > 0 && password.length > 0;

  const submit = async () => {
    if (!canSubmit) return;
    setSubmitting(true);
    setError(null);
    try {
      const session = await api.authLogin(username, password);
      // Confirm the resulting role is actually root_admin so the
      // user doesn't think they elevated when they actually just
      // signed in as a non-root account. The backend already rejects
      // bad credentials; this guard catches a "wrong username typed"
      // case (root_admin role landed on the wrong user record).
      if (session.user.role !== 'root_admin') {
        setError(
          `Sign-in succeeded but the resulting role is "${session.user.role}", `
            + 'not root_admin. Use the actual root account name to elevate.',
        );
        // Discard the token rather than silently swap; the user
        // probably did not mean to drop to a non-root account.
        return;
      }
      onSuccess(session.access_token);
    } catch (e) {
      setError(String(e));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Modal onClose={onClose} title="Login as root" width={380}>
        <span className="help" style={{ display: 'block', color: 'var(--text-dim)', fontSize: 12, marginBottom: 12 }}>
          Sign in as the root account. This replaces your current
          session entirely; logging out afterward does not return you
          to the previous account.
        </span>
        <div className="field">
          <span className="label">Username</span>
          <input
            type="text"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            disabled={submitting}
            autoFocus
          />
        </div>
        <div className="field" style={{ marginTop: 8 }}>
          <span className="label">Password</span>
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter') void submit(); }}
            disabled={submitting}
          />
        </div>
        {error && (
          <div className="banner error" style={{ fontSize: 12, marginTop: 12 }}>
            {error}
          </div>
        )}
        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8, marginTop: 16 }}>
          <button onClick={onClose} disabled={submitting}>Cancel</button>
          <button onClick={() => void submit()} disabled={!canSubmit}>
            {submitting ? 'Signing in…' : 'Sign in'}
          </button>
        </div>
    </Modal>
  );
}


// String-literal alias used in one place only.
type JobPayloadState = 'idle' | 'queued' | 'running' | 'completed' | 'failed' | 'cancelled';


// ── Item 1: topbar elevation chip ────────────────────────────────────────────
//
// Renders a small "Elevated · MM:SS" pill when the current session
// has a live sudo-style elevation, with a click handler to drop it.
// Visible only to root_admins; other roles can't elevate and would
// never see a live expiry.

function TopbarElevationChip() {
  const { expiresAt, tick, dropElevation } = useElevation();
  if (expiresAt === null) return null;
  // Re-read time via the per-second tick so the countdown repaints
  // without us depending on Date.now() inside React's render cycle.
  void tick;
  const secondsLeft = Math.max(0, expiresAt - Math.floor(Date.now() / 1000));
  const mm = Math.floor(secondsLeft / 60).toString().padStart(2, '0');
  const ss = (secondsLeft % 60).toString().padStart(2, '0');
  return (
    <button
      onClick={() => { void dropElevation(); }}
      title="Click to drop elevation now (sudo -k equivalent)."
      style={{
        fontSize: 11,
        fontWeight: 700,
        letterSpacing: 0.3,
        textTransform: 'uppercase',
        padding: '2px 10px',
        borderRadius: 999,
        background: '#2e5a3f',
        color: '#fff',
        border: 'none',
        cursor: 'pointer',
      }}
    >
      Elevated · {mm}:{ss}
    </button>
  );
}
