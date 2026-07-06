// Sync Subscriptions tab.
//
// Lives under the top-level Server Syncing page, next to Library
// Mapping. Operator-declared subscriptions tell the polling worker
// what to reconcile between which servers / libraries.
//
// Subscription scope:
//   - Library-pair: source AND dest library IDs set → worker walks
//     just that pair
//   - Server-pair: BOTH library IDs empty → worker expands to every
//     library_mapping row between the two servers at poll time
//
// Sync types: watch_counts, ratings, favorites, last_watched, playlists.
// Conflict policies: max, sum, latest_wins, source_of_truth.
//
// Safety rails:
//   - new subscriptions default to dry_run=true (logs intents, no writes)
//   - explicit "Enable real writes" toggle after operator reviews dry-run
//   - Per-subscription "View writes" panel surfaces the recent audit log

import { Fragment, useEffect, useMemo, useState } from 'react';
import { api } from '../api';
import { useConfirm } from './ConfirmModal';
import type {
  SyncSubscription, SyncWriteRow, ConflictPolicy, SyncType,
  ServerView, ServerUser,
} from '../api';

type UserScope = 'owner' | 'all' | 'specific';

const SYNC_TYPES: { value: SyncType; label: string; help: string }[] = [
  { value: 'watch_counts', label: 'Watch counts', help: 'Number of plays per item per user' },
  { value: 'ratings',      label: 'Ratings',      help: 'User-set numeric ratings' },
  { value: 'favorites',    label: 'Favorites',    help: 'IsFavorite boolean flag' },
  { value: 'last_watched', label: 'Last watched', help: 'LastPlayedDate timestamp' },
  { value: 'playlists',    label: 'Playlists',    help: 'Auto-migrate playlist contents' },
];

const POLICIES: { value: ConflictPolicy; label: string; help: string }[] = [
  { value: 'max', label: 'Max (safest)', help: 'Target = max(source, dest). Never lose a play.' },
  { value: 'sum', label: 'Sum', help: 'Target = source + dest. Use when sides serve different people and total listens matter.' },
  { value: 'latest_wins', label: 'Latest wins', help: 'Whichever side changed most recently wins.' },
  { value: 'source_of_truth', label: 'Source is truth', help: 'Source side is authoritative; destination always mirrors it.' },
];

// FEUI-06: the four inline mutators below (saveIntervalEdit,
// saveScopeEdit, toggleEnabled, toggleDryRun) each hand-rebuilt the
// identical ~14-field syncSaveSubscription payload from `sub`,
// varying only 1-2 fields. This helper builds the full payload once
// with the same || null / || '' / || undefined normalisation; each
// mutator then spreads it and overrides the field(s) it changes.
type SyncSaveSubscriptionInput = Parameters<typeof api.syncSaveSubscription>[0];

function subscriptionToPayload(sub: SyncSubscription): SyncSaveSubscriptionInput {
  return {
    source_server_id: sub.source_server_id,
    dest_server_id:   sub.dest_server_id,
    sync_type:        sub.sync_type,
    source_library_id:   sub.source_library_id || null,
    source_library_name: sub.source_library_name || '',
    dest_library_id:     sub.dest_library_id || null,
    dest_library_name:   sub.dest_library_name || '',
    conflict_policy:  sub.conflict_policy,
    enabled:          sub.enabled,
    dry_run:          sub.dry_run,
    bidirectional:    sub.bidirectional,
    user_scope:       sub.user_scope,
    user_filter:      sub.user_filter || undefined,
    poll_interval_seconds: sub.poll_interval_seconds,
    auto_sync_new_playlists: sub.auto_sync_new_playlists,
  };
}

// Shared user-pick checkbox grid for the new-subscription and
// edit-subscription user_filter pickers. Both sites previously
// inlined ~36 lines of identical .map() markup that varied only in
// which user list + which selection state to read/write. The
// `selected` array uses raw_name as the matching key (consistent
// with both prior sites).
function UserFilterCheckboxGrid(props: {
  users: ServerUser[];
  selected: string[];
  onChange: (next: string[]) => void;
}) {
  return (
    <div style={{
      display: 'grid',
      gridTemplateColumns: 'repeat(auto-fill, minmax(180px, 1fr))',
      gap: 4,
      maxHeight: 200,
      overflowY: 'auto',
    }}>
      {props.users.map((u) => {
        const checked = props.selected.includes(u.raw_name);
        return (
          <label
            key={u.plex_id || u.raw_name}
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: 6,
              fontSize: 12,
              padding: '4px 6px',
              borderRadius: 3,
              background: checked ? 'var(--bg-panel-alt, rgba(74,122,252,0.08))' : undefined,
              cursor: 'pointer',
            }}
          >
            <input
              type="checkbox"
              checked={checked}
              onChange={(e) => {
                if (e.target.checked) {
                  props.onChange([...props.selected, u.raw_name]);
                } else {
                  props.onChange(props.selected.filter((x) => x !== u.raw_name));
                }
              }}
            />
            <span style={{ flex: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
              {u.display_name || u.raw_name}
            </span>
            {u.kind === 'owner' && (
              <span className="tag" style={{ fontSize: 9 }}>owner</span>
            )}
          </label>
        );
      })}
    </div>
  );
}


