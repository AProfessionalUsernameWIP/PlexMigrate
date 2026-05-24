// User Management sub-tab under Servers.
//
// Three render modes inside one panel:
//
//   * List view   : server selector + table of users for the selected
//                   server. Click a row to drill into the detail view.
//                   Sync button refreshes the table from the live API.
//   * Detail view : full row metadata + write-only inputs for the
//                   three credential cells (auth token, Plex Home PIN,
//                   service password). Each save attempt opens the
//                   DbAdminAuthModal to collect the db_admin credentials
//                   needed to authorise the write.
//   * Modal       : DbAdminAuthModal is shared by every write path
//                   (save credential, clear credential, save display
//                   name, delete user). Collects username + password,
//                   passes them to the caller-supplied submit fn, never
//                   persists them anywhere.
//
// Stored credential values are never returned by the API - the row
// only carries ``has_token`` / ``has_pin`` / ``has_password`` booleans
// and the UI renders "(stored - re-enter to replace)" hints next to
// inputs that already have a value on the server side.

import { useEffect, useMemo, useState } from 'react';
import { api, GlobalTombstone, ServerManagedUser, ServerView, UserIdentityMap } from '../api';
import { useResourceQuery } from '../hooks/useResourceQuery';
import { DbAdminAuthModal } from './DbAdminAuthModal';
import { InfoTip } from './InfoTip';
import { useElevation } from '../contexts/ElevationContext';
import { useAuthContext } from '../contexts/AuthContext';
import { UserCopyPanel } from './UserCopyPanel';
import { UserMappingPanel } from './UserMappingPanel';
import { RevealCredentialsModal } from './RevealCredentialsModal';
import {
  BackendTabStrip,
  BackendType,
  backendCounts,
  serversForBackend,
} from './BackendTabStrip';


