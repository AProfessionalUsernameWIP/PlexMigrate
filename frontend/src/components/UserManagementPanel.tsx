// PR-10 - User Management sub-tab under Servers.
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

import { useEffect, useState } from 'react';
import { api, GlobalTombstone, ServerManagedUser, ServerView } from '../api';
import { InfoTip } from './InfoTip';


export function UserManagementPanel() {
  const [servers, setServers] = useState<ServerView[] | null>(null);
  const [serverError, setServerError] = useState<string | null>(null);
  const [selectedServerId, setSelectedServerId] = useState<string | null>(null);
  const [detailUsername, setDetailUsername] = useState<string | null>(null);
  // ``refreshKey`` is bumped after a global sync so the currently-
  // visible UserListView re-fetches without the operator having to
  // switch servers manually.
  const [refreshKey, setRefreshKey] = useState(0);
  // PR-11.1 follow-up - global "Sync all servers" state.
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
      // Force the visible UserListView to refetch so the operator
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

      <ServerSelector
        servers={servers}
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
  // PR-11.1 - 'Show hidden' toggle. Includes both per-server and
  // globally tombstoned rows. Default off so the picker contract
  // (visible users only) and the everyday operator view match.
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
              never touches stored credentials.
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
                <th>Last seen</th>
                <th>State</th>
              </tr>
            </thead>
            <tbody>
              {users.map((u) => {
                const isHidden = u.hidden_scope !== 'none';
                return (
                  <tr
                    key={u.username}
                    onClick={() => onSelect(u.username)}
                    style={{
                      cursor: 'pointer',
                      opacity: isHidden ? 0.55 : 1,
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
                      <StatusPill
                        stored={u.has_pin || u.has_password}
                        label={
                          u.has_pin && u.has_password ? 'PIN + password' :
                          u.has_pin ? 'PIN stored' :
                          u.has_password ? 'Password stored' :
                          'Not stored'
                        }
                      />
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

      {/* PR-11.1 - global tombstones panel. Lists usernames hidden
          across every server; lets the operator unhide directly from
          here (useful when the username has no row on the current
          server because the sync has been skipping it). */}
      <GlobalTombstonesPanel onChange={refresh} />
    </>
  );
}


function GlobalTombstonesPanel({ onChange }: { onChange: () => void | Promise<void> }) {
  const [rows, setRows] = useState<GlobalTombstone[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState<string | null>(null);

  const refresh = async () => {
    setError(null);
    try {
      const r = await api.listGlobalTombstones();
      setRows(r.usernames);
    } catch (e) {
      setError(String(e));
    }
  };
  useEffect(() => { void refresh(); }, []);

  const submitUnhide = async (creds: { username: string; password: string }) => {
    if (!pending) return;
    try {
      await api.removeGlobalTombstone(pending, {
        db_admin_username: creds.username,
        db_admin_password: creds.password,
      });
      setPending(null);
      await refresh();
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
  // Per-server credential writes (existing PR-10 behaviour).
  | { kind: 'save_display'; display_name: string | null }
  | { kind: 'save_token'; auth_token: string }
  | { kind: 'clear_token' }
  | { kind: 'save_pin'; plex_home_pin: string }
  | { kind: 'clear_pin' }
  | { kind: 'save_password'; service_password: string }
  | { kind: 'clear_password' }
  // PR-11.1 - tombstones replace the old purge-on-delete. Per-server
  // hide preserves the row + credentials; global hide adds the
  // username to the global tombstones table so the sync helper skips
  // it on every server going forward.
  | { kind: 'hide_server' }
  | { kind: 'hide_global' }
  | { kind: 'unhide_server' }
  | { kind: 'unhide_global' }
  // PR-13 fix #2 - same credential applied to every server where
  // this username has a row. db_admin gated; backend walks the
  // registry and writes to each matching managed_users row.
  | { kind: 'global_save_token'; auth_token: string }
  | { kind: 'global_save_pin'; plex_home_pin: string }
  | { kind: 'global_save_password'; service_password: string };


function UserDetailView({
  serverId,
  username,
  onBack,
}: {
  serverId: string;
  username: string;
  onBack: () => void;
}) {
  const [user, setUser] = useState<ServerManagedUser | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [ok, setOk] = useState<string | null>(null);
  const [displayDraft, setDisplayDraft] = useState('');
  const [tokenDraft, setTokenDraft] = useState('');
  const [pinDraft, setPinDraft] = useState('');
  const [passwordDraft, setPasswordDraft] = useState('');
  const [pendingWrite, setPendingWrite] = useState<WritePayload | null>(null);

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
      // PR-13 fix #2 - global credential apply. One endpoint per
      // credential kind, walks every registered server.
      if (
        pendingWrite.kind === 'global_save_token' ||
        pendingWrite.kind === 'global_save_pin' ||
        pendingWrite.kind === 'global_save_password'
      ) {
        const kind: 'auth_token' | 'plex_home_pin' | 'service_password' =
          pendingWrite.kind === 'global_save_token' ? 'auth_token' :
          pendingWrite.kind === 'global_save_pin' ? 'plex_home_pin' :
          'service_password';
        const plaintext =
          pendingWrite.kind === 'global_save_token' ? pendingWrite.auth_token :
          pendingWrite.kind === 'global_save_pin' ? pendingWrite.plex_home_pin :
          pendingWrite.service_password;
        const r = await api.setGlobalManagedUserCredential(username, {
          db_admin_username: creds.username,
          db_admin_password: creds.password,
          kind,
          plaintext,
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
          break;
        case 'clear_pin':
          body.clear_plex_home_pin = true;
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
      // Keep the modal open on failure so the operator can retype.
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
        inputType="password"
      />

      {user.service_type === 'plex' && (
        <CredentialSection
          title="Plex Home PIN"
          tip="Numeric PIN protecting this managed user's profile. Required to access PIN-scoped content; PR-12 uses this for the pre-flight check before jobs run."
          stored={user.has_pin}
          draft={pinDraft}
          setDraft={setPinDraft}
          onSave={() => setPendingWrite({ kind: 'save_pin', plex_home_pin: pinDraft })}
          onClear={() => setPendingWrite({ kind: 'clear_pin' })}
          onSaveGlobal={() => setPendingWrite({ kind: 'global_save_pin', plex_home_pin: pinDraft })}
          inputType="password"
          inputMode="numeric"
          pattern="[0-9]*"
        />
      )}

      {(user.service_type === 'emby' || user.service_type === 'jellyfin') && (
        <CredentialSection
          title="Service password"
          tip="Login password for this user on the Emby / Jellyfin server. Encrypted at rest; used by the engine to authenticate per-user calls."
          stored={user.has_password}
          draft={passwordDraft}
          setDraft={setPasswordDraft}
          onSave={() => setPendingWrite({ kind: 'save_password', service_password: passwordDraft })}
          onClear={() => setPendingWrite({ kind: 'clear_password' })}
          onSaveGlobal={() => setPendingWrite({ kind: 'global_save_password', service_password: passwordDraft })}
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
    </>
  );
}


function describeAction(p: WritePayload): string {
  switch (p.kind) {
    case 'save_display':    return p.display_name === null ? 'Clear display name' : 'Save display name';
    case 'save_token':      return 'Save auth token';
    case 'clear_token':     return 'Clear auth token';
    case 'save_pin':        return 'Save Plex Home PIN';
    case 'clear_pin':       return 'Clear Plex Home PIN';
    case 'save_password':       return 'Save service password';
    case 'clear_password':      return 'Clear service password';
    case 'hide_server':         return 'Hide user on this server';
    case 'hide_global':         return 'Hide username globally';
    case 'unhide_server':       return 'Unhide user on this server';
    case 'unhide_global':       return 'Remove global tombstone';
    case 'global_save_token':   return 'Save auth token on every server';
    case 'global_save_pin':     return 'Save Plex Home PIN on every server';
    case 'global_save_password':return 'Save service password on every server';
  }
}


// ── Credential section (one per credential cell) ────────────────────────────

function CredentialSection({
  title, tip, stored, draft, setDraft,
  onSave, onClear, onSaveGlobal, inputType, inputMode, pattern,
}: {
  title: string;
  tip: string;
  stored: boolean;
  draft: string;
  setDraft: (v: string) => void;
  onSave: () => void;
  onClear: () => void;
  // PR-13 fix #2 - optional global-apply hook. When provided, the
  // section renders an "Apply to every server where this user
  // exists" checkbox. Checking it flips the Save button's callback
  // from ``onSave`` (this server only) to ``onSaveGlobal`` (walk the
  // registry).
  onSaveGlobal?: () => void;
  inputType: 'text' | 'password';
  inputMode?: 'numeric' | 'text';
  pattern?: string;
}) {
  const [globalApply, setGlobalApply] = useState(false);
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
            Writes the same value into every registered server's
            <code> managed_users</code> row matching this username.
            Use this when the same Plex Home user has the same PIN /
            password / token across multiple servers. Backend walks
            the server registry; servers where the username has no
            row yet are skipped (run a sync first to populate them).
          </span>
        </label>
      )}
      <div className="row-buttons">
        <button
          className="primary"
          disabled={!draft}
          onClick={globalApply && onSaveGlobal ? onSaveGlobal : onSave}
        >
          {globalApply && onSaveGlobal ? 'Save on every server' : 'Save'}
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


// ── DB admin auth modal ─────────────────────────────────────────────────────

function DbAdminAuthModal({
  action,
  onCancel,
  onSubmit,
}: {
  action: string;
  onCancel: () => void;
  onSubmit: (creds: { username: string; password: string }) => Promise<void>;
}) {
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const canSubmit = !submitting && username.length > 0 && password.length > 0;

  const submit = async () => {
    if (!canSubmit) return;
    setError(null);
    setSubmitting(true);
    try {
      await onSubmit({ username, password });
      // onSubmit closes the modal on success; we leave state here so
      // if the caller decides to keep the modal open (e.g. for a
      // multi-step flow) the user doesn't lose their typing.
    } catch (e) {
      setError(String(e));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div
      onClick={onCancel}
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
        <h2 style={{ marginTop: 0 }}>Confirm with database admin</h2>
        <span className="help" style={{ display: 'block', color: 'var(--text-dim)', fontSize: 12, marginBottom: 12 }}>
          <strong>{action}</strong> requires Database Admin Account
          credentials. Set up or rotate these under
          Settings → Account Management → Database Admin Account.
        </span>
        {error && <div className="banner error">{error}</div>}
        <label className="field">
          <span className="label">Database admin username</span>
          <input
            type="text"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            autoComplete="username"
            autoFocus
          />
        </label>
        <label className="field">
          <span className="label">Database admin password</span>
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete="current-password"
          />
        </label>
        <div className="row-buttons" style={{ marginTop: 12 }}>
          <button className="primary" disabled={!canSubmit} onClick={submit}>
            {submitting ? 'Confirming…' : 'Confirm'}
          </button>
          <button onClick={onCancel} disabled={submitting}>Cancel</button>
        </div>
      </div>
    </div>
  );
}
