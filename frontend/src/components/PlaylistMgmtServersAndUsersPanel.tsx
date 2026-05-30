// Top panel for the Playlist Management surface. Supports multi-select
// on users with a per-user playlists fan-out below.
//
// Layout, modelled on Run Job's Mode-and-Servers panel:
//
//   +-------------------+ +-------------------+
//   |  Source server    | |  Dest server      |   ← single picks
//   +-------------------+ +-------------------+
//   |  Source users     | |  Dest users       |   ← checkbox lists
//   |  [x] alice        | |  [ ] alice        |
//   |  [ ] bob          | |  [x] charlie      |
//   +-------------------+ +-------------------+
//
// State (server ids + checked user-id sets) is owned by the parent
// (PlaylistManagementPanel) so the playlists fan-out below can read
// the same checked sets. Cache warming on first server selection is
// also parent-owned and surfaced here via the autoWarming flags.

import { useEffect, useState } from 'react';
import { api } from '../api';
import type {
  PingResult,
  PlaylistCacheStatus,
  PlaylistMgmtUser,
  ServerView,
} from '../api';
import { ServerPicker } from './ServerPicker';
import { PlaylistCacheStatusChip } from './PlaylistCacheStatusChip';
import { errorText } from '../utils/format';

// Stable per-row identity for a Playlist Management user.
//
// Prefer the canonical `app_user_uuid` - guaranteed unique per
// (server, user) by the boot-time backfill. The fallback chain
// handles the edge where a user appears live on the adapter but
// hasn't been written to managed_users yet:
//   1. app_user_uuid    (canonical, always unique once persisted)
//   2. backend_user_id  (the backend's own ID; per-backend, may be '')
//   3. username         (always present; unique within a server)
//
// Raw backend_user_id must not be used as the key: adapters that
// don't populate the field return `""` for every user, so all rows
// would collide on the empty-string key and select together.
function userKey(u: PlaylistMgmtUser): string {
  return u.app_user_uuid || u.backend_user_id || u.username;
}

interface Props {
  // Source picker server list. In Smart Playlist mode the parent
  // passes Plex servers only (smart playlists are a Plex-only
  // concept); otherwise it matches the destination list.
  sourceServers: ServerView[];
  // Destination picker server list. Every backend in Smart Playlist
  // and Cross modes; the active backend's servers otherwise.
  destServers: ServerView[];
  pings: Record<string, PingResult>;
  cacheStatus: Record<string, PlaylistCacheStatus>;

  sourceServerId: string;
  onSelectSourceServer: (id: string) => void;
  destServerId: string;
  onSelectDestServer: (id: string) => void;

  checkedSourceUsers: Set<string>;
  onToggleSourceUser: (userId: string) => void;
  checkedDestUsers: Set<string>;
  onToggleDestUser: (userId: string) => void;

  // Passed up so the parent can target the playlists fan-out cards.
  // Each side reports its full users list whenever it refreshes so
  // the parent can map user_id -> username / role for child cards.
  onSourceUsersChange: (users: PlaylistMgmtUser[]) => void;
  onDestUsersChange: (users: PlaylistMgmtUser[]) => void;

  sourceAutoWarming: boolean;
  destAutoWarming: boolean;
  // Per-server refresh nonce; bumping forces a user-list re-fetch
  // (used after a save-token success to flip has_token chips).
  sourceUsersNonce: number;
  destUsersNonce: number;

  // Per-row Save Token affordance on dest side; visible only when
  // dest server is Plex AND tunable mode is per_user_token.
  showDestTokenAffordance: boolean;
  onSaveDestToken: (user: PlaylistMgmtUser) => void;

  // Manual refresh button per side. Forces a cache refresh + status
  // re-fetch on the relevant server.
  onManualRefresh: (side: 'source' | 'dest') => void;

  // Transferability predicate (dest side). Users for whom this returns
  // false are hidden from the dest list — same rule the snapshot UI
  // applies for users who can't be written to (e.g. Plex per_user_token
  // mode with strict_identity_resolution=true and no saved token).
  // Defaults to "show all" when omitted. Follows an all-or-nothing
  // exclusion model: users who'd definitely fail at copy time don't
  // appear in the picker at all.
  destUserIsTransferable?: (u: PlaylistMgmtUser) => boolean;
}

function cacheKey(serverId: string, userId: string): string {
  return `${serverId}::${userId}`;
}

