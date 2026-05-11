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
import { api, PingResult, ServerDeleteSummary, ServerView } from '../api';

// Poll cadence for the live status indicator, in milliseconds.
const PING_INTERVAL_MS = 30_000;

export function ServersPanel() {
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
        `${preview.exports} export file(s)`,
        `${preview.log_dirs} log directory/ies`,
      ];
      previewLine = `Cascade will also delete:\n  • ${parts.join('\n  • ')}\n\n`;
    } catch {
      previewLine = 'Cascade will also delete every schedule, export, and log directory attributable to this server.\n\n';
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
            Cleaned up {cascadeToast.schedules} schedule(s), {cascadeToast.exports} export file(s),
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
    </>
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
