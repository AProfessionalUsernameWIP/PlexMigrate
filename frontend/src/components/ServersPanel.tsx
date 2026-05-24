// Server management tab.
//
// Server list and Add Server form:
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
import type { PinMigrationSuggestion, ServerManagedUser } from '../api';
import { usePermission } from '../hooks/usePermission';
import { pausableInterval } from '../utils/pausableInterval';
import { PinMigrationModal } from './PinMigrationModal';
import { RecentRuntimesPanel } from './RecentRuntimesPanel';
import { DriftHistoryTab } from './DriftHistoryTab';
import { FirstRunMirrorDialog } from './FirstRunMirrorDialog';
import { ServerMirrorBadge, type ServerMirrorStateRow } from './ServerMirrorBadge';
import { LogsPanel } from './LogsPanel';
import { useConfirm } from './ConfirmModal';
import { errorText } from '../utils/format';
import {
  BackendTabStrip,
  BackendType,
  backendCounts,
  serversForBackend,
} from './BackendTabStrip';

// Default poll cadence for the live status indicator, in milliseconds.
// The actual cadence is loaded from settings.tunables.frontend_server_ping_interval_ms
// at mount time and applied to the polling interval; saves to the
// tunable take effect on the next ServersPanel mount.
const PING_INTERVAL_MS_DEFAULT = 30_000;