interface SideProps {
  label: 'Source' | 'Destination';
  servers: ServerView[];
  pings: Record<string, PingResult>;
  cacheStatus: Record<string, PlaylistCacheStatus>;
  serverId: string;
  onSelectServer: (id: string) => void;
  checked: Set<string>;
  onToggle: (userId: string) => void;
  onUsersChange: (users: PlaylistMgmtUser[]) => void;
  // Optional filter applied to the displayed user list. Unmatched
  // users are hidden from the picker AND from `onUsersChange` (parent
  // never sees them), so checked sets stay aligned with what's visible.
  filterUser?: (u: PlaylistMgmtUser) => boolean;
  autoWarming: boolean;
  usersNonce: number;
  showTokenAffordance?: boolean;
  onSaveToken?: (user: PlaylistMgmtUser) => void;
  onManualRefresh: () => void;
}

function Side({
  label,
  servers,
  pings,
  cacheStatus,
  serverId,
  onSelectServer,
  checked,
  onToggle,
  onUsersChange,
  filterUser,
  autoWarming,
  usersNonce,
  showTokenAffordance,
  onSaveToken,
  onManualRefresh,
}: SideProps) {
  const [users, setUsers] = useState<PlaylistMgmtUser[] | null>(null);
  const [usersError, setUsersError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);

  useEffect(() => {
    if (!serverId) {
      setUsers(null);
      setUsersError(null);
      onUsersChange([]);
      return;
    }
    let cancelled = false;
    setUsers(null);
    setUsersError(null);
    // Initial load (usersNonce === 0) uses the cache-first endpoint
    // path so server selection is near-instant. Manual Refresh clicks
    // bump the nonce and pass force_refresh=true so the backend
    // re-syncs from the live adapter before returning.
    const forceRefresh = usersNonce > 0;
    api.playlistMgmtListUsers(serverId, forceRefresh)
      .then((r) => {
        if (cancelled) return;
        const raw = r.users || [];
        // Hide unauthorized / unwriteable users entirely (same model
        // the snapshot picker uses for non-transferable users). The
        // parent's checked-Set never sees them, so they can't be
        // accidentally targeted.
        const next = filterUser ? raw.filter(filterUser) : raw;
        setUsers(next);
        onUsersChange(next);
      })
      .catch((e) => {
        if (cancelled) return;
        setUsersError(errorText(e));
        onUsersChange([]);
      });
    return () => { cancelled = true; };
    // onUsersChange + filterUser intentionally omitted; parent callback
    // identity shouldn't drive a re-fetch. The parent re-mounts the
    // panel (via key) if it wants the filter to re-apply.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [serverId, usersNonce]);

  const runRefresh = async () => {
    if (!serverId) return;
    setRefreshing(true);
    try {
      await onManualRefresh();
    } finally {
      setRefreshing(false);
    }
  };

  const userCacheStatus = (userId: string): PlaylistCacheStatus | undefined => {
    if (!serverId || !userId) return undefined;
    return cacheStatus[cacheKey(serverId, userId)];
  };

  const sideKey = label === 'Source' ? 'source' : 'dest';
  return (
    <div className="panel" style={{ display: 'flex', flexDirection: 'column' }}>
      <h3 style={{ marginTop: 0, marginBottom: 8 }}>{label}</h3>

      <div
        className="field"
        data-testid={sideKey === 'source' ? 'plmgmt-source-server' : 'plmgmt-dest-server'}
        style={{ marginBottom: 12 }}
      >
        <span className="label">Server</span>
        <ServerPicker
          value={serverId}
          onChange={onSelectServer}
          servers={servers}
          pings={pings}
        />
      </div>

      {serverId && (
        <div className="field">
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 4 }}>
            <span className="label" style={{ marginBottom: 0 }}>Users</span>
            <button
              type="button"
              onClick={() => void runRefresh()}
              disabled={refreshing}
              style={{ fontSize: 11 }}
              title="Force-refresh this server's playlist cache. Bypasses TTL."
            >
              {refreshing ? 'Refreshing…' : 'Refresh'}
            </button>
          </div>

          {autoWarming && (
            <div className="banner info" style={{ fontSize: 11, marginBottom: 6 }}>
              Warming playlist cache for this server… this happens once per session.
            </div>
          )}

          {usersError ? (
            <div className="banner error" style={{ fontSize: 12 }}>
              Could not load users: {usersError}
            </div>
          ) : users === null ? (
            <div className="empty" style={{ fontSize: 12 }}>Loading users…</div>
          ) : users.length === 0 ? (
            <div className="empty" style={{ fontSize: 12 }}>No users reported.</div>
          ) : (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 4, maxHeight: 260, overflowY: 'auto' }}>
              {users.map((u) => {
                const isChecked = checked.has(userKey(u));
                const status = userCacheStatus(userKey(u));
                const roleLabel = u.role.charAt(0).toUpperCase() + u.role.slice(1);
                const roleClass = u.role === 'owner' ? 'started' : u.role === 'admin' ? 'failed' : 'phase';
                return (
                  <div
                    key={userKey(u)}
                    data-testid={`plmgmt-${sideKey}-user-${u.username}`}
                    style={{
                      display: 'flex',
                      alignItems: 'center',
                      gap: 8,
                      padding: '6px 10px',
                      borderRadius: 4,
                      background: isChecked ? 'var(--bg-panel)' : 'transparent',
                      border: isChecked
                        ? '1px solid var(--accent, #4a7afc)'
                        : '1px solid transparent',
                    }}
                  >
                    <label
                      style={{
                        flex: 1,
                        display: 'flex',
                        alignItems: 'center',
                        gap: 8,
                        cursor: 'pointer',
                        fontSize: 12,
                      }}
                    >
                      <input
                        type="checkbox"
                        checked={isChecked}
                        onChange={() => onToggle(userKey(u))}
                      />
                      <span style={{ flex: 1 }}>
                        <strong>{u.username}</strong>
                        <span
                          className={`tag ${roleClass}`}
                          style={{ fontSize: 10, marginLeft: 6 }}
                        >
                          {roleLabel}
                        </span>
                        {showTokenAffordance && (
                          <span
                            className={`tag ${u.has_token ? 'done' : 'failed'}`}
                            style={{ fontSize: 10, marginLeft: 6 }}
                            title={u.has_token
                              ? 'A Plex Home token is saved for this user; per-user-token copies will use it.'
                              : 'No Plex Home token saved. Copies under per_user_token mode will fail until one is saved.'}
                          >
                            {u.has_token ? 'token ✓' : 'no token'}
                          </span>
                        )}
                      </span>
                    </label>
                    <PlaylistCacheStatusChip status={status} />
                    {showTokenAffordance && (
                      <button
                        type="button"
                        onClick={(e) => {
                          e.stopPropagation();
                          onSaveToken?.(u);
                        }}
                        style={{
                          fontSize: 10,
                          padding: '2px 6px',
                          border: '1px solid var(--border, #2a3146)',
                          borderRadius: 3,
                          background: 'transparent',
                          color: 'var(--text-dim)',
                          cursor: 'pointer',
                        }}
                        title="Open the Plex Home token form for this user"
                      >
                        {u.has_token ? 'Replace token' : 'Save token'}
                      </button>
                    )}
                  </div>
                );
              })}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export function PlaylistMgmtServersAndUsersPanel({
  sourceServers,
  destServers,
  pings,
  cacheStatus,
  sourceServerId,
  onSelectSourceServer,
  destServerId,
  onSelectDestServer,
  checkedSourceUsers,
  onToggleSourceUser,
  checkedDestUsers,
  onToggleDestUser,
  onSourceUsersChange,
  onDestUsersChange,
  sourceAutoWarming,
  destAutoWarming,
  sourceUsersNonce,
  destUsersNonce,
  showDestTokenAffordance,
  onSaveDestToken,
  onManualRefresh,
  destUserIsTransferable,
}: Props) {
  return (
    <div className="grid-2" style={{ marginTop: 12 }}>
      <Side
        label="Source"
        servers={sourceServers}
        pings={pings}
        cacheStatus={cacheStatus}
        serverId={sourceServerId}
        onSelectServer={onSelectSourceServer}
        checked={checkedSourceUsers}
        onToggle={onToggleSourceUser}
        onUsersChange={onSourceUsersChange}
        autoWarming={sourceAutoWarming}
        usersNonce={sourceUsersNonce}
        onManualRefresh={() => onManualRefresh('source')}
      />
      <Side
        label="Destination"
        servers={destServers}
        pings={pings}
        cacheStatus={cacheStatus}
        serverId={destServerId}
        onSelectServer={onSelectDestServer}
        checked={checkedDestUsers}
        onToggle={onToggleDestUser}
        onUsersChange={onDestUsersChange}
        filterUser={destUserIsTransferable}
        autoWarming={destAutoWarming}
        usersNonce={destUsersNonce}
        showTokenAffordance={showDestTokenAffordance}
        onSaveToken={onSaveDestToken}
        onManualRefresh={() => onManualRefresh('dest')}
      />
    </div>
  );
}
