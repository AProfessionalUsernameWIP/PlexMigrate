// Plan[RUN-JOB-UI] follow-up: standalone cross-server identity
// mapping panel. Surfaces under Servers > User Management as a
// separate section beneath the per-server user list.
//
// Two responsibilities:
//   1. List every identity-map row across all registered servers,
//      showing (server A / user A) <-> (server B / user B) with
//      provenance badge ('manual' vs 'auto_copy').
//   2. Provide a small inline form to add a new mapping: pick
//      server A, pick user A from that server's known managed users,
//      pick server B, pick user B, save.
//
// Mappings are bidirectional at the storage layer; the panel
// presents them as a simple pair without caring about insert order.
// Same-backend mappings (Plex <-> Plex with different usernames) are
// supported just like cross-backend ones.
//
// Admin-only writes; viewer+ can list.

import { useEffect, useMemo, useState } from 'react';
import {
  api, ServerView, ServerManagedUser, UserIdentityMap,
} from '../api';


export interface UserMappingPanelProps {
  /** All registered servers, used for the picker dropdowns and to
   * render server names for each map row. */
  allServers: ServerView[];
  /** Optional: when rendered inside a single-user detail view, lock
   * the panel to that user's context. Filters the list to only
   * mappings that involve this user, and auto-fills the add-form's
   * server A + user A side. The end user only picks server B + user
   * B (with user B defaulting to the same handle in case no rename
   * is wanted). */
  scopedTo?: {
    server: ServerView;
    userHandle: string;
    userDisplayName?: string;
  };
}


