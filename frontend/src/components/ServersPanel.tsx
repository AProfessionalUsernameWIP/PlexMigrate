// Server management tab.
//
// v0.9.1 update - server list and Add Server form only:
//   * Server list row: friendly name, URL, live status dot, ping ms,
//     library count, last-contacted timestamp, manual refresh button,
//     remove button with a confirmation prompt that reminds the user
//     that removal does not delete any snapshot files or logs.
//   * Add Server form: Test Connection button that probes reachability
//     and displays the response time before saving; the Save button is
//     disabled until a successful test has been completed.
//   * Background poll: every 30 s the panel pings every registered
//     server (lightweight, hits Plex's /identity) and updates the
//     status dot + response-time chip in place.
//
// Nothing else on the page is touched - the threading-note banner
// above and the cached library catalogue panel below stay as-is.

import { useEffect, useRef, useState } from 'react';
import { api, LibraryDescriptor, PingResult, ProbeUnsavedResult, ServerUser, ServerUsersResponse, ServerDeleteSummary, ServerView } from '../api';
import type { ServerManagedUser } from '../api';
import { usePermission } from '../hooks/usePermission';

// Default poll cadence for the live status indicator, in milliseconds.
// The actual cadence is loaded from settings.tunables.frontend_server_ping_interval_ms
// at mount time and applied to the polling interval; saves to the
// tunable take effect on the next ServersPanel mount.
const PING_INTERVAL_MS_DEFAULT = 30_000;

