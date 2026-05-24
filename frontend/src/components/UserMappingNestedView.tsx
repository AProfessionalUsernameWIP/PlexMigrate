// Nested User Mapping view.
//
// Wraps UserMappingPanel in a per-server + per-user navigation shell.
// Rationale: with N registered servers and M users per server, the
// flat list of identity_map rows gets unwieldy fast. The shell
// provides:
//   - top-level sub-tab strip per source server
//   - under each server, a per-user sub-tab strip showing every
//     user that lives on that server, and how they are mapped to
//     accounts on other servers
//
// Data flow:
//   - api.listServers          → the server sub-tab strip
//   - api.listServerUsers(id)  → the per-user sub-tab strip for
//                                the currently selected server
//   - api.listUserIdentityMaps → all identity-map rows; filtered
//                                client-side for the count badges
//                                and the overview list
//   - UserMappingPanel scopedTo={server, userHandle} renders the
//     identity-link list + add-form locked to the picked user
//
// The component does not own the identity map state mutation -
// UserMappingPanel handles add / delete and re-fetches on its own.
// We just give it the right scope.

import { useEffect, useMemo, useState } from 'react';
import { api } from '../api';
import type {
  ServerView, ServerUser, UserIdentityMap,
} from '../api';
import { UserMappingPanel } from './UserMappingPanel';


interface Props {
  servers: ServerView[];
}