export function ServersPanel() {
  // Viewers / end users / managers see this panel read-only. Add /
  // Edit / Remove buttons are hidden when ``servers.edit`` is absent.
  // The backend (require_role('root_admin') on the CRUD routes) is
  // the authoritative gate.
  const canEditServers = usePermission('servers.edit');

  const [servers, setServers] = useState<ServerView[]>([]);
  const [error, setError] = useState<string | null>(null);
  // Top-level sub-tab routing inside the Servers page. Each tab
  // scopes which panel renders below; the tabbar itself stays
  // visible across all tabs so the user can pivot without scrolling.
  // The tabs - Overview (registered Plex servers + add/edit
  // controls), Library Catalogues, Server Users, and Recent
  // Runtimes - organise the surface without hiding any information.
  const [activeTab, setActiveTab] = useState<
    'overview' | 'libraries' | 'users' | 'runtimes' | 'drift' | 'logs'
  >('overview');

  // First-run mirror prompt. Lives inline at the top of the Overview
  // tab (not a modal; belongs with the server registration surface
  // since the mirror is server-scoped, not a job). Gated on the
  // ``engine_mirror_first_run_dialog_seen`` tunable; either button
  // flips that flag to true so the panel never re-renders.
  const [firstRunMirrorOpen, setFirstRunMirrorOpen] = useState<boolean>(false);
  // Global mirror mode default; ServerMirrorBadge uses this to label
  // "Use global (auto)" / "Use global (always-live)" correctly.
  const [globalMirrorMode, setGlobalMirrorMode] = useState<'auto' | 'always-live'>('auto');
  useEffect(() => {
    let cancelled = false;
    api.getSettings()
      .then((s) => {
        if (cancelled) return;
        const seen = !!(s.tunables
          && s.tunables.engine_mirror_first_run_dialog_seen);
        if (!seen) setFirstRunMirrorOpen(true);
        const mode = (s.tunables
          && s.tunables.engine_mirror_mode) as
          | 'auto' | 'always-live' | undefined;
        if (mode === 'always-live') setGlobalMirrorMode('always-live');
      })
      .catch(() => {
        // Best-effort: a failure to probe means the panel stays
        // closed this session. It will re-evaluate on next mount.
      });
    return () => { cancelled = true; };
  }, []);

  // Per-server mirror state for the badge column. Indexed by
  // server_id; missing entries render a "no mirror state yet" badge.
  const [mirrorState, setMirrorState] = useState<
    Record<string, ServerMirrorStateRow>
  >({});
  const refreshMirrorState = async () => {
    try {
      const r = await api.getServerMirrorState();
      const next: Record<string, ServerMirrorStateRow> = {};
      for (const row of r.servers) next[row.server_id] = row;
      setMirrorState(next);
    } catch {
      // Best-effort: failure means badge column renders empty.
    }
  };
  useEffect(() => {
    void refreshMirrorState();
  }, []);

  // Collection-children cache stats. Per-server map of cached
  // collection counts + freshness so the operator can verify cache
  // is populating and force-invalidate when retesting.
  const [collectionCacheState, setCollectionCacheState] = useState<
    Record<string, {
      collections: number;
      items: number;
      last_fetched_at: number;
    }>
  >({});
  const refreshCollectionCacheState = async () => {
    try {
      const r = await api.getCollectionCacheStatus();
      const next: Record<string, {
        collections: number; items: number; last_fetched_at: number;
      }> = {};
      for (const row of r.servers) {
        next[row.server_id] = {
          collections: row.collections,
          items: row.items,
          last_fetched_at: row.last_fetched_at,
        };
      }
      setCollectionCacheState(next);
    } catch {
      // Best-effort.
    }
  };
  useEffect(() => {
    void refreshCollectionCacheState();
  }, []);
  const [busyCollectionCacheId, setBusyCollectionCacheId] = useState<
    string | null
  >(null);
  const confirm = useConfirm();
  const clearCollectionCacheForServer = async (serverId: string) => {
    if (!(await confirm({
      body:
        `Clear the collection-children cache for ${serverId}? The next ` +
        `snapshot of this server will re-fetch every collection's ` +
        `children from Plex (slow first run; fast subsequent runs).`,
      danger: true,
    }))) return;
    setBusyCollectionCacheId(serverId);
    try {
      await api.invalidateCollectionCache(serverId);
      await refreshCollectionCacheState();
    } finally {
      setBusyCollectionCacheId(null);
    }
  };
  const buildCollectionCacheForServer = async (serverId: string) => {
    setBusyCollectionCacheId(serverId);
    setCacheNotice(null);
    try {
      const result = await api.warmCollectionCache(serverId);
      await refreshCollectionCacheState();
      const libs = (result.libraries || [])
        .map((l) => `${l.name}: ${l.collections} cached (${l.errors} error)`)
        .join('; ');
      // FECORE-15: surface success inline instead of window.alert.
      setCacheNotice({
        kind: 'good',
        message:
          `Collection cache built for ${serverId}: ` +
          `${result.total_collections} collection(s), ` +
          `${result.total_items} item(s) in ${result.elapsed_seconds.toFixed(1)}s. ` +
          `Per library — ${libs || '(no libraries)'}.`,
      });
    } catch (e) {
      // FECORE-15: surface failure inline instead of window.alert.
      setCacheNotice({
        kind: 'error',
        message: `Build cache failed for ${serverId}: ${(e as Error).message}`,
      });
    } finally {
      setBusyCollectionCacheId(null);
    }
  };

  // Bulk action: warm one or both caches for every registered server
  // in one click. Operator picks which cache types to build via the
  // checkboxes next to the button; the label updates to reflect the
  // selection. Per-server failures (with reasons) are surfaced in the
  // result panel below the button so the operator can see WHICH server
  // failed and WHY, instead of a single opaque alert.
  const [busyAllCaches, setBusyAllCaches] = useState<boolean>(false);
  const [buildCollections, setBuildCollections] = useState<boolean>(true);
  const [buildPlaylists, setBuildPlaylists] = useState<boolean>(true);
  // Per-server failure rows from the last bulk build. Cleared on the
  // next click. Each row carries the cache type so the panel can
  // explain "Plex-A failed for playlists because <reason>".
  type BulkFailure = {
    server_id: string;
    cache_type: 'collections' | 'playlists';
    reason: string;
  };
  const [bulkFailures, setBulkFailures] = useState<BulkFailure[]>([]);
  // Per-server success rows: shown as a compact list under the button
  // so the operator can see which servers warmed and how much.
  type BulkSuccess = {
    server_id: string;
    cache_type: 'collections' | 'playlists';
    note: string;
  };
  const [bulkSuccesses, setBulkSuccesses] = useState<BulkSuccess[]>([]);
  // Wall-clock + headline counts from the last bulk run. Rendered
  // above the per-server rows.
  const [bulkSummary, setBulkSummary] = useState<string | null>(null);

  const bulkButtonLabel = (busy: boolean): string => {
    if (busy) return 'Building caches…';
    const parts: string[] = [];
    if (buildCollections) parts.push('Collections');
    if (buildPlaylists) parts.push('Playlists');
    if (parts.length === 0) return 'Build caches (pick at least one)';
    return `Build ${parts.join(' + ')}`;
  };

  // Global Sync mirrors button. Companion to the per-row Sync Now
  // buttons in ServerMirrorBadge. The endpoint launches one
  // background walker per registered server (each runs independently
  // - a slow server doesn't gate the rest). Per-server status rows
  // let us surface a failure list inline so a server that can't
  // connect doesn't quietly disappear.
  const [busyAllSync, setBusyAllSync] = useState<boolean>(false);
  type SyncFailure = { server_id: string; reason: string };
  const [syncFailures, setSyncFailures] = useState<SyncFailure[]>([]);
  const [syncSummary, setSyncSummary] = useState<string | null>(null);
  const syncAllMirrors = async () => {
    if (!(await confirm({
      body:
        'Launch a background mirror sync for every registered server? ' +
        'Each server walks its libraries via the adapter and writes the ' +
        'result to server_mirror.db. Per-server walkers run independently; ' +
        'one slow server does not gate the rest.',
    }))) return;
    setBusyAllSync(true);
    setSyncFailures([]);
    setSyncSummary(null);
    try {
      const r = await api.syncAllServerMirrors();
      const failures: SyncFailure[] = [];
      let started = 0;
      let already = 0;
      for (const row of r.results) {
        if (row.status === 'error') {
          failures.push({
            server_id: row.server_id,
            reason: row.error || 'unknown',
          });
        } else if (row.status === 'in_progress') {
          already += 1;
        } else {
          started += 1;
        }
      }
      setSyncFailures(failures);
      setSyncSummary(
        `${started} walker(s) launched, ${already} already in progress, ${failures.length} failed to launch. ` +
        'Badges will update as each walker finishes.',
      );
      // Refresh the per-row mirror state several times so the operator
      // sees ``last_full_sync_at`` flip as walkers finish. Cadence
      // matches the per-row poll in ServerMirrorBadge.doSync.
      let i = 0;
      let stopPoll: (() => void) | null = null;
      stopPoll = pausableInterval(() => {
        void refreshMirrorState();
        i += 1;
        if (i >= 10) stopPoll?.();
      }, 3000);
    } catch (e) {
      setSyncFailures([{
        server_id: '(all servers)',
        reason: `endpoint failed: ${(e as Error).message}`,
      }]);
    } finally {
      setBusyAllSync(false);
    }
  };

  const buildAllCaches = async () => {
    if (!buildCollections && !buildPlaylists) {
      // FECORE-15: inline guard notice instead of window.alert.
      setCacheNotice({
        kind: 'error',
        message: 'Pick at least one cache type to build.',
      });
      return;
    }
    setCacheNotice(null);
    const parts: string[] = [];
    if (buildCollections) parts.push('collection-children');
    if (buildPlaylists) parts.push('playlist');
    if (!(await confirm({
      body:
        `Warm the ${parts.join(' + ')} cache for ALL registered servers? ` +
        `Each server walks its libraries (collections) or fetches every ` +
        `user's playlists. First-run cost can be minutes per server on ` +
        `large libraries; subsequent runs are fast because the cache is ` +
        `populated.`,
    }))) return;
    setBusyAllCaches(true);
    setBulkFailures([]);
    setBulkSuccesses([]);
    setBulkSummary(null);
    const failures: BulkFailure[] = [];
    const successes: BulkSuccess[] = [];
    let collectionsTotal = 0;
    let playlistsTotal = 0;
    let serversWarmed = 0;
    let serversFailed = 0;
    let elapsed = 0;
    try {
      // Run both bulk endpoints in parallel so total wall-clock is
      // max(collections, playlists), not the sum.
      const collectionsPromise = buildCollections
        ? api.warmAllCollectionCaches()
        : Promise.resolve(null);
      const playlistsPromise = buildPlaylists
        ? api.warmAllPlaylistCaches('servers-bulk')
        : Promise.resolve(null);
      const [collResult, plResult] = await Promise.all([
        collectionsPromise.catch((e) => {
          // Top-level endpoint failure (e.g. auth) — surface as a
          // single failure row rather than a generic toast.
          failures.push({
            server_id: '(all servers)',
            cache_type: 'collections',
            reason: `endpoint failed: ${(e as Error).message}`,
          });
          return null;
        }),
        playlistsPromise.catch((e) => {
          failures.push({
            server_id: '(all servers)',
            cache_type: 'playlists',
            reason: `endpoint failed: ${(e as Error).message}`,
          });
          return null;
        }),
      ]);
      if (collResult) {
        collectionsTotal = collResult.total_collections;
        serversWarmed += collResult.servers_warmed;
        serversFailed += collResult.servers_failed;
        elapsed = Math.max(elapsed, collResult.elapsed_seconds);
        for (const r of collResult.results) {
          if (r.error) {
            failures.push({
              server_id: r.server_id,
              cache_type: 'collections',
              reason: r.error,
            });
          } else {
            successes.push({
              server_id: r.server_id,
              cache_type: 'collections',
              note: `${r.total_collections ?? 0} collection(s), ${r.total_items ?? 0} item(s)`,
            });
          }
        }
      }
      if (plResult) {
        playlistsTotal = plResult.total_playlists;
        serversWarmed += plResult.servers_warmed;
        serversFailed += plResult.servers_failed;
        elapsed = Math.max(elapsed, plResult.elapsed_seconds);
        for (const r of plResult.results) {
          if (r.error) {
            failures.push({
              server_id: r.server_id,
              cache_type: 'playlists',
              reason: r.error,
            });
          } else {
            const perUserErrors = r.per_user_errors ?? 0;
            const note = perUserErrors > 0
              ? `${r.playlists_count ?? 0} playlist(s), ${r.items_count ?? 0} item(s); ${perUserErrors} per-user error(s)`
              : `${r.playlists_count ?? 0} playlist(s), ${r.items_count ?? 0} item(s)`;
            successes.push({
              server_id: r.server_id,
              cache_type: 'playlists',
              note,
            });
          }
        }
      }
      await refreshCollectionCacheState();
      const summary = [
        `${serversWarmed} server-pass(es) warmed`,
        `${serversFailed} failed`,
        ...(buildCollections ? [`${collectionsTotal} collection(s)`] : []),
        ...(buildPlaylists ? [`${playlistsTotal} playlist(s)`] : []),
        `in ${elapsed.toFixed(1)}s`,
      ].join(', ');
      setBulkSummary(summary);
      setBulkFailures(failures);
      setBulkSuccesses(successes);
    } finally {
      setBusyAllCaches(false);
    }
  };
  // One backend tier shared across all four sub-tabs so the user
  // stays in the chosen backend's context as they pivot. Defaults to
  // 'plex' since every install today has at least one Plex server.
  const [activeBackend, setActiveBackend] = useState<BackendType>('plex');

  // Auto-correct activeBackend when it points at an empty backend
  // and another backend has servers. Fires after the server list
  // refreshes (e.g. user deleted the last Plex server while on the
  // Plex tab). Without this, the panel would render its empty state
  // forever even though Jellyfin servers are registered.
  useEffect(() => {
    if (servers.length === 0) return;
    const counts = backendCounts(servers);
    if (counts[activeBackend] === 0) {
      const fallback = (['plex', 'jellyfin', 'emby'] as BackendType[])
        .find((b) => counts[b] > 0);
      if (fallback) setActiveBackend(fallback);
    }
  }, [servers, activeBackend]);

  // Derived: the registered-server list filtered to the active backend.
  // Used by the Overview table + passed down to per-backend sub-panels.
  const backendServers = serversForBackend(servers, activeBackend);
  // User-facing label for the active backend's panel heading.
  const backendLabel = activeBackend === 'plex' ? 'Plex'
    : activeBackend === 'jellyfin' ? 'Jellyfin'
    : 'Emby';
  const [editing, setEditing] = useState<ServerView | 'new' | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  // Tracks which servers have a playlist-cache warm in flight (kicked
  // off as a fire-and-forget side effect of Refresh). The cache warm
  // can take 10-30s for a multi-user Plex Home, so we surface a tiny
  // "warming cache..." chip on the row while it runs rather than block
  // the Refresh button. Cleared when the bulk refresh promise settles.
  const [cacheWarmingIds, setCacheWarmingIds] = useState<Set<string>>(new Set());
  // Last cascade summary from a successful delete. Rendered as a
  // green toast banner at the top of the panel and dismissed by the
  // user (or by the next action).
  const [cascadeToast, setCascadeToast] = useState<ServerDeleteSummary | null>(null);
  // Refresh-button token-capture toast. Cleared by Dismiss or by a
  // subsequent refresh that does no work (the toast only renders when
  // there's something worth saying).
  const [refreshToast, setRefreshToast] = useState<{ name: string; message: string } | null>(null);
  // FECORE-15: inline banner for the per-server cache-build outcome
  // and the "pick a cache type" guard. Replaces the three window.alert
  // calls so these paths surface results the same way the bulk paths
  // do (bulkSummary / bulkFailures), not via a native modal.
  const [cacheNotice, setCacheNotice] = useState<
    { kind: 'good' | 'error'; message: string } | null
  >(null);

  // Cross-server PIN migration modal state. Triggered after
  // Add-Server and Refresh-Server flows discover an overlap between
  // this server's managed users and another server's stored PINs.
  const [pinMigration, setPinMigration] = useState<
    | null
    | { serverId: string; serverName: string; suggestions: PinMigrationSuggestion[] }
  >(null);

  // Probe the suggestions endpoint and open the modal if non-empty.
  // Failure is silent: the prompt is a nicety, not a blocking
  // requirement, and the user can still capture PINs manually.
  const probeAndMaybeOpenPinMigration = async (serverId: string, serverName: string) => {
    try {
      const res = await api.pinMigrationSuggestions(serverId);
      if (res.suggestions && res.suggestions.length > 0) {
        setPinMigration({ serverId, serverName, suggestions: res.suggestions });
      }
    } catch {
      /* role likely insufficient; nothing to show */
    }
  };

  // Live ping state, keyed by server id. We keep this *separate* from
  // ``servers`` so a ping update can happen without re-rendering the
  // whole list row (which would briefly flash if we patched the row
  // dict directly). Each value is the most recent PingResult or
  // ``undefined`` if no ping has come back yet for that id.
  const [pings, setPings] = useState<Record<string, PingResult>>({});

  // Per-server user lists fetched on tab visit. Keyed by server id.
  // ``undefined`` = not yet fetched / refetching;
  // resolved values may carry a non-null ``error`` when systemAccounts
  // failed but the owner row is still there. The ``error`` shape with
  // a single ``message`` field flags total fetch failures (connect
  // refused / 502) so the panel can render a recoverable inline error.
  const [users, setUsers] = useState<Record<string, ServerUsersResponse | { error: string }>>({});

  // Self-firing sync. When the Servers page mounts (or the server
  // list changes), we diff the live user list against what's in the
  // local managed_users DB. New users surface as dismissible toasts
  // at the top of the panel and a background sync runs to bring the
  // DB up to date so the JobFormPanel picker reflects the current
  // state on the next visit.
  const [syncToasts, setSyncToasts] = useState<
    { id: string; server_name: string; added: string[] }[]
  >([]);
  // In-flight sync guard. A ref-based Set lets the effect re-run on
  // every ``servers`` change without stacking duplicate sync calls
  // for the same server (which would hammer Plex with parallel
  // ``systemAccounts()`` calls). Cleared when the sync promise
  // settles (success or failure).
  const inFlightSyncsRef = useRef<Set<string>>(new Set());

  // Track the polling timer so we can clear it on unmount.
  const pollTimerRef = useRef<(() => void) | null>(null);

  // Live ping cadence. Loaded once from settings on mount so a user
  // who bumps it via Settings ▸ Tunables doesn't need a code change.
  // A panel re-mount picks up subsequent changes; the effect that arms
  // the timer below depends on this value, so a new cadence rearms
  // automatically once the load completes.
  const [pingIntervalMs, setPingIntervalMs] = useState<number>(PING_INTERVAL_MS_DEFAULT);
  // Optional UID column toggle. Reads `servers_panel_show_server_uid`
  // (developer tunable). Off by default - flip via Settings ▸
  // Tunables ▸ UI & Display ▸ Admin & UX toggles.
  const [showServerUid, setShowServerUid] = useState(false);
  useEffect(() => {
    api.getSettings()
      .then((s) => {
        const raw = (s as unknown as Record<string, unknown>).tunables;
        const tunables = raw && typeof raw === 'object' ? (raw as Record<string, unknown>) : {};
        const v = tunables['frontend_server_ping_interval_ms'];
        if (typeof v === 'number' && v >= 1000) {
          setPingIntervalMs(v);
        }
        const uidFlag = tunables['servers_panel_show_server_uid'];
        if (typeof uidFlag === 'boolean') {
          setShowServerUid(uidFlag);
        }
      })
      .catch(() => { /* non-fatal - fall back to the default */ });
  }, []);

  const refresh = async () => {
    try {
      const next = await api.listServers();
      // Defensive: only commit the new list when it's non-empty OR
      // when we KNOW the registry is genuinely empty (no servers
      // existed before either). A transient empty response - e.g.
      // because the parallel playlist refresh briefly tied up the
      // registry read - would otherwise wipe every row from the UI
      // until a hard reload.
      setServers((prev) => {
        if (next.length === 0 && prev.length > 0) {
          // Suspicious - keep the prior list rather than blank the UI.
          // The next refresh tick repopulates correctly.
          // eslint-disable-next-line no-console
          console.warn(
            'ServersPanel.refresh: listServers returned empty but prior '
            + 'state had %d server(s); keeping prior state.',
            prev.length,
          );
          return prev;
        }
        return next;
      });
    } catch (e) {
      setError(String(e));
    }
  };

  // Initial load + start the background ping poll.
  useEffect(() => {
    refresh();
    return () => {
      if (pollTimerRef.current !== null) {
        pollTimerRef.current();
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
        pollTimerRef.current();
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
    if (pollTimerRef.current !== null) pollTimerRef.current();
    pollTimerRef.current = pausableInterval(pingAll, pingIntervalMs);

    return () => {
      if (pollTimerRef.current !== null) {
        pollTimerRef.current();
        pollTimerRef.current = null;
      }
    };
    // Re-arm when the server list changes (the closure binds the
    // current list) or when the end user changes the ping cadence
    // via Settings ▸ Tunables.
  }, [servers, pingIntervalMs]);

  // Fetch per-server user lists in parallel whenever the server list
  // changes. No caching - the user list on a Plex server can change
  // at any time (add/remove managed users), so a stale cache would
  // mislead. One round-trip per registered server per Servers tab
  // visit is acceptable; if performance becomes a concern with many
  // servers, add a short TTL later.
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
      // JobFormPanel reads.
      if (liveRes.status === 'fulfilled') {
        setUsers((prev) => ({ ...prev, [s.id]: liveRes.value }));
      } else {
        setUsers((prev) => ({ ...prev, [s.id]: { error: String(liveRes.reason) } }));
      }
      // Diff. If the live API surfaced users that aren't in the local
      // managed_users DB (visible OR hidden), fire a background sync
      // and queue a toast for the user. Only "additions" trigger this
      // - removals never auto-fire deletes (additive-only rule from
      // clp.md). The DB call failing is non-fatal; we just skip the
      // diff and let the next visit retry.
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
          // Concurrency guard - skip if a sync for this server is
          // already in flight. The diff effect re-runs whenever
          // ``servers`` changes (test, refresh, edit) and without
          // this guard each re-run would stack another background
          // ``systemAccounts()`` call against Plex.
          if (!inFlightSyncsRef.current.has(s.id)) {
            inFlightSyncsRef.current.add(s.id);
            api.syncServerManagedUsers(s.id)
              .catch(() => {
                /* user can hit the manual sync button if this fails */
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
      const result = await api.testServer(id);
      await refresh();
      // Surface the token-capture sweep result. captured > 0 means
      // new tokens landed; throttled true means the user clicked
      // Refresh faster than the throttle allows (rare, since Refresh
      // bypasses the throttle by design); errors get a quiet mention
      // but don't fail the refresh.
      const cap = result?.token_capture;
      if (cap && (cap.captured > 0 || cap.errors.length > 0)) {
        const name = result?.name ?? 'server';
        const parts: string[] = [];
        if (cap.captured > 0) parts.push(`captured ${cap.captured} new user token(s)`);
        if (cap.skipped_existing > 0) parts.push(`${cap.skipped_existing} already stored`);
        if (cap.errors.length > 0) parts.push(`${cap.errors.length} error(s) - see runtime.log`);
        setRefreshToast({ name, message: parts.join('; ') });
      }
      // After a refresh, also probe for cross-server PIN overlaps.
      // The refresh might have discovered a new managed user that
      // matches a stored PIN on another server.
      const serverName = (result && result.name) || id;
      void probeAndMaybeOpenPinMigration(id, serverName);
      // The playlist-cache warm fires AFTER the synchronous
      // testServer + listServers refresh completes, NOT in parallel
      // with it. Running it in parallel contends with the registry
      // sync and can surface as an empty server list until the page
      // is reloaded. The bulk refresh runs strictly in the background
      // once the server-list state is stable.
      setCacheWarmingIds((prev) => new Set(prev).add(id));
      void api.playlistMgmtRefreshServer(id, 'servers-refresh')
        .catch(() => {
          /* best-effort - failures are visible in Settings ▸ Application Logs ▸ Playlist Cache */
        })
        .finally(() => {
          setCacheWarmingIds((prev) => {
            const next = new Set(prev);
            next.delete(id);
            return next;
          });
        });
    } catch (e) {
      setError(String(e));
    } finally {
      setBusyId(null);
    }
  };

  const remove = async (s: ServerView) => {
    // Fetch the cascade preview first so the confirmation dialog can
    // show concrete counts. If the preview call itself fails (e.g.
    // server is mid-cascade by another request), fall back to a
    // text-only prompt so the user can still cancel the action
    // safely.
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
    if (!(await confirm({
      body:
        `Delete server "${s.name}" and everything attributable to it?\n\n` +
        previewLine +
        'This cannot be undone.',
      danger: true,
    }))) return;
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

      {/* FECORE-15: inline banner for per-server cache-build outcomes
          and the cache-type guard (formerly window.alert calls). */}
      {cacheNotice && (
        <div
          className={`banner ${cacheNotice.kind}`}
          style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', gap: 12 }}
        >
          <div>{cacheNotice.message}</div>
          <button onClick={() => setCacheNotice(null)} style={{ flexShrink: 0 }}>Dismiss</button>
        </div>
      )}

      {refreshToast && (
        <div
          className="banner good"
          style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', gap: 12 }}
        >
          <div>
            <strong>Refreshed {refreshToast.name}.</strong>{' '}
            {refreshToast.message}.
          </div>
          <button onClick={() => setRefreshToast(null)} style={{ flexShrink: 0 }}>Dismiss</button>
        </div>
      )}

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

      {/* New-user detection toasts. One per server, queued by the
          diff effect above. Background sync has already fired by the
          time this renders; the toast is purely informational. */}
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

      {/* Top-level sub-tab nav. Stays visible regardless of which tab
          is active so a single click switches surface without
          scrolling. The Overview tab carries the registered-server
          table + add/edit controls; the other tabs carry their own
          panels. */}
      <nav className="tabs sub-tabs" style={{ marginBottom: 8 }}>
        <button
          className={activeTab === 'overview' ? 'active' : ''}
          onClick={() => setActiveTab('overview')}
        >
          Overview
        </button>
        <button
          className={activeTab === 'libraries' ? 'active' : ''}
          onClick={() => setActiveTab('libraries')}
          disabled={servers.length === 0}
          title={servers.length === 0 ? 'Register a server first.' : undefined}
        >
          Library Catalogues
        </button>
        <button
          className={activeTab === 'users' ? 'active' : ''}
          onClick={() => setActiveTab('users')}
          disabled={servers.length === 0}
          title={servers.length === 0 ? 'Register a server first.' : undefined}
        >
          Server Users
        </button>
        <button
          className={activeTab === 'runtimes' ? 'active' : ''}
          onClick={() => setActiveTab('runtimes')}
          disabled={servers.length === 0}
          title={servers.length === 0 ? 'Register a server first.' : undefined}
        >
          Recent Runtimes
        </button>
        {/* Drift History sub-tab. Lists drift_events rows newest
            first; operators use this to audit cache freshness. */}
        <button
          className={activeTab === 'drift' ? 'active' : ''}
          onClick={() => setActiveTab('drift')}
          disabled={servers.length === 0}
          title={servers.length === 0 ? 'Register a server first.' : undefined}
        >
          Drift History
        </button>
        {/* Per-run job logs. The inner view restructures the run
            list into per-server sub-tabs + a Direct Transfers tab
            instead of a per-backend filter. The same panel also
            mounts under Settings > Logs for muscle-memory
            continuity. */}
        <button
          className={activeTab === 'logs' ? 'active' : ''}
          onClick={() => setActiveTab('logs')}
          title="Per-server job logs. Sub-tabs let you pick a server; the 'Direct Transfers' view surfaces server-to-server runs under either participating server."
        >
          Logs
        </button>
        {/* Library Mapping lives on the top-level Server Syncing tab,
            alongside the Sync Subscriptions UI. */}
      </nav>

      {activeTab === 'drift' && (
        <DriftHistoryTab />
      )}

      {activeTab === 'logs' && (
        <LogsPanel />
      )}

      {activeTab === 'overview' && (
        <FirstRunMirrorDialog
          open={firstRunMirrorOpen}
          onClose={(decision) => {
            setFirstRunMirrorOpen(false);
            void api.saveSettings({
              tunables: {
                engine_mirror_first_run_dialog_seen: 1,
              } as Record<string, number | string>,
            }).catch(() => {
              // A save failure means the panel re-shows next mount,
              // which is acceptable.
            });
            void decision;
          }}
        />
      )}

      {activeTab === 'overview' && (
      <div className="banner info">
        <strong>Threading note:</strong> Every registered server adds API load when used.
        Running transfers to or from multiple servers at the same time multiplies that load
        across all involved servers. Schedule recurring exports at staggered times rather
        than the same minute, and see the README's <em>Understanding Performance and Threading</em>
        section for guidance on worker counts.
      </div>
      )}

      {/* Global mirror sync button. Companion to the per-row Sync Now
          in ServerMirrorBadge. Each per-server walker launches
          independently in a daemon thread; per-server status
          (started / in_progress / error) lands in the failure list
          so a server that can't connect doesn't quietly disappear. */}
      {activeTab === 'overview' && servers.length > 0 && canEditServers && (
      <div
        className="panel"
        style={{ marginBottom: 12 }}
      >
        <div style={{ display: 'flex', gap: 10, alignItems: 'center', flexWrap: 'wrap' }}>
          <strong style={{ marginRight: 4 }}>Mirror:</strong>
          <button
            type="button"
            onClick={() => { void syncAllMirrors(); }}
            disabled={busyAllSync}
            title={
              "Launch a background mirror walker for every " +
              "registered server. Each walker is independent — a slow " +
              "server does not gate the rest. Per-row badges update " +
              "as each walker finishes (badge auto-refreshes for ~30s)."
            }
          >
            {busyAllSync ? 'Launching walkers…' : 'Sync all mirrors'}
          </button>
          <span className="help" style={{ flex: 1, minWidth: 240 }}>
            Each per-row badge in the table below also has its own
            Sync Now control with the same single-flight semantics.
          </span>
        </div>
        {(syncSummary || syncFailures.length > 0) && (
          <div style={{ marginTop: 10 }}>
            {syncSummary && (
              <div style={{ marginBottom: 6 }}>
                <strong>Last sync launch:</strong> {syncSummary}
              </div>
            )}
            {syncFailures.length > 0 && (
              <div className="banner error" style={{ padding: 8 }}>
                <strong>Failed to launch:</strong>
                <ul style={{ margin: '4px 0 0 16px', padding: 0 }}>
                  {syncFailures.map((f, i) => (
                    <li key={`${f.server_id}-${i}`}>
                      <code>{f.server_id}</code>: {f.reason}
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </div>
        )}
      </div>
      )}

      {/* Bulk cache control bar - parity with the per-row controls in
          the table below. The operator picks which cache types to
          (re)build via the two checkboxes; the button label updates
          to reflect the selection. Per-server failures land in the
          result panel below the button with a reason string so the
          operator can see WHICH server failed and WHY, rather than a
          single opaque alert. */}
      {activeTab === 'overview' && servers.length > 0 && canEditServers && (
      <div
        className="panel"
        style={{ marginBottom: 12 }}
      >
        <div
          style={{ display: 'flex', gap: 10, alignItems: 'center', flexWrap: 'wrap' }}
        >
          <strong style={{ marginRight: 4 }}>Build caches:</strong>
          <label
            style={{ display: 'flex', alignItems: 'center', gap: 4 }}
            title={
              "Include the collection-children cache. Used by " +
              "snapshots' Collections phase to skip the expensive " +
              "per-collection /children fetch on unchanged collections."
            }
          >
            <input
              type="checkbox"
              checked={buildCollections}
              onChange={(e) => setBuildCollections(e.target.checked)}
              disabled={busyAllCaches}
            />
            Collections
          </label>
          <label
            style={{ display: 'flex', alignItems: 'center', gap: 4 }}
            title={
              "Include the per-user playlist cache. Used by " +
              "Playlist Transfer + snapshot's Playlists phase to " +
              "skip a per-user list-playlists round-trip."
            }
          >
            <input
              type="checkbox"
              checked={buildPlaylists}
              onChange={(e) => setBuildPlaylists(e.target.checked)}
              disabled={busyAllCaches}
            />
            Playlists
          </label>
          <button
            type="button"
            onClick={() => { void buildAllCaches(); }}
            disabled={busyAllCaches || (!buildCollections && !buildPlaylists)}
            title={
              "Warm the selected caches for ALL registered servers. " +
              "First run per server is slow on libraries with many " +
              "collections (Plex serializes /library/metadata/X/" +
              "children server-side) or many home-user playlists; " +
              "subsequent calls are fast because each collection's " +
              "updatedAt is checked and unchanged ones skip the fetch."
            }
          >
            {bulkButtonLabel(busyAllCaches)}
          </button>
          <span className="help" style={{ flex: 1, minWidth: 240 }}>
            Both caches also populate as a side effect of snapshots
            (collections) or Refresh (playlists); these buttons let
            you pre-warm without paying the full snapshot cost.
          </span>
        </div>
        {(bulkSummary || bulkFailures.length > 0 || bulkSuccesses.length > 0) && (
          <div style={{ marginTop: 10 }}>
            {bulkSummary && (
              <div style={{ marginBottom: 6 }}>
                <strong>Last bulk run:</strong> {bulkSummary}
              </div>
            )}
            {bulkSuccesses.length > 0 && (
              <details style={{ marginBottom: 6 }}>
                <summary>
                  Successful: {bulkSuccesses.length} server-pass(es)
                </summary>
                <ul style={{ margin: '4px 0 0 16px', padding: 0 }}>
                  {bulkSuccesses.map((s, i) => (
                    <li key={`${s.server_id}-${s.cache_type}-${i}`}>
                      <code>{s.server_id}</code>
                      {' '}({s.cache_type}): {s.note}
                    </li>
                  ))}
                </ul>
              </details>
            )}
            {bulkFailures.length > 0 && (
              <div
                className="banner error"
                style={{ padding: 8 }}
              >
                <strong>
                  Failed: {bulkFailures.length} server-pass(es). Each
                  row shows the cache type and the reason returned by
                  the backend.
                </strong>
                <ul style={{ margin: '4px 0 0 16px', padding: 0 }}>
                  {bulkFailures.map((f, i) => (
                    <li key={`${f.server_id}-${f.cache_type}-${i}`}>
                      <code>{f.server_id}</code>
                      {' '}({f.cache_type}): {f.reason}
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </div>
        )}
      </div>
      )}

      {activeTab === 'overview' && (
      <>
      <BackendTabStrip
        servers={servers}
        activeBackend={activeBackend}
        onChange={setActiveBackend}
      />
      <div className="panel">
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12 }}>
          <h2 style={{ margin: 0 }}>Registered {backendLabel} Servers</h2>
          {canEditServers && (
            <button className="primary" onClick={() => setEditing('new')}>+ Add Server</button>
          )}
        </div>

        {servers.length === 0 ? (
          <div className="empty">
            No servers registered. Click <strong>+ Add Server</strong> to register your first server.
          </div>
        ) : backendServers.length === 0 ? (
          <div className="empty">
            No {backendLabel} servers registered. Click <strong>+ Add Server</strong>{' '}
            to add one, or switch to a backend with registered servers above.
          </div>
        ) : (
          <table className="list">
            <thead>
              <tr>
                <th style={{ width: 24 }}></th>
                <th>Name</th>
                <th>Backend</th>
                {showServerUid && <th>UID</th>}
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
                <th>Mirror</th>
                <th>Collection cache</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {backendServers.map((s) => {
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
                    <td>{(() => {
                      const t = (s.service_type as 'plex' | 'jellyfin' | 'emby' | undefined) ?? 'plex';
                      return t === 'plex' ? 'Plex' : t === 'jellyfin' ? 'Jellyfin' : 'Emby';
                    })()}</td>
                    {showServerUid && (
                      <td className="mono" style={{ fontSize: 11 }}>
                        <button
                          type="button"
                          onClick={() => { void navigator.clipboard?.writeText(s.id); }}
                          title="Click to copy"
                          style={{
                            background: 'none',
                            border: 'none',
                            padding: 0,
                            color: 'inherit',
                            font: 'inherit',
                            cursor: 'pointer',
                            textAlign: 'left',
                          }}
                        >
                          {s.id}
                        </button>
                      </td>
                    )}
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
                      {mirrorState[s.id] ? (
                        <ServerMirrorBadge
                          state={mirrorState[s.id]}
                          globalMode={globalMirrorMode}
                          onChange={() => { void refreshMirrorState(); }}
                        />
                      ) : (
                        <span
                          className="muted"
                          title="No mirror state yet. Trigger a sync from any job or via Sync Now button on the mirror badge once mirror data lands."
                          style={{ fontSize: 11 }}
                        >
                          —
                        </span>
                      )}
                    </td>
                    <td>
                      {(() => {
                        const cc = collectionCacheState[s.id];
                        const isEmpty = !cc || cc.collections === 0;
                        // FECORE-16: a malformed row with last_fetched_at
                        // == 0 (or null/undefined) would otherwise render
                        // an age of ~56 years. Treat a non-positive /
                        // missing timestamp as unknown ("—").
                        const hasTs = !!cc && cc.last_fetched_at > 0;
                        const ageSec = !isEmpty && hasTs ? Math.max(
                          0,
                          Math.floor(Date.now() / 1000 - cc.last_fetched_at),
                        ) : 0;
                        const ageStr =
                          !hasTs ? '—' :
                          ageSec < 60 ? `${ageSec}s` :
                          ageSec < 3600 ? `${Math.floor(ageSec / 60)}m` :
                          ageSec < 86400 ? `${Math.floor(ageSec / 3600)}h` :
                          `${Math.floor(ageSec / 86400)}d`;
                        return (
                          <div style={{ display: 'flex', gap: 4, alignItems: 'center', flexWrap: 'wrap' }}>
                            {isEmpty ? (
                              <span
                                className="muted"
                                title={
                                  "No collections cached for this server yet. " +
                                  "The cache populates either (a) during the next snapshot of " +
                                  "a library with collections (typically Movies), or (b) when " +
                                  "you click the Build button below. Note: the 'Refresh' " +
                                  "button warms the playlist cache + pings the server; it " +
                                  "does NOT touch the collection cache."
                                }
                                style={{ fontSize: 11 }}
                              >
                                empty
                              </span>
                            ) : (
                              <span
                                className="tag"
                                title={
                                  `${cc.collections} collection(s), ${cc.items} item(s) cached. ` +
                                  // FECORE-16: phrase the age line for the unknown case.
                                  (hasTs
                                    ? `Last refresh ${ageStr} ago. `
                                    : 'Last refresh time unknown. ') +
                                  `The next snapshot will skip ` +
                                  `the collection.items() call for these collections — ` +
                                  `unless their Plex updatedAt has advanced, in which case ` +
                                  `the cache entry is invalidated automatically and refetched.`
                                }
                                style={{ fontSize: 10 }}
                              >
                                {cc.collections} · {ageStr}
                              </span>
                            )}
                            {canEditServers && (
                              <>
                                <button
                                  type="button"
                                  onClick={() => { void buildCollectionCacheForServer(s.id); }}
                                  disabled={busyCollectionCacheId === s.id || busyAllCaches}
                                  title={
                                    "Populate the collection-children cache for this server " +
                                    "WITHOUT running a full snapshot. Walks every library, " +
                                    "fetches every collection's children, writes results to " +
                                    "the cache. Wall-clock equals one snapshot's collection " +
                                    "phase (slow first run; fast if you re-click without " +
                                    "Plex-side changes). Use this to test cache speed and " +
                                    "fidelity without paying the full snapshot cost."
                                  }
                                  style={{ fontSize: 10, padding: '2px 6px' }}
                                >
                                  {busyCollectionCacheId === s.id ? 'Building…' : 'Build'}
                                </button>
                                <button
                                  type="button"
                                  onClick={() => clearCollectionCacheForServer(s.id)}
                                  disabled={busyCollectionCacheId === s.id || busyAllCaches || isEmpty}
                                  title={
                                    "Drop the cached collections for this server. The next " +
                                    "snapshot or Build click will re-fetch every collection's " +
                                    "children from Plex. Useful for testing the cache miss-then-hit " +
                                    "speedup or after data on the server has changed materially."
                                  }
                                  style={{ fontSize: 10, padding: '2px 6px' }}
                                >
                                  {busyCollectionCacheId === s.id ? '…' : 'Clear'}
                                </button>
                              </>
                            )}
                          </div>
                        );
                      })()}
                    </td>
                    <td>
                      <div className="row-buttons">
                        <button
                          onClick={() => test(s.id)}
                          disabled={busyId === s.id}
                          title={
                            "Refresh status, re-enumerate libraries, and warm the " +
                            "playlist cache for this server. Note: this does NOT " +
                            "populate the Collection cache (use the Build button in " +
                            "the Collection cache column for that)."
                          }
                        >
                          {busyId === s.id ? 'Refreshing…' : 'Refresh'}
                        </button>
                        {cacheWarmingIds.has(s.id) && (
                          <span
                            className="tag"
                            style={{ fontSize: 10, background: 'var(--bg-panel-alt, rgba(74, 122, 252, 0.10))' }}
                            title="Playlist cache rebuild in progress. Each user's playlists are being re-fetched from the live server; lines appear in Settings ▸ Application Logs ▸ Playlist Cache as they complete. Bulk refresh typically takes 10-30s for a Plex Home with 5+ users."
                          >
                            warming cache…
                          </span>
                        )}
                        <WalkButton serverId={s.id} />
                        {canEditServers && s.has_pending_token && (
                          <PendingTokenChip
                            serverId={s.id}
                            firstSeenAt={s.pending_token_first_seen_at}
                            lastProbedAt={s.pending_token_last_probed_at}
                            onSwapped={() => { void refresh(); }}
                          />
                        )}
                        {canEditServers && (
                          <>
                            <button onClick={() => setEditing(s)}>Edit</button>
                            <button className="danger" onClick={() => remove(s)}>Remove</button>
                          </>
                        )}
                        {/* No ``View`` button is rendered for viewer
                            / end user. The ``ServerEditor`` used for
                            Edit has input fields that aren't
                            read-only, so exposing it to a
                            lower-privilege role would let them see
                            (and try to type into) the server URL +
                            token. The backend CRUD routes reject the
                            write, but the UI shouldn't expose those
                            fields at all - connection settings are
                            strictly admin / root_admin. */}
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>
      </>
      )}

      {editing && (
        <ServerEditor
          server={editing === 'new' ? null : editing}
          existingServers={servers}
          onClose={async (changed) => {
            setEditing(null);
            if (changed) await refresh();
          }}
          onAddedServerId={(id, name) => {
            // Item 3: after a successful Add-Server, probe for cross-
            // server PIN overlaps. The backend already ran the
            // initial token-capture sweep at add time; this surfaces
            // any PIN-on-another-server matches the end user may
            // want to migrate over.
            void probeAndMaybeOpenPinMigration(id, name);
          }}
        />
      )}

      {/* Cross-server PIN migration modal. Triggered by both
          Add-Server and Refresh-Server paths via probeAndMaybeOpenPinMigration. */}
      {pinMigration && (
        <PinMigrationModal
          open={true}
          serverId={pinMigration.serverId}
          serverName={pinMigration.serverName}
          suggestions={pinMigration.suggestions}
          onCancel={() => setPinMigration(null)}
          onApplied={(result) => {
            setPinMigration(null);
            const parts: string[] = [];
            if (result.applied.length > 0) parts.push(`migrated ${result.applied.length} PIN(s)`);
            if (result.skipped.length > 0) parts.push(`${result.skipped.length} already stored`);
            if (result.errors.length > 0) parts.push(`${result.errors.length} error(s)`);
            setRefreshToast({
              name: pinMigration.serverName,
              message: parts.join('; ') || 'no changes',
            });
            void refresh();
          }}
        />
      )}

      {activeTab === 'libraries' && servers.length > 0 && (
        <>
          <BackendTabStrip
            servers={servers}
            activeBackend={activeBackend}
            onChange={setActiveBackend}
          />
          <LibraryCataloguesPanel servers={backendServers} />
        </>
      )}

      {activeTab === 'users' && servers.length > 0 && (
        <>
          <BackendTabStrip
            servers={servers}
            activeBackend={activeBackend}
            onChange={setActiveBackend}
          />
          <ServerUsersPanel
            servers={backendServers}
            users={users}
            onPatched={applyServerPatch}
          />
        </>
      )}

      {/* Per-run history surface. Reads from /api/runs/recent which
          carries one row per completed job (snapshot / restore /
          direct / fan-out). Lives in its own sub-tab so the
          historical view doesn't compete with the Overview surface
          for screen space. */}
      {activeTab === 'runtimes' && servers.length > 0 && (
        <>
          <BackendTabStrip
            servers={servers}
            activeBackend={activeBackend}
            onChange={setActiveBackend}
          />
          <RecentRuntimesPanel servers={backendServers} />
        </>
      )}
    </>
  );
}

// ── Sub-component: per-server user list ──────────────────────────────────────

function UsersForServer({
  server,
  payload,
  // Display-name editing lives on the User Management tab so there is
  // one source of truth across the app. This sub-tab is a view-only
  // roster. ``onPatched`` is kept as an unused prop so the call-site
  // at ``ServerUsersPanel`` doesn't need to change; safe to delete in
  // a later cleanup pass.
  onPatched: _onPatched,
}: {
  server: ServerView;
  payload?: ServerUsersResponse | { error: string };
  onPatched?: (next: ServerView) => void;
}) {
  void _onPatched;

  if (!payload) {
    return (
      <div className="col" style={{ minWidth: 280 }}>
        <h3 style={{ fontSize: 13, margin: '0 0 6px' }}>{server.name}</h3>
        <div className="empty" style={{ fontSize: 12 }}>Loading users…</div>
      </div>
    );
  }
  if ('error' in payload && !('users' in payload)) {
    // Total fetch failure (e.g. 502 - server unreachable). The backend
    // error message points at URL / token, but a working server that
    // starts failing usually means the Plex Server itself is down,
    // restarting, or otherwise unreachable from this host - the
    // ConnectionError catch-all in server_registry.sync_managed_users_from_live
    // can't distinguish bad credentials from network failure. Show a
    // short hint inline and a richer diagnostic list on hover so the
    // user knows where else to look.
    const diagnosticHint =
      'Things to check:\n' +
      '  1. Plex Media Server is running on the host (open its web UI directly).\n' +
      '  2. The host is reachable from this backend (DNS, IP, port forwarding).\n' +
      '  3. URL + token in the Servers tab match the live server.\n' +
      '  4. Plex token has not been revoked under plex.tv > Authorized Devices.\n' +
      '  5. Docker / firewall rules between this backend and the Plex host.';
    return (
      <div className="col" style={{ minWidth: 280 }}>
        <h3 style={{ fontSize: 13, margin: '0 0 6px' }}>{server.name}</h3>
        <div
          className="banner error"
          style={{ fontSize: 12, cursor: 'help' }}
          title={diagnosticHint}
        >
          {payload.error}
        </div>
        <div
          style={{ fontSize: 11, color: 'var(--text-dim)', marginTop: 4 }}
          title={diagnosticHint}
        >
          If the URL and token are correct, verify the Plex Server itself is
          running and reachable from this host. Hover for a full checklist.
        </div>
      </div>
    );
  }

  const resp = payload as ServerUsersResponse;
  const owner = resp.users.find((u) => u.kind === 'owner') || null;
  const managed = resp.users.filter((u) => u.kind === 'managed');

  return (
    <div className="col" style={{ minWidth: 280 }}>
      <h3 style={{ fontSize: 13, margin: '0 0 6px' }}>{server.name}</h3>
      {/* This surface is view-only. Display-name editing lives only
          on the User Management tab, which is the source of truth
          across the app. Operators looking to set / change a display
          name navigate to Settings ▸ User Management. */}
      {owner ? (
        <div style={{ marginBottom: 10, padding: '6px 8px', background: 'var(--panel-alt, #1b2233)', borderRadius: 4 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 12 }}>
            <span className="tag started" style={{ fontSize: 10 }}>Owner</span>
            <span className="mono" style={{ color: 'var(--text-dim)', wordBreak: 'break-all' }}>
              {owner.raw_name}
            </span>
          </div>
          <div style={{ marginTop: 4, fontSize: 13 }}>
            {owner.display_name ? (
              <strong>{owner.display_name}</strong>
            ) : (
              <span style={{ color: 'var(--text-dim)', fontStyle: 'italic' }}>
                (no display name set)
              </span>
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
  // Item 3: optional callback fired with the id+name of a newly
  // created server so the parent can probe for PIN-migration
  // suggestions. Unused for the edit path.
  onAddedServerId?: (id: string, name: string) => void;
}) {
  const { server, existingServers, onClose, onAddedServerId } = props;
  const [name, setName] = useState(server?.name ?? '');
  const [url, setUrl] = useState(server?.url ?? 'http://host.docker.internal:32400');
  const [token, setToken] = useState('');
  // Backend picker. Reads from the existing row when editing (so the
  // user can't accidentally rebrand a Plex row as Jellyfin); fresh
  // adds default to Plex. The radio is hidden in the edit path -
  // changing a registered server's backend type isn't supported.
  const [serviceType, setServiceType] = useState<'plex' | 'jellyfin' | 'emby'>(
    (server?.service_type as 'plex' | 'jellyfin' | 'emby' | undefined) ?? 'plex',
  );
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Per-server auto-tombstone opt-in. Only visible on the edit path
  // (a brand-new server has nothing to tombstone yet). Persisted via
  // its own PATCH endpoint so the main Save / Test flow is
  // untouched.
  const [autoTombstoneEnabled, setAutoTombstoneEnabled] = useState<boolean>(
    Boolean(server?.auto_tombstone_inactive_users_enabled),
  );
  const [autoTombstoneSaving, setAutoTombstoneSaving] = useState(false);
  const [autoTombstoneError, setAutoTombstoneError] = useState<string | null>(null);

  const toggleAutoTombstone = async (next: boolean) => {
    if (!server) return;
    setAutoTombstoneSaving(true);
    setAutoTombstoneError(null);
    try {
      const r = await api.setServerAutoTombstone(server.id, next);
      setAutoTombstoneEnabled(
        Boolean(r.auto_tombstone_inactive_users_enabled),
      );
    } catch (e) {
      setAutoTombstoneError(
        errorText(e),
      );
    } finally {
      setAutoTombstoneSaving(false);
    }
  };

  // Duplicate-token soft warning. After a successful Test Connection
  // the probe response includes the ``owner_name`` of the Plex
  // account the supplied token belongs to. If that owner already
  // appears under one or more other registered servers, the user is
  // reusing the same Plex.tv account's token across servers - valid,
  // but worth surfacing because the servers will share authentication
  // context (revoking one revokes all).
  //
  // We can't compare raw tokens client-side because the API never
  // returns them, but ``owner_name`` is exposed on every ServerView
  // and is a reliable proxy for "this token belongs to that account."
  // The warning is soft / informational; the user can save anyway.

  // ── Test Connection state ────────────────────────────────────────
  // The test uses /api/servers/test-unsaved which probes URL+token
  // without touching the registry. Probing this way avoids leaving a
  // half-registered server behind whenever the test passes.
  //
  // The probe response carries the connected server's friendly name
  // and machine identifier; the UI surfaces them so the user can
  // confirm they hit the right Plex install before saving (a Plex
  // account's token works on every server it owns, so a successful
  // connect does not by itself prove which server you reached).
  const [testing, setTesting] = useState(false);
  const [probe, setProbe] = useState<ProbeUnsavedResult | null>(null);
  // When the user clicks "Try fallback token" on the auth_error
  // banner, this captures the borrowed-from server's id. The Save
  // path then submits ``use_fallback_from_server_id`` so the backend
  // borrows that server's token and stashes the typed one as pending.
  // Cleared whenever the user changes URL or token (the borrow offer
  // was scoped to the original probe).
  const [acceptedFallbackFromId, setAcceptedFallbackFromId] = useState<string | null>(null);

  // Reset the probe result whenever the user changes a connection
  // field - the previous "passed" result no longer applies.
  useEffect(() => {
    setProbe(null);
    setAcceptedFallbackFromId(null);
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
      const result = await api.testUnsavedServer({
        name, url, token,
        // Tell the backend which adapter to probe with. Backend
        // defaults to 'plex' if the field is omitted (legacy clients).
        service_type: serviceType,
      });
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
        // create_server on the backend re-probes and rejects
        // duplicate machine_identifiers, so passing a probed-OK row
        // through here is safe. The frontend pre-test in testConnection
        // is for fast UX feedback; the backend still enforces.
        //
        // Auto-fallback: when the user accepted the "Try fallback
        // token" offer on the Test result, we submit
        // ``use_fallback_from_server_id`` so the backend uses the
        // borrowed token as the active credential and stashes the
        // typed one as pending. The user's typed token still rides
        // along in ``token`` so the backend can encrypt it into the
        // pending_token slot.
        const created = await api.createServer({
          name,
          url,
          token,
          // Tell the backend which adapter to wrap this connection
          // with. Backend persists service_type on the row so
          // subsequent connects route to the right adapter.
          service_type: serviceType,
          ...(acceptedFallbackFromId
            ? { use_fallback_from_server_id: acceptedFallbackFromId }
            : {}),
        });
        // Stash the new id on the close payload so the parent panel
        // can probe pin-migration suggestions right after.
        if (created && typeof created === 'object' && 'id' in created) {
          onAddedServerId?.(String((created as { id: string }).id), String((created as { name: string }).name));
        }
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
  //
  // Fallback-accepted path: after the user clicks "Try fallback
  // token" we record ``acceptedFallbackFromId`` and the save flow
  // submits ``use_fallback_from_server_id`` so the backend stores
  // the borrowed token and stashes the typed one as pending. In
  // this state ``probe.ok`` is still false (the typed token still
  // 401s; that's the whole reason fallback was offered), so the
  // probe.ok guard above would lock Save forever. Treat the
  // fallback acceptance as the equivalent of a successful probe
  // for the purpose of the canSave gate; the backend re-probes
  // with the borrowed token at save time and rejects if anything
  // changed between Test and Save.
  const fallbackAcceptedAndUsable = !!(
    acceptedFallbackFromId
    && probe?.fallback
    && probe.fallback.borrowed_from_server_id === acceptedFallbackFromId
  );
  const canSave = server
    ? !!(name.trim() && url.trim())
    : !!(
        name.trim() && url.trim() && token.trim()
        && (
          (probe?.ok && !probe.duplicate_of)
          || fallbackAcceptedAndUsable
        )
      );

  return (
    <div className="panel">
      <h2>{server ? `Edit Server: ${server.name}` : 'Add Server'}</h2>
      {error && <div className="banner error">{error}</div>}
      {probe && !probe.ok && probe.status === 'auth_error' && !acceptedFallbackFromId && (
        // 401/403 gets its own banner so the user's first reaction
        // is "wrong token" rather than "network down." The token-finder
        // link points at Plex's own canonical docs because the
        // procedure (browser dev tools / View XML) is Plex-specific
        // and changes faster than anything we'd duplicate here.
        <div className="banner error">
          <strong>Token rejected.</strong>{' '}
          {probe.detail || 'The server returned an authorization error.'}
          {probe.fallback && (
            <div style={{ marginTop: 8, padding: 8, background: 'rgba(255,165,0,0.08)', border: '1px solid var(--accent-warn, orange)', borderRadius: 4 }}>
              <strong>Fallback available.</strong>{' '}
              Your saved token from <strong>{probe.fallback.borrowed_from_server_name}</strong> connected
              successfully against this URL (server reports itself as
              <strong> {probe.fallback.friendly_name || '(no name)'}</strong>,
              owner <code>{probe.fallback.owner_name}</code>).
              <div style={{ fontSize: 12, marginTop: 4 }}>
                New Plex servers can take a few minutes to propagate their
                token to Plex.tv. You can save now with your existing
                working token; the typed token will be stashed as a
                retry-able pending token on this server's row.
              </div>
              <div style={{ marginTop: 8 }}>
                <button
                  onClick={() => setAcceptedFallbackFromId(
                    probe.fallback?.borrowed_from_server_id ?? null,
                  )}
                  style={{ fontSize: 12 }}
                >
                  Try fallback token
                </button>
              </div>
            </div>
          )}
          {serviceType === 'plex' && (
            <div style={{ marginTop: 6, fontSize: 12 }}>
              Common causes: the token belongs to a different Plex account,
              the token was revoked under{' '}
              <a
                href="https://app.plex.tv/desktop#!/settings/account/devices"
                target="_blank"
                rel="noopener noreferrer"
              >
                plex.tv &gt; Authorized Devices
              </a>
              , or you pasted a Plex Pass key / Home PIN instead of an
              X-Plex-Token.{' '}
              <a
                href="https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/"
                target="_blank"
                rel="noopener noreferrer"
              >
                How to find your X-Plex-Token
              </a>
            </div>
          )}
          {serviceType === 'jellyfin' && (
            <div style={{ marginTop: 6, fontSize: 12 }}>
              Common causes: the API key was revoked or never existed.
              Generate or copy a fresh admin API key under Dashboard &gt;
              API Keys on the Jellyfin server, and confirm the URL points
              at the same install where the key was issued.
            </div>
          )}
          {serviceType === 'emby' && (
            <div style={{ marginTop: 6, fontSize: 12 }}>
              Common causes: the API key was revoked or never existed.
              Generate or copy a fresh admin API key under Settings &gt;
              Advanced &gt; API Keys on the Emby server, and confirm the
              URL points at the same install where the key was issued.
            </div>
          )}
        </div>
      )}
      {probe && probe.fallback && acceptedFallbackFromId && (
        // After the user clicks "Try fallback token", the original
        // error banner stays hidden and this acceptance banner takes
        // over. Save will submit with use_fallback_from_server_id so
        // the backend stores the borrowed token and stashes the typed
        // one as pending.
        <div className="banner good">
          <strong>Fallback accepted.</strong>{' '}
          Saving will use the borrowed token from
          <strong> {probe.fallback.borrowed_from_server_name}</strong>.
          Your typed token will be stashed on this server's row as a
          pending retry; the Servers list shows a chip with a Retry
          button once Plex.tv propagates the new server's
          authorization.
          <div style={{ marginTop: 6 }}>
            <button
              onClick={() => setAcceptedFallbackFromId(null)}
              style={{ fontSize: 11 }}
            >
              Cancel fallback
            </button>
          </div>
        </div>
      )}
      {probe && !probe.ok && probe.status === 'ssl_error' && (
        <div className="banner error">
          <strong>TLS error.</strong> {probe.detail || 'Certificate verification failed.'}
          <div style={{ marginTop: 6, fontSize: 12 }}>
            If this is a local LAN {
              serviceType === 'plex' ? 'Plex' :
              serviceType === 'jellyfin' ? 'Jellyfin' :
              'Emby'
            } server without a public certificate, try the plain{' '}
            <code>http://</code> URL on its LAN port (usually{' '}
            <code>{
              serviceType === 'plex' ? ':32400' : ':8096'
            }</code>) instead of <code>https://</code>.
          </div>
        </div>
      )}
      {probe && !probe.ok && probe.status === 'timeout' && (
        <div className="banner error">
          <strong>{
            serviceType === 'plex' ? 'Plex' :
            serviceType === 'jellyfin' ? 'Jellyfin' :
            'Emby'
          } did not respond.</strong> {probe.detail || 'The connection timed out.'}
          <div style={{ marginTop: 6, fontSize: 12 }}>
            The server may be starting up, on a slow network, or
            unreachable from this host. Confirm you can reach the URL
            from a browser on the same network.
          </div>
        </div>
      )}
      {probe && !probe.ok
        && probe.status !== 'auth_error'
        && probe.status !== 'ssl_error'
        && probe.status !== 'timeout' && (
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
      {/* Soft duplicate warning for the (name, URL, service) triple.
          The hard duplicate check by machine_identifier (the banner
          below) misses the case where the operator removed a server
          and is re-adding one that has the same friendly identity
          (same name + URL + backend) but a fresh machine_id allocation.
          Surface the match as a warning rather than block, since the
          two ARE different physical installs at the SQLite level. The
          Exports panel collapses snapshots from this triple back into
          a single tab automatically, so re-adding under the same name
          is fine - the warning just nudges the operator in case they
          actually meant to Edit instead of Add. */}
      {!server && (() => {
        const matches = (existingServers || []).filter(
          (s) =>
            s.name === name.trim()
            && s.url === url.trim()
            && (s.service_type || 'plex') === serviceType,
        );
        if (matches.length === 0) return null;
        return (
          <div className="banner warning">
            <strong>Existing server matches this name + URL.</strong>{' '}
            A registered server already has this exact friendly name and URL
            (<strong>{matches[0].name}</strong>). Snapshots from the old
            entry will be grouped under the same Exports sub-tab as the new
            one, but you may have intended to <strong>edit</strong> the
            existing row instead of adding a new one.
          </div>
        );
      })()}
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
        // Soft duplicate-token warning. Surface every *other*
        // registered server whose owner matches the one this probe
        // authenticated as - strong signal the same Plex.tv account
        // (and likely the same token) is being reused.
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
        <input type="text" value={name} onChange={(e) => setName(e.target.value)} placeholder={
          serviceType === 'jellyfin' ? 'My Jellyfin' :
          serviceType === 'emby' ? 'My Emby' :
          'My Plex'
        } />
      </label>
      {/* Backend picker. Hidden in the edit path - changing a
          registered server's backend type isn't supported (the
          adapter, stored credentials, and on-disk identifiers all
          assume one backend). For new servers the operator picks once
          at registration time and the form below adapts its labels +
          placeholders. */}
      {!server && (
        <label className="field">
          <span className="label">Backend</span>
          <span className="help">
            Pick the server software at the URL above. Plex uses the X-Plex-Token model;
            Jellyfin and Emby use server-issued API keys (Dashboard → API Keys).
          </span>
          <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap' }}>
            {(['plex', 'jellyfin', 'emby'] as const).map((opt) => (
              <label key={opt} style={{ display: 'inline-flex', alignItems: 'center', gap: 6, fontWeight: 'normal' }}>
                <input
                  type="radio"
                  name="server-backend"
                  value={opt}
                  checked={serviceType === opt}
                  onChange={() => {
                    setServiceType(opt);
                    // Reset the probe whenever backend changes - the
                    // previous probe was scoped to a different adapter.
                    setProbe(null);
                    setAcceptedFallbackFromId(null);
                    // Suggest a sensible default port per backend if
                    // the operator hasn't already customised the URL.
                    if (url === 'http://host.docker.internal:32400' || url === 'http://host.docker.internal:8096') {
                      setUrl(opt === 'plex'
                        ? 'http://host.docker.internal:32400'
                        : 'http://host.docker.internal:8096');
                    }
                  }}
                />
                <span style={{ textTransform: 'capitalize' }}>{opt}</span>
              </label>
            ))}
          </div>
        </label>
      )}
      <label className="field">
        <span className="label">Server URL</span>
        <span className="help">
          {serviceType === 'plex'
            ? <>Full URL including protocol and port. On a single-host setup with Plex on the same machine, use <code>http://host.docker.internal:32400</code>.</>
            : <>Full URL including protocol and port. Default {serviceType === 'jellyfin' ? 'Jellyfin' : 'Emby'} port is <code>8096</code> (HTTP) or <code>8920</code> (HTTPS).</>}
        </span>
        <input type="text" value={url} onChange={(e) => setUrl(e.target.value)} />
      </label>
      <label className="field">
        <span className="label">
          {serviceType === 'plex' ? 'Plex authentication token' : 'API key'}
        </span>
        <span className="help">
          {server
            ? 'A credential is already saved. Leave blank to keep it; type a new value to replace it.'
            : serviceType === 'plex'
              ? 'Find yours in any X-Plex-Token URL from the Plex web UI.'
              : serviceType === 'jellyfin'
                ? 'In Jellyfin Dashboard -> API Keys, create a new key and paste it here.'
                : 'In Emby Dashboard -> API Keys, create a new key and paste it here.'}
        </span>
        <input type="password" value={token} onChange={(e) => setToken(e.target.value)} placeholder={server ? '••••••••' : ''} />
      </label>
      {server && (
        <div
          className="panel"
          style={{ marginTop: 12, padding: 10, fontSize: 12 }}
          title="Per-server opt-in for the auto-tombstone sweeper."
        >
          <label style={{ display: 'flex', alignItems: 'center', gap: 8, cursor: 'pointer' }}>
            <input
              type="checkbox"
              checked={autoTombstoneEnabled}
              disabled={autoTombstoneSaving}
              onChange={(e) => void toggleAutoTombstone(e.target.checked)}
            />
            <span>
              <strong>Auto-tombstone inactive users</strong>
              <span style={{ color: 'var(--text-dim)', marginLeft: 6 }}>
                (off by default)
              </span>
            </span>
          </label>
          <p style={{ color: 'var(--text-dim)', marginTop: 6, marginBottom: 0 }}>
            If a user on this server fails auth (per the trigger types
            enabled under <em>Settings &rsaquo; Tunables &rsaquo;
            Polling &rsaquo; User Activity</em>) for N consecutive
            sweep cycles, the engine will tombstone them automatically
            so future snapshots / sync / etc. skip them. Reversible
            via the User Management panel. The global sweeper master
            switch must also be on for this toggle to fire any
            tombstones.
          </p>
          {autoTombstoneError && (
            <div className="banner error" style={{ marginTop: 6 }}>
              {autoTombstoneError}
            </div>
          )}
        </div>
      )}
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
      {!server && !probe?.ok && !fallbackAcceptedAndUsable && (
        <p style={{ color: 'var(--text-dim)', fontSize: 12, marginTop: 8 }}>
          For a new server, the Save button is enabled after a successful
          Test Connection, or after clicking "Try fallback token" on the
          auth_error banner when a fallback is on offer.
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
// Pending-token chip + Retry button. Visible only on registry rows
// that have ``has_pending_token=true`` (the end user's typed token
// failed at Add-server time but a borrowed token connected; the
// typed token is sitting in pending_token waiting for Plex.tv to
// propagate the new server's authorization). Click Retry to re-probe
// the pending token; on success the backend swaps it into the
// active slot and clears the pending fields, and the parent panel
// refreshes the row.
function PendingTokenChip({
  serverId,
  firstSeenAt,
  lastProbedAt,
  onSwapped,
}: {
  serverId: string;
  firstSeenAt?: number;
  lastProbedAt?: number;
  onSwapped: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<
    | { ok: boolean; swapped: boolean; status: string; detail: string }
    | null
  >(null);

  const ageHint = firstSeenAt && firstSeenAt > 0
    ? ` (stashed ${new Date(firstSeenAt * 1000).toLocaleDateString()})`
    : '';
  const lastTriedHint = lastProbedAt && lastProbedAt > 0
    ? ` · last tried ${new Date(lastProbedAt * 1000).toLocaleString()}`
    : '';

  const onRetry = async () => {
    setBusy(true);
    setResult(null);
    try {
      const r = await api.retryPendingToken(serverId);
      setResult(r);
      if (r.swapped) {
        // Parent refresh: re-pulls the server list so this row
        // loses its pending chip + any other identity fields update
        // to the post-swap state.
        onSwapped();
      }
    } catch (e) {
      setResult({
        ok: false,
        swapped: false,
        status: 'error',
        detail: errorText(e),
      });
    } finally {
      setBusy(false);
    }
  };

  return (
    <span
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: 4,
        padding: '2px 6px',
        borderRadius: 999,
        background: 'rgba(255,165,0,0.12)',
        border: '1px solid var(--accent-warn, orange)',
        fontSize: 11,
      }}
      title={
        'A previously-typed token is parked on this server because it '
        + 'returned 401 at Add time. Retry it: if Plex.tv has finished '
        + 'propagating its authorization, the pending token becomes the '
        + 'active token and this chip disappears. Otherwise the chip '
        + 'remains and you can try again later.'
        + ageHint
        + lastTriedHint
      }
    >
      <span>Pending token{ageHint}</span>
      <button
        onClick={onRetry}
        disabled={busy}
        style={{ fontSize: 10, padding: '1px 6px' }}
      >
        {busy ? 'Probing…' : 'Retry'}
      </button>
      {result && !busy && (
        <span style={{ color: result.swapped ? 'var(--accent-ok, green)' : 'var(--text-dim)' }}>
          {result.swapped
            ? '✓ swapped'
            : `${result.status}: ${result.detail.slice(0, 60)}${result.detail.length > 60 ? '…' : ''}`}
        </span>
      )}
    </span>
  );
}


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
    return pausableInterval(() => {
      if (status.running) refresh();
    }, 5000);
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
// type + leaf_counts into the end user-facing strings the catalogue
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
  // end users can tell stale metadata from a confirmed zero.
  if (value === null || value === undefined) {
    return `? ${plural}`;
  }
  return `${value.toLocaleString()} ${value === 1 ? singular : plural}`;
}


// Library Catalogues: one sub-tab per registered server, mirroring the
// nav-strip pattern used elsewhere in the Help panel. Keeps the
// catalogue panel a fixed height regardless of how many servers are
// registered, and lets the end user focus on one server's libraries at
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
        Read-only roster of the owner + Plex Home managed users on each
        registered server. Display-name editing lives on{' '}
        <strong>Settings ▸ User Management</strong> — the canonical
        identity-management surface across the app. Display names set
        there propagate to the dashboard run header, activity feed,
        and user pickers everywhere.
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