export function SyncSubscriptionsTab({ servers }: { servers: ServerView[] }) {
  const [subs, setSubs] = useState<SyncSubscription[]>([]);
  const [busy, setBusy] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const [info, setInfo] = useState<string | null>(null);
  const [openWritesFor, setOpenWritesFor] = useState<number | null>(null);
  const [writes, setWrites] = useState<SyncWriteRow[]>([]);

  // ── New subscription form state ──
  const [newSrc, setNewSrc] = useState<string>('');
  const [newDst, setNewDst] = useState<string>('');
  const [newType, setNewType] = useState<SyncType>('watch_counts');
  const [newPolicy, setNewPolicy] = useState<ConflictPolicy>('max');
  const [newBidi, setNewBidi] = useState<boolean>(false);
  const [newInterval, setNewInterval] = useState<number>(300);
  const [newAutoNewPlaylists, setNewAutoNewPlaylists] = useState<boolean>(true);
  // Per-user opt-in/out for the new subscription. Default 'owner' is
  // owner-only sync. 'all' syncs every user on the source. 'specific'
  // surfaces the user multi-select below, sourced from
  // listServerUsers(newSrc) so the operator picks from real handles.
  const [newScope, setNewScope] = useState<UserScope>('owner');
  const [newFilter, setNewFilter] = useState<string[]>([]);
  // Source-server user list for the picker. Lazy-loaded when the
  // source changes AND scope is set to 'specific'.
  const [srcUsers, setSrcUsers] = useState<ServerUser[]>([]);
  const [srcUsersError, setSrcUsersError] = useState<string | null>(null);
  const [srcUsersLoading, setSrcUsersLoading] = useState<boolean>(false);

  // Lazy-load source-server users for the picker when scope is
  // 'specific'. Refetches when newSrc changes; clears when source
  // becomes empty.
  useEffect(() => {
    if (!newSrc || newScope !== 'specific') {
      setSrcUsers([]);
      setSrcUsersError(null);
      return;
    }
    let cancelled = false;
    setSrcUsersLoading(true);
    api.listServerUsers(newSrc).then(
      (r) => {
        if (cancelled) return;
        setSrcUsers(r.users || []);
        setSrcUsersError(r.error || null);
      },
      (e) => {
        if (cancelled) return;
        setSrcUsersError(`Could not load users: ${(e as Error).message}`);
        setSrcUsers([]);
      },
    ).finally(() => {
      if (!cancelled) setSrcUsersLoading(false);
    });
    return () => { cancelled = true; };
  }, [newSrc, newScope]);

  // Drop stale filter entries when scope flips back to owner/all so
  // we don't carry hidden state across mode changes.
  useEffect(() => {
    if (newScope !== 'specific') setNewFilter([]);
  }, [newScope]);

  // ── Inline poll-interval editor on existing subscriptions ──
  // The interval is a per-subscription value the operator can want
  // to retune as workloads change (e.g. drop a 5-min sub down to
  // 30s while debugging a playlist drift). We don't open a full
  // editor for one field; just an inline number-input that swaps in
  // for the display cell and saves on blur / Enter.
  const [editIntervalFor, setEditIntervalFor] = useState<number | null>(null);
  const [editIntervalValue, setEditIntervalValue] = useState<number>(300);

  const openIntervalEdit = (sub: SyncSubscription) => {
    setEditIntervalFor(sub.id);
    setEditIntervalValue(sub.poll_interval_seconds);
  };

  const saveIntervalEdit = async (sub: SyncSubscription) => {
    const clamped = Math.max(5, Math.min(86400, Math.floor(editIntervalValue || 300)));
    if (clamped === sub.poll_interval_seconds) {
      setEditIntervalFor(null);
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await api.syncSaveSubscription({
        ...subscriptionToPayload(sub),
        poll_interval_seconds: clamped,
      });
      setInfo(`Interval for subscription #${sub.id} set to ${formatInterval(clamped)}.`);
      setEditIntervalFor(null);
      await reload();
    } catch (e) {
      setError(`Interval update failed: ${(e as Error).message}`);
    } finally {
      setBusy(false);
    }
  };

  // Force-run a subscription now (bypass the next-tick wait).
  // Server-side this dispatches the reconcile to a background task
  // and returns 202; the row's Last poll timestamp updates when the
  // run completes (we trigger a reload a few seconds later as a
  // best-effort refresh).
  const runNow = async (sub: SyncSubscription) => {
    setBusy(true);
    setError(null);
    try {
      const r = await api.syncRunNow(sub.id);
      setInfo(`Subscription #${r.subscription_id} queued for immediate reconcile.`);
      // Reload after a short delay so the operator sees the
      // refreshed Last-poll timestamp once the background task
      // completes. Best-effort; longer cycles will land on the
      // next manual / interval-driven reload.
      window.setTimeout(() => { void reload(); }, 2500);
    } catch (e) {
      setError(`Run-now failed: ${(e as Error).message}`);
    } finally {
      setBusy(false);
    }
  };

  // ── Inline scope editor for existing subscriptions ──
  // editScopeFor is the subscription id whose row is currently
  // expanded into an editor; null when no editor is open. Editor
  // state mirrors the create-form fields so the picker UI can be
  // reused across both surfaces.
  const [editScopeFor, setEditScopeFor] = useState<number | null>(null);
  const [editScope, setEditScope] = useState<UserScope>('owner');
  const [editFilter, setEditFilter] = useState<string[]>([]);
  const [editScopeUsers, setEditScopeUsers] = useState<ServerUser[]>([]);
  const [editScopeUsersError, setEditScopeUsersError] = useState<string | null>(null);
  const [editScopeUsersLoading, setEditScopeUsersLoading] = useState<boolean>(false);

  const openEditScope = async (sub: SyncSubscription) => {
    if (editScopeFor === sub.id) {
      // Toggle off.
      setEditScopeFor(null);
      return;
    }
    setEditScopeFor(sub.id);
    setEditScope((sub.user_scope as UserScope) || 'owner');
    setEditFilter(sub.user_filter ? [...sub.user_filter] : []);
    setEditScopeUsersError(null);
    setEditScopeUsers([]);
    if (!sub.source_server_id) return;
    setEditScopeUsersLoading(true);
    try {
      const r = await api.listServerUsers(sub.source_server_id);
      setEditScopeUsers(r.users || []);
      setEditScopeUsersError(r.error || null);
    } catch (e) {
      setEditScopeUsersError(`Could not load users: ${(e as Error).message}`);
    } finally {
      setEditScopeUsersLoading(false);
    }
  };

  const saveEditScope = async (sub: SyncSubscription) => {
    if (editScope === 'specific' && editFilter.length === 0) {
      setError(
        'User scope is set to "Specific users" but no users are '
        + 'selected. Pick at least one, or pick a different scope.',
      );
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await api.syncSaveSubscription({
        ...subscriptionToPayload(sub),
        user_scope:  editScope,
        user_filter: editScope === 'specific' ? editFilter : undefined,
      });
      setInfo(`Scope updated for subscription #${sub.id}.`);
      setEditScopeFor(null);
      await reload();
    } catch (e) {
      setError(`Save failed: ${(e as Error).message}`);
    } finally {
      setBusy(false);
    }
  };

  const reload = async () => {
    setBusy(true);
    setError(null);
    try {
      const r = await api.syncListSubscriptions();
      setSubs(r.subscriptions);
    } catch (e) {
      setError(`Failed to load subscriptions: ${(e as Error).message}`);
    } finally {
      setBusy(false);
    }
  };

  useEffect(() => { void reload(); }, []);

  const serverLabel = (id: string): string => {
    const s = servers.find((x) => x.id === id);
    if (!s) return id || '(unknown)';
    const backend = (s as ServerView & { service_type?: string }).service_type;
    return backend && backend !== 'plex' ? `${s.name} [${backend}]` : s.name;
  };

  // Server-scope subscription only (library-scope is added via the
  // Library Mapping tab's per-row toggle to keep this form simple).
  const createSubscription = async () => {
    if (!newSrc || !newDst || newSrc === newDst) {
      setError('Pick a source and a different destination server.');
      return;
    }
    setBusy(true);
    setError(null);
    setInfo(null);
    try {
      if (newScope === 'specific' && newFilter.length === 0) {
        setError(
          'User scope is set to "Specific users" but no users are '
          + 'selected. Pick at least one user, or change scope.',
        );
        setBusy(false);
        return;
      }
      const r = await api.syncSaveSubscription({
        source_server_id: newSrc,
        dest_server_id: newDst,
        sync_type: newType,
        conflict_policy: newPolicy,
        enabled: false,            // start dormant
        dry_run: true,             // safety default
        bidirectional: newBidi,
        poll_interval_seconds: newInterval,
        auto_sync_new_playlists: newType === 'playlists' ? newAutoNewPlaylists : false,
        user_scope: newScope,
        user_filter: newScope === 'specific' ? newFilter : undefined,
      });
      setInfo(`Subscription #${r.id} created (dry-run, dormant). `
        + `Review and click Enable to start.`);
      await reload();
    } catch (e) {
      setError(`Create failed: ${(e as Error).message}`);
    } finally {
      setBusy(false);
    }
  };

  const toggleEnabled = async (sub: SyncSubscription) => {
    setBusy(true);
    setError(null);
    try {
      await api.syncSaveSubscription({
        ...subscriptionToPayload(sub),
        enabled: !sub.enabled,
      });
      await reload();
    } catch (e) {
      setError(`Toggle failed: ${(e as Error).message}`);
    } finally {
      setBusy(false);
    }
  };

  const toggleDryRun = async (sub: SyncSubscription) => {
    setBusy(true);
    setError(null);
    try {
      await api.syncSaveSubscription({
        ...subscriptionToPayload(sub),
        dry_run: !sub.dry_run,
      });
      await reload();
    } catch (e) {
      setError(`Toggle failed: ${(e as Error).message}`);
    } finally {
      setBusy(false);
    }
  };

  const confirm = useConfirm();
  const deleteSub = async (sub: SyncSubscription) => {
    if (!(await confirm({
      body: `Delete subscription #${sub.id} (${sub.sync_type})?`,
      danger: true,
    }))) return;
    setBusy(true);
    try {
      await api.syncDeleteSubscription(sub.id);
      setInfo(`Deleted subscription #${sub.id}.`);
      await reload();
    } catch (e) {
      setError(`Delete failed: ${(e as Error).message}`);
    } finally {
      setBusy(false);
    }
  };

  const viewWrites = async (sub: SyncSubscription) => {
    if (openWritesFor === sub.id) {
      setOpenWritesFor(null);
      setWrites([]);
      return;
    }
    setBusy(true);
    try {
      const r = await api.syncGetWrites(sub.id, 50);
      setWrites(r.writes);
      setOpenWritesFor(sub.id);
    } catch (e) {
      setError(`View failed: ${(e as Error).message}`);
    } finally {
      setBusy(false);
    }
  };

  // Pretty-print a poll interval for the table. 5..59 -> "12s",
  // 60..3599 -> "5m" / "10m 30s", 3600+ -> "2h 15m". Used by the
  // Interval column's display mode (the inline editor is a plain
  // number input).
  const formatInterval = (sec: number): string => {
    const s = Math.max(0, Math.floor(sec));
    if (s < 60) return `${s}s`;
    const mins = Math.floor(s / 60);
    const rem_s = s % 60;
    if (s < 3600) {
      return rem_s === 0 ? `${mins}m` : `${mins}m ${rem_s}s`;
    }
    const hrs = Math.floor(s / 3600);
    const rem_m = Math.floor((s % 3600) / 60);
    if (s < 86400) {
      return rem_m === 0 ? `${hrs}h` : `${hrs}h ${rem_m}m`;
    }
    const days = Math.floor(s / 86400);
    return `${days}d`;
  };

  const fmtAge = (ts: number | null): string => {
    if (!ts) return 'never';
    const diff = (Date.now() / 1000) - ts;
    if (diff < 60) return 'just now';
    if (diff < 3600) return `${Math.round(diff / 60)}m ago`;
    if (diff < 86400) return `${Math.round(diff / 3600)}h ago`;
    return `${Math.round(diff / 86400)}d ago`;
  };

  const subsByPair = useMemo(() => {
    // FEUI-05: copy before sorting - Array.sort mutates in place and
    // ``subs`` is React state; sorting it directly mutates state.
    return [...subs].sort((a, b) => a.id - b.id);
  }, [subs]);

  return (
    <div>
      {error && <div className="banner error" style={{ marginBottom: 8 }}>{error}</div>}
      {info && <div className="banner info" style={{ marginBottom: 8 }}>{info}</div>}

      <div className="panel" style={{ marginBottom: 12 }}>
        <h3 style={{ marginTop: 0 }}>Sync Subscriptions</h3>
        <p className="help" style={{ marginBottom: 8, fontSize: 13 }}>
          <strong>What this is:</strong> an ongoing reconciliation
          contract between two servers. Each subscription says "keep
          this kind of data (watch counts, ratings, favorites, last
          watched, or playlists) in agreement on this schedule, under
          this conflict policy." A polling worker wakes on the
          configured interval, reads both sides, computes the target
          value with the chosen policy, and (when not in dry-run)
          issues exact-target writes to whichever side is behind.
        </p>
        <p className="help" style={{ marginBottom: 8, fontSize: 13 }}>
          <strong>How this differs from Library Mapping:</strong>{' '}
          mapping is the <em>contract</em> (a state file declaring
          what is equivalent); subscriptions are the <em>process</em>{' '}
          (an active worker that reads + writes on a cadence). A
          subscription has nothing to walk until a mapping exists for
          the libraries involved — set those up under{' '}
          <strong>Library Mapping</strong> first. Server-scope
          subscriptions (library fields left blank) automatically
          expand to every mapped library pair between the two servers
          at poll time; library-scope subscriptions reconcile exactly
          the one pair you name.
        </p>
        <p className="help" style={{ marginBottom: 8, fontSize: 13 }}>
          <strong>Safety rails:</strong> new subscriptions start in
          dry-run mode and dormant. Click <em>Enable</em> to start
          the worker, then review the per-row write log; the worker
          logs intended writes there even while dry-running so you
          can sanity-check the math before flipping{' '}
          <em>Real writes</em> on. Conflict policies are deliberately
          conservative by default — Max is safest (you can never
          lose a play that happened on either side); Sum is the right
          pick when different people use each server; Latest-wins and
          Source-of-truth are stronger statements about which side
          should overwrite which.
        </p>
        <p className="help" style={{ marginBottom: 12, fontSize: 13 }}>
          <strong>What this is not:</strong> subscriptions don't move
          libraries, playlists, or users between backends as a one-shot
          (use <em>Run Job</em> for that), and they don't decide what
          two libraries are equivalent (that's <em>Library Mapping</em>).
          They keep two already-equivalent libraries in agreement over
          time.
        </p>
        <div style={{ display: 'flex', gap: 12, alignItems: 'flex-end', flexWrap: 'wrap' }}>
          <label style={{ display: 'flex', flexDirection: 'column', gap: 2, fontSize: 12 }}>
            <span><strong>Source</strong></span>
            <select value={newSrc} onChange={(e) => setNewSrc(e.target.value)} style={{ minWidth: 180 }}>
              <option value="">— pick —</option>
              {servers.map((s) => <option key={s.id} value={s.id}>{serverLabel(s.id)}</option>)}
            </select>
          </label>
          <label style={{ display: 'flex', flexDirection: 'column', gap: 2, fontSize: 12 }}>
            <span><strong>Destination</strong></span>
            <select value={newDst} onChange={(e) => setNewDst(e.target.value)} style={{ minWidth: 180 }}>
              <option value="">— pick —</option>
              {servers.filter((s) => s.id !== newSrc).map((s) => <option key={s.id} value={s.id}>{serverLabel(s.id)}</option>)}
            </select>
          </label>
          <label style={{ display: 'flex', flexDirection: 'column', gap: 2, fontSize: 12 }}>
            <span><strong>Sync type</strong></span>
            <select value={newType} onChange={(e) => setNewType(e.target.value as SyncType)}>
              {SYNC_TYPES.map((t) => (
                <option key={t.value} value={t.value} title={t.help}>{t.label}</option>
              ))}
            </select>
          </label>
          <label style={{ display: 'flex', flexDirection: 'column', gap: 2, fontSize: 12 }}>
            <span><strong>Conflict policy</strong></span>
            <select value={newPolicy} onChange={(e) => setNewPolicy(e.target.value as ConflictPolicy)}>
              {POLICIES.map((p) => (
                <option key={p.value} value={p.value} title={p.help}>{p.label}</option>
              ))}
            </select>
          </label>
          <label
            style={{ display: 'flex', flexDirection: 'column', gap: 2, fontSize: 12 }}
            title="Seconds between poll cycles for this subscription. 5s minimum, 1 day maximum. Near-realtime polling (5-30s) is fine for an actively-used pair; long intervals (hours) are right for playlist subs that change rarely."
          >
            <span><strong>Poll interval (s)</strong></span>
            <input
              type="number" min={5} max={86400}
              value={newInterval}
              onChange={(e) => setNewInterval(Math.max(5, Math.min(86400, Number(e.target.value) || 300)))}
              style={{ width: 100 }}
            />
          </label>
          <label style={{ display: 'flex', alignItems: 'center', gap: 4, fontSize: 12, paddingBottom: 4 }}
            title="Reconcile in BOTH directions. Default off (source → dest only).">
            <input type="checkbox" checked={newBidi} onChange={(e) => setNewBidi(e.target.checked)} />
            Bidirectional
          </label>
          {newType === 'playlists' && (
            <label style={{ display: 'flex', alignItems: 'center', gap: 4, fontSize: 12, paddingBottom: 4 }}
              title="When source has a NEW playlist not yet in this subscription's selections, automatically add + sync it.">
              <input
                type="checkbox"
                checked={newAutoNewPlaylists}
                onChange={(e) => setNewAutoNewPlaylists(e.target.checked)}
              />
              Auto-sync new playlists
            </label>
          )}
          <label style={{ display: 'flex', flexDirection: 'column', gap: 2, fontSize: 12 }}>
            <span><strong>User scope</strong></span>
            <select
              value={newScope}
              onChange={(e) => setNewScope(e.target.value as UserScope)}
              title="Owner only = sync only the server owner. All users = sync every user on the source. Specific users = pick exactly which users to sync. Users not in the selected set are never touched."
            >
              <option value="owner">Owner only (default)</option>
              <option value="all">All users</option>
              <option value="specific">Specific users…</option>
            </select>
          </label>
          <button type="button" onClick={() => void createSubscription()} disabled={busy}>
            Create subscription
          </button>
        </div>

        {/* Specific-user picker - only renders when scope='specific'.
            Picks from listServerUsers(newSrc) so the operator selects
            from real handles, not a freeform text box. The owner row
            is included as a normal user; pick 'owner' explicitly when
            you want owner-and-some-managed-users syncing under
            'specific'. */}
        {newScope === 'specific' && (
          <div style={{ marginTop: 12, paddingTop: 12, borderTop: '1px solid var(--border, rgba(255,255,255,0.08))' }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6 }}>
              <strong style={{ fontSize: 12 }}>
                Pick users to sync (source: {serverLabel(newSrc) || '— pick source first —'})
              </strong>
              {srcUsersLoading && (
                <span style={{ fontSize: 11, color: 'var(--text-dim)' }}>Loading…</span>
              )}
              {srcUsers.length > 0 && (
                <span style={{ marginLeft: 'auto', display: 'flex', gap: 6 }}>
                  <button
                    type="button"
                    style={{ fontSize: 10, padding: '2px 6px' }}
                    onClick={() => setNewFilter(srcUsers.map((u) => u.raw_name))}
                  >
                    Select all
                  </button>
                  <button
                    type="button"
                    style={{ fontSize: 10, padding: '2px 6px' }}
                    onClick={() => setNewFilter([])}
                  >
                    Clear
                  </button>
                </span>
              )}
            </div>
            {srcUsersError && (
              <div className="banner info" style={{ marginBottom: 8, fontSize: 11 }}>
                {srcUsersError}
              </div>
            )}
            {!newSrc ? (
              <div className="empty" style={{ fontSize: 12 }}>
                Pick a source server above first; the user list will load
                from that server.
              </div>
            ) : srcUsers.length === 0 && !srcUsersLoading ? (
              <div className="empty" style={{ fontSize: 12 }}>
                No users captured for this server yet. Try{' '}
                <strong>Servers &rsaquo; Refresh users</strong> for the
                source server.
              </div>
            ) : (
              <UserFilterCheckboxGrid
                users={srcUsers}
                selected={newFilter}
                onChange={setNewFilter}
              />
            )}
            <p className="help" style={{ marginTop: 8, marginBottom: 0, fontSize: 11, color: 'var(--text-dim)' }}>
              Only users with a check mark above will be touched by this
              subscription. Owner is treated like any other user here —
              tick the owner explicitly if you want it included.
              Today playlist + watch-count sync targeting Plex managed
              users is gated (Plex needs a per-user token the worker
              doesn't have yet); those writes are logged as skipped in
              Sync Activity rather than misattributed to owner.
            </p>
          </div>
        )}
      </div>

      {subsByPair.length === 0 ? (
        <div className="empty">
          No sync subscriptions yet. Create one above to start reconciling
          watch counts, ratings, or playlists between two servers.
        </div>
      ) : (
        <div className="panel">
          <table className="list" style={{ width: '100%' }}>
            <thead>
              <tr>
                <th>#</th>
                <th>Source → Dest</th>
                <th>Scope</th>
                <th>Users</th>
                <th>Type / Policy</th>
                <th>State</th>
                <th title="Seconds between poll cycles. Click to edit.">Interval</th>
                <th>Last poll</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {subsByPair.map((sub) => (
                <Fragment key={sub.id}>
                  <tr>
                    <td>{sub.id}</td>
                    <td>
                      <strong>{serverLabel(sub.source_server_id)}</strong>
                      <span style={{ margin: '0 6px' }}>→</span>
                      <strong>{serverLabel(sub.dest_server_id)}</strong>
                      {sub.bidirectional && (
                        <span className="tag" style={{ fontSize: 10, marginLeft: 6 }}>
                          bidirectional
                        </span>
                      )}
                    </td>
                    <td>
                      {sub.scope === 'server' ? (
                        <span className="tag" style={{ fontSize: 10 }}>server-wide</span>
                      ) : (
                        <span style={{ fontSize: 11 }}>
                          {sub.source_library_name || sub.source_library_id}
                          {' → '}
                          {sub.dest_library_name || sub.dest_library_id}
                        </span>
                      )}
                    </td>
                    <td style={{ fontSize: 11 }}>
                      {sub.user_scope === 'owner' && (
                        <span className="tag" style={{ fontSize: 10 }} title="Only the server owner is synced.">owner</span>
                      )}
                      {sub.user_scope === 'all' && (
                        <span className="tag phase" style={{ fontSize: 10 }} title="Every user on the source is synced (subject to backend support).">all</span>
                      )}
                      {sub.user_scope === 'specific' && (
                        <span title={(sub.user_filter || []).join(', ') || 'no users selected'}>
                          <span className="tag good" style={{ fontSize: 10 }}>specific</span>
                          <span style={{ marginLeft: 4, color: 'var(--text-dim)' }}>
                            ({(sub.user_filter || []).length})
                          </span>
                        </span>
                      )}
                    </td>
                    <td style={{ fontSize: 11 }}>
                      <strong>{sub.sync_type}</strong>
                      <div style={{ color: 'var(--text-dim)' }}>{sub.conflict_policy}</div>
                    </td>
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
                    <td style={{ fontSize: 11 }}>
                      {editIntervalFor === sub.id ? (
                        <span style={{ display: 'inline-flex', gap: 4, alignItems: 'center' }}>
                          <input
                            type="number" min={5} max={86400}
                            value={editIntervalValue}
                            autoFocus
                            onChange={(e) => setEditIntervalValue(
                              Math.max(5, Math.min(86400, Number(e.target.value) || 5)),
                            )}
                            onKeyDown={(e) => {
                              if (e.key === 'Enter') void saveIntervalEdit(sub);
                              if (e.key === 'Escape') setEditIntervalFor(null);
                            }}
                            onBlur={() => void saveIntervalEdit(sub)}
                            style={{ width: 70, fontSize: 11 }}
                            title="Seconds between poll cycles. 5..86400. Enter to save, Esc to cancel."
                          />
                          <span style={{ color: 'var(--text-dim)', fontSize: 10 }}>s</span>
                        </span>
                      ) : (
                        <button
                          type="button"
                          onClick={() => openIntervalEdit(sub)}
                          style={{
                            background: 'none', border: 'none',
                            padding: 0, fontSize: 11, cursor: 'pointer',
                            color: 'inherit',
                            textDecoration: 'underline dotted var(--text-dim)',
                          }}
                          title={`Every ${sub.poll_interval_seconds}s. Click to edit.`}
                        >
                          {formatInterval(sub.poll_interval_seconds)}
                        </button>
                      )}
                    </td>
                    <td style={{ fontSize: 11 }}>
                      {fmtAge(sub.last_polled_at)}
                      {sub.last_synced_at && (
                        <div style={{ color: 'var(--text-dim)', fontSize: 10 }}>
                          wrote {fmtAge(sub.last_synced_at)}
                        </div>
                      )}
                    </td>
                    <td style={{ display: 'flex', gap: 4, flexWrap: 'wrap' }}>
                      <button
                        type="button" onClick={() => void toggleEnabled(sub)}
                        disabled={busy} style={{ fontSize: 11 }}
                        title={sub.enabled ? 'Pause the worker for this subscription.' : 'Start polling on the configured interval.'}
                      >
                        {sub.enabled ? 'Pause' : 'Enable'}
                      </button>
                      <button
                        type="button" onClick={() => void runNow(sub)}
                        disabled={busy} style={{ fontSize: 11 }}
                        title="Force an immediate reconcile of this subscription. Bypasses the next-tick wait. Dry-run subscriptions still only log intents; only Real-writes subs issue writes."
                      >
                        Run now
                      </button>
                      <button
                        type="button" onClick={() => void toggleDryRun(sub)}
                        disabled={busy} style={{ fontSize: 11 }}
                        title={sub.dry_run ? 'Switch to real writes. Worker stops dry-running and starts issuing exact-target writes.' : 'Switch back to dry-run. Worker logs intents but stops writing.'}
                      >
                        {sub.dry_run ? 'Real writes' : 'Dry-run'}
                      </button>
                      <button
                        type="button" onClick={() => void viewWrites(sub)}
                        disabled={busy} style={{ fontSize: 11 }}
                      >
                        {openWritesFor === sub.id ? 'Hide log' : 'View log'}
                      </button>
                      <button
                        type="button" onClick={() => void openEditScope(sub)}
                        disabled={busy} style={{ fontSize: 11 }}
                        title="Edit which users this subscription syncs. Owner-only / All users / pick specific users."
                      >
                        {editScopeFor === sub.id ? 'Close scope' : 'Edit scope'}
                      </button>
                      <button
                        type="button" className="danger"
                        onClick={() => void deleteSub(sub)}
                        disabled={busy} style={{ fontSize: 11 }}
                      >
                        Delete
                      </button>
                    </td>
                  </tr>
                  {editScopeFor === sub.id && (
                    <tr key={`editscope-${sub.id}`}>
                      <td colSpan={9}>
                        <div style={{ padding: 12, borderTop: '1px solid var(--border, rgba(255,255,255,0.08))' }}>
                          <div style={{ display: 'flex', gap: 12, alignItems: 'center', marginBottom: 8 }}>
                            <strong style={{ fontSize: 13 }}>
                              Edit user scope for subscription #{sub.id}
                            </strong>
                            <label style={{ display: 'flex', alignItems: 'center', gap: 4, fontSize: 12 }}>
                              <span>Scope:</span>
                              <select
                                value={editScope}
                                onChange={(e) => setEditScope(e.target.value as UserScope)}
                              >
                                <option value="owner">Owner only</option>
                                <option value="all">All users</option>
                                <option value="specific">Specific users…</option>
                              </select>
                            </label>
                            <button
                              type="button"
                              className="primary"
                              onClick={() => void saveEditScope(sub)}
                              disabled={busy}
                              style={{ fontSize: 11, marginLeft: 'auto' }}
                            >
                              Save scope
                            </button>
                            <button
                              type="button"
                              onClick={() => setEditScopeFor(null)}
                              disabled={busy}
                              style={{ fontSize: 11 }}
                            >
                              Cancel
                            </button>
                          </div>
                          {editScope === 'specific' && (
                            <>
                              <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6 }}>
                                <span style={{ fontSize: 12 }}>
                                  Pick users to sync (source: {serverLabel(sub.source_server_id)}):
                                </span>
                                {editScopeUsersLoading && (
                                  <span style={{ fontSize: 11, color: 'var(--text-dim)' }}>Loading…</span>
                                )}
                                {editScopeUsers.length > 0 && (
                                  <span style={{ marginLeft: 'auto', display: 'flex', gap: 6 }}>
                                    <button
                                      type="button"
                                      style={{ fontSize: 10, padding: '2px 6px' }}
                                      onClick={() => setEditFilter(editScopeUsers.map((u) => u.raw_name))}
                                    >
                                      Select all
                                    </button>
                                    <button
                                      type="button"
                                      style={{ fontSize: 10, padding: '2px 6px' }}
                                      onClick={() => setEditFilter([])}
                                    >
                                      Clear
                                    </button>
                                  </span>
                                )}
                              </div>
                              {editScopeUsersError && (
                                <div className="banner info" style={{ marginBottom: 8, fontSize: 11 }}>
                                  {editScopeUsersError}
                                </div>
                              )}
                              {editScopeUsers.length === 0 && !editScopeUsersLoading ? (
                                <div className="empty" style={{ fontSize: 12 }}>
                                  No users captured for this server. Try{' '}
                                  <strong>Servers &rsaquo; Refresh users</strong>.
                                </div>
                              ) : (
                                <UserFilterCheckboxGrid
                                  users={editScopeUsers}
                                  selected={editFilter}
                                  onChange={setEditFilter}
                                />
                              )}
                            </>
                          )}
                        </div>
                      </td>
                    </tr>
                  )}
                  {openWritesFor === sub.id && (
                    <tr key={`writes-${sub.id}`}>
                      <td colSpan={9}>
                        <div style={{ padding: 8, fontSize: 11 }}>
                          <strong>Recent writes (most recent first):</strong>
                          {writes.length === 0 ? (
                            <div className="empty" style={{ fontSize: 11, marginTop: 6 }}>
                              No writes recorded yet. Worker runs at the configured poll interval;
                              dry-run subscriptions still log intents here.
                            </div>
                          ) : (
                            <table className="list" style={{ width: '100%', marginTop: 6 }}>
                              <thead>
                                <tr>
                                  <th>When</th><th>Type</th>
                                  <th>Item</th><th>User</th>
                                  <th>Before → After</th>
                                  <th>Status</th>
                                  <th>Error</th>
                                </tr>
                              </thead>
                              <tbody>
                                {writes.map((w) => (
                                  <tr key={w.id}>
                                    <td>{fmtAge(w.written_at)}</td>
                                    <td>{w.sync_type}</td>
                                    <td>
                                      <code style={{ fontSize: 10 }}>
                                        {w.target_item_rating_key}
                                      </code>
                                    </td>
                                    <td>{w.target_user_id || '(owner)'}</td>
                                    <td>
                                      {w.before_value !== null && w.after_value !== null
                                        ? `${w.before_value} → ${w.after_value}`
                                        : '—'}
                                    </td>
                                    <td>
                                      <span className={`tag ${w.issued ? 'good' : w.error ? 'failed' : 'phase'}`}
                                        style={{ fontSize: 10 }}>
                                        {w.issued ? 'wrote' : w.error ? 'failed' : 'intent only'}
                                      </span>
                                    </td>
                                    <td style={{ color: 'var(--text-dim)' }}>{w.error || ''}</td>
                                  </tr>
                                ))}
                              </tbody>
                            </table>
                          )}
                        </div>
                      </td>
                    </tr>
                  )}
                </Fragment>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
