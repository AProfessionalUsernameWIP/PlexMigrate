// Server management tab.
//
// v0.9.1 update — server list and Add Server form only:
//   * Server list row: friendly name, URL, live status dot, ping ms,
//     library count, last-contacted timestamp, manual refresh button,
//     remove button with a confirmation prompt that reminds the user
//     that removal does not delete any export files or logs.
//   * Add Server form: Test Connection button that probes reachability
//     and displays the response time before saving; the Save button is
//     disabled until a successful test has been completed.
//   * Background poll: every 30 s the panel pings every registered
//     server (lightweight, hits Plex's /identity) and updates the
//     status dot + response-time chip in place.
//
// Nothing else on the page is touched — the threading-note banner
// above and the cached library catalogue panel below stay as-is.

import { useEffect, useRef, useState } from 'react';
import { api, LibraryDescriptor, PingResult, ServerUser, ServerUsersResponse, ServerView } from '../api';

// Poll cadence for the live status indicator, in milliseconds.
const PING_INTERVAL_MS = 30_000;

export function ServersPanel() {
  const [servers, setServers] = useState<ServerView[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState<ServerView | 'new' | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);

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

  // Track the polling timer so we can clear it on unmount.
  const pollTimerRef = useRef<number | null>(null);

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
          // Network or server-side error — fall through; the next
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
    pollTimerRef.current = window.setInterval(pingAll, PING_INTERVAL_MS);

    return () => {
      if (pollTimerRef.current !== null) {
        window.clearInterval(pollTimerRef.current);
        pollTimerRef.current = null;
      }
    };
    // The poll task references ``servers`` so we must re-arm when
    // the list changes; using server.id values as the dep would also
    // work but ``servers`` is simpler and equally correct.
  }, [servers]);

  // v0.9.6 Feature 3: fetch per-server user lists in parallel whenever
  // the server list changes. No caching — the user list on a Plex
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
      try {
        const res = await api.listServerUsers(s.id);
        if (!cancelled) {
          setUsers((prev) => ({ ...prev, [s.id]: res }));
        }
      } catch (e) {
        if (!cancelled) {
          setUsers((prev) => ({ ...prev, [s.id]: { error: String(e) } }));
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
    // Confirmation prompt explicitly reminds the user that the
    // registry entry is being removed, not any artefacts on disk.
    if (!confirm(
      `Remove server "${s.name}" from the registry?\n\n` +
      `This removes the registry entry only. Any export files in ` +
      `plex_exports/ or log directories in plex_logs/ that were ` +
      `produced by this server are NOT deleted and stay on disk.`
    )) return;
    try {
      await api.deleteServer(s.id);
      await refresh();
    } catch (e) {
      setError(String(e));
    }
  };

  return (
    <>
      {error && <div className="banner error">{error}</div>}

      <div className="banner info">
        <strong>Threading note:</strong> Every registered server adds API load when used.
        Running transfers to or from multiple servers at the same time multiplies that load
        across all involved servers. Schedule recurring backups at staggered times rather
        than the same minute, and see the README's <em>Understanding Performance and Threading</em>
        section for guidance on worker counts.
      </div>

      <div className="panel">
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12 }}>
          <h2 style={{ margin: 0 }}>Registered Plex Servers</h2>
          <button className="primary" onClick={() => setEditing('new')}>+ Add Server</button>
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
                <th>URL</th>
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
                // the cached status on the registry row — it's
                // strictly more recent.
                const effectiveStatus = ping?.status ?? s.last_status;
                const effectiveMs = ping?.response_ms ?? s.last_response_ms ?? null;
                return (
                  <tr key={s.id}>
                    <td><StatusDot status={effectiveStatus} detail={ping?.detail ?? s.last_status_detail} /></td>
                    <td><strong>{s.name}</strong></td>
                    <td className="mono">{s.url}</td>
                    <td className="num">
                      {effectiveMs !== null && effectiveStatus === 'ok'
                        ? `${effectiveMs.toFixed(0)} ms`
                        : effectiveStatus === 'unreachable'
                          ? '—'
                          : effectiveStatus === 'auth_error'
                            ? 'auth'
                            : '?'}
                    </td>
                    <td className="num">{s.last_libraries.length || '—'}</td>
                    <td>{s.last_checked_at ? new Date(s.last_checked_at * 1000).toLocaleString() : '—'}</td>
                    <td>
                      <div className="row-buttons">
                        <button onClick={() => test(s.id)} disabled={busyId === s.id} title="Refresh status and re-enumerate libraries">
                          {busyId === s.id ? 'Refreshing…' : 'Refresh'}
                        </button>
                        <button onClick={() => setEditing(s)}>Edit</button>
                        <button className="danger" onClick={() => remove(s)}>Remove</button>
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
          onClose={async (changed) => {
            setEditing(null);
            if (changed) await refresh();
          }}
        />
      )}

      {servers.length > 0 && (
        <div className="panel">
          <h2>Library Catalogues</h2>
          <p style={{ color: 'var(--text-dim)', fontSize: 12, marginTop: 0 }}>
            Cached from the most recent successful connection. Click <strong>Refresh</strong> on a row
            above to re-enumerate that server's catalogue.
          </p>
          <div className="row">
            {servers.map((s) => (
              <div className="col" key={s.id} style={{ minWidth: 280 }}>
                <h3 style={{ fontSize: 13, margin: '0 0 6px' }}>{s.name}</h3>
                {s.last_libraries.length === 0 ? (
                  <div className="empty">No libraries cached. Refresh the connection to populate.</div>
                ) : (
                  <ul style={{ paddingLeft: 18, margin: 0 }}>
                    {s.last_libraries.map((lib) => (
                      <li key={lib.name}>{lib.name} <span style={{ color: 'var(--text-dim)' }}>({lib.type}, {lib.count.toLocaleString()})</span></li>
                    ))}
                  </ul>
                )}
              </div>
            ))}
          </div>
        </div>
      )}

      {servers.length > 0 && (
        <div className="panel">
          <h2>Server Users</h2>
          <p style={{ color: 'var(--text-dim)', fontSize: 12, marginTop: 0 }}>
            Owner + Plex Home managed users on each registered server. Edit the owner's
            display name inline — that name propagates to the dashboard run header, the
            activity feed, and the direct-transfer user selector. Managed users' display
            names are not editable in this version.
          </p>
          <div className="row">
            {servers.map((s) => (
              <UsersForServer
                key={s.id}
                server={s}
                payload={users[s.id]}
                onPatched={applyServerPatch}
              />
            ))}
          </div>
        </div>
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
    // Total fetch failure (e.g. 502 — server unreachable).
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
        <div className="empty" style={{ fontSize: 12 }}>Owner unavailable — check the server's token.</div>
      )}

      {/* Managed users — read-only in this version. */}
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