export function UserMappingNestedView({ servers }: Props) {
  // Top-level: which server's users we're looking at. Defaults to
  // the first registered server.
  const [activeServerId, setActiveServerId] = useState<string>('');
  // Within the active server, which user is selected. Null means
  // we're on the per-server overview (list of users + their map
  // counts) rather than a specific user.
  const [activeUserHandle, setActiveUserHandle] = useState<string | null>(null);

  // Per-server user lists. Loaded lazily on first visit to each
  // server's tab so a first paint with 10 servers doesn't fire 10
  // parallel listServerUsers() requests.
  const [usersByServer, setUsersByServer] = useState<
    Record<string, ServerUser[]>
  >({});
  const [userLoadError, setUserLoadError] = useState<
    Record<string, string | null>
  >({});
  const [loadingUsers, setLoadingUsers] = useState<boolean>(false);

  // All identity map rows. Refreshed whenever the operator switches
  // tabs (cheap; one round-trip; keeps the count badges accurate
  // after an add/delete inside UserMappingPanel).
  const [maps, setMaps] = useState<UserIdentityMap[]>([]);
  const [mapsError, setMapsError] = useState<string | null>(null);

  const serverLabel = (id: string): string => {
    const s = servers.find((x) => x.id === id);
    if (!s) return id || '(unknown)';
    const backend = (s as ServerView & { service_type?: string }).service_type;
    return backend && backend !== 'plex' ? `${s.name} [${backend}]` : s.name;
  };

  // Seed the active server when servers first arrive (or change).
  useEffect(() => {
    if (!activeServerId && servers.length > 0) {
      setActiveServerId(servers[0].id);
    }
    // If the active server got removed, fall back to the first one.
    if (activeServerId && !servers.some((s) => s.id === activeServerId)) {
      setActiveServerId(servers[0]?.id || '');
      setActiveUserHandle(null);
    }
  }, [servers, activeServerId]);

  const refreshMaps = async () => {
    try {
      const r = await api.listUserIdentityMaps();
      setMaps(r.maps);
      setMapsError(null);
    } catch (e) {
      setMapsError(`Could not load identity maps: ${(e as Error).message}`);
    }
  };

  const loadServerUsers = async (serverId: string) => {
    if (!serverId || usersByServer[serverId]) return;
    setLoadingUsers(true);
    try {
      const r = await api.listServerUsers(serverId);
      setUsersByServer((prev) => ({ ...prev, [serverId]: r.users }));
      setUserLoadError((prev) => ({
        ...prev,
        [serverId]: r.error || null,
      }));
    } catch (e) {
      setUserLoadError((prev) => ({
        ...prev,
        [serverId]: `Could not load users: ${(e as Error).message}`,
      }));
    } finally {
      setLoadingUsers(false);
    }
  };

  // Initial map load + reload on server switch (refresh badges).
  useEffect(() => {
    void refreshMaps();
  }, []);
  useEffect(() => {
    if (activeServerId) void loadServerUsers(activeServerId);
    // Clear the active user when switching servers so we land on the
    // server's overview by default.
    setActiveUserHandle(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeServerId]);

  // ── Derived data ─────────────────────────────────────────────

  // Maps involving the active server (either side).
  const mapsForActiveServer = useMemo<UserIdentityMap[]>(() => {
    if (!activeServerId) return [];
    return maps.filter(
      (m) => m.server_a_id === activeServerId || m.server_b_id === activeServerId,
    );
  }, [maps, activeServerId]);

  // For each user on the active server, count how many maps involve
  // them (used for the per-user tab badge). User identity is tracked
  // by raw_name (handle) since that's what UserMappingPanel's
  // scopedTo prop expects.
  const mapCountByHandle = useMemo<Record<string, number>>(() => {
    const out: Record<string, number> = {};
    for (const m of mapsForActiveServer) {
      const handle = (m.server_a_id === activeServerId
        ? m.user_a_handle
        : m.user_b_handle) || '';
      if (handle) out[handle] = (out[handle] || 0) + 1;
    }
    return out;
  }, [mapsForActiveServer, activeServerId]);

  const usersOnActiveServer: ServerUser[] = usersByServer[activeServerId] || [];

  const activeServer: ServerView | undefined = servers.find(
    (s) => s.id === activeServerId,
  );

  const activeUser: ServerUser | undefined = usersOnActiveServer.find(
    (u) => u.raw_name === activeUserHandle,
  );

  // ── Render ───────────────────────────────────────────────────

  if (servers.length === 0) {
    return (
      <div className="empty">
        No servers registered yet. Add one under the{' '}
        <strong>Servers</strong> tab to start mapping users.
      </div>
    );
  }

  return (
    <div>
      {mapsError && (
        <div className="banner error" style={{ marginBottom: 8 }}>{mapsError}</div>
      )}

      {/* ── Per-server top tab strip ─────────────────────── */}
      <nav
        className="subnav"
        style={{ display: 'flex', gap: 4, marginBottom: 12, flexWrap: 'wrap' }}
      >
        {servers.map((s) => {
          const count = maps.filter(
            (m) => m.server_a_id === s.id || m.server_b_id === s.id,
          ).length;
          return (
            <button
              key={s.id}
              className={activeServerId === s.id ? 'active' : ''}
              onClick={() => setActiveServerId(s.id)}
              title={`Show users on ${serverLabel(s.id)} and how they map to other servers.`}
            >
              {serverLabel(s.id)}
              {count > 0 && (
                <span className="tag" style={{ marginLeft: 6, fontSize: 10 }}>
                  {count}
                </span>
              )}
            </button>
          );
        })}
      </nav>

      {activeServerId && (
        <>
          {/* ── Per-user sub-tab strip ──────────────────── */}
          <nav
            className="subnav"
            style={{
              display: 'flex',
              gap: 4,
              marginBottom: 12,
              flexWrap: 'wrap',
              paddingLeft: 12,
              borderLeft: '2px solid var(--border, rgba(255,255,255,0.08))',
            }}
          >
            <button
              className={activeUserHandle === null ? 'active' : ''}
              onClick={() => setActiveUserHandle(null)}
              title={`Show every user on ${serverLabel(activeServerId)} with their map counts. Click a user below to drill into that user's identity links.`}
            >
              Overview
            </button>
            {usersOnActiveServer.map((u) => {
              const cnt = mapCountByHandle[u.raw_name] || 0;
              const ownerBadge = u.kind === 'owner' ? ' [owner]' : '';
              const label = (u.display_name || u.raw_name) + ownerBadge;
              return (
                <button
                  key={u.plex_id}
                  className={activeUserHandle === u.raw_name ? 'active' : ''}
                  onClick={() => setActiveUserHandle(u.raw_name)}
                  title={`Show identity links for ${label} on ${serverLabel(activeServerId)}.`}
                >
                  {label}
                  {cnt > 0 && (
                    <span
                      className={`tag ${cnt > 0 ? 'good' : ''}`}
                      style={{ marginLeft: 6, fontSize: 10 }}
                    >
                      {cnt}
                    </span>
                  )}
                </button>
              );
            })}
            {loadingUsers && usersOnActiveServer.length === 0 && (
              <span style={{ fontSize: 11, color: 'var(--text-dim)', alignSelf: 'center' }}>
                Loading users…
              </span>
            )}
          </nav>

          {userLoadError[activeServerId] && (
            <div className="banner info" style={{ marginBottom: 8 }}>
              {userLoadError[activeServerId]}
            </div>
          )}

          {/* ── Body ─────────────────────────────────────── */}
          {activeUserHandle === null ? (
            <ServerOverviewPanel
              server={activeServer}
              users={usersOnActiveServer}
              maps={mapsForActiveServer}
              mapCountByHandle={mapCountByHandle}
              serverLabel={serverLabel}
              onPickUser={setActiveUserHandle}
            />
          ) : (
            <div>
              <div className="panel" style={{ marginBottom: 8 }}>
                <p className="help" style={{ margin: 0, fontSize: 12 }}>
                  Showing identity links for{' '}
                  <strong>{activeUser?.display_name || activeUserHandle}</strong>
                  {activeUser?.kind === 'owner' && (
                    <span className="tag" style={{ marginLeft: 6, fontSize: 10 }}>owner</span>
                  )}{' '}
                  on <strong>{serverLabel(activeServerId)}</strong>. The list
                  below shows every account on every other server that has
                  been declared as the same person. Use the add-form to
                  create a new link.
                </p>
              </div>
              {activeServer && (
                <UserMappingPanel
                  allServers={servers}
                  scopedTo={{
                    server: activeServer,
                    userHandle: activeUserHandle,
                    userDisplayName: activeUser?.display_name,
                  }}
                />
              )}
            </div>
          )}
        </>
      )}
    </div>
  );
}


// ── Server overview panel ─────────────────────────────────────
//
// Shown when no specific user is picked under a server tab. Lists
// every user on the server with a count of their existing maps,
// click-to-drill into a user. Also shows the raw identity-map rows
// involving the server so the operator can see the full picture
// without picking a user first.

interface OverviewProps {
  server: ServerView | undefined;
  users: ServerUser[];
  maps: UserIdentityMap[];
  mapCountByHandle: Record<string, number>;
  serverLabel: (id: string) => string;
  onPickUser: (handle: string) => void;
}

function ServerOverviewPanel({
  server, users, maps, mapCountByHandle, serverLabel, onPickUser,
}: OverviewProps) {
  if (!server) return null;
  return (
    <div>
      <div className="panel" style={{ marginBottom: 12 }}>
        <h4 style={{ marginTop: 0 }}>
          Users on {serverLabel(server.id)} ({users.length})
        </h4>
        {users.length === 0 ? (
          <div className="empty" style={{ fontSize: 12 }}>
            No users found yet. The server may not be reachable, or its
            user list hasn&apos;t been captured yet. Try{' '}
            <strong>Servers &rsaquo; Refresh users</strong>.
          </div>
        ) : (
          <table className="list" style={{ width: '100%' }}>
            <thead>
              <tr>
                <th>User</th>
                <th>Kind</th>
                <th>Existing maps</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {users.map((u) => {
                const cnt = mapCountByHandle[u.raw_name] || 0;
                return (
                  <tr key={u.plex_id}>
                    <td>
                      <strong>{u.display_name || u.raw_name}</strong>
                      {u.display_name && u.display_name !== u.raw_name && (
                        <div style={{ fontSize: 11, color: 'var(--text-dim)' }}>
                          ({u.raw_name})
                        </div>
                      )}
                    </td>
                    <td style={{ fontSize: 12 }}>
                      {u.kind === 'owner' ? (
                        <span className="tag" style={{ fontSize: 10 }}>owner</span>
                      ) : 'managed'}
                    </td>
                    <td style={{ fontSize: 12 }}>
                      {cnt > 0 ? (
                        <span className="tag good" style={{ fontSize: 10 }}>{cnt}</span>
                      ) : (
                        <span style={{ color: 'var(--text-dim)' }}>none</span>
                      )}
                    </td>
                    <td>
                      <button
                        type="button"
                        onClick={() => onPickUser(u.raw_name)}
                        style={{ fontSize: 11 }}
                        title={`Drill into ${u.display_name || u.raw_name}'s identity links.`}
                      >
                        Open
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>

      <div className="panel">
        <h4 style={{ marginTop: 0 }}>
          All maps involving {serverLabel(server.id)} ({maps.length})
        </h4>
        {maps.length === 0 ? (
          <div className="empty" style={{ fontSize: 12 }}>
            No identity links involving this server yet. Pick a user
            above and use the add-form to create one.
          </div>
        ) : (
          <table className="list" style={{ width: '100%' }}>
            <thead>
              <tr>
                <th>Side A</th>
                <th></th>
                <th>Side B</th>
                <th>Source</th>
              </tr>
            </thead>
            <tbody>
              {maps.map((m) => (
                <tr key={m.id}>
                  <td style={{ fontSize: 12 }}>
                    <strong>{m.user_a_handle || '(unresolved)'}</strong>
                    <div style={{ fontSize: 10, color: 'var(--text-dim)' }}>
                      {m.server_a_id ? serverLabel(m.server_a_id) : '(server removed)'}
                    </div>
                  </td>
                  <td style={{ fontSize: 14 }}>↔</td>
                  <td style={{ fontSize: 12 }}>
                    <strong>{m.user_b_handle || '(unresolved)'}</strong>
                    <div style={{ fontSize: 10, color: 'var(--text-dim)' }}>
                      {m.server_b_id ? serverLabel(m.server_b_id) : '(server removed)'}
                    </div>
                  </td>
                  <td style={{ fontSize: 11 }}>
                    <span
                      className={`tag ${m.source === 'manual' ? 'good' : 'phase'}`}
                      style={{ fontSize: 10 }}
                    >
                      {m.source}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
