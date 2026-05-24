// Sync Activity tab.
//
// Read-only health surface for the sync engine. Shows what the
// polling worker has been doing recently across every subscription,
// surfaces failures so they don't get buried inside per-subscription
// write logs, and gives the operator one place to land when they
// want to answer "is the sync working?"
//
// Three sections:
//   1. At-a-glance counts (subscriptions / observations / writes
//      pulled from /api/sync/stats).
//   2. Per-subscription health: last poll, last write, dry-run vs
//      real-writes, last status payload from the worker.
//   3. Recent writes feed: aggregated from per-subscription write
//      logs (up to 20 each, merged + sorted by time). The feed
//      highlights failures with a red tag and shows the before/after
//      math the worker computed.
//
// All data is read-only. Operator actions live in the Sync
// Subscriptions tab (enable/pause/dry-run toggles, delete, view
// per-row log).

import { useEffect, useMemo, useState } from 'react';
import { api } from '../api';
import type {
  ServerView, SyncSubscription, SyncWriteRow,
} from '../api';

interface AggregatedWrite extends SyncWriteRow {
  subscription_label: string;
}

export function SyncActivityTab({ servers }: { servers: ServerView[] }) {
  const [subs, setSubs] = useState<SyncSubscription[]>([]);
  const [writes, setWrites] = useState<AggregatedWrite[]>([]);
  const [stats, setStats] = useState<{
    subscriptions: number; observations: number; writes: number;
  } | null>(null);
  const [busy, setBusy] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const [showOnlyFailures, setShowOnlyFailures] = useState<boolean>(false);

  const serverLabel = (id: string): string => {
    const s = servers.find((x) => x.id === id);
    if (!s) return id || '(unknown)';
    const backend = (s as ServerView & { service_type?: string }).service_type;
    return backend && backend !== 'plex' ? `${s.name} [${backend}]` : s.name;
  };

  const subLabel = (sub: SyncSubscription): string => {
    const src = serverLabel(sub.source_server_id);
    const dst = serverLabel(sub.dest_server_id);
    const scope = sub.scope === 'library'
      ? ` (${sub.source_library_name || sub.source_library_id} → ${sub.dest_library_name || sub.dest_library_id})`
      : ' (server-wide)';
    return `${src} → ${dst}${scope} · ${sub.sync_type}`;
  };

  // Refresh pulls stats, the subscription list, and the latest 20
  // writes from each subscription. The per-subscription cap keeps
  // the round-trip bounded even with many subscriptions; the merged
  // feed is sorted by written_at descending.
  const refresh = async () => {
    setBusy(true);
    setError(null);
    try {
      const [statsR, subsR] = await Promise.all([
        api.syncStats(),
        api.syncListSubscriptions(),
      ]);
      setStats(statsR);
      setSubs(subsR.subscriptions);

      // Fetch recent writes per subscription in parallel. A subscription
      // with zero writes still resolves cleanly (writes: []).
      const writeBatches = await Promise.all(
        subsR.subscriptions.map(async (sub) => {
          try {
            const r = await api.syncGetWrites(sub.id, 20);
            return r.writes.map((w) => ({
              ...w,
              subscription_label: subLabel(sub),
            }));
          } catch {
            // Individual subscription failures don't break the
            // aggregate view - just skip this batch.
            return [] as AggregatedWrite[];
          }
        }),
      );
      const merged = writeBatches.flat().sort(
        (a, b) => b.written_at - a.written_at,
      );
      setWrites(merged.slice(0, 200));
    } catch (e) {
      setError(`Failed to load activity: ${(e as Error).message}`);
    } finally {
      setBusy(false);
    }
  };

  useEffect(() => {
    void refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const fmtAge = (ts: number | null): string => {
    if (!ts) return 'never';
    const diff = (Date.now() / 1000) - ts;
    if (diff < 60) return 'just now';
    if (diff < 3600) return `${Math.round(diff / 60)}m ago`;
    if (diff < 86400) return `${Math.round(diff / 3600)}h ago`;
    return `${Math.round(diff / 86400)}d ago`;
  };

  const fmtAbsolute = (ts: number | null): string => {
    if (!ts) return '—';
    return new Date(ts * 1000).toLocaleString();
  };

  const visibleWrites = useMemo(() => {
    if (!showOnlyFailures) return writes;
    return writes.filter((w) => !!w.error);
  }, [writes, showOnlyFailures]);

  const failureCount = useMemo(
    () => writes.filter((w) => !!w.error).length,
    [writes],
  );

  return (
    <div>
      {error && <div className="banner error" style={{ marginBottom: 8 }}>{error}</div>}

      <div className="panel" style={{ marginBottom: 12 }}>
        <h3 style={{ marginTop: 0 }}>Sync Activity</h3>
        <p className="help" style={{ marginBottom: 8, fontSize: 13 }}>
          <strong>What this is:</strong> a read-only health view of
          the sync engine. Use it to answer "is the worker running",
          "what did it touch recently", and "did anything fail."
          Toggles and edits live under <em>Sync Subscriptions</em>.
        </p>
        <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
          <button type="button" onClick={() => void refresh()} disabled={busy}>
            {busy ? 'Loading…' : 'Refresh'}
          </button>
          <label style={{ display: 'flex', alignItems: 'center', gap: 4, fontSize: 12 }}>
            <input
              type="checkbox"
              checked={showOnlyFailures}
              onChange={(e) => setShowOnlyFailures(e.target.checked)}
            />
            Show only failures
            {failureCount > 0 && (
              <span className="tag failed" style={{ fontSize: 10, marginLeft: 4 }}>
                {failureCount}
              </span>
            )}
          </label>
        </div>
      </div>

      {/* ── At-a-glance counts ──────────────────────────── */}
      <div className="panel" style={{ marginBottom: 12 }}>
        <h4 style={{ marginTop: 0 }}>At a glance</h4>
        {stats ? (
          <div style={{ display: 'flex', gap: 24, fontSize: 13 }}>
            <div>
              <div style={{ color: 'var(--text-dim)', fontSize: 11 }}>Subscriptions</div>
              <div style={{ fontSize: 20 }}>{stats.subscriptions.toLocaleString()}</div>
            </div>
            <div>
              <div style={{ color: 'var(--text-dim)', fontSize: 11 }}>Observations logged</div>
              <div style={{ fontSize: 20 }}>{stats.observations.toLocaleString()}</div>
            </div>
            <div>
              <div style={{ color: 'var(--text-dim)', fontSize: 11 }}>Writes recorded</div>
              <div style={{ fontSize: 20 }}>{stats.writes.toLocaleString()}</div>
            </div>
          </div>
        ) : (
          <div className="empty" style={{ fontSize: 12 }}>No stats yet.</div>
        )}
        <p className="help" style={{ marginTop: 8, marginBottom: 0, fontSize: 11, color: 'var(--text-dim)' }}>
          Observations count every per-cycle reading of source + dest
          state (logged regardless of dry-run). Writes count every
          target write the worker computed — issued + intent-only
          combined. Subscriptions includes dormant + paused.
        </p>
      </div>

      {/* ── Per-subscription health ────────────────────── */}
      <div className="panel" style={{ marginBottom: 12 }}>
        <h4 style={{ marginTop: 0 }}>Per-subscription health</h4>
        {subs.length === 0 ? (
          <div className="empty" style={{ fontSize: 12 }}>
            No subscriptions yet. Create one under{' '}
            <em>Sync Subscriptions</em>.
          </div>
        ) : (
          <table className="list" style={{ width: '100%' }}>
            <thead>
              <tr>
                <th>#</th>
                <th>Subscription</th>
                <th>State</th>
                <th>Last poll</th>
                <th>Last write</th>
                <th>Last status</th>
              </tr>
            </thead>
            <tbody>
              {subs.map((sub) => {
                const lastStatus = sub.last_status;
                const lastStatusText = lastStatus
                  ? typeof lastStatus === 'object'
                    ? JSON.stringify(lastStatus)
                    : String(lastStatus)
                  : '—';
                return (
                  <tr key={sub.id}>
                    <td>{sub.id}</td>
                    <td style={{ fontSize: 12 }}>{subLabel(sub)}</td>
                    <td>
                      {sub.enabled ? (
                        sub.dry_run ? (
                          <span className="tag phase" style={{ fontSize: 10 }}>dry-run</span>
                        ) : (
                          <span className="tag good" style={{ fontSize: 10 }}>active</span>
                        )
                      ) : (
                        <span className="tag" style={{ fontSize: 10 }}>dormant</span>
                      )}
                    </td>
                    <td style={{ fontSize: 11 }} title={fmtAbsolute(sub.last_polled_at)}>
                      {fmtAge(sub.last_polled_at)}
                    </td>
                    <td style={{ fontSize: 11 }} title={fmtAbsolute(sub.last_synced_at)}>
                      {fmtAge(sub.last_synced_at)}
                    </td>
                    <td style={{ fontSize: 11, color: 'var(--text-dim)', maxWidth: 280, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={lastStatusText}>
                      {lastStatusText}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>

      {/* ── Recent writes feed ─────────────────────────── */}
      <div className="panel">
        <h4 style={{ marginTop: 0 }}>
          Recent writes
          {showOnlyFailures && failureCount === 0 && (
            <span style={{ marginLeft: 8, fontSize: 11, color: 'var(--text-dim)', fontWeight: 400 }}>
              (no failures in the last 200 writes)
            </span>
          )}
        </h4>
        {visibleWrites.length === 0 ? (
          <div className="empty" style={{ fontSize: 12 }}>
            {showOnlyFailures
              ? 'No failures recorded. Nice.'
              : 'No writes recorded yet. The worker runs at each subscription\'s configured poll interval; dry-run subscriptions still log intents here.'}
          </div>
        ) : (
          <table className="list" style={{ width: '100%' }}>
            <thead>
              <tr>
                <th>When</th>
                <th>Subscription</th>
                <th>Type</th>
                <th>Item</th>
                <th>User</th>
                <th>Before → After</th>
                <th>Status</th>
                <th>Error</th>
              </tr>
            </thead>
            <tbody>
              {visibleWrites.map((w) => (
                <tr key={`${w.subscription_id}-${w.id}`}>
                  <td style={{ fontSize: 11 }} title={fmtAbsolute(w.written_at)}>
                    {fmtAge(w.written_at)}
                  </td>
                  <td style={{ fontSize: 11 }}>{w.subscription_label}</td>
                  <td style={{ fontSize: 11 }}>{w.sync_type}</td>
                  <td>
                    <code style={{ fontSize: 10 }}>{w.target_item_rating_key}</code>
                  </td>
                  <td style={{ fontSize: 11 }}>{w.target_user_id || '(owner)'}</td>
                  <td style={{ fontSize: 11 }}>
                    {w.before_value !== null && w.after_value !== null
                      ? `${w.before_value} → ${w.after_value}`
                      : '—'}
                  </td>
                  <td>
                    <span
                      className={`tag ${w.issued ? 'good' : w.error ? 'failed' : 'phase'}`}
                      style={{ fontSize: 10 }}
                    >
                      {w.issued ? 'wrote' : w.error ? 'failed' : 'intent only'}
                    </span>
                  </td>
                  <td style={{ fontSize: 11, color: 'var(--text-dim)' }}>{w.error || ''}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