function ServerEditor(props: { server: ServerView | null; onClose: (changed: boolean) => void }) {
  const { server, onClose } = props;
  const [name, setName] = useState(server?.name ?? '');
  const [url, setUrl] = useState(server?.url ?? 'http://host.docker.internal:32400');
  const [token, setToken] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // ── v0.9.1: Test Connection state ────────────────────────────────
  // For *new* server registration, Save stays disabled until a Test
  // Connection succeeds. ``testResult`` carries the most recent probe.
  // For *editing* an existing server, the Save button is always
  // available — testing is optional. Editing is allowed without a
  // fresh test because the existing token may already be valid and
  // forcing a re-probe would be friction.
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<PingResult | null>(null);

  // Reset the test result whenever the user changes a connection
  // field — the previous "passed" result no longer applies.
  useEffect(() => {
    setTestResult(null);
  }, [name, url, token]);

  // Run a probe BEFORE saving. Because the server hasn't been
  // registered yet, we can't hit /api/servers/{id}/ping; we have to
  // add the row first, ping it, then either keep it (test passed)
  // or delete it (test failed). To keep this simple and avoid
  // leaving "ghost" rows in the registry, the Test button registers
  // the server (or temporarily so), pings, then deletes if it fails.
  //
  // A cleaner long-term path would be a /api/servers/test-unsaved
  // endpoint that probes a URL+token without touching the registry.
  // That's pure additive backend work; punted to a future revision.
  const testConnection = async () => {
    setError(null);
    setTestResult(null);
    setTesting(true);
    try {
      // For new servers, do the dance described above. For edits,
      // ping the existing row directly.
      if (server) {
        // Existing server — if the form fields differ from the row,
        // update them first so the ping uses the new values.
        const needsUpdate = name !== server.name || url !== server.url || (token && token !== '');
        if (needsUpdate) {
          await api.updateServer(server.id, { name, url, token });
        }
        const result = await api.pingServer(server.id);
        setTestResult(result);
      } else {
        // New server flow: create a temporary entry, ping, decide.
        let created;
        try {
          created = await api.createServer({ name, url, token });
        } catch (e) {
          setError(`Could not create temporary registry entry for test: ${e}`);
          return;
        }
        try {
          const result = await api.pingServer(created.id);
          setTestResult(result);
          if (!result.ok) {
            // Delete the just-created row so the user isn't left with
            // a broken registration. They can try again with corrected
            // credentials.
            await api.deleteServer(created.id);
          }
          // If the test passed, leave the row in place — we'll treat
          // the form's Save button as "you already saved it; close now".
        } catch (e) {
          // Network blip during the ping itself — clean up.
          await api.deleteServer(created.id);
          throw e;
        }
      }
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
        // For new servers, a successful Test Connection has already
        // registered the row (see testConnection above). Save just
        // closes the editor in that case. If for some reason the
        // test wasn't completed, fall through and create now.
        if (!testResult?.ok) {
          await api.createServer({ name, url, token });
        }
      }
      onClose(true);
    } catch (e) {
      setError(String(e));
    } finally {
      setSubmitting(false);
    }
  };

  // For *new* servers, Save is disabled until a successful test.
  // For *editing*, Save is enabled whenever the form has required
  // fields filled.
  const canSave = server
    ? !!(name.trim() && url.trim())
    : !!(name.trim() && url.trim() && token.trim() && testResult?.ok);

  return (
    <div className="panel">
      <h2>{server ? `Edit Server: ${server.name}` : 'Add Server'}</h2>
      {error && <div className="banner error">{error}</div>}
      {testResult && (
        <div className={`banner ${testResult.ok ? 'good' : 'error'}`}>
          {testResult.ok
            ? `Connection OK — ${testResult.response_ms.toFixed(0)} ms.`
            : `Connection failed: ${testResult.detail || testResult.status}`}
        </div>
      )}
      <label className="field">
        <span className="label">Friendly name</span>
        <span className="help">Used in CLI flags (<code>--source-server NAME</code>), schedules, and log/export filenames. Must be unique.</span>
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
      {!server && !testResult?.ok && (
        <p style={{ color: 'var(--text-dim)', fontSize: 12, marginTop: 8 }}>
          For a new server, the Save button is enabled only after a successful Test Connection.
        </p>
      )}
    </div>
  );
}