export function UserManagementPanel() {
  const [servers, setServers] = useState<ServerView[] | null>(null);
  const [serverError, setServerError] = useState<string | null>(null);
  const [selectedServerId, setSelectedServerId] = useState<string | null>(null);
  // Backend tier above the existing per-server ServerSelector so the
  // user can narrow to Plex / Jellyfin / Emby users separately.
  const [activeBackend, setActiveBackend] = useState<BackendType>('plex');
  const [detailUsername, setDetailUsername] = useState<string | null>(null);
  // ``refreshKey`` is bumped after a global sync so the currently-
  // visible UserListView re-fetches without the user having to
  // switch servers manually.
  const [refreshKey, setRefreshKey] = useState(0);
  // Global "Sync all servers" state.
  const [syncingAll, setSyncingAll] = useState(false);
  const [globalSyncResult, setGlobalSyncResult] = useState<
    | null
    | {
        synced_total: number;
        per_server: { id: string; name: string; synced: number; error: string | null }[];
      }
  >(null);

  // Load registered servers once on mount. The Servers tab's existing
  // poll keeps the registry warm; here we just need a snapshot of
  // available servers for the selector.
  useEffect(() => {
    api.listServers()
      .then((rows) => {
        setServers(rows);
        if (rows.length > 0 && selectedServerId === null) {
          setSelectedServerId(rows[0].id);
        }
      })
      .catch((e) => setServerError(String(e)));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Derived: registered-server list filtered to the active backend.
  // Passed into ServerSelector so its per-server strip only shows
  // backends the user chose. Memoised so the dependency-tracked
  // effect below doesn't fire on every render.
  const backendServers = useMemo(
    () => serversForBackend(servers || [], activeBackend),
    [servers, activeBackend],
  );

  // Auto-correct activeBackend when its bucket is empty and another
  // backend has servers. Mirrors the same pattern in ServersPanel
  // so a user who deletes the last server of the active backend
  // doesn't stare at an empty pane.
  useEffect(() => {
    if (!servers || servers.length === 0) return;
    const counts = backendCounts(servers);
    if (counts[activeBackend] === 0) {
      const fallback = (['plex', 'jellyfin', 'emby'] as BackendType[])
        .find((b) => counts[b] > 0);
      if (fallback) setActiveBackend(fallback);
    }
  }, [servers, activeBackend]);

  // When the active backend changes (or its server list refreshes),
  // ensure the per-server selection still points at a row of the
  // active backend. Otherwise switching to Jellyfin while a Plex
  // serverId was selected would leave the panel showing stale
  // (cross-backend) detail.
  useEffect(() => {
    if (selectedServerId === null) return;
    const stillValid = backendServers.some((s) => s.id === selectedServerId);
    if (!stillValid) {
      setSelectedServerId(backendServers[0]?.id || null);
      setDetailUsername(null);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [backendServers]);

  const syncAllServers = async () => {
    if (servers === null || servers.length === 0) return;
    setSyncingAll(true);
    setGlobalSyncResult(null);
    try {
      const results = await Promise.allSettled(
        servers.map((s) =>
          api.syncServerManagedUsers(s.id).then((r) => ({ server: s, r })),
        ),
      );
      const per_server = results.map((res, i) => {
        const s = servers[i];
        if (res.status === 'fulfilled') {
          return {
            id: s.id,
            name: s.name,
            synced: res.value.r.synced,
            error: res.value.r.source_error,
          };
        }
        return {
          id: s.id,
          name: s.name,
          synced: 0,
          error: String((res as PromiseRejectedResult).reason),
        };
      });
      const synced_total = per_server.reduce((acc, p) => acc + p.synced, 0);
      setGlobalSyncResult({ synced_total, per_server });
      // Force the visible UserListView to refetch so the user
      // sees fresh data immediately.
      setRefreshKey((k) => k + 1);
    } finally {
      setSyncingAll(false);
    }
  };

  if (serverError) {
    return <div className="banner error">{serverError}</div>;
  }
  if (servers === null) {
    return <div className="panel"><div className="empty">Loading servers…</div></div>;
  }

  return (
    <>
      <DangerBanner />

      <div className="panel">
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 12, flexWrap: 'wrap' }}>
          <h2 style={{ margin: 0 }}>
            Sync managed users
            <InfoTip>
              Fires the live-API sync against every registered server
              in parallel. Metadata only - stored credentials and
              tombstone flags are preserved. Globally tombstoned
              usernames are skipped regardless of how many servers
              they exist on.
            </InfoTip>
          </h2>
          <button
            className="primary"
            onClick={syncAllServers}
            disabled={syncingAll || servers.length === 0}
          >
            {syncingAll
              ? 'Syncing all servers…'
              : `Sync all ${servers.length} server${servers.length === 1 ? '' : 's'}`}
          </button>
        </div>

        {globalSyncResult && (
          <div className="banner info" style={{ marginTop: 12, fontSize: 12 }}>
            <strong>
              Synced {globalSyncResult.synced_total} user
              {globalSyncResult.synced_total === 1 ? '' : 's'} across{' '}
              {globalSyncResult.per_server.length} server
              {globalSyncResult.per_server.length === 1 ? '' : 's'}.
            </strong>
            <ul style={{ margin: '6px 0 0 18px' }}>
              {globalSyncResult.per_server.map((p) => (
                <li key={p.id}>
                  <strong>{p.name}:</strong> {p.synced} synced
                  {p.error && <span style={{ color: 'var(--text-dim)' }}> - {p.error}</span>}
                </li>
              ))}
            </ul>
          </div>
        )}
      </div>

      <BackendTabStrip
        servers={servers || []}
        activeBackend={activeBackend}
        onChange={setActiveBackend}
      />

      <ServerSelector
        servers={backendServers}
        selectedId={selectedServerId}
        onSelect={(id) => {
          setSelectedServerId(id);
          setDetailUsername(null);
        }}
      />

      {selectedServerId === null ? (
        <div className="panel"><div className="empty">No registered servers. Add one under Servers to begin.</div></div>
      ) : detailUsername ? (
        <UserDetailView
          serverId={selectedServerId}
          username={detailUsername}
          allServers={servers}
          onBack={() => setDetailUsername(null)}
        />
      ) : (
        <UserListView
          serverId={selectedServerId}
          serverName={servers.find((s) => s.id === selectedServerId)?.name || ''}
          onSelect={(u) => setDetailUsername(u)}
          refreshKey={refreshKey}
        />
      )}

      {/* The per-user detail view hosts a scoped UserMappingPanel
          that filters to the viewed user; the user manages mappings
          from inside each user's detail page rather than from a
          single global list at the bottom of the User Management
          tab. */}
    </>
  );
}


// ── Persistent danger banner ────────────────────────────────────────────────

function DangerBanner() {
  return (
    <div
      className="banner"
      style={{
        background: '#3d2f10',
        border: '1px solid #8a6a1f',
        color: '#f1d27a',
        marginBottom: 16,
        fontSize: 13,
      }}
    >
      <strong>Danger zone.</strong>{' '}
      Changes made here directly affect the database and can impact active
      jobs. Every credential write requires the Database Admin Account
      configured under Settings → Account Management.
    </div>
  );
}


// ── Server selector (tabs along the top) ────────────────────────────────────

function ServerSelector({
  servers,
  selectedId,
  onSelect,
}: {
  servers: ServerView[];
  selectedId: string | null;
  onSelect: (id: string) => void;
}) {
  if (servers.length === 0) return null;
  return (
    <nav className="tabs sub-tabs" style={{ marginBottom: 16 }}>
      {servers.map((s) => (
        <button
          key={s.id}
          className={s.id === selectedId ? 'active' : ''}
          onClick={() => onSelect(s.id)}
          title={s.url}
        >
          {s.name}
        </button>
      ))}
    </nav>
  );
}


// ── List view ───────────────────────────────────────────────────────────────

function UserListView({
  serverId,
  serverName,
  onSelect,
  refreshKey = 0,
}: {
  serverId: string;
  serverName: string;
  onSelect: (username: string) => void;
  // Bumped externally (e.g. after a global sync) to force a re-fetch
  // without changing the server selection.
  refreshKey?: number;
}) {
  const [users, setUsers] = useState<ServerManagedUser[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [info, setInfo] = useState<string | null>(null);
  const [syncing, setSyncing] = useState(false);
  // 'Show hidden' toggle. Includes both per-server and globally
  // tombstoned rows. Default off so the picker contract (visible
  // users only) and the everyday user view match.
  const [showHidden, setShowHidden] = useState(false);

  const refresh = async () => {
    setError(null);
    try {
      const r = await api.listServerManagedUsers(serverId, showHidden);
      setUsers(r.users);
    } catch (e) {
      setError(String(e));
    }
  };
  useEffect(() => {
    setUsers(null);
    void refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [serverId, showHidden, refreshKey]);

  const runSync = async () => {
    setError(null);
    setInfo(null);
    setSyncing(true);
    try {
      const r = await api.syncServerManagedUsers(serverId);
      setInfo(
        `Synced ${r.synced} user${r.synced === 1 ? '' : 's'}` +
        (r.source_error ? ` (warning: ${r.source_error})` : ''),
      );
      await refresh();
    } catch (e) {
      setError(String(e));
    } finally {
      setSyncing(false);
    }
  };

  return (
    <>
      <div className="panel">
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8, gap: 12, flexWrap: 'wrap' }}>
          <h2 style={{ margin: 0 }}>
            Users on {serverName}
            <InfoTip>
              Read from the local database. The list does not hit Plex
              on every visit; click <em>Sync users from server</em> to
              refresh against the live API. Sync is metadata-only and
              never touches stored credentials. The same sync also
              refreshes share state: which users currently have an
              active share on this server (per
              <code>shared_servers</code>) and which Plex Home users
              are PIN-protected.
            </InfoTip>
          </h2>
          <div style={{ display: 'inline-flex', alignItems: 'center', gap: 12 }}>
            <label style={{ display: 'inline-flex', alignItems: 'center', gap: 6, fontSize: 12, color: 'var(--text-dim)' }}>
              <input
                type="checkbox"
                checked={showHidden}
                onChange={(e) => setShowHidden(e.target.checked)}
              />
              Show hidden
            </label>
            <button onClick={runSync} disabled={syncing}>
              {syncing ? 'Syncing…' : 'Sync users from server'}
            </button>
          </div>
        </div>

        {error && <div className="banner error">{error}</div>}
        {info && <div className="banner info" style={{ fontSize: 12 }}>{info}</div>}

        {users === null ? (
          <div className="empty">Loading users…</div>
        ) : users.length === 0 ? (
          <div className="empty">
            {showHidden
              ? 'No users in the database for this server yet.'
              : (<>No visible users for this server. Toggle <strong>Show hidden</strong> to see tombstoned users, or click <strong>Sync users from server</strong> to pull the live list.</>)}
          </div>
        ) : (
          <table className="list" style={{ width: '100%' }}>
            <thead>
              <tr>
                <th>Username</th>
                <th>Display name</th>
                <th>Service</th>
                <th>Token</th>
                <th>PIN / password</th>
                <th>Share</th>
                <th>Last seen</th>
                <th>State</th>
              </tr>
            </thead>
            <tbody>
              {users.map((u) => {
                const isHidden = u.hidden_scope !== 'none';
                // Grey out rows where Plex.tv reports no active share
                // on this server. The picker also filters them;
                // surfacing them here (rather than hiding) lets the
                // user confirm the cache reflects the change they
                // made on Plex.tv.
                const inactiveShare = u.kind !== 'owner' && u.active_share === false;
                const sharedAtLabel = u.shared_state_refreshed_at
                  ? `Share state refreshed ${new Date(u.shared_state_refreshed_at * 1000).toLocaleString()}`
                  : 'Share state has not been refreshed yet. Click Sync users from server.';
                return (
                  <tr
                    key={u.username}
                    onClick={() => onSelect(u.username)}
                    style={{
                      cursor: 'pointer',
                      opacity: isHidden || inactiveShare ? 0.55 : 1,
                    }}
                  >
                    <td className="mono">{u.username}</td>
                    <td>
                      {u.display_name || <em style={{ color: 'var(--text-dim)' }}>-</em>}
                    </td>
                    <td>{u.service_type}</td>
                    <td>
                      <StatusPill stored={u.has_token} label={u.has_token ? 'Stored' : 'Not stored'} />
                    </td>
                    <td>
                      {/* List view recognises all three per-backend
                          PIN columns from migration v16. Checking
                          only ``has_pin`` would be Plex-specific and
                          would show "Not stored" here for an Emby /
                          Jellyfin user whose EasyPassword was saved
                          via the detail view. */}
                      {(() => {
                        const anyPin = u.has_pin || u.has_emby_pin || u.has_jellyfin_pin;
                        const stored = anyPin || u.has_password;
                        const label = (() => {
                          if (anyPin && u.has_password) return 'PIN + password';
                          if (u.has_pin) return 'PIN stored';
                          if (u.has_emby_pin) return 'EasyPassword stored';
                          if (u.has_jellyfin_pin) return 'EasyPassword stored';
                          if (u.has_password) return 'Password stored';
                          return 'Not stored';
                        })();
                        return <StatusPill stored={stored} label={label} />;
                      })()}
                    </td>
                    <td title={sharedAtLabel}>
                      {u.kind === 'owner' ? (
                        <StatusPill stored={true} label="Owner" />
                      ) : inactiveShare ? (
                        <StatusPill stored={false} label="No active share" />
                      ) : (
                        <StatusPill stored={true} label="Active" />
                      )}
                      {u.is_pin_protected && (() => {
                        // ``is_pin_protected`` is sourced from the
                        // backend's share-state column (Plex live API
                        // for Plex rows; backend-specific signal
                        // elsewhere). The badge reflects whether a PIN
                        // of any backend has actually been stored on
                        // this row.
                        const anyPin = u.has_pin || u.has_emby_pin || u.has_jellyfin_pin;
                        return (
                          <span style={{ marginLeft: 6 }}>
                            <StatusPill
                              stored={anyPin}
                              label={anyPin ? 'PIN-protected (stored)' : 'PIN-protected (no PIN stored)'}
                            />
                          </span>
                        );
                      })()}
                    </td>
                    <td>
                      {u.last_seen
                        ? new Date(u.last_seen * 1000).toLocaleString()
                        : <em style={{ color: 'var(--text-dim)' }}>never</em>}
                    </td>
                    <td>
                      {u.hidden_scope === 'global' ? (
                        <StatusPill stored={false} label="Hidden (global)" />
                      ) : u.hidden_scope === 'server' ? (
                        <StatusPill stored={false} label="Hidden (this server)" />
                      ) : (
                        <StatusPill stored={true} label="Visible" />
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>

      {/* Global tombstones panel. Lists usernames hidden across every
          server; lets the user unhide directly from here (useful when
          the username has no row on the current server because the
          sync has been skipping it). */}
      <GlobalTombstonesPanel onChange={refresh} />
    </>
  );
}


function GlobalTombstonesPanel({ onChange }: { onChange: () => void | Promise<void> }) {
  const [pending, setPending] = useState<string | null>(null);
  const { data: rows, error, reload: refresh, setError } = useResourceQuery<GlobalTombstone[] | null>(
    () => api.listGlobalTombstones().then((r) => r.usernames),
    [],
    null,
  );

  const submitUnhide = async (creds: { username: string; password: string }) => {
    if (!pending) return;
    try {
      await api.removeGlobalTombstone(pending, {
        db_admin_username: creds.username,
        db_admin_password: creds.password,
      });
      setPending(null);
      refresh();
      await onChange();
    } catch (e) {
      setError(String(e));
      throw e;
    }
  };

  if (rows === null) return null;
  if (rows.length === 0) return null;

  return (
    <>
      <div className="panel">
        <h2>
          Globally hidden usernames
          <InfoTip>
            These usernames are skipped by every server's sync. Listed
            here so you can unhide one even if it no longer has a row
            on the currently-selected server.
          </InfoTip>
        </h2>
        <table className="list" style={{ width: '100%' }}>
          <thead>
            <tr>
              <th>Username</th>
              <th>Tombstoned at</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.username}>
                <td className="mono">{r.username}</td>
                <td>{new Date(r.tombstoned_at * 1000).toLocaleString()}</td>
                <td style={{ textAlign: 'right' }}>
                  <button onClick={() => setPending(r.username)}>Unhide</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {error && <div className="banner error" style={{ marginTop: 8 }}>{error}</div>}
      </div>
      {pending && (
        <DbAdminAuthModal
          action={`Unhide global tombstone for ${pending}`}
          onCancel={() => setPending(null)}
          onSubmit={submitUnhide}
        />
      )}
    </>
  );
}


function StatusPill({ stored, label }: { stored: boolean; label: string }) {
  return (
    <span style={{
      background: stored ? '#1f4a2a' : '#3a4150',
      color: stored ? '#9ddca8' : '#cfd6e4',
      padding: '2px 8px',
      borderRadius: 999,
      fontSize: 11,
      fontWeight: 600,
    }}>
      {label}
    </span>
  );
}


// ── Detail view ─────────────────────────────────────────────────────────────

type WritePayload =
  // Per-server credential writes. PIN variants carry
  // ``cross_backend`` which, when true, triggers
  // cross-backend identity-link propagation downstream - that's the
  // "Save & apply across backends" two-button affordance.
  | { kind: 'save_display'; display_name: string | null }
  | { kind: 'save_token'; auth_token: string }
  | { kind: 'clear_token' }
  | { kind: 'save_pin'; plex_home_pin: string; cross_backend?: boolean }
  | { kind: 'clear_pin'; cross_backend?: boolean }
  | { kind: 'save_emby_pin'; emby_easy_pin: string; cross_backend?: boolean }
  | { kind: 'clear_emby_pin'; cross_backend?: boolean }
  | { kind: 'save_jellyfin_pin'; jellyfin_easy_pin: string; cross_backend?: boolean }
  | { kind: 'clear_jellyfin_pin'; cross_backend?: boolean }
  | { kind: 'save_password'; service_password: string }
  | { kind: 'clear_password' }
  // Per-server hide preserves the row + credentials; global hide adds
  // the username to the global tombstones table so the sync helper
  // skips it on every server going forward.
  | { kind: 'hide_server' }
  | { kind: 'hide_global' }
  | { kind: 'unhide_server' }
  | { kind: 'unhide_global' }
  // Same credential applied to every server where this username has a
  // row. db_admin gated; backend walks the registry and writes to
  // each matching managed_users row.
  //
  // The every_backend flag governs the service_type_filter forwarded
  // to the backend. Default (undefined / false) scopes the sweep to
  // the origin row's backend; true tells the backend to skip the
  // filter and walk every registered server regardless of backend.
  | { kind: 'global_save_token'; auth_token: string; every_backend?: boolean }
  | { kind: 'global_save_pin'; plex_home_pin: string; cross_backend?: boolean; every_backend?: boolean }
  | { kind: 'global_save_emby_pin'; emby_easy_pin: string; cross_backend?: boolean; every_backend?: boolean }
  | { kind: 'global_save_jellyfin_pin'; jellyfin_easy_pin: string; cross_backend?: boolean; every_backend?: boolean }
  | { kind: 'global_save_password'; service_password: string; every_backend?: boolean };


function UserDetailView({
  serverId,
  username,
  allServers,
  onBack,
}: {
  serverId: string;
  username: string;
  allServers: ServerView[];
  onBack: () => void;
}) {
  // Copy-user-to-destination panel. Visibility toggle gates the panel
  // so the detail view does not grow vertically by default.
  const [copyPanelOpen, setCopyPanelOpen] = useState(false);
  const [user, setUser] = useState<ServerManagedUser | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [ok, setOk] = useState<string | null>(null);
  const [displayDraft, setDisplayDraft] = useState('');
  const [tokenDraft, setTokenDraft] = useState('');
  // Single PIN draft regardless of backend. The user is on one row at
  // a time, so only one PIN section is rendered; the draft + the
  // payload kind are picked from the row's service_type at submit
  // time (see ``pinConfig`` below).
  const [pinDraft, setPinDraft] = useState('');
  const [passwordDraft, setPasswordDraft] = useState('');
  // Count of cross-backend identity_map links for this user. >0
  // enables the "Save & apply across backends" affordance on PIN
  // sections. We fetch once on detail-view load + refresh after any
  // successful write that could change link state (unlikely for
  // credential writes, but cheap).
  const [crossBackendLinkCount, setCrossBackendLinkCount] = useState(0);
  const [pendingWrite, setPendingWrite] = useState<WritePayload | null>(null);
  // Per-user token rotation. The button calls requireElevation()
  // (opens the ElevationModal if the operator isn't currently
  // elevated), then POSTs to the dedicated rotate-token endpoint.
  // Refresh + toast on success.
  const elevation = useElevation();
  const auth = useAuthContext();
  const [rotating, setRotating] = useState(false);
  // Root-admin-only "Reveal credentials" affordance. Modal handles
  // the three-gate auth + the 30-second auto-hide on disclosure.
  const [revealOpen, setRevealOpen] = useState(false);
  const rotateToken = async () => {
    const elev = await elevation.requireElevation(`rotate token for ${username}`);
    if (!elev) return;
    setError(null); setOk(null); setRotating(true);
    try {
      const res = await api.rotateManagedUserToken(serverId, username);
      if (res.captured > 0) {
        setOk(`Token rotated for ${username}.`);
      } else if (res.errors && res.errors.length > 0) {
        setError(`Rotation failed: ${res.errors[0]}`);
      } else {
        setError('Rotation produced no new token (user may be PIN-protected with no stored PIN).');
      }
      await refresh();
    } catch (e) {
      setError(String(e));
    } finally {
      setRotating(false);
    }
  };

  // Per-user "Sync this user" affordance. Distinct from rotateToken
  // above: no elevation required (operator role is enough), scoped to
  // ONE username only via the /capture-token endpoint, and the
  // bulk-sync's full-server scope is never used. Useful for capturing
  // a missing token on a single user (e.g. after the operator just
  // saved their PIN under User Management) without sweeping every
  // other row.
  const [syncingThisUser, setSyncingThisUser] = useState(false);
  const syncThisUser = async () => {
    setError(null); setOk(null); setSyncingThisUser(true);
    try {
      const res = await api.captureUserToken(serverId, username);
      const cap = res.token_capture;
      if (cap.captured > 0) {
        setOk(`Captured per-user token for ${username}.`);
      } else if (cap.throttled) {
        setError('Capture throttled; try again in a moment.');
      } else if (cap.errors && cap.errors.length > 0) {
        setError(`Capture did not store a new token: ${cap.errors[0]}`);
      } else {
        setError('No token was captured (no credential stored for this user, or AuthenticateByName rejected).');
      }
      await refresh();
    } catch (e) {
      setError(String(e));
    } finally {
      setSyncingThisUser(false);
    }
  };

  const refresh = async () => {
    setError(null);
    try {
      const u = await api.getServerManagedUser(serverId, username);
      setUser(u);
      setDisplayDraft(u.display_name || '');
    } catch (e) {
      setError(String(e));
    }
  };
  useEffect(() => {
    void refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [serverId, username]);

  // Compute the number of cross-backend identity_map links for this
  // user. A link is "cross-backend" when the OTHER server's
  // service_type differs from this server's service_type. The count
  // gates the "Save & apply across backends" PIN affordance — there's
  // nothing to apply to if every linked row is on the same backend.
  useEffect(() => {
    let cancelled = false;
    const thisServer = allServers.find((s) => s.id === serverId);
    const thisServiceType = (thisServer?.service_type || '').toLowerCase();
    api.listUserIdentityMaps()
      .then((r) => {
        if (cancelled) return;
        let count = 0;
        for (const m of r.maps || []) {
          const aMatches = m.server_a_id === serverId
            && (m.user_a_handle || '').toLowerCase() === username.toLowerCase();
          const bMatches = m.server_b_id === serverId
            && (m.user_b_handle || '').toLowerCase() === username.toLowerCase();
          if (!aMatches && !bMatches) continue;
          const otherServerId = aMatches ? m.server_b_id : m.server_a_id;
          const other = allServers.find((s) => s.id === otherServerId);
          const otherSvc = (other?.service_type || '').toLowerCase();
          if (otherSvc && thisServiceType && otherSvc !== thisServiceType) {
            count += 1;
          }
        }
        setCrossBackendLinkCount(count);
      })
      .catch(() => {
        if (!cancelled) setCrossBackendLinkCount(0);
      });
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [serverId, username, allServers]);

  const submitWrite = async (creds: { username: string; password: string }) => {
    if (!pendingWrite) return;
    setError(null);
    setOk(null);
    try {
      // Tombstone actions take their own endpoints, so handle them
      // first - they don't share the PATCH body shape with the
      // credential updates.
      if (pendingWrite.kind === 'hide_server') {
        await api.updateServerManagedUser(serverId, username, {
          db_admin_username: creds.username,
          db_admin_password: creds.password,
          tombstoned: true,
        });
        setPendingWrite(null);
        await refresh();
        setOk('User hidden on this server.');
        return;
      }
      if (pendingWrite.kind === 'unhide_server') {
        await api.updateServerManagedUser(serverId, username, {
          db_admin_username: creds.username,
          db_admin_password: creds.password,
          tombstoned: false,
        });
        setPendingWrite(null);
        await refresh();
        setOk('User unhidden on this server.');
        return;
      }
      if (pendingWrite.kind === 'hide_global') {
        await api.addGlobalTombstone({
          db_admin_username: creds.username,
          db_admin_password: creds.password,
          username,
        });
        setPendingWrite(null);
        await refresh();
        setOk('Username hidden globally on every server.');
        return;
      }
      if (pendingWrite.kind === 'unhide_global') {
        await api.removeGlobalTombstone(username, {
          db_admin_username: creds.username,
          db_admin_password: creds.password,
        });
        setPendingWrite(null);
        await refresh();
        setOk('Global tombstone cleared. Username will resync.');
        return;
      }
      // Global credential apply. One endpoint per credential kind,
      // walks every registered server. Migration v16 adds
      // emby/jellyfin PIN kinds + forwards cross_backend_pin when the
      // operator picked "Save & apply across backends".
      if (
        pendingWrite.kind === 'global_save_token' ||
        pendingWrite.kind === 'global_save_pin' ||
        pendingWrite.kind === 'global_save_emby_pin' ||
        pendingWrite.kind === 'global_save_jellyfin_pin' ||
        pendingWrite.kind === 'global_save_password'
      ) {
        const kind:
          | 'auth_token'
          | 'plex_home_pin'
          | 'emby_easy_pin'
          | 'jellyfin_easy_pin'
          | 'service_password' =
          pendingWrite.kind === 'global_save_token' ? 'auth_token' :
          pendingWrite.kind === 'global_save_pin' ? 'plex_home_pin' :
          pendingWrite.kind === 'global_save_emby_pin' ? 'emby_easy_pin' :
          pendingWrite.kind === 'global_save_jellyfin_pin' ? 'jellyfin_easy_pin' :
          'service_password';
        const plaintext =
          pendingWrite.kind === 'global_save_token' ? pendingWrite.auth_token :
          pendingWrite.kind === 'global_save_pin' ? pendingWrite.plex_home_pin :
          pendingWrite.kind === 'global_save_emby_pin' ? pendingWrite.emby_easy_pin :
          pendingWrite.kind === 'global_save_jellyfin_pin' ? pendingWrite.jellyfin_easy_pin :
          pendingWrite.service_password;
        const crossBackend =
          pendingWrite.kind === 'global_save_pin' ||
          pendingWrite.kind === 'global_save_emby_pin' ||
          pendingWrite.kind === 'global_save_jellyfin_pin'
            ? Boolean(pendingWrite.cross_backend)
            : undefined;
        // Scope the sweep to the origin row's backend by default; the
        // operator's "Include other backends" toggle sets
        // every_backend=true on the payload to omit the filter.
        const originSvc = (user?.service_type || '').toLowerCase();
        const serviceTypeFilter =
          !pendingWrite.every_backend
          && (originSvc === 'plex' || originSvc === 'emby' || originSvc === 'jellyfin')
            ? (originSvc as 'plex' | 'emby' | 'jellyfin')
            : undefined;
        const r = await api.setGlobalManagedUserCredential(username, {
          db_admin_username: creds.username,
          db_admin_password: creds.password,
          kind,
          plaintext,
          ...(crossBackend !== undefined ? { cross_backend_pin: crossBackend } : {}),
          ...(serviceTypeFilter !== undefined ? { service_type_filter: serviceTypeFilter } : {}),
        });
        setPendingWrite(null);
        setTokenDraft('');
        setPinDraft('');
        setPasswordDraft('');
        await refresh();
        setOk(
          `Applied to ${r.applied} server${r.applied === 1 ? '' : 's'}` +
          (r.missing ? ` (${r.missing} server${r.missing === 1 ? '' : 's'} had no row for ${username}).` : '.'),
        );
        return;
      }
      const body: Parameters<typeof api.updateServerManagedUser>[2] = {
        db_admin_username: creds.username,
        db_admin_password: creds.password,
      };
      switch (pendingWrite.kind) {
        case 'save_display':
          if (pendingWrite.display_name === null) body.clear_display_name = true;
          else body.display_name = pendingWrite.display_name;
          break;
        case 'save_token':
          body.auth_token = pendingWrite.auth_token;
          break;
        case 'clear_token':
          body.clear_auth_token = true;
          break;
        case 'save_pin':
          body.plex_home_pin = pendingWrite.plex_home_pin;
          if (pendingWrite.cross_backend) body.cross_backend_pin = true;
          break;
        case 'clear_pin':
          body.clear_plex_home_pin = true;
          if (pendingWrite.cross_backend) body.cross_backend_pin = true;
          break;
        case 'save_emby_pin':
          body.emby_easy_pin = pendingWrite.emby_easy_pin;
          if (pendingWrite.cross_backend) body.cross_backend_pin = true;
          break;
        case 'clear_emby_pin':
          body.clear_emby_easy_pin = true;
          if (pendingWrite.cross_backend) body.cross_backend_pin = true;
          break;
        case 'save_jellyfin_pin':
          body.jellyfin_easy_pin = pendingWrite.jellyfin_easy_pin;
          if (pendingWrite.cross_backend) body.cross_backend_pin = true;
          break;
        case 'clear_jellyfin_pin':
          body.clear_jellyfin_easy_pin = true;
          if (pendingWrite.cross_backend) body.cross_backend_pin = true;
          break;
        case 'save_password':
          body.service_password = pendingWrite.service_password;
          break;
        case 'clear_password':
          body.clear_service_password = true;
          break;
      }
      await api.updateServerManagedUser(serverId, username, body);
      setOk('Saved.');
      setTokenDraft('');
      setPinDraft('');
      setPasswordDraft('');
      setPendingWrite(null);
      await refresh();
    } catch (e) {
      setError(String(e));
      // Keep the modal open on failure so the end user can retype.
      // Re-throw so the modal knows the submit failed and stays open.
      throw e;
    }
  };

  if (error && user === null) {
    return (
      <div className="panel">
        <button onClick={onBack} style={{ marginBottom: 12 }}>← Back to user list</button>
        <div className="banner error">{error}</div>
      </div>
    );
  }
  if (user === null) {
    return (
      <div className="panel">
        <button onClick={onBack} style={{ marginBottom: 12 }}>← Back to user list</button>
        <div className="empty">Loading user…</div>
      </div>
    );
  }

  const displayDirty = (displayDraft.trim() || null) !== (user.display_name || null);

  return (
    <>
      <div className="panel">
        <button onClick={onBack} style={{ marginBottom: 12 }}>← Back to user list</button>
        <h2 style={{ marginTop: 0 }}>
          {user.display_name || user.username}{' '}
          <span style={{
            background: '#3a5fb0',
            color: '#fff',
            padding: '2px 8px',
            borderRadius: 999,
            fontSize: 11,
            fontWeight: 600,
            verticalAlign: 'middle',
          }}>
            {user.service_type}
          </span>
        </h2>
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 24, fontSize: 13 }}>
          <div>
            <span className="label">Username:</span>{' '}
            <strong className="mono">{user.username}</strong>
          </div>
          <div>
            <span className="label">Last seen:</span>{' '}
            {user.last_seen
              ? new Date(user.last_seen * 1000).toLocaleString()
              : <em style={{ color: 'var(--text-dim)' }}>never</em>}
          </div>
          {user.machine_identifier && (
            <div>
              <span className="label">Machine ID:</span>{' '}
              <strong className="mono" style={{ fontSize: 11 }}>{user.machine_identifier}</strong>
            </div>
          )}
        </div>
        {error && <div className="banner error" style={{ marginTop: 12 }}>{error}</div>}
        {ok && <div className="banner good" style={{ marginTop: 12 }}>{ok}</div>}
      </div>

      <div className="panel">
        <h2>Display name</h2>
        <label className="field">
          <span className="label">Friendly name (cosmetic)</span>
          <input
            type="text"
            value={displayDraft}
            onChange={(e) => setDisplayDraft(e.target.value)}
            placeholder={user.username}
            autoComplete="off"
          />
        </label>
        <div className="row-buttons">
          <button
            className="primary"
            disabled={!displayDirty}
            onClick={() =>
              setPendingWrite({
                kind: 'save_display',
                display_name: displayDraft.trim() || null,
              })
            }
          >
            Save display name
          </button>
        </div>
      </div>

      <CredentialSection
        title="Auth token"
        tip="Per-user Plex token. Used when the engine impersonates this user during fan-out or direct transfers. Encrypted at rest."
        stored={user.has_token}
        draft={tokenDraft}
        setDraft={setTokenDraft}
        onSave={() => setPendingWrite({ kind: 'save_token', auth_token: tokenDraft })}
        onClear={() => setPendingWrite({ kind: 'clear_token' })}
        onSaveGlobal={() => setPendingWrite({ kind: 'global_save_token', auth_token: tokenDraft })}
        onSaveGlobalEveryBackend={() => setPendingWrite({ kind: 'global_save_token', auth_token: tokenDraft, every_backend: true })}
        inputType="password"
      />

      {/* Per-user "Sync this user" button. Captures a missing or
          stale auth_token for this single user without sweeping the
          rest of the server. Operator role only - no elevation gate,
          because the action is no more destructive than the bulk
          "Sync users from server" button (which is also
          operator-only). For the fully-destructive "rotate a
          known-leaked token" case the existing elevation-gated
          Rotate-token button below is the right choice. */}
      <div className="panel" style={{ paddingTop: 8, paddingBottom: 8 }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 12 }}>
          <span className="help" style={{ color: 'var(--text-dim)', fontSize: 12 }}>
            Capture or refresh this user's per-user auth token from
            the live API. Scoped to this user only — does not touch
            anyone else on the server. Use after saving a new PIN /
            password to immediately try AuthenticateByName for this
            user.
          </span>
          <button
            onClick={syncThisUser}
            disabled={syncingThisUser}
            title="Capture a per-user auth_token for THIS user only. Other users on the server are untouched."
          >
            {syncingThisUser ? 'Syncing…' : 'Sync this user'}
          </button>
        </div>
      </div>

      {/* Per-user manual token rotation. Bypasses the additive-only
          contract on the Refresh sweep so the user can re-capture a
          stale token without overwriting other users' stored tokens.
          Requires sudo-style elevation. */}
      <div className="panel" style={{ paddingTop: 8, paddingBottom: 8 }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 12 }}>
          <span className="help" style={{ color: 'var(--text-dim)', fontSize: 12 }}>
            Re-capture this user's Plex auth token from the live API. Use after
            the user has signed out and back in on Plex's side (which rotates
            their token). Bypasses the per-server throttle. Requires re-confirming
            your password.
          </span>
          <button
            onClick={rotateToken}
            disabled={rotating}
            className="primary"
            title="Force a fresh token capture for this one user."
          >
            {rotating ? 'Rotating…' : 'Rotate token'}
          </button>
        </div>
      </div>

      {/* Credential reveal. Root-admin only; the button is hidden for
          every other role so the forensic trail is shorter. The modal
          handles its own three-gate auth and 30-second auto-hide on
          disclosure. */}
      {auth.effectiveRole === 'root_admin' && (
        <div className="panel" style={{ paddingTop: 8, paddingBottom: 8 }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 12 }}>
            <span className="help" style={{ color: 'var(--text-dim)', fontSize: 12 }}>
              Decrypt + display this user's stored auth token + Plex Home PIN
              in cleartext for 30 seconds. Requires the database admin
              credentials AND your own root admin password. Every reveal is
              recorded to the database access log.
            </span>
            <button
              type="button"
              onClick={() => setRevealOpen(true)}
              className="danger"
              title="Root admin only. Reveals stored credentials in cleartext for 30 seconds."
            >
              Reveal credentials
            </button>
          </div>
        </div>
      )}
      {revealOpen && (
        <RevealCredentialsModal
          serverId={serverId}
          username={username}
          displayName={user.display_name}
          onClose={() => setRevealOpen(false)}
        />
      )}

      {/* ONE PIN section, config driven by the row's backend. Only
          the title + the underlying payload kind change per backend;
          the input control, the draft state, the checkbox semantics,
          the cross-backend propagation behaviour, and the validation
          rules are identical, so we render one ``CredentialSection``
          and feed it the per-backend config. */}
      {(user.service_type === 'plex' || user.service_type === 'emby' || user.service_type === 'jellyfin') && (
        (() => {
          const svc = user.service_type;
          const cfg = svc === 'plex'
            ? {
                title: 'Plex Home PIN',
                tip: "Numeric PIN protecting this managed user's profile. Required to access PIN-scoped content; it is also used for the pre-flight check before jobs run.",
                stored: user.has_pin,
                numeric: true,
              }
            : svc === 'emby'
            ? {
                title: 'Emby EasyPassword (PIN)',
                tip: "Short Emby EasyPassword for this user. Encrypted at rest; the operator stores it once here for future per-user impersonation work + identity-link propagation.",
                stored: user.has_emby_pin,
                numeric: false,
              }
            : {
                title: 'Jellyfin EasyPassword (PIN)',
                tip: "Short Jellyfin EasyPassword for this user. Encrypted at rest; the operator stores it once here for future per-user impersonation work + identity-link propagation.",
                stored: user.has_jellyfin_pin,
                numeric: false,
              };
          const makeSave = (cross_backend: boolean): WritePayload =>
            svc === 'plex'
              ? { kind: 'save_pin', plex_home_pin: pinDraft, ...(cross_backend ? { cross_backend: true } : {}) }
              : svc === 'emby'
              ? { kind: 'save_emby_pin', emby_easy_pin: pinDraft, ...(cross_backend ? { cross_backend: true } : {}) }
              : { kind: 'save_jellyfin_pin', jellyfin_easy_pin: pinDraft, ...(cross_backend ? { cross_backend: true } : {}) };
          const makeClear = (): WritePayload =>
            svc === 'plex' ? { kind: 'clear_pin' }
              : svc === 'emby' ? { kind: 'clear_emby_pin' }
              : { kind: 'clear_jellyfin_pin' };
          const makeGlobal = (every_backend: boolean): WritePayload =>
            svc === 'plex'
              ? { kind: 'global_save_pin', plex_home_pin: pinDraft, ...(every_backend ? { every_backend: true } : {}) }
              : svc === 'emby'
              ? { kind: 'global_save_emby_pin', emby_easy_pin: pinDraft, ...(every_backend ? { every_backend: true } : {}) }
              : { kind: 'global_save_jellyfin_pin', jellyfin_easy_pin: pinDraft, ...(every_backend ? { every_backend: true } : {}) };
          return (
            <CredentialSection
              title={cfg.title}
              tip={cfg.tip}
              stored={cfg.stored}
              draft={pinDraft}
              setDraft={setPinDraft}
              onSave={() => setPendingWrite(makeSave(false))}
              onClear={() => setPendingWrite(makeClear())}
              onSaveGlobal={() => setPendingWrite(makeGlobal(false))}
              onSaveGlobalEveryBackend={() => setPendingWrite(makeGlobal(true))}
              onSaveAcrossBackends={
                crossBackendLinkCount > 0
                  ? () => setPendingWrite(makeSave(true))
                  : undefined
              }
              crossBackendLinkCount={crossBackendLinkCount}
              inputType="password"
              inputMode={cfg.numeric ? 'numeric' : undefined}
              pattern={cfg.numeric ? '[0-9]*' : undefined}
            />
          );
        })()
      )}

      {(user.service_type === 'emby' || user.service_type === 'jellyfin') && (
        <CredentialSection
          title="Service password"
          tip="Full login password for this user on the Emby / Jellyfin server (distinct from the short EasyPassword/PIN above). Encrypted at rest; used by the engine to authenticate per-user calls."
          stored={user.has_password}
          draft={passwordDraft}
          setDraft={setPasswordDraft}
          onSave={() => setPendingWrite({ kind: 'save_password', service_password: passwordDraft })}
          onClear={() => setPendingWrite({ kind: 'clear_password' })}
          onSaveGlobal={() => setPendingWrite({ kind: 'global_save_password', service_password: passwordDraft })}
          onSaveGlobalEveryBackend={() => setPendingWrite({ kind: 'global_save_password', service_password: passwordDraft, every_backend: true })}
          inputType="password"
        />
      )}

      <div className="panel">
        <h2>
          Visibility
          <InfoTip>
            Hiding a user does not delete the row or its stored
            credentials. Per-server hide just filters them out of
            this server's list + JobFormPanel picker. Global hide
            tells the sync helper to skip this username on every
            registered server going forward. Both are reversible
            from this same panel.
          </InfoTip>
        </h2>
        <span className="help" style={{ display: 'block', color: 'var(--text-dim)', fontSize: 12, marginBottom: 8 }}>
          {user.hidden_scope === 'global' ? (
            <>
              <strong>Globally hidden.</strong>{' '}
              This username is in the global tombstones table and will
              be skipped on every server's sync.
            </>
          ) : user.hidden_scope === 'server' ? (
            <>
              <strong>Hidden on this server.</strong>{' '}
              The row is kept in the database with credentials intact;
              it is filtered from the default list and the picker.
            </>
          ) : (
            <>Currently visible. Choose a scope below to hide.</>
          )}
        </span>
        <div className="row-buttons">
          {user.hidden_scope === 'none' && (
            <>
              <button onClick={() => setPendingWrite({ kind: 'hide_server' })}>
                Hide on this server
              </button>
              <button
                className="danger"
                onClick={() => setPendingWrite({ kind: 'hide_global' })}
              >
                Hide globally (all servers)
              </button>
            </>
          )}
          {user.hidden_scope === 'server' && (
            <button
              className="primary"
              onClick={() => setPendingWrite({ kind: 'unhide_server' })}
            >
              Unhide on this server
            </button>
          )}
          {user.hidden_scope === 'global' && (
            <button
              className="primary"
              onClick={() => setPendingWrite({ kind: 'unhide_global' })}
            >
              Remove global tombstone
            </button>
          )}
        </div>
      </div>

      {pendingWrite && (
        <DbAdminAuthModal
          action={describeAction(pendingWrite)}
          onCancel={() => setPendingWrite(null)}
          onSubmit={submitWrite}
        />
      )}

      {/* USER-MGMT-IDENTITY-AUDIT: identity links. The user's own
          app_user_uuid is the validation handle every cross-server
          resolution keys off; every linked row (manual mapping OR
          backend_user_id auto-link) appears here so the end user
          can see who this user is mapped to across servers. */}
      {user && (
        <IdentityLinksPanel
          serverId={serverId}
          serverName={
            allServers.find((s) => s.id === serverId)?.name || serverId
          }
          username={username}
          user={user}
          allServers={allServers}
        />
      )}

      {/* Per-user copy-to-destination panel. Toggled by the button
          below; opens inline so the user does not lose the
          detail-view context. The panel drives
          /api/users/copy_to_destination and, when "Run automatic
          transfer" is checked, also submits a follow-up
          /api/job/direct constrained to this single user. */}
      {user && (
        <div className="panel" style={{ marginTop: 12 }}>
          {!copyPanelOpen ? (
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
              <div style={{ fontSize: 13 }}>
                <strong>Copy this user to another server</strong>
                <span style={{ color: 'var(--text-dim)', marginLeft: 8, fontSize: 12 }}>
                  Creates a matching account on a Jellyfin or Emby
                  destination and (optionally) transfers their data.
                </span>
              </div>
              <button
                type="button"
                onClick={() => setCopyPanelOpen(true)}
                style={{ fontSize: 12, padding: '5px 12px' }}
              >
                Open copy panel
              </button>
            </div>
          ) : (
            <UserCopyPanel
              sourceServer={
                allServers.find((s) => s.id === serverId) || {
                  id: serverId, name: serverId,
                  // Server IDs carry the backend prefix
                  // (emby_/jellyfin_/plex_) since the SERVER-UID
                  // migration, so derive service_type from the id
                  // when the registry lookup misses. Hardcoding
                  // 'plex' here would mislabel Emby/Jellyfin servers
                  // in fallback panels.
                  service_type: serverId.startsWith('emby_') ? 'emby'
                              : serverId.startsWith('jellyfin_') ? 'jellyfin'
                              : 'plex',
                } as ServerView
              }
              sourceUser={user}
              allServers={allServers}
              onClose={() => setCopyPanelOpen(false)}
              onCopySucceeded={() => { void refresh(); }}
            />
          )}
        </div>
      )}

      {/* Scoped identity-map panel. When inside a user detail view,
          the mapping list is filtered to rows involving this user;
          the add-form's server A + user A are locked to the viewed
          user. The user only picks server B and (optionally) edits
          user B if the target uses a different name. The global
          mapping panel at the bottom of the User Management page
          stays unscoped for cases where the user wants the full
          list. */}
      {user && (
        <div style={{ marginTop: 12 }}>
          <UserMappingPanel
            allServers={allServers}
            scopedTo={{
              server:
                allServers.find((s) => s.id === serverId) || {
                  id: serverId, name: serverId,
                  // Server IDs carry the backend prefix
                  // (emby_/jellyfin_/plex_) since the SERVER-UID
                  // migration, so derive service_type from the id
                  // when the registry lookup misses. Hardcoding
                  // 'plex' here would mislabel Emby/Jellyfin servers
                  // in fallback panels.
                  service_type: serverId.startsWith('emby_') ? 'emby'
                              : serverId.startsWith('jellyfin_') ? 'jellyfin'
                              : 'plex',
                } as ServerView,
              userHandle: username,
              userDisplayName: user.display_name || undefined,
            }}
          />
        </div>
      )}
    </>
  );
}


function describeAction(p: WritePayload): string {
  switch (p.kind) {
    case 'save_display':    return p.display_name === null ? 'Clear display name' : 'Save display name';
    case 'save_token':      return 'Save auth token';
    case 'clear_token':     return 'Clear auth token';
    case 'save_pin':        return p.cross_backend
      ? 'Save Plex Home PIN (apply across backends)'
      : 'Save Plex Home PIN';
    case 'clear_pin':       return p.cross_backend
      ? 'Clear Plex Home PIN (apply across backends)'
      : 'Clear Plex Home PIN';
    case 'save_emby_pin':   return p.cross_backend
      ? 'Save Emby EasyPassword (apply across backends)'
      : 'Save Emby EasyPassword';
    case 'clear_emby_pin':  return p.cross_backend
      ? 'Clear Emby EasyPassword (apply across backends)'
      : 'Clear Emby EasyPassword';
    case 'save_jellyfin_pin': return p.cross_backend
      ? 'Save Jellyfin EasyPassword (apply across backends)'
      : 'Save Jellyfin EasyPassword';
    case 'clear_jellyfin_pin': return p.cross_backend
      ? 'Clear Jellyfin EasyPassword (apply across backends)'
      : 'Clear Jellyfin EasyPassword';
    case 'save_password':       return 'Save service password';
    case 'clear_password':      return 'Clear service password';
    case 'hide_server':         return 'Hide user on this server';
    case 'hide_global':         return 'Hide username globally';
    case 'unhide_server':       return 'Unhide user on this server';
    case 'unhide_global':       return 'Remove global tombstone';
    case 'global_save_token':   return p.every_backend
      ? 'Save auth token on every server (all backends)'
      : 'Save auth token on every server (same backend)';
    case 'global_save_pin':     return (
      (p.every_backend ? 'Save Plex Home PIN on every server (all backends)'
                       : 'Save Plex Home PIN on every server (same backend)')
      + (p.cross_backend ? ' (apply across backends via identity map)' : '')
    );
    case 'global_save_emby_pin': return (
      (p.every_backend ? 'Save Emby EasyPassword on every server (all backends)'
                       : 'Save Emby EasyPassword on every server (same backend)')
      + (p.cross_backend ? ' (apply across backends via identity map)' : '')
    );
    case 'global_save_jellyfin_pin': return (
      (p.every_backend ? 'Save Jellyfin EasyPassword on every server (all backends)'
                       : 'Save Jellyfin EasyPassword on every server (same backend)')
      + (p.cross_backend ? ' (apply across backends via identity map)' : '')
    );
    case 'global_save_password':return p.every_backend
      ? 'Save service password on every server (all backends)'
      : 'Save service password on every server (same backend)';
  }
}


// ── Credential section (one per credential cell) ────────────────────────────

function CredentialSection({
  title, tip, stored, draft, setDraft,
  onSave, onClear, onSaveGlobal, onSaveGlobalEveryBackend,
  onSaveAcrossBackends, crossBackendLinkCount,
  inputType, inputMode, pattern,
}: {
  title: string;
  tip: string;
  stored: boolean;
  draft: string;
  setDraft: (v: string) => void;
  onSave: () => void;
  onClear: () => void;
  // Optional global-apply hook. When provided, the section renders
  // an "Apply to every server where this user exists" checkbox.
  // Checking it flips the Save button's callback from ``onSave``
  // (this server only) to ``onSaveGlobal`` (walk the registry).
  //
  // The default sweep is scoped to the origin row's backend. A
  // second sub-toggle "Include other backends" promotes the sweep to
  // every-backend by dispatching ``onSaveGlobalEveryBackend``
  // instead. The two paths are kept as separate callbacks so the
  // WritePayload kind carries ``every_backend: true`` explicitly when
  // the operator opts in.
  onSaveGlobal?: () => void;
  onSaveGlobalEveryBackend?: () => void;
  // Optional cross-backend hook. When provided, the section renders
  // a second "Save & apply across backends" button next to the
  // default Save. The second button is shown only on PIN sections
  // AND only when the user has at least one identity_map link to a
  // row on a different backend (the operator explicitly linked a
  // Plex managed user to an Emby / Jellyfin row as the same human).
  // Pressing it triggers cross-backend PIN propagation downstream in
  // the backend.
  onSaveAcrossBackends?: () => void;
  crossBackendLinkCount?: number;
  inputType: 'text' | 'password';
  inputMode?: 'numeric' | 'text';
  pattern?: string;
}) {
  const [globalApply, setGlobalApply] = useState(false);
  // Both checkboxes are visible at the same time;
  // ``includeOtherBackends`` is the cross-backend signal regardless
  // of whether ``globalApply`` is on. The single Save button picks
  // its action + label from the combined state. ``onSaveAcrossBackends``
  // only fires when the operator can reach a cross-backend identity
  // link from this row (count > 0); the checkbox is hidden otherwise.
  const [includeOtherBackends, setIncludeOtherBackends] = useState(false);
  const hasCrossBackendOption = !!onSaveAcrossBackends && (crossBackendLinkCount ?? 0) > 0;

  // Single-save-button action + label, decided from the four
  // checkbox-combination states.
  const usingEveryBackendSweep = globalApply && includeOtherBackends && !!onSaveGlobalEveryBackend;
  const usingSameBackendSweep = globalApply && !usingEveryBackendSweep && !!onSaveGlobal;
  const usingCrossBackendOnly = !globalApply && includeOtherBackends && !!onSaveAcrossBackends;
  const saveAction = usingEveryBackendSweep
    ? onSaveGlobalEveryBackend!
    : usingSameBackendSweep
    ? onSaveGlobal!
    : usingCrossBackendOnly
    ? onSaveAcrossBackends!
    : onSave;
  const saveLabel = usingEveryBackendSweep
    ? 'Save on every server (all backends)'
    : usingSameBackendSweep
    ? 'Save on every server (same backend)'
    : usingCrossBackendOnly
    ? 'Save & apply across backends'
    : 'Save';
  return (
    <div className="panel">
      <h2>
        {title}
        <InfoTip>{tip}</InfoTip>
      </h2>
      <label className="field">
        <span className="label">
          {stored ? 'Currently stored (type to replace)' : 'Not stored yet'}
        </span>
        <input
          type={inputType}
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          placeholder={stored ? '- - - - - -' : ''}
          autoComplete="new-password"
          inputMode={inputMode}
          pattern={pattern}
        />
      </label>
      {onSaveGlobal && (
        <label className="switch" style={{ marginTop: 4 }}>
          <input
            type="checkbox"
            checked={globalApply}
            onChange={(e) => setGlobalApply(e.target.checked)}
          />
          <span>Apply to every server where this user exists</span>
          <span className="help">
            Walks the server registry and writes the same value into
            every <code>managed_users</code> row matching this
            username. By default the sweep is scoped to the same
            backend as the current row; check &quot;Include other
            backends&quot; below to extend it across backends. Rows
            that don&apos;t exist yet on a server are skipped (run a
            sync there first to populate them).
          </span>
        </label>
      )}
      {hasCrossBackendOption && (
        <label className="switch" style={{ marginTop: 4 }}>
          <input
            type="checkbox"
            checked={includeOtherBackends}
            onChange={(e) => setIncludeOtherBackends(e.target.checked)}
          />
          <span>Include other backends</span>
          <span className="help">
            Extends the write across backends for the
            {' '}{crossBackendLinkCount} identity-linked
            {(crossBackendLinkCount ?? 0) === 1 ? ' account' : ' accounts'}
            {' '}on Emby/Jellyfin/Plex. Without &quot;Apply to every
            server&quot;, this acts on identity-linked rows only.
            With both checked, the same-backend sweep is extended to
            every backend; each row receives the value into its
            backend-natural PIN column.
          </span>
        </label>
      )}
      <div className="row-buttons">
        <button
          className="primary"
          disabled={!draft}
          onClick={saveAction}
        >
          {saveLabel}
        </button>
        {stored && (
          <button className="danger" onClick={onClear}>
            Clear stored value
          </button>
        )}
      </div>
    </div>
  );
}


// ── USER-MGMT-IDENTITY-AUDIT: identity links panel ─────────────────────────
//
// Renders the user's own app_user_uuid (the canonical cross-server
// validation handle) plus every link to another (server, user) pair
// authored manually OR derived automatically by the
// auto_link_identity_map_by_backend_user_id helper. Lives above the
// Copy-to-Account panel in the per-user detail view.

function IdentityLinksPanel({
  serverId,
  serverName,
  username,
  user,
  allServers,
}: {
  serverId: string;
  serverName: string;
  username: string;
  user: ServerManagedUser;
  allServers: ServerView[];
}) {
  // Filter to rows that name this (server, user) on either side.
  const { data: links, error } = useResourceQuery<UserIdentityMap[] | null>(
    () => api.listUserIdentityMaps().then((r) => (r.maps || []).filter(
      (m) =>
        (m.server_a_id === serverId && (m.user_a_handle || '').toLowerCase() === username.toLowerCase())
        || (m.server_b_id === serverId && (m.user_b_handle || '').toLowerCase() === username.toLowerCase()),
    )),
    [serverId, username],
    null,
  );

  const ownUuid = user.app_user_uuid;
  const serverNameById = (sid: string | null | undefined): string => {
    if (!sid) return '(unresolved)';
    const s = allServers.find((x) => x.id === sid);
    return s ? s.name : sid;
  };

  return (
    <div className="panel" style={{ marginTop: 12 }}>
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 8, marginBottom: 6 }}>
        <strong style={{ fontSize: 13 }}>Identity links</strong>
        <span style={{ color: 'var(--text-dim)', fontSize: 11 }}>
          Cross-server identity handle + every linked account.
        </span>
      </div>

      {ownUuid ? (
        <div style={{ marginBottom: 8 }}>
          <div style={{ fontSize: 11, color: 'var(--text-dim)' }}>
            This user&apos;s app identifier
          </div>
          <code style={{
            display: 'inline-block', fontSize: 11, padding: '2px 6px',
            background: 'var(--code-bg, rgba(127,127,127,0.12))',
            borderRadius: 3, marginTop: 2, wordBreak: 'break-all',
          }}>
            {ownUuid}
          </code>
          <div style={{ fontSize: 11, color: 'var(--text-dim)', marginTop: 4 }}>
            On <strong>{serverName}</strong> as <strong>{username}</strong>
            {user.kind === 'owner' ? ' (server owner)' : ''}
          </div>
        </div>
      ) : (
        <div style={{ fontSize: 12, color: 'var(--text-dim)', marginBottom: 8 }}>
          No app identifier yet for this user; will be assigned on the
          next sync.
        </div>
      )}

      <div style={{ borderTop: '1px solid var(--border, rgba(127,127,127,0.2))', paddingTop: 8 }}>
        <div style={{ fontSize: 11, color: 'var(--text-dim)', marginBottom: 4 }}>
          Linked to {links?.length ?? 0} other account{links?.length === 1 ? '' : 's'}
        </div>
        {error && (
          <div className="banner error" style={{ fontSize: 12, marginTop: 4 }}>
            Could not load links: {error}
          </div>
        )}
        {links !== null && links.length === 0 && !error && (
          <div style={{ fontSize: 11, color: 'var(--text-dim)' }}>
            None yet. Links are created manually via the Mapping panel
            below, or automatically by the backend when two servers
            report the same Plex.tv / Jellyfin / Emby user ID.
          </div>
        )}
        {links !== null && links.length > 0 && (
          <table className="table" style={{ marginTop: 4, fontSize: 12 }}>
            <thead>
              <tr>
                <th style={{ textAlign: 'left' }}>Other server</th>
                <th style={{ textAlign: 'left' }}>Other user</th>
                <th style={{ textAlign: 'left' }}>Source</th>
                <th style={{ textAlign: 'left' }}>App identifier</th>
              </tr>
            </thead>
            <tbody>
              {links.map((m) => {
                const isThisOnA = m.server_a_id === serverId
                  && (m.user_a_handle || '').toLowerCase() === username.toLowerCase();
                const otherServerId = isThisOnA ? m.server_b_id : m.server_a_id;
                const otherHandle = isThisOnA ? m.user_b_handle : m.user_a_handle;
                const otherUuid = isThisOnA ? m.user_b_uuid : m.user_a_uuid;
                return (
                  <tr key={m.id}>
                    <td>{serverNameById(otherServerId)}</td>
                    <td>{otherHandle || <em style={{ color: 'var(--text-dim)' }}>(unresolved)</em>}</td>
                    <td>
                      <span style={{
                        fontSize: 10, padding: '1px 5px',
                        borderRadius: 2,
                        background: m.source === 'auto_copy'
                          ? 'rgba(80,150,200,0.18)'
                          : 'rgba(150,150,150,0.18)',
                      }}>
                        {m.source === 'auto_copy' ? 'auto' : 'manual'}
                      </span>
                    </td>
                    <td>
                      <code style={{
                        fontSize: 10, padding: '1px 4px',
                        background: 'var(--code-bg, rgba(127,127,127,0.12))',
                        borderRadius: 2, wordBreak: 'break-all',
                      }}>
                        {otherUuid}
                      </code>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