export function UserMappingPanel({ allServers, scopedTo }: UserMappingPanelProps) {
  const [maps, setMaps] = useState<UserIdentityMap[]>([]);
  const [loading, setLoading] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);

  // Add-form state. When scopedTo is set, server A + handle A are
  // locked to the viewed user's context. Handle B is intentionally
  // NOT auto-filled: the whole purpose of the mapping is to record
  // that the same person uses a different handle on server B, so
  // pre-filling with server A's handle would mis-suggest that names
  // match. End user types the target handle explicitly.
  const [addServerA, setAddServerA] = useState<string>(
    scopedTo?.server.id || '',
  );
  const [addHandleA, setAddHandleA] = useState<string>(
    scopedTo?.userHandle || '',
  );
  const [addServerB, setAddServerB] = useState<string>('');
  const [addHandleB, setAddHandleB] = useState<string>('');
  const [adding, setAdding] = useState<boolean>(false);
  const [addStatus, setAddStatus] = useState<string | null>(null);

  // Per-server user caches for the handle pickers. We fetch on
  // demand when the end user picks a server; cache the result so
  // they can switch back without re-fetching.
  const [userCache, setUserCache] = useState<Record<string, ServerManagedUser[]>>({});

  // Load identity maps on mount + refresh button.
  const refresh = async () => {
    setLoading(true);
    setError(null);
    try {
      const r = await api.listUserIdentityMaps();
      setMaps(r.maps);
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  };
  useEffect(() => {
    void refresh();
  }, []);

  // Re-seed the scoped add-form when the end user switches user
  // detail views. The dependency on scopedTo's server.id + userHandle
  // ensures a re-render at the boundary, not on every parent paint.
  // ``addHandleB`` is intentionally cleared on re-seed: target names
  // are end user-typed because the whole purpose of the mapping is
  // to capture that handles differ across servers.
  useEffect(() => {
    if (!scopedTo) return;
    setAddServerA(scopedTo.server.id);
    setAddHandleA(scopedTo.userHandle);
    setAddServerB('');
    setAddHandleB('');
    setAddStatus(null);
  }, [scopedTo?.server.id, scopedTo?.userHandle]);

  // Lazy-load managed users for the picked server side.
  const fetchUsersFor = async (server_id: string) => {
    if (!server_id) return;
    if (userCache[server_id]) return;
    try {
      const r = await api.listServerManagedUsers(server_id);
      setUserCache((prev) => ({ ...prev, [server_id]: r.users || [] }));
    } catch {
      setUserCache((prev) => ({ ...prev, [server_id]: [] }));
    }
  };
  useEffect(() => { void fetchUsersFor(addServerA); }, [addServerA]);
  useEffect(() => { void fetchUsersFor(addServerB); }, [addServerB]);

  const usersA = userCache[addServerA] || [];
  const usersB = userCache[addServerB] || [];

  const serverById = useMemo(() => {
    const m: Record<string, ServerView> = {};
    for (const s of allServers) m[s.id] = s;
    return m;
  }, [allServers]);

  // When scopedTo is set, narrow the displayed list to mappings that
  // involve the viewed user on EITHER side of the pair. The full
  // unfiltered set is still loaded from the API; the filter is a
  // local view operation so refreshing the cache shows updates from
  // either context without a round trip.
  const visibleMaps = useMemo(() => {
    if (!scopedTo) return maps;
    const sId = scopedTo.server.id;
    const handle = scopedTo.userHandle;
    return maps.filter((m) =>
      (m.server_a_id === sId && m.user_a_handle === handle)
      || (m.server_b_id === sId && m.user_b_handle === handle),
    );
  }, [maps, scopedTo?.server.id, scopedTo?.userHandle]);

  const canAdd =
    addServerA.length > 0
    && addHandleA.length > 0
    && addServerB.length > 0
    && addHandleB.length > 0
    && !(addServerA === addServerB && addHandleA === addHandleB)
    && !adding;

  const submitAdd = async () => {
    setAdding(true);
    setAddStatus(null);
    try {
      const r = await api.addUserIdentityMap({
        server_a_id: addServerA,
        user_a_handle: addHandleA,
        server_b_id: addServerB,
        user_b_handle: addHandleB,
        source: 'manual',
      });
      if (r.duplicate) {
        setAddStatus('That pair is already mapped. Skipped.');
      } else {
        setAddStatus(`Mapping added (id ${r.id}).`);
        // When scoped to a user, re-seed the source side so the
        // end user can immediately add another mapping for the same
        // user without re-locking it. Clear only the target side.
        if (scopedTo) {
          setAddServerA(scopedTo.server.id);
          setAddHandleA(scopedTo.userHandle);
        } else {
          setAddHandleA('');
        }
        setAddHandleB('');
        await refresh();
      }
    } catch (e) {
      setAddStatus(`Failed to add: ${String(e)}`);
    } finally {
      setAdding(false);
    }
  };

  const removeMap = async (id: number) => {
    try {
      await api.deleteUserIdentityMap(id);
      await refresh();
    } catch (e) {
      setError(`Failed to remove ${id}: ${String(e)}`);
    }
  };

  return (
    <div className="panel" style={{ padding: '12px 14px' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <h3 style={{ marginTop: 0, marginBottom: 4 }}>
          {scopedTo
            ? `Identity mappings for ${scopedTo.userDisplayName || scopedTo.userHandle}`
            : 'User identity mappings'}
        </h3>
        <div style={{ display: 'flex', gap: 6 }}>
          {/* USER-MGMT-IDENTITY-AUDIT follow-on: explicit end user
              trigger for the backend_user_id auto-link helper. The
              helper also fires automatically after every
              managed-users sync; this button is for "I just added a
              manual mapping" or "I just registered a new server and
              want immediate cross-server detection" moments. */}
          <button
            type="button"
            onClick={async () => {
              try {
                const r = await api.rerunAutoLinkIdentityMap();
                const msg = (r.pairs_written > 0)
                  ? `Auto-link added ${r.pairs_written} new pair${r.pairs_written === 1 ? '' : 's'}.`
                  : (r.pairs_skipped_duplicate > 0)
                    ? `Auto-link found ${r.pairs_skipped_duplicate} pair${r.pairs_skipped_duplicate === 1 ? '' : 's'} already mapped.`
                    : 'No cross-server duplicates detected.';
                // eslint-disable-next-line no-alert
                window.alert(msg);
                refresh();
              } catch (e) {
                // eslint-disable-next-line no-alert
                window.alert(`Auto-link failed: ${String(e)}`);
              }
            }}
            title="Re-run the backend_user_id auto-link helper. Normally fires on every sync; click to force a rerun now."
            style={{ fontSize: 11, padding: '3px 10px' }}
          >
            Rerun auto-link
          </button>
          <button
            type="button"
            onClick={refresh}
            disabled={loading}
            style={{ fontSize: 11, padding: '3px 10px' }}
          >
            {loading ? 'Loading...' : 'Refresh'}
          </button>
        </div>
      </div>
      {scopedTo ? (
        <p style={{ fontSize: 12, color: 'var(--text-dim)', marginTop: 0, marginBottom: 10 }}>
          Showing mappings that involve <strong>{scopedTo.userHandle}</strong>{' '}
          on <strong>{scopedTo.server.name}</strong>. The add-form's
          side A is locked to this user; pick a target server and
          type the target user's handle (mappings exist precisely
          because handles can differ across servers).
        </p>
      ) : (
        <p style={{ fontSize: 12, color: 'var(--text-dim)', marginTop: 0, marginBottom: 10 }}>
          Link the same person across servers when their usernames differ.
          Example: Plex user "nlovlyn" and Emby user "Aries" are the same person.
          These mappings inform the engine's cross-server user filter
          and the per-user data transfer path. Same-backend mappings
          (Plex {`<->`} Plex) are also supported.
        </p>
      )}

      {error && (
        <div style={{
          padding: '6px 10px',
          background: 'rgba(239, 68, 68, 0.10)',
          border: '1px solid var(--danger, #ef4444)',
          color: 'var(--danger, #ef4444)',
          borderRadius: 4,
          fontSize: 12,
          marginBottom: 10,
        }}>
          {error}
        </div>
      )}

      {/* Existing mappings table */}
      {visibleMaps.length === 0 ? (
        <div style={{ fontSize: 12, color: 'var(--text-dim)', padding: '8px 0' }}>
          {scopedTo ? 'No mappings yet for this user.' : 'No mappings yet.'}
        </div>
      ) : (
        <table className="list" style={{ width: '100%', fontSize: 12 }}>
          <thead>
            <tr>
              <th style={{ textAlign: 'left' }}>Server A</th>
              <th style={{ textAlign: 'left' }}>User A</th>
              <th style={{ textAlign: 'center', width: 40 }}>{'<->'}</th>
              <th style={{ textAlign: 'left' }}>Server B</th>
              <th style={{ textAlign: 'left' }}>User B</th>
              <th style={{ textAlign: 'left' }}>Source</th>
              <th style={{ textAlign: 'right' }}>Actions</th>
            </tr>
          </thead>
          <tbody>
            {visibleMaps.map((m) => (
              <tr key={m.id}>
                <td>{(m.server_a_id && serverById[m.server_a_id]?.name) || m.server_a_id || '(unresolved)'}</td>
                <td>{m.user_a_handle || <em style={{ color: 'var(--text-dim)' }}>(unresolved)</em>}</td>
                <td style={{ textAlign: 'center', color: 'var(--text-dim)' }}>↔</td>
                <td>{(m.server_b_id && serverById[m.server_b_id]?.name) || m.server_b_id || '(unresolved)'}</td>
                <td>{m.user_b_handle || <em style={{ color: 'var(--text-dim)' }}>(unresolved)</em>}</td>
                <td>
                  <span style={{
                    padding: '1px 6px',
                    borderRadius: 3,
                    fontSize: 10,
                    background: m.source === 'auto_copy'
                      ? 'rgba(74, 122, 252, 0.15)'
                      : 'rgba(255, 255, 255, 0.06)',
                    color: m.source === 'auto_copy'
                      ? 'var(--accent, #4a7afc)'
                      : 'var(--text-dim)',
                  }}>
                    {m.source}
                  </span>
                </td>
                <td style={{ textAlign: 'right' }}>
                  <button
                    type="button"
                    onClick={() => removeMap(m.id)}
                    style={{ fontSize: 10, padding: '2px 8px' }}
                    title="Remove this mapping"
                  >
                    Remove
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {/* Add-form */}
      <div style={{
        marginTop: 16,
        padding: '10px 12px',
        background: 'rgba(74, 122, 252, 0.04)',
        border: '1px solid var(--border, #444)',
        borderRadius: 6,
      }}>
        <div style={{ fontWeight: 600, marginBottom: 8, fontSize: 13 }}>
          {scopedTo ? 'Map this user to another server' : 'Add a new mapping'}
        </div>
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12, alignItems: 'end' }}>
          <div>
            <div style={{ fontSize: 11, color: 'var(--text-dim)', marginBottom: 2 }}>
              Server A {scopedTo && <span style={{ color: 'var(--accent, #4a7afc)' }}>(locked)</span>}
            </div>
            <select
              value={addServerA}
              onChange={(e) => { setAddServerA(e.target.value); setAddHandleA(''); }}
              disabled={!!scopedTo}
              style={{ width: '100%', fontSize: 12 }}
            >
              <option value="">-- pick a server --</option>
              {allServers.map((s) => (
                <option key={s.id} value={s.id}>{s.name} ({s.service_type})</option>
              ))}
            </select>
            <div style={{ marginTop: 6 }}>
              <div style={{ fontSize: 11, color: 'var(--text-dim)', marginBottom: 2 }}>
                User on A {scopedTo && <span style={{ color: 'var(--accent, #4a7afc)' }}>(locked)</span>}
              </div>
              {scopedTo ? (
                <input
                  type="text"
                  value={addHandleA}
                  readOnly
                  style={{
                    width: '100%',
                    fontSize: 12,
                    background: 'rgba(74,122,252,0.06)',
                    color: 'var(--text-dim)',
                  }}
                />
              ) : usersA.length > 0 ? (
                <select
                  value={addHandleA}
                  onChange={(e) => setAddHandleA(e.target.value)}
                  style={{ width: '100%', fontSize: 12 }}
                >
                  <option value="">-- pick a user --</option>
                  {usersA.map((u) => (
                    <option key={u.username} value={u.username}>
                      {u.display_name || u.username}
                      {u.display_name ? ` (${u.username})` : ''}
                    </option>
                  ))}
                </select>
              ) : (
                <input
                  type="text"
                  value={addHandleA}
                  onChange={(e) => setAddHandleA(e.target.value)}
                  placeholder="username (free-text)"
                  style={{ width: '100%', fontSize: 12 }}
                />
              )}
            </div>
          </div>
          <div>
            <div style={{ fontSize: 11, color: 'var(--text-dim)', marginBottom: 2 }}>
              Server B
            </div>
            <select
              value={addServerB}
              onChange={(e) => { setAddServerB(e.target.value); setAddHandleB(''); }}
              style={{ width: '100%', fontSize: 12 }}
            >
              <option value="">-- pick a server --</option>
              {allServers.map((s) => (
                <option key={s.id} value={s.id}>{s.name} ({s.service_type})</option>
              ))}
            </select>
            <div style={{ marginTop: 6 }}>
              <div style={{ fontSize: 11, color: 'var(--text-dim)', marginBottom: 2 }}>
                User on B
              </div>
              {usersB.length > 0 ? (
                <select
                  value={addHandleB}
                  onChange={(e) => setAddHandleB(e.target.value)}
                  style={{ width: '100%', fontSize: 12 }}
                >
                  <option value="">-- pick a user --</option>
                  {usersB.map((u) => (
                    <option key={u.username} value={u.username}>
                      {u.display_name || u.username}
                      {u.display_name ? ` (${u.username})` : ''}
                    </option>
                  ))}
                </select>
              ) : (
                <input
                  type="text"
                  value={addHandleB}
                  onChange={(e) => setAddHandleB(e.target.value)}
                  placeholder="username (free-text)"
                  style={{ width: '100%', fontSize: 12 }}
                />
              )}
            </div>
          </div>
        </div>
        <div style={{ marginTop: 10, display: 'flex', gap: 8, alignItems: 'center', justifyContent: 'flex-end' }}>
          {addStatus && (
            <span style={{ fontSize: 11, color: 'var(--text-dim)', flex: 1 }}>
              {addStatus}
            </span>
          )}
          <button
            type="button"
            className="primary"
            disabled={!canAdd}
            onClick={submitAdd}
            style={{ fontSize: 12, padding: '5px 12px' }}
          >
            {adding ? 'Adding...' : 'Add mapping'}
          </button>
        </div>
      </div>
    </div>
  );
}