export function ServersPanel() {
  // PR-A4 - viewers / operators / managers see this panel read-only.
  // Add / Edit / Remove buttons are hidden when ``servers.edit`` is
  // absent. The backend (require_role('root_admin') on the CRUD
  // routes) is the authoritative gate.
  const canEditServers = usePermission('servers.edit');

  const [servers, setServers] = useState<ServerView[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState<ServerView | 'new' | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  // Last cascade summary from a successful delete. Rendered as a
  // green toast banner at the top of the panel and dismissed by the
  // operator (or by the next action).
  const [cascadeToast, setCascadeToast] = useState<ServerDeleteSummary | null>(null);

  // Live ping state, keyed by server id. We keep this *separate* from
  // ``servers`` so a ping update can happen without re-rendering the
  // whole list row (which would briefly flash if we patched the row
  // dict directly). Each value is the most recent PingResult or
  // ``undefined`` if no ping has come back yet for that id.
  const [pings, setPings] = useState<Record<string, PingResult>>({});

  // v0.9.6 Feature 3: per-server user lists fetched on tab visit.
  // Keyed by server id. ``undefined`` = not yet fetched / refetching;
  // resolved values may carry a non-null ``error`` when systemAccounts
  // failed but the owner row is still there. The ``error`` shape with
  // a single ``message`` field flags total fetch failures (connect
  // refused / 502) so the panel can render a recoverable inline error.
  const [users, setUsers] = useState<Record<string, ServerUsersResponse | { error: string }>>({});

  // PR-11 - self-firing sync. When the Servers page mounts (or the
  // server list changes), we diff the live user list against what's
  // in the local managed_users DB. New users surface as dismissible
  // toasts at the top of the panel and a background sync runs to
  // bring the DB up to date so the JobFormPanel picker reflects the
  // current state on the next visit.
  const [syncToasts, setSyncToasts] = useState<
    { id: string; server_name: string; added: string[] }[]
  >([]);
  // PR-11.1 - in-flight sync guard. A ref-based Set lets the effect
  // re-run on every ``servers`` change without stacking duplicate
  // sync calls for the same server (which would hammer Plex with
  // parallel ``systemAccounts()`` calls). Cleared when the sync
  // promise settles (success or failure).
  const inFlightSyncsRef = useRef<Set<string>>(new Set());

  // Track the polling timer so we can clear it on unmount.
  const pollTimerRef = useRef<number | null>(null);

  // Live ping cadence. Loaded once from settings on mount so an operator
  // who bumps it via Settings ▸ Tunables doesn't need a code change.
  // A panel re-mount picks up subsequent changes; the effect that arms
  // the timer below depends on this value, so a new cadence rearms
  // automatically once the load completes.
  const [pingIntervalMs, setPingIntervalMs] = useState<number>(PING_INTERVAL_MS_DEFAULT);
  useEffect(() => {
    api.getSettings()
      .then((s) => {
        const raw = (s as unknown as Record<string, unknown>).tunables;
        const tunables = raw && typeof raw === 'object' ? (raw as Record<string, unknown>) : {};
        const v = tunables['frontend_server_ping_interval_ms'];
        if (typeof v === 'number' && v >= 1000) {
          setPingIntervalMs(v);
        }
      })
      .catch(() => { /* non-fatal — fall back to the default */ });
  }, []);

  const refresh = async () => {
    try {
      setServers(await api.listServers());
    } catch (e) {
      setError(String(e));
    }
  };

  // Initial load + start the background ping poll.
  useEffect(() => {
    refresh();
    return () => {
      if (pollTimerRef.current !== null) {
        window.clearInterval(pollTimerRef.current);
        pollTimerRef.current = null;
      }
    };
  }, []);

  // Once the server list is loaded, kick off the ping poll. We
  // re-arm whenever the list of servers changes (e.g. after Add or
  // Remove) so newly-added rows get pinged immediately.
  useEffect(() => {
    if (servers.length === 0) {
      if (pollTimerRef.current !== null) {
        window.clearInterval(pollTimerRef.current);
        pollTimerRef.current = null;
      }
      return;
    }

    const pingAll = async () => {
      // Ping every server in parallel; each call is independent and
      // the registry row update is the same regardless of order.
      const tasks = servers.map(async (s) => {
        try {
          const result = await api.pingServer(s.id);
          setPings((prev) => ({ ...prev, [s.id]: result }));
        } catch {
          // Network or server-side error - fall through; the next
          // tick will retry. The cached status row still shows
          // whatever the last successful ping recorded.
        }
      });
      await Promise.allSettled(tasks);
    };

    // Fire one ping right now so the dots aren't grey on first paint
    // for the 30 seconds until the first interval fires.
    pingAll();
    if (pollTimerRef.current !== null) window.clearInterval(pollTimerRef.current);
    pollTimerRef.current = window.setInterval(pingAll, pingIntervalMs);

    return () => {
      if (pollTimerRef.current !== null) {
        window.clearInterval(pollTimerRef.current);
        pollTimerRef.current = null;
      }
    };
    // Re-arm when the server list changes (the closure binds the
    // current list) or when the operator changes the ping cadence
    // via Settings ▸ Tunables.
  }, [servers, pingIntervalMs]);

  // v0.9.6 Feature 3: fetch per-server user lists in parallel whenever
  // the server list changes. No caching - the user list on a Plex
  // server can change at any time (add/remove managed users), so a
  // stale cache would mislead. One round-trip per registered server
  // per Servers tab visit is acceptable; if performance becomes a
  // concern with many servers, add a short TTL later.
  useEffect(() => {
    if (servers.length === 0) {
      setUsers({});
      return;
    }
    let cancelled = false;
    const tasks = servers.map(async (s) => {
      // Run live + DB calls in parallel so the panel paints quickly
      // and the diff check below has both data sources. The DB call
      // passes ``include_hidden=true`` so tombstoned rows count
      // toward "already known" - otherwise the diff would re-flag a
      // hidden user as "newly detected" on every Servers-page visit.
      const [liveRes, dbRes] = await Promise.allSettled([
        api.listServerUsers(s.id),
        api.listServerManagedUsers(s.id, true),
      ]);
      if (cancelled) return;
      // Live user list - same surface the panel renders and the
      // JobFormPanel used to read on its own before PR-11.
      if (liveRes.status === 'fulfilled') {
        setUsers((prev) => ({ ...prev, [s.id]: liveRes.value }));
      } else {
        setUsers((prev) => ({ ...prev, [s.id]: { error: String(liveRes.reason) } }));
      }
      // PR-11 diff. If the live API surfaced users that aren't in
      // the local managed_users DB (visible OR hidden), fire a
      // background sync and queue a toast for the operator. Only
      // "additions" trigger this - removals never auto-fire deletes
      // (additive-only rule from clp.md). The DB call failing is
      // non-fatal; we just skip the diff and let the next visit retry.
      if (
        liveRes.status === 'fulfilled' &&
        dbRes.status === 'fulfilled'
      ) {
        const liveUsers = liveRes.value.users || [];
        const dbUsers: ServerManagedUser[] = dbRes.value.users || [];
        const dbNames = new Set(dbUsers.map((u) => u.username));
        const newlyDetected = liveUsers
          .map((u) => u.raw_name)
          .filter((n) => !!n && !dbNames.has(n));
        if (newlyDetected.length > 0) {
          // PR-11.1 concurrency guard - skip if a sync for this
          // server is already in flight. The diff effect re-runs
          // whenever ``servers`` changes (test, refresh, edit) and
          // without this guard each re-run would stack another
          // background ``systemAccounts()`` call against Plex.
          if (!inFlightSyncsRef.current.has(s.id)) {
            inFlightSyncsRef.current.add(s.id);
            api.syncServerManagedUsers(s.id)
              .catch(() => {
                /* operator can hit the manual sync button if this fails */
              })
              .finally(() => {
                inFlightSyncsRef.current.delete(s.id);
              });
          }
          setSyncToasts((prev) => {
            // De-dup: don't push another toast for a server already
            // queued with the same additions (effect may re-fire).
            const existing = prev.find(
              (t) => t.id === s.id &&
                     t.added.length === newlyDetected.length &&
                     t.added.every((x, i) => x === newlyDetected[i]),
            );
            if (existing) return prev;
            return [
              ...prev.filter((t) => t.id !== s.id),
              { id: s.id, server_name: s.name, added: newlyDetected },
            ];
          });
        }
      }
    });
    Promise.allSettled(tasks);
    return () => { cancelled = true; };
  }, [servers]);

  // Replace one row in the cached server list with the response the
  // PATCH endpoint returned. Cheaper than refresh() because it skips
  // the full list reload + re-ping cycle.
  const applyServerPatch = (patched: ServerView) => {
    setServers((prev) => prev.map((s) => (s.id === patched.id ? patched : s)));
  };

  const test = async (id: string) => {
    setBusyId(id);
    try {
      await api.testServer(id);
      await refresh();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusyId(null);
    }
  };

  const remove = async (s: ServerView) => {
    // v0.9.5: fetch the cascade preview first so the confirmation
    // dialog can show concrete counts. If the preview call itself
    // fails (e.g. server is mid-cascade by another request), fall
    // back to the previous text-only prompt so the operator can
    // still cancel the action safely.
    let previewLine = '';
    try {
      const preview = await api.previewServerCascade(s.id);
      const parts = [
        `${preview.schedules} schedule(s)`,
        `${preview.snapshots} snapshot file(s)`,
        `${preview.log_dirs} log directory/ies`,
      ];
      previewLine = `Cascade will also delete:\n  • ${parts.join('\n  • ')}\n\n`;
    } catch {
      previewLine = 'Cascade will also delete every schedule, snapshot, and log directory attributable to this server.\n\n';
    }
    if (!confirm(
      `Delete server "${s.name}" and everything attributable to it?\n\n` +
      previewLine +
      'This cannot be undone.'
    )) return;
    setCascadeToast(null);
    try {
      const summary = await api.deleteServer(s.id);
      setCascadeToast(summary);
      await refresh();
    } catch (e) {
      setError(String(e));
    }
  };

  return (
    <>
      {error && <div className="banner error">{error}</div>}

      {cascadeToast && (
        <div
          className="banner good"
          style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', gap: 12 }}
        >
          <div>
            <strong>Deleted {cascadeToast.name}.</strong>{' '}
            Cleaned up {cascadeToast.schedules} schedule(s), {cascadeToast.snapshots} snapshot file(s),
            and {cascadeToast.log_dirs} log directory/ies.
            {(cascadeToast.exports_failed > 0 || cascadeToast.log_dirs_failed > 0) && (
              <div style={{ marginTop: 6, fontSize: 12 }}>
                {cascadeToast.exports_failed + cascadeToast.log_dirs_failed} item(s) could not be removed:
                <ul style={{ margin: '4px 0 0 18px' }}>
                  {cascadeToast.errors.map((e, i) => <li key={i} className="mono">{e}</li>)}
                </ul>
              </div>
            )}
          </div>
          <button onClick={() => setCascadeToast(null)} style={{ flexShrink: 0 }}>Dismiss</button>
        </div>
      )}

      {/* PR-11 - new-user detection toasts. One per server, queued
          by the diff effect above. Background sync has already fired
          by the time this renders; the toast is purely informational. */}
      {syncToasts.map((t) => (
        <div
          key={t.id}
          className="banner good"
          style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', gap: 12 }}
        >
          <div>
            <strong>New user{t.added.length === 1 ? '' : 's'} detected on {t.server_name}</strong>{' '}
            - user list updated. Added:{' '}
            <span className="mono">{t.added.join(', ')}</span>
          </div>
          <button
            onClick={() => setSyncToasts((prev) => prev.filter((x) => x.id !== t.id))}
            style={{ flexShrink: 0 }}
          >
            Dismiss
          </button>
        </div>
      ))}

      <div className="banner info">
        <strong>Threading note:</strong> Every registered server adds API load when used.
        Running transfers to or from multiple servers at the same time multiplies that load
        across all involved servers. Schedule recurring exports at staggered times rather
        than the same minute, and see the README's <em>Understanding Performance and Threading</em>
        section for guidance on worker counts.
      </div>

      <div className="panel">
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12 }}>
          <h2 style={{ margin: 0 }}>Registered Plex Servers</h2>
          {canEditServers && (
            <button className="primary" onClick={() => setEditing('new')}>+ Add Server</button>
          )}
        </div>

        {servers.length === 0 ? (
          <div className="empty">
            No servers registered. Click <strong>+ Add Server</strong> to register your first Plex.
          </div>
        ) : (
          <table className="list">
            <thead>
              <tr>
                <th style={{ width: 24 }}></th>
                <th>Name</th>
                {/* Bugfix: URL column hidden from viewer / operator.
                    Same rationale as the View-button removal: lower
                    roles can SEE the Servers tab so they know which
                    servers exist (operationally useful for job
                    setup), but the Plex URL itself is a connection
                    detail that should be admin-only. */}
                {canEditServers && <th>URL</th>}
                <th style={{ textAlign: 'right' }}>Ping</th>
                <th style={{ textAlign: 'right' }}>Libraries</th>
                <th>Last contact</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {servers.map((s) => {
                const ping = pings[s.id];
                // The live ping result, when we have one, supersedes
                // the cached status on the registry row - it's
                // strictly more recent.
                const effectiveStatus = ping?.status ?? s.last_status;
                const effectiveMs = ping?.response_ms ?? s.last_response_ms ?? null;
                return (
                  <tr key={s.id}>
                    <td><StatusDot status={effectiveStatus} detail={ping?.detail ?? s.last_status_detail} /></td>
                    <td><strong>{s.name}</strong></td>
                    {canEditServers && <td className="mono">{s.url}</td>}
                    <td className="num">
                      {effectiveMs !== null && effectiveStatus === 'ok'
                        ? `${effectiveMs.toFixed(0)} ms`
                        : effectiveStatus === 'unreachable'
                          ? '-'
                          : effectiveStatus === 'auth_error'
                            ? 'auth'
                            : '?'}
                    </td>
                    <td className="num">{s.last_libraries.length || '-'}</td>
                    <td>{s.last_checked_at ? new Date(s.last_checked_at * 1000).toLocaleString() : '-'}</td>
                    <td>
                      <div className="row-buttons">
                        <button onClick={() => test(s.id)} disabled={busyId === s.id} title="Refresh status and re-enumerate libraries">
                          {busyId === s.id ? 'Refreshing…' : 'Refresh'}
                        </button>
                        <WalkButton serverId={s.id} />
                        {canEditServers && (
                          <>
                            <button onClick={() => setEditing(s)}>Edit</button>
                            <button className="danger" onClick={() => remove(s)}>Remove</button>
                          </>
                        )}
                        {/* Bugfix: pre-PR-13 there was a ``View`` button
                            rendered for viewer / operator that opened
                            the same ``ServerEditor`` used for Edit. The
                            editor's input fields aren't read-only, so a
                            lower-privilege role could see (and try to
                            type into) the server URL + token. The
                            backend CRUD routes reject the write, but the
                            UI shouldn't expose those fields at all.
                            Connection settings are now strictly
                            admin / root_admin. */}
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>

      {editing && (
        <ServerEditor
          server={editing === 'new' ? null : editing}
          existingServers={servers}
          onClose={async (changed) => {
            setEditing(null);
            if (changed) await refresh();
          }}
        />
      )}

      {servers.length > 0 && (
        <LibraryCataloguesPanel servers={servers} />
      )}

      {servers.length > 0 && (
        <ServerUsersPanel
          servers={servers}
          users={users}
          onPatched={applyServerPatch}
        />
      )}
    </>
  );
}

// ── Sub-component: per-server user list ──────────────────────────────────────

function UsersForServer({
  server,
  payload,
  onPatched,
}: {
  server: ServerView;
  payload?: ServerUsersResponse | { error: string };
  onPatched: (next: ServerView) => void;
}) {
  // The owner row's "edit display name" input is local UI state.
  // Stored separately so each server's editor opens / closes
  // independently.
  const [editingOwner, setEditingOwner] = useState(false);
  const [draft, setDraft] = useState('');
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  if (!payload) {
    return (
      <div className="col" style={{ minWidth: 280 }}>
        <h3 style={{ fontSize: 13, margin: '0 0 6px' }}>{server.name}</h3>
        <div className="empty" style={{ fontSize: 12 }}>Loading users…</div>
      </div>
    );
  }
  if ('error' in payload && !('users' in payload)) {
    // Total fetch failure (e.g. 502 - server unreachable).
    return (
      <div className="col" style={{ minWidth: 280 }}>
        <h3 style={{ fontSize: 13, margin: '0 0 6px' }}>{server.name}</h3>
        <div className="banner error" style={{ fontSize: 12 }}>{payload.error}</div>
      </div>
    );
  }

  const resp = payload as ServerUsersResponse;
  const owner = resp.users.find((u) => u.kind === 'owner') || null;
  const managed = resp.users.filter((u) => u.kind === 'managed');

  const startOwnerEdit = () => {
    if (!owner) return;
    setDraft(owner.display_name || '');
    setEditingOwner(true);
    setSaveError(null);
  };

  const cancelOwnerEdit = () => {
    setEditingOwner(false);
    setDraft('');
    setSaveError(null);
  };

  const commitOwnerEdit = async () => {
    if (!owner) return;
    setSaving(true);
    setSaveError(null);
    try {
      const updated = await api.setUserDisplayName(server.id, owner.plex_id, draft);
      onPatched(updated);
      setEditingOwner(false);
    } catch (e) {
      setSaveError(String(e));
    } finally {
      setSaving(false);
    }
  };

  const onOwnerKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      void commitOwnerEdit();
    } else if (e.key === 'Escape') {
      e.preventDefault();
      cancelOwnerEdit();
    }
  };

  return (
    <div className="col" style={{ minWidth: 280 }}>
      <h3 style={{ fontSize: 13, margin: '0 0 6px' }}>{server.name}</h3>
      {/* Owner row with inline-editable display name. */}
      {owner ? (
        <div style={{ marginBottom: 10, padding: '6px 8px', background: 'var(--panel-alt, #1b2233)', borderRadius: 4 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 12 }}>
            <span className="tag started" style={{ fontSize: 10 }}>Owner</span>
            <span className="mono" style={{ color: 'var(--text-dim)', wordBreak: 'break-all' }}>
              {owner.raw_name}
            </span>
          </div>
          <div style={{ marginTop: 4 }}>
            {editingOwner ? (
              <div style={{ display: 'flex', gap: 6 }}>
                <input
                  type="text"
                  value={draft}
                  onChange={(e) => setDraft(e.target.value)}
                  onKeyDown={onOwnerKeyDown}
                  autoFocus
                  placeholder="Display name (Enter to save, Esc to cancel)"
                  style={{ flex: 1 }}
                  disabled={saving}
                />
                <button onClick={commitOwnerEdit} disabled={saving} className="primary" style={{ padding: '2px 8px' }}>
                  {saving ? '…' : 'Save'}
                </button>
                <button onClick={cancelOwnerEdit} disabled={saving} style={{ padding: '2px 8px' }}>
                  Cancel
                </button>
              </div>
            ) : (
              <div
                style={{ cursor: 'pointer', fontSize: 13 }}
                title="Click to edit display name"
                onClick={startOwnerEdit}
              >
                {owner.display_name ? (
                  <strong>{owner.display_name}</strong>
                ) : (
                  <span style={{ color: 'var(--text-dim)', fontStyle: 'italic' }}>Click to set a display name…</span>
                )}
              </div>
            )}
            {saveError && (
              <div className="banner error" style={{ fontSize: 12, marginTop: 4 }}>{saveError}</div>
            )}
          </div>
        </div>
      ) : (
        <div className="empty" style={{ fontSize: 12 }}>Owner unavailable - check the server's token.</div>
      )}

      {/* Managed users - read-only in this version. */}
      <div style={{ fontSize: 12, color: 'var(--text-dim)', marginBottom: 4 }}>Managed users</div>
      {managed.length === 0 ? (
        <div className="empty" style={{ fontSize: 12 }} title={resp.error ?? ''}>
          {resp.error ? 'No managed users found.' : 'No managed users on this server.'}
        </div>
      ) : (
        <ul style={{ paddingLeft: 18, margin: 0 }}>
          {managed.map((u) => (
            <li key={u.plex_id} style={{ fontSize: 13 }}>
              {u.display_name ? (
                <>
                  <strong>{u.display_name}</strong>{' '}
                  <span style={{ color: 'var(--text-dim)' }}>({u.raw_name})</span>
                </>
              ) : (
                <span>{u.raw_name}</span>
              )}
              <span className="tag phase" style={{ fontSize: 10, marginLeft: 6 }}>Managed</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

// ── Sub-component: status dot ────────────────────────────────────────────────

function StatusDot({ status, detail }: { status: ServerView['last_status']; detail?: string }) {
  const cls = status === 'ok' ? 'green'
    : status === 'unreachable' || status === 'auth_error' ? 'red'
    : 'amber';
  const title = detail || status;
  return <span className={`dot ${cls}`} title={title} />;
}

// ── Sub-component: add / edit form ───────────────────────────────────────────

function ServerEditor(props: {
  server: ServerView | null;
  existingServers: ServerView[];
  onClose: (changed: boolean) => void;
}) {
  const { server, existingServers, onClose } = props;
  const [name, setName] = useState(server?.name ?? '');
  const [url, setUrl] = useState(server?.url ?? 'http://host.docker.internal:32400');
  const [token, setToken] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // PR-2 / Phase C (auth refactor - duplicate-token soft warning).
  // After a successful Test Connection the probe response includes the
  // ``owner_name`` of the Plex account the supplied token belongs to.
  // If that owner already appears under one or more other registered
  // servers, the operator is reusing the same Plex.tv account's token
  // across servers - valid, but worth surfacing because the servers
  // will share authentication context (revoking one revokes all).
  //
  // We can't compare raw tokens client-side because the API never
  // returns them, but ``owner_name`` is exposed on every ServerView
  // and is a reliable proxy for "this token belongs to that account."
  // The warning is soft / informational; the operator can save anyway.

  // ── Test Connection state ────────────────────────────────────────
  // v0.10.0: the test now uses /api/servers/test-unsaved which probes
  // URL+token without touching the registry. The previous "create a
  // row, ping it, delete on failure (keep on success)" dance left a
  // half-registered server behind whenever the test passed - that's
  // the "Test Connection adds it to the list" bug.
  //
  // The probe response carries the connected server's friendly name
  // and machine identifier; the UI surfaces them so the operator can
  // confirm they hit the right Plex install before saving (a Plex
  // account's token works on every server it owns, so a successful
  // connect does not by itself prove which server you reached).
  const [testing, setTesting] = useState(false);
  const [probe, setProbe] = useState<ProbeUnsavedResult | null>(null);

  // Reset the probe result whenever the user changes a connection
  // field - the previous "passed" result no longer applies.
  useEffect(() => {
    setProbe(null);
  }, [name, url, token]);

  const testConnection = async () => {
    setError(null);
    setProbe(null);
    setTesting(true);
    try {
      // Editing path: an empty token in the form means "use the saved
      // one." There's no way to send the saved token from the browser
      // (we never expose it), so for edits without a fresh token the
      // probe runs against just URL - Plex will 401 and the UI shows
      // an actionable message. The cheaper path for edits is the
      // existing /test endpoint, which uses the saved token directly.
      if (server && !token.trim()) {
        // Refresh the existing row's probe instead of running unsaved.
        await api.refreshServer(server.id);
        setProbe({
          ok: true,
          status: 'ok',
          detail: 'Refreshed using saved token.',
          friendly_name: server.friendly_name ?? '',
          machine_identifier: server.machine_identifier ?? '',
          owner_name: server.owner_name ?? '',
          libraries: server.last_libraries ?? [],
          response_ms: server.last_response_ms ?? null,
          name_mismatch: false,
          duplicate_of: null,
        });
        return;
      }
      const result = await api.testUnsavedServer({ name, url, token });
      setProbe(result);
    } catch (e) {
      setError(String(e));
    } finally {
      setTesting(false);
    }
  };

  const submit = async () => {
    setError(null);
    setSubmitting(true);
    try {
      if (server) {
        await api.updateServer(server.id, { name, url, token });
      } else {
        // v0.10.0: create_server on the backend re-probes and rejects
        // duplicate machine_identifiers, so passing a probed-OK row
        // through here is safe. The frontend pre-test in testConnection
        // is for fast UX feedback; the backend still enforces.
        await api.createServer({ name, url, token });
      }
      onClose(true);
    } catch (e) {
      setError(String(e));
    } finally {
      setSubmitting(false);
    }
  };

  // For *new* servers, Save is disabled until a successful test AND
  // the duplicate-machine-identifier check passes. For *editing*,
  // Save is enabled whenever the form has required fields filled.
  const canSave = server
    ? !!(name.trim() && url.trim())
    : !!(
        name.trim() && url.trim() && token.trim()
        && probe?.ok
        && !probe.duplicate_of
      );

  return (
    <div className="panel">
      <h2>{server ? `Edit Server: ${server.name}` : 'Add Server'}</h2>
      {error && <div className="banner error">{error}</div>}
      {probe && !probe.ok && (
        <div className="banner error">
          Connection failed: {probe.detail || probe.status}
        </div>
      )}
      {probe && probe.ok && (
        <div className="banner good">
          <strong>Connected.</strong>{' '}
          {probe.response_ms !== null && `${probe.response_ms.toFixed(0)} ms · `}
          {probe.libraries.length} {probe.libraries.length === 1 ? 'library' : 'libraries'}
          {probe.owner_name && <> · owner <code>{probe.owner_name}</code></>}
          <div style={{ marginTop: 6, fontSize: 12 }}>
            Server reports itself as <strong>{probe.friendly_name || '(no name)'}</strong>
            {probe.machine_identifier && (
              <> · machine ID <code style={{ fontSize: 11 }}>{probe.machine_identifier}</code></>
            )}
          </div>
        </div>
      )}
      {probe?.ok && probe.name_mismatch && (
        <div className="banner warn">
          <strong>Friendly-name mismatch.</strong>{' '}
          You entered <strong>{name}</strong> but the server identifies as{' '}
          <strong>{probe.friendly_name}</strong>. A Plex account's token works on every
          server that account owns, so a successful connection doesn't by itself prove
          which server you reached. Double-check you typed the right URL for the server
          you intended.
        </div>
      )}
      {probe?.ok && probe.duplicate_of && (
        <div className="banner error">
          <strong>Already registered.</strong>{' '}
          This Plex install (machine ID <code>{probe.machine_identifier}</code>) is
          already on file as <strong>{probe.duplicate_of}</strong>. Each physical Plex
          server may only be registered once - edit the existing row instead, or remove
          it before adding under a new name.
        </div>
      )}
      {probe?.ok && !probe.duplicate_of && probe.owner_name && (() => {
        // PR-2 / Phase C: soft duplicate-token warning. Surface every
        // *other* registered server whose owner matches the one this
        // probe authenticated as - strong signal the same Plex.tv
        // account (and likely the same token) is being reused.
        const otherSiblings = existingServers.filter((s) =>
          s.id !== (server?.id ?? '') &&
          (s.owner_name ?? '').toLowerCase().trim() === probe.owner_name!.toLowerCase().trim()
        );
        if (otherSiblings.length === 0) return null;
        const labels = otherSiblings.map((s) => s.name).join(', ');
        return (
          <div className="banner warn">
            <strong>This token is already registered to {labels}.</strong>{' '}
            Plex account tokens are global - using the same token for multiple servers
            is valid, but every server will share the same authentication context
            (revoking one revokes them all). Consider using a server-specific admin
            account if you need isolated credentials. You can save anyway.
          </div>
        );
      })()}
      <label className="field">
        <span className="label">Friendly name</span>
        <span className="help">Used in CLI flags (<code>--source-server NAME</code>), schedules, and log/snapshot filenames. Must be unique.</span>
        <input type="text" value={name} onChange={(e) => setName(e.target.value)} placeholder="My Plex" />
      </label>
      <label className="field">
        <span className="label">Server URL</span>
        <span className="help">Full URL including protocol and port. On a single-host setup with Plex on the same machine, use <code>http://host.docker.internal:32400</code>.</span>
        <input type="text" value={url} onChange={(e) => setUrl(e.target.value)} />
      </label>
      <label className="field">
        <span className="label">Plex authentication token</span>
        <span className="help">
          {server
            ? 'A token is already saved. Leave blank to keep it; type a new value to replace it.'
            : 'Find yours in any X-Plex-Token URL from the Plex web UI.'}
        </span>
        <input type="password" value={token} onChange={(e) => setToken(e.target.value)} placeholder={server ? '••••••••' : ''} />
      </label>
      <div className="row-buttons">
        <button
          onClick={testConnection}
          disabled={testing || !name.trim() || !url.trim() || (!server && !token.trim())}
        >
          {testing ? 'Testing…' : 'Test Connection'}
        </button>
        <button className="primary" onClick={submit} disabled={submitting || !canSave}>
          {submitting ? 'Saving…' : (server ? 'Save Changes' : 'Save')}
        </button>
        <button onClick={() => onClose(false)}>Cancel</button>
      </div>
      {!server && !probe?.ok && (
        <p style={{ color: 'var(--text-dim)', fontSize: 12, marginTop: 8 }}>
          For a new server, the Save button is enabled only after a successful Test Connection.
        </p>
      )}
      {!server && probe?.ok && probe.duplicate_of && (
        <p style={{ color: 'var(--text-dim)', fontSize: 12, marginTop: 8 }}>
          Save is disabled because this physical Plex server is already registered.
        </p>
      )}
    </div>
  );
}

// Rule 2: library-walk trigger button + last-walked tooltip. One per
// server row. Polls the server's walk status on mount so the button's
// title reflects the most recent walk time without needing the parent
// to thread the data down.
function WalkButton({ serverId }: { serverId: string }) {
  const [status, setStatus] = useState<{
    running: boolean;
    lastFinishedAt: number | null;
    lastItemsSeen: number;
  }>({ running: false, lastFinishedAt: null, lastItemsSeen: 0 });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = async () => {
    try {
      const s = await api.getLibraryWalkStatus(serverId);
      setStatus({
        running: s.running,
        lastFinishedAt: s.last?.finished_at ?? null,
        lastItemsSeen: s.last?.items_seen ?? 0,
      });
    } catch {
      // Non-fatal: button still works, tooltip just won't show.
    }
  };

  useEffect(() => {
    refresh();
    // Light polling while running: every 5s. Stop polling once the
    // walk finishes so we don't generate background traffic forever.
    const t = window.setInterval(() => {
      if (status.running) refresh();
    }, 5000);
    return () => window.clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [serverId, status.running]);

  const handleClick = async () => {
    setBusy(true);
    setError(null);
    try {
      // M11: the walk now runs in the background on the server. The
      // POST returns immediately - flip to "running" and let the 5s
      // poll loop above track it through to completion + final count.
      await api.triggerLibraryWalk(serverId);
      setStatus((s) => ({ ...s, running: true }));
      refresh();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };

  const tooltip = status.lastFinishedAt
    ? `Last walk: ${new Date(status.lastFinishedAt * 1000).toLocaleString()} ` +
      `(${status.lastItemsSeen} items)`
    : 'No walk recorded yet';

  return (
    <button
      onClick={handleClick}
      disabled={busy || status.running}
      title={error || tooltip}
      style={error ? { color: 'var(--err, #dc2626)' } : undefined}
    >
      {status.running ? 'Walking…' : busy ? 'Walking…' : 'Walk'}
    </button>
  );
}

// Library Catalogue hybrid display helpers.
//
// The Plex section type is a structural label ("movie", "show",
// "artist") that doesn't read well as a unit. These helpers turn the
// type + leaf_counts into the operator-facing strings the catalogue
// renders for each library row.

function _topLevelUnit(type: string, count: number): string {
  // The word that follows the top-level count, pluralised against the
  // number. The Plex section types map cleanly to one unit each.
  const singular: Record<string, string> = {
    movie: 'movie',
    show: 'show',
    artist: 'artist',
    photo: 'photo',
  };
  const s = singular[type] ?? type;
  return count === 1 ? s : s + 's';
}

function _leafSuffix(lib: LibraryDescriptor): string {
  // For show libraries we surface the episode count; for artist
  // libraries we surface the track count. Movies and other types
  // have no leaf level the timing engine cares about, so the suffix
  // stays empty.
  const leaf = lib.leaf_counts;
  if (!leaf) return '';
  if (lib.type === 'show' && typeof leaf.episodes === 'number') {
    return ` · ${leaf.episodes.toLocaleString()} ${leaf.episodes === 1 ? 'episode' : 'episodes'}`;
  }
  if (lib.type === 'artist' && typeof leaf.tracks === 'number') {
    return ` · ${leaf.tracks.toLocaleString()} ${leaf.tracks === 1 ? 'track' : 'tracks'}`;
  }
  return '';
}

function _renderServerCount(
  value: number | null | undefined,
  singular: string,
  plural: string,
): string {
  // null / undefined = never captured. Render as a question mark so
  // operators can tell stale metadata from a confirmed zero.
  if (value === null || value === undefined) {
    return `? ${plural}`;
  }
  return `${value.toLocaleString()} ${value === 1 ? singular : plural}`;
}


// Library Catalogues: one sub-tab per registered server, mirroring the
// nav-strip pattern used elsewhere in the Help panel. Keeps the
// catalogue panel a fixed height regardless of how many servers are
// registered, and lets the operator focus on one server's libraries at
// a time without scrolling past sibling cards.
function LibraryCataloguesPanel({ servers }: { servers: ServerView[] }) {
  const [selectedId, setSelectedId] = useState<string>(servers[0]?.id ?? '');

  // Keep selection valid as the server list changes (rename / remove /
  // new add). If the selected id no longer exists, fall back to the
  // first server so the panel never renders a blank pane.
  useEffect(() => {
    if (!servers.some((s) => s.id === selectedId)) {
      setSelectedId(servers[0]?.id ?? '');
    }
  }, [servers, selectedId]);

  const selected = servers.find((s) => s.id === selectedId) ?? servers[0];
  if (!selected) return null;

  return (
    <div className="panel">
      <h2>Library Catalogues</h2>
      <p style={{ color: 'var(--text-dim)', fontSize: 12, marginTop: 0 }}>
        Cached from the most recent successful connection. Click <strong>Refresh</strong> on a row
        above to re-enumerate that server's catalogue.
      </p>
      <nav className="tabs sub-tabs" style={{ marginBottom: 12 }}>
        {servers.map((s) => (
          <button
            key={s.id}
            className={selectedId === s.id ? 'active' : ''}
            onClick={() => setSelectedId(s.id)}
          >
            {s.name}
          </button>
        ))}
      </nav>
      <h3 style={{ fontSize: 13, margin: '0 0 6px' }}>{selected.name}</h3>
      {selected.last_libraries.length === 0 ? (
        <div className="empty">No libraries cached. Refresh the connection to populate.</div>
      ) : (
        <ul style={{ paddingLeft: 18, margin: 0 }}>
          {selected.last_libraries.map((lib) => (
            <li key={lib.name}>
              {lib.name}{' '}
              <span style={{ color: 'var(--text-dim)' }}>
                <em>{lib.type}</em>
                {' · '}
                {lib.count.toLocaleString()}{' '}{_topLevelUnit(lib.type, lib.count)}
                {_leafSuffix(lib)}
              </span>
            </li>
          ))}
        </ul>
      )}
      <div style={{
        marginTop: 8, paddingTop: 6,
        borderTop: '1px solid var(--border, #2a2a2a)',
        fontSize: 11, color: 'var(--text-dim)',
      }}>
        Server-wide:{' '}
        {_renderServerCount(selected.playlist_count, 'playlist', 'playlists')}
        {' · '}
        {_renderServerCount(selected.collection_count, 'collection', 'collections')}
        {selected.counts_refreshed_at ? (
          <span style={{ marginLeft: 8, fontStyle: 'italic' }}>
            (refreshed {new Date(selected.counts_refreshed_at * 1000).toLocaleString()})
          </span>
        ) : (
          <span style={{ marginLeft: 8, fontStyle: 'italic' }}>
            (counts not yet captured, click Refresh)
          </span>
        )}
      </div>
    </div>
  );
}


// Server Users: same sub-tab pattern as Library Catalogues. One
// nav button per registered server; only the selected server's
// users render in the body. Mirrors the existing UsersForServer
// child component so per-server edit / save behaviour is unchanged.
function ServerUsersPanel({
  servers,
  users,
  onPatched,
}: {
  servers: ServerView[];
  users: Record<string, ServerUsersResponse | { error: string }>;
  onPatched: (next: ServerView) => void;
}) {
  const [selectedId, setSelectedId] = useState<string>(servers[0]?.id ?? '');

  useEffect(() => {
    if (!servers.some((s) => s.id === selectedId)) {
      setSelectedId(servers[0]?.id ?? '');
    }
  }, [servers, selectedId]);

  const selected = servers.find((s) => s.id === selectedId) ?? servers[0];
  if (!selected) return null;

  return (
    <div className="panel">
      <h2>Server Users</h2>
      <p style={{ color: 'var(--text-dim)', fontSize: 12, marginTop: 0 }}>
        Owner + Plex Home managed users on each registered server. Edit the owner's
        display name inline, that name propagates to the dashboard run header, the
        activity feed, and the direct-transfer user selector. Managed users' display
        names are not editable in this version.
      </p>
      <nav className="tabs sub-tabs" style={{ marginBottom: 12 }}>
        {servers.map((s) => (
          <button
            key={s.id}
            className={selectedId === s.id ? 'active' : ''}
            onClick={() => setSelectedId(s.id)}
          >
            {s.name}
          </button>
        ))}
      </nav>
      <UsersForServer
        server={selected}
        payload={users[selected.id]}
        onPatched={onPatched}
      />
    </div>
  );
}
