// Top-level Playlist Management surface (Jobs ▸ Playlist Management).
//
// A multi-select layout that mirrors Run Job's Mode-and-Servers panel:
//
//   ┌─ Source server ─┐ ┌─ Dest server ───┐
//   │  picker          │ │  picker          │
//   ├──────────────────┤ ├──────────────────┤
//   │  Source users    │ │  Dest users      │
//   │  [x] alice       │ │  [x] charlie     │
//   │  [ ] bob         │ │  [x] dave        │
//   └──────────────────┘ └──────────────────┘
//
//   For each checked user, a playlists card appears below:
//     ┌─ alice (source) playlists ──────────────┐
//     │ [x] Movies playlist  (45 items)  [↗]    │
//     │ [ ] Workout mix      (132 items) [↗]    │
//     └─────────────────────────────────────────┘
//     ┌─ charlie (dest) playlists ── read-only ─┐
//     │     Movies playlist  (45 items)  [↗]    │  ← name-match badge
//     │     My stuff         (12 items)  [↗]    │
//     └─────────────────────────────────────────┘
//
//   Deploy bar at the bottom: pick ONE destination user from the
//   checked dest users, fire N copies (one per checked source playlist).
//
// The cache auto-warms on first server selection, and the Plex Home
// per-user-token UI provides a Save Token affordance plus
// DEST_USER_TOKEN_MISSING error handling.

import { useEffect, useMemo, useRef, useState } from 'react';
import { api } from '../api';
import type {
  PingResult,
  PlaylistCacheStatus,
  PlaylistCopyIn,
  PlaylistCopyResult,
  PlaylistMgmtUser,
  PlaylistSpec,
  ServerView,
  SmartMigrateItem,
} from '../api';

// Cross-source batches: when the queued source playlists come from
// MULTIPLE source servers, the operator picks at runtime whether to
// submit ONE combined batch (single job, single ThreadPoolExecutor
// across mixed sources) OR one batch PER source server (N jobs, each
// honoring its own per-source backpressure semaphore). The current
// PlaylistManagementPanel layout pins source to a single server, so
// the radio is non-operative until multi-source queueing lands; it is
// surfaced now so the operator sees the choice, defaulted to
// 'per_source'.
type CrossSourceMode = 'per_source' | 'combined';
import { BackendTabStrip, type BackendType, serversForBackend } from './BackendTabStrip';
import { errorText } from '../utils/format';
import { PlaylistMgmtServersAndUsersPanel } from './PlaylistMgmtServersAndUsersPanel';
import { UserPlaylistsCard } from './UserPlaylistsCard';
import { PlaylistMgmtDeployBar, type QueuedPlaylist, type SmartMigrateMode } from './PlaylistMgmtDeployBar';
import { ActiveDeploysPanel } from './ActiveDeploysPanel';
import { SmartMigrationProgressPanel } from './SmartMigrationProgressPanel';
import { SmartMigrationModePanel } from './SmartMigrationModePanel';
import { PlexHomeTokenModal, type AdminCreds } from './PlexHomeTokenModal';

// 'smart' = Smart Playlist Migration mode. A non-backend mode tab:
// the source must be Plex (only Plex has smart playlists), the
// destination is any backend, and the queued smart playlists are
// migrated via the smart_playlist_migrate job rather than copied.
type WorkflowMode = 'plex' | 'jellyfin' | 'emby' | 'cross' | 'smart';

interface QueuedEntry extends QueuedPlaylist {
  // Identical to QueuedPlaylist; alias kept for parity with internal
  // keying via `${userId}::${playlist_id}`.
}

// Stable per-user identifier for FRONTEND state (React keys, Set
// membership, queue Map keys). See PlaylistMgmtServersAndUsersPanel
// for the chain (app_user_uuid → backend_user_id → username) and bug
// history.
function userKey(u: PlaylistMgmtUser): string {
  return u.app_user_uuid || u.backend_user_id || u.username;
}

// Backend-resolvable user identifier — what we send as `user_id` on
// /api/playlist-mgmt/* calls. The backend's _find_user resolves
// backend_user_id then case-insensitive username; it does NOT yet
// understand app_user_uuid as a primary lookup, so we must NEVER send
// that string on the wire or every request 404s.
function apiUserId(u: PlaylistMgmtUser): string {
  return u.backend_user_id || u.username;
}

function queueKey(userId: string, playlistId: string): string {
  return `${userId}::${playlistId}`;
}

export function PlaylistManagementPanel() {
  // ── Server registry + cache status ────────────────────────────
  const [servers, setServers] = useState<ServerView[]>([]);
  const [pings] = useState<Record<string, PingResult>>({});
  const [serversError, setServersError] = useState<string | null>(null);
  const [cacheStatus, setCacheStatus] = useState<Record<string, PlaylistCacheStatus>>({});

  // ── Backend tab + server picks ────────────────────────────────
  const [workflowMode, setWorkflowMode] = useState<WorkflowMode>('plex');
  const [sourceServerId, setSourceServerId] = useState('');
  const [destServerId, setDestServerId] = useState('');

  // ── User check sets (per side) ────────────────────────────────
  const [checkedSourceUsers, setCheckedSourceUsers] = useState<Set<string>>(new Set());
  const [checkedDestUsers, setCheckedDestUsers] = useState<Set<string>>(new Set());

  // ── Playlist-type filter ──────────────────────────────────────
  // Checkboxes with the same shape as the bulk-cache
  // Collections/Playlists pair on Servers > Overview. Filters EVERY
  // source-side UserPlaylistsCard simultaneously by the playlist's
  // playlist_type (audio / video / photo). All three on by default
  // so the panel opens with everything visible; unchecking a type
  // hides that family across every user card at once. Playlists
  // with no playlist_type (empty / older cache rows from before the
  // playlist_type schema column was added) always render so the
  // operator never loses sight of them on a stale-cache install.
  const [typeFilterAudio, setTypeFilterAudio] = useState<boolean>(true);
  const [typeFilterVideo, setTypeFilterVideo] = useState<boolean>(true);
  const [typeFilterPhoto, setTypeFilterPhoto] = useState<boolean>(true);
  const playlistTypeFilter = useMemo<Set<string>>(() => {
    const s = new Set<string>();
    if (typeFilterAudio) s.add('audio');
    if (typeFilterVideo) s.add('video');
    if (typeFilterPhoto) s.add('photo');
    return s;
  }, [typeFilterAudio, typeFilterVideo, typeFilterPhoto]);
  // Mirror of the users list each child Side reports up; lets us
  // resolve user_id -> username/role for the playlists cards below.
  const [sourceUsers, setSourceUsers] = useState<PlaylistMgmtUser[]>([]);
  const [destUsers, setDestUsers] = useState<PlaylistMgmtUser[]>([]);

  // ── Playlist multi-select (source side) ───────────────────────
  // Key = `${sourceUserId}::${playlist_id}` so two users with the
  // same playlist name don't collide. Value is the full QueuedEntry.
  const [queued, setQueued] = useState<Map<string, QueuedEntry>>(new Map());

  // ── Result state ──────────────────────────────────────────────
  // Copies fan out cartesian-style: every queued source playlist
  // gets copied to every checked dest user. No explicit target
  // state needed.
  const [submitting, setSubmitting] = useState(false);
  // Last deploy-submission error, surfaced as a banner. Cleared at the
  // start of each deploy; set when the batch / migrate POST itself
  // fails so the operator's queued work is not silently discarded.
  const [deployError, setDeployError] = useState<string | null>(null);
  // Bumped every time the end user clicks Deploy (or Clone Deploy)
  // so the ActiveDeploysPanel kicks an immediate poll for the freshly-
  // queued jobs instead of waiting up to 2s for its next interval.
  const [submissionTick, setSubmissionTick] = useState(0);
  // Smart Playlist mode: the active migration job id + the item count
  // it carried, fed to SmartMigrationProgressPanel for the live log +
  // per-playlist results poll.
  const [activeSmartJobId, setActiveSmartJobId] = useState<string | null>(null);
  const [smartExpectedCount, setSmartExpectedCount] = useState(0);
  // Smart Playlist mode: the migration mode a newly-checked smart
  // playlist inherits. The mode panel's "Set all" control updates it.
  const [smartDefaultMode, setSmartDefaultMode] = useState<SmartMigrateMode>('filter');

  // ── Pre-warm tunable ──
  // The frontend fires a debounced POST /api/playlist-mgmt/prewarm/
  // {server_id} after the operator picks a destination, so the
  // path-index build happens in the background while they're
  // selecting playlists. Default delay 3s; tunable [0, 10000ms].
  const [prewarmDelayMs, setPrewarmDelayMs] = useState<number>(3000);

  // ── Batch submission options ──
  // Per-submit overrides:
  //  - parallelism: empty -> server uses playlist_mgmt_batch_workers
  //    tunable; explicit value 1..N clamps to playlist_mgmt_batch_max_size.
  //  - label: optional operator-supplied tag shown in the activity feed.
  //  - cross-source mode: when multi-source queueing lands this
  //    radio decides "1 batch per source" vs "1 combined batch".
  const [batchParallelismOverride, setBatchParallelismOverride] = useState<string>('');
  const [batchLabel, setBatchLabel] = useState<string>('');
  const [crossSourceMode, setCrossSourceMode] = useState<CrossSourceMode>('per_source');
  // Server-side caps (loaded from tunables on mount). Drives the
  // per-submit override input's max attribute + a tooltip explaining
  // the default.
  const [batchMaxSize, setBatchMaxSize] = useState<number>(200);
  const [batchDefaultWorkers, setBatchDefaultWorkers] = useState<number>(8);

  // ── Plex Home per-user-token surface ──────────────────────────
  const [authMode, setAuthMode] = useState<'owner_token' | 'per_user_token'>('owner_token');
  // developer tunable. When true, the orchestrator refuses to fall back
  // to the admin token if a user's per-user token is missing. We mirror
  // it here so the dest user list can pre-exclude users who would
  // definitely fail at copy time (Plex + per_user_token + strict +
  // no saved token).
  const [strictIdentityResolution, setStrictIdentityResolution] = useState(false);
  const [tokenModalForUser, setTokenModalForUser] = useState<PlaylistMgmtUser | null>(null);
  const [cachedAdmin, setCachedAdmin] = useState<AdminCreds | null>(null);
  // Bumped after save-token success so dest user list re-fetches
  // (refreshes the has_token chip) and the in-flight modal's row gets
  // its current has_token from the parent next open.
  const [destUsersNonce, setDestUsersNonce] = useState(0);
  // Source side gets its own nonce purely for symmetry; not used by
  // the token flow but lets manual-refresh trigger a re-fetch too.
  const [sourceUsersNonce, setSourceUsersNonce] = useState(0);

  // ── Cache auto-warm (W2) ──────────────────────────────────────
  const warmedServersRef = useRef<Set<string>>(new Set());
  const [warmingServers, setWarmingServers] = useState<Set<string>>(new Set());

  // ── Backend filtering ─────────────────────────────────────────
  // The source list is Plex-only in Smart Playlist mode (smart
  // playlists are a Plex concept); the destination list is every
  // backend in Smart Playlist + Cross modes. A plain backend tab
  // filters both sides to that one backend.
  const sourceServers = useMemo(() => {
    if (workflowMode === 'smart') return serversForBackend(servers, 'plex');
    if (workflowMode === 'cross') return servers;
    return serversForBackend(servers, workflowMode);
  }, [servers, workflowMode]);
  const destServers = useMemo(() => {
    if (workflowMode === 'smart' || workflowMode === 'cross') return servers;
    return serversForBackend(servers, workflowMode);
  }, [servers, workflowMode]);

  // ── Initial load: servers + auth_mode tunable ─────────────────
  useEffect(() => {
    let cancelled = false;
    api.listServers()
      .then((r) => { if (!cancelled) setServers(r); })
      .catch((e) => { if (!cancelled) setServersError(errorText(e)); });
    api.getSettings()
      .then((s) => {
        if (cancelled) return;
        const raw = (s as unknown as Record<string, unknown>).tunables;
        const t = raw && typeof raw === 'object' ? (raw as Record<string, unknown>) : {};
        const mode = t['playlist_mgmt_plex_home_auth_mode'];
        if (mode === 'per_user_token' || mode === 'owner_token') {
          setAuthMode(mode);
        }
        const strict = t['strict_identity_resolution'];
        if (typeof strict === 'boolean') {
          setStrictIdentityResolution(strict);
        }
        const maxSize = t['playlist_mgmt_batch_max_size'];
        if (typeof maxSize === 'number' && maxSize >= 1) {
          setBatchMaxSize(maxSize);
        }
        const workers = t['playlist_mgmt_batch_workers'];
        if (typeof workers === 'number' && workers >= 1) {
          setBatchDefaultWorkers(workers);
        }
        const prewarmDelay = t['playlist_mgmt_prewarm_delay_ms'];
        if (typeof prewarmDelay === 'number' && prewarmDelay >= 0) {
          setPrewarmDelayMs(Math.min(prewarmDelay, 10000));
        }
      })
      .catch(() => { /* non-fatal */ });
    return () => { cancelled = true; };
  }, []);

  const destServer = useMemo(
    () => servers.find((s) => s.id === destServerId) ?? null,
    [servers, destServerId],
  );
  const destIsPlex = (destServer?.service_type ?? 'plex') === 'plex';
  const showDestTokenAffordance = destIsPlex && authMode === 'per_user_token';

  // Transferability predicate. A user is shown in the destination
  // picker only when a copy could authenticate as them.
  //
  // The owner / admin case is ABSOLUTE: admin tokens can always write
  // to or as the owner (it IS the owner's token). The owner must not
  // be hidden under strict + per_user_token mode even though they
  // typically have ``has_token=false`` (their token is the admin one,
  // not a per-user-saved one) - the owner is always a writable
  // target.
  //
  // For everyone else:
  //   - Non-Plex destinations always authenticate as admin → show all.
  //   - Plex + owner_token mode → admin writes for everyone → show all.
  //   - Plex + per_user_token:
  //       has_token=true             → per-user auth works → show.
  //       no token + strict=false    → orchestrator falls back to admin → show.
  //       no token + strict=true     → no fallback path exists → HIDE.
  const destUserIsTransferable = (u: PlaylistMgmtUser): boolean => {
    if (u.role === 'owner' || u.role === 'admin') return true;
    if (!destIsPlex) return true;
    if (authMode !== 'per_user_token') return true;
    if (u.has_token) return true;
    if (!strictIdentityResolution) return true;
    return false;
  };

  const sourceServerLabel = servers.find((s) => s.id === sourceServerId)?.name || '';
  const destServerLabel = servers.find((s) => s.id === destServerId)?.name || '';

  // ── Cache status fetch + warm ─────────────────────────────────
  //
  // Build the playlist cache on first server selection per session.
  // The rebuild fires on every first-selection regardless of whether
  // cache_status already has rows (even stale ones from a prior
  // session), because the end user's mental model is "picking a
  // server should build/refresh the cache". The `warmedServersRef`
  // guard still prevents repeated rebuilds within one session.
  const refreshCacheStatusFor = async (
    serverId: string,
    { warmIfCold = true }: { warmIfCold?: boolean } = {},
  ) => {
    if (!serverId) return;
    try {
      const resp = await api.playlistMgmtCacheStatus(serverId);
      const rows = resp.rows || [];
      setCacheStatus((prev) => {
        const next = { ...prev };
        for (const k of Object.keys(next)) {
          if (k.startsWith(`${serverId}::`)) delete next[k];
        }
        for (const r of rows) {
          next[`${r.server_id}::${r.user_id}`] = r;
        }
        return next;
      });
      if (warmIfCold && !warmedServersRef.current.has(serverId)) {
        warmedServersRef.current.add(serverId);
        setWarmingServers((prev) => new Set(prev).add(serverId));
        try {
          await api.playlistMgmtRefreshServer(serverId);
          await refreshCacheStatusFor(serverId, { warmIfCold: false });
        } catch {
          /* best-effort; manual refresh remains available */
        } finally {
          setWarmingServers((prev) => {
            const n = new Set(prev);
            n.delete(serverId);
            return n;
          });
        }
      }
    } catch {
      /* best-effort */
    }
  };

  useEffect(() => {
    if (sourceServerId) void refreshCacheStatusFor(sourceServerId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sourceServerId]);
  useEffect(() => {
    if (destServerId) void refreshCacheStatusFor(destServerId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [destServerId]);

  // Debounced pre-warm on dest pick. After the operator settles on a
  // destination, fire a single POST
  // /api/playlist-mgmt/prewarm/{server_id} so the path-index build
  // starts in the background. Tunable delay (default 3s) prevents a
  // quick mis-click from triggering an expensive walk. The effect
  // cancels the pending timeout if the operator changes their pick
  // before the delay elapses.
  useEffect(() => {
    if (!destServerId) return;
    const handle = window.setTimeout(() => {
      api.prewarmPlaylistMgmtServer(destServerId).catch((e) => {
        // Best-effort; surface to console but don't disturb the UI.
        // The next batch will trigger the build path normally if
        // the pre-warm didn't land.
        // eslint-disable-next-line no-console
        console.warn('prewarm failed', destServerId, e);
      });
    }, prewarmDelayMs);
    return () => window.clearTimeout(handle);
  }, [destServerId, prewarmDelayMs]);

  // Manual refresh: bypass auto-warm, force a server-bulk refresh.
  const handleManualRefresh = async (side: 'source' | 'dest') => {
    const id = side === 'source' ? sourceServerId : destServerId;
    if (!id) return;
    try {
      await api.playlistMgmtRefreshServer(id);
      await refreshCacheStatusFor(id, { warmIfCold: false });
      if (side === 'source') {
        setSourceUsersNonce((n) => n + 1);
      } else {
        setDestUsersNonce((n) => n + 1);
      }
    } catch {
      /* best-effort */
    }
  };

  // ── Workflow tab switch invalidation ──────────────────────────
  useEffect(() => {
    const sourceVisible = new Set(sourceServers.map((s) => s.id));
    const destVisible = new Set(destServers.map((s) => s.id));
    if (sourceServerId && !sourceVisible.has(sourceServerId)) {
      setSourceServerId('');
      setCheckedSourceUsers(new Set());
      setSourceUsers([]);
    }
    if (destServerId && !destVisible.has(destServerId)) {
      setDestServerId('');
      setCheckedDestUsers(new Set());
      setDestUsers([]);
    }
    // A mode switch flips which playlist kind is selectable (smart
    // versus regular), so the queue is always dropped.
    setQueued(new Map());
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [workflowMode]);

  // When source server changes, drop checked source users + queued playlists
  // (their userIds belonged to the prior server).
  const handleSelectSourceServer = (id: string) => {
    setSourceServerId(id);
    setCheckedSourceUsers(new Set());
    setSourceUsers([]);
    setQueued(new Map());
  };
  const handleSelectDestServer = (id: string) => {
    setDestServerId(id);
    setCheckedDestUsers(new Set());
    setDestUsers([]);
  };

  // Source and dest selections are independent: checking a source
  // user must NOT auto-check the matching same-identity user on the
  // dest side. Picking a source user encodes nothing about the dest
  // target, and vice versa. The only side-effect of unchecking a
  // source user is dropping any of their queued playlists.
  const handleToggleSourceUser = (userId: string) => {
    setCheckedSourceUsers((prev) => {
      const next = new Set(prev);
      if (next.has(userId)) {
        next.delete(userId);
        setQueued((q) => {
          const m = new Map(q);
          for (const k of Array.from(m.keys())) {
            if (k.startsWith(`${userId}::`)) m.delete(k);
          }
          return m;
        });
      } else {
        next.add(userId);
      }
      return next;
    });
  };
  const handleToggleDestUser = (userId: string) => {
    setCheckedDestUsers((prev) => {
      const next = new Set(prev);
      if (next.has(userId)) {
        next.delete(userId);
      } else {
        next.add(userId);
      }
      return next;
    });
  };

  // ── Source playlist multi-select ──────────────────────────────
  // Accepts the full user object so we can capture BOTH the frontend
  // key (for Set/Map keying) and the backend-resolvable id (for the
  // copy POST body). Sending the frontend userKey to the wire 404s
  // because _find_user doesn't understand app_user_uuid yet.
  const togglePlaylistFor = (u: PlaylistMgmtUser) => (p: PlaylistSpec) => {
    // Copy mode queues regular playlists; Smart Playlist mode queues
    // smart ones. The card only ever offers the right kind, so this
    // just guards against a stale row.
    if (workflowMode === 'smart' ? !p.is_smart : p.is_smart) return;
    const uid = userKey(u);
    setQueued((prev) => {
      const m = new Map(prev);
      const k = queueKey(uid, p.playlist_id);
      if (m.has(k)) {
        m.delete(k);
      } else {
        m.set(k, {
          sourceUserId: uid,
          sourceApiUserId: apiUserId(u),
          sourceUsername: u.username,
          playlist: p,
          // Smart Playlist mode: a new pick inherits the current
          // default migration mode (the mode panel's "Set all").
          smartMode: workflowMode === 'smart' ? smartDefaultMode : undefined,
        });
      }
      return m;
    });
  };
  const selectedPlaylistIdsFor = (userId: string): Set<string> => {
    const out = new Set<string>();
    for (const [k, entry] of queued) {
      if (entry.sourceUserId === userId) out.add(entry.playlist.playlist_id);
      // suppress unused k
      void k;
    }
    return out;
  };

  // ── Smart Playlist mode: per-playlist migration mode ──────────
  // Set one queued smart playlist's mode.
  const setQueuedMode = (entry: QueuedPlaylist, mode: SmartMigrateMode) => {
    setQueued((prev) => {
      const m = new Map(prev);
      const k = queueKey(entry.sourceUserId, entry.playlist.playlist_id);
      const cur = m.get(k);
      if (cur) m.set(k, { ...cur, smartMode: mode });
      return m;
    });
  };
  // Set every queued playlist's mode AND the default for new picks.
  const setAllQueuedMode = (mode: SmartMigrateMode) => {
    setSmartDefaultMode(mode);
    setQueued((prev) => {
      const m = new Map(prev);
      for (const [k, entry] of m) m.set(k, { ...entry, smartMode: mode });
      return m;
    });
  };

  const queuedArr = useMemo(() => Array.from(queued.values()), [queued]);

  // FEUI-03: ActiveDeploysPanel resolves a deploy row's
  // source_playlist_id → name via playlistNameById. That map was
  // rebuilt every render purely from queuedArr, so the moment
  // doDeploy clears the queue (setQueued(new Map())) the labels went
  // empty and the just-created deploy rows lost their playlist names.
  // Keep an accumulating cache that only ever MERGES in names from
  // the live queue and is never reset, so labels survive the clear.
  const playlistNameCacheRef = useRef<Record<string, string>>({});
  for (const q of queuedArr) {
    playlistNameCacheRef.current[q.playlist.playlist_id] = q.playlist.name;
  }

  // Checked dest users -> objects (for the deploy bar dropdown).
  const checkedDestUserObjects = useMemo(
    () => destUsers.filter((u) => checkedDestUsers.has(userKey(u))),
    [destUsers, checkedDestUsers],
  );

  // ── Deploy ────────────────────────────────────────────────────
  //
  // Cartesian fan-out: every queued source playlist gets copied to
  // every checked destination user. N source playlists × K dest users
  // = N×K copies fired serially (Plex/Jellyfin/Emby write paths aren't
  // safe to parallelise — same-user concurrent writes can race on the
  // playlist row create).
  //
  // Per-copy errors are captured into a CopyResultEntry and the batch
  // continues. The result panel below stacks them so the end user can
  // see exactly which (source playlist, dest user) pair failed. For
  // DEST_USER_TOKEN_MISSING we mark the result + remember which dest
  // user is missing a token; when the batch finishes we open the token
  // modal for the first such user, so the end user can save a token
  // and re-deploy without re-checking everyone.
  // Every queued (source playlist, dest user) pair becomes its own
  // JobRecord on the JobQueue. The Deploy click submits the batch
  // quickly (one POST per pair, in parallel) and the
  // ActiveDeploysPanel takes over for visibility + persistence. The
  // panel polls /api/playlist-mgmt/copy-jobs every 2s, survives page
  // reload, and exposes Clone Deploy + Dismiss / Refresh on each row.
  const doDeploy = async () => {
    if (queuedArr.length === 0 || checkedDestUserObjects.length === 0 || submitting) return;
    setSubmitting(true);
    setDeployError(null);

    // Smart Playlist mode: the queued smart playlists are migrated,
    // not copied. The cartesian fan-out matches copy mode: each
    // queued smart playlist x each checked destination user becomes
    // one migration item. The migrated playlist is created OWNED BY
    // that destination user - a true smart playlist on a Plex
    // destination, a static materialisation on a Jellyfin / Emby
    // destination - so checking several destination users produces
    // one migration per user.
    if (workflowMode === 'smart') {
      const smartItems: SmartMigrateItem[] = [];
      for (const targetUser of checkedDestUserObjects) {
        const destApiUserId = apiUserId(targetUser);
        for (const entry of queuedArr) {
          smartItems.push({
            source_server_id: sourceServerId,
            source_user_id: entry.sourceApiUserId,
            source_playlist_id: entry.playlist.playlist_id,
            dest_server_id: destServerId,
            dest_user_id: destApiUserId,
            // Per-playlist mode: hard copy transfers the current
            // matched items; filter mode re-applies the saved filter.
            hard_copy: entry.smartMode === 'hard_copy',
          });
        }
      }
      try {
        const r = await api.submitSmartPlaylistMigrateJob({
          items: smartItems,
          label: batchLabel.trim() || null,
        });
        setActiveSmartJobId(r.job_id);
        setSmartExpectedCount(smartItems.length);
        // Only clear the queue once the job is actually accepted.
        setQueued(new Map());
      } catch (e) {
        // Keep the queue intact so the operator can retry.
        setDeployError(errorText(e));
      }
      setSubmitting(false);
      return;
    }

    // Build every (source playlist × dest user) pair as items in ONE
    // batch submission. The internal ThreadPoolExecutor inside
    // services.playlist_copy.copy_playlist_batch runs the per-item
    // copies in parallel; the queue's serial worker pump still runs
    // ONE batch at a time. Per-item failures land as structured
    // error_code rows inside batch.results without aborting the
    // batch.
    const items: PlaylistCopyIn[] = [];
    for (const targetUser of checkedDestUserObjects) {
      const destApiUserId = apiUserId(targetUser);
      for (const entry of queuedArr) {
        items.push({
          source_server_id: sourceServerId,
          source_user_id: entry.sourceApiUserId,
          source_playlist_id: entry.playlist.playlist_id,
          dest_server_id: destServerId,
          dest_user_id: destApiUserId,
        });
      }
    }
    // Per-submit parallelism override: empty -> server uses
    // playlist_mgmt_batch_workers tunable. Numeric input parsed
    // tolerantly; out-of-range values fall back to undefined so the
    // server picks the default.
    let parallelism: number | undefined = undefined;
    if (batchParallelismOverride.trim() !== '') {
      const n = Number.parseInt(batchParallelismOverride, 10);
      if (Number.isFinite(n) && n >= 1) {
        parallelism = Math.min(n, batchMaxSize);
      }
    }
    // Cross-source mode is no-op in the single-source-server layout
    // (every item shares one source_server_id); when multi-source
    // queueing lands, 'per_source' splits items into N submissions
    // (one per distinct source_server_id) and 'combined' submits one
    // batch as below. Either way the same /copy-batch-job endpoint
    // handles it - there's no separate API surface.
    void crossSourceMode;
    try {
      await api.submitPlaylistCopyBatchJob({
        items,
        parallelism: parallelism ?? null,
        label: batchLabel.trim() || null,
      });
    } catch (e) {
      // Submission failed: keep the queue intact and surface the
      // error rather than silently discarding the operator's work.
      setDeployError(errorText(e));
      setSubmitting(false);
      return;
    }
    setSubmitting(false);
    // Bump the submission tick so the ActiveDeploysPanel kicks an
    // immediate poll instead of waiting up to 2s.
    setSubmissionTick((n) => n + 1);
    // Clear the queue + refresh dest cache. User checks are left
    // intact so the end user can deploy more without re-checking
    // everyone.
    setQueued(new Map());
    void refreshCacheStatusFor(destServerId, { warmIfCold: false });
  };

  // Clone-deploy from the ActiveDeploysPanel — re-submit a single
  // copy with the same params and nudge the poller.
  const cloneDeploy = async (body: PlaylistCopyIn) => {
    try {
      await api.submitPlaylistCopyJob(body);
    } catch (e) {
      // eslint-disable-next-line no-console
      console.warn('clone submitPlaylistCopyJob failed', body, e);
    }
    setSubmissionTick((n) => n + 1);
  };

  return (
    <div data-testid="playlist-mgmt-panel">
      {serversError && (
        <div className="banner error">Could not load servers: {serversError}</div>
      )}
      {servers.length === 0 && !serversError && (
        <div className="banner info">
          No servers registered yet. Open the <strong>Servers</strong> tab to add one.
        </div>
      )}

      <BackendTabStrip
        servers={servers}
        activeBackend={workflowMode === 'cross' || workflowMode === 'smart'
          ? 'plex'
          : (workflowMode as BackendType)}
        onChange={(b) => setWorkflowMode(b as WorkflowMode)}
        extraTabs={[{
          id: 'smart',
          label: 'Smart Playlist',
          title: 'Migrate Plex smart playlists (saved filters) to another server.',
        }]}
        activeExtraTab={workflowMode === 'smart' ? 'smart' : null}
        onExtraTabSelect={() => setWorkflowMode('smart')}
      />

      {workflowMode === 'smart' && (
        <div className="banner info" style={{ fontSize: 12, marginBottom: 8 }}>
          <strong>Smart Playlist Migration.</strong>{' '}
          A Plex smart playlist is a saved filter. Pick a Plex source,
          check the smart playlists to migrate, then pick a destination
          server and user. A Plex destination gets a true smart
          playlist re-created; a Jellyfin or Emby destination, which
          has no smart concept, gets a static snapshot of the
          playlist's current contents. Either way the migrated playlist
          is created as the destination user you check, so its filter
          evaluates against that user's own library.
        </div>
      )}

      <PlaylistMgmtServersAndUsersPanel
        sourceServers={sourceServers}
        destServers={destServers}
        pings={pings}
        cacheStatus={cacheStatus}
        sourceServerId={sourceServerId}
        onSelectSourceServer={handleSelectSourceServer}
        destServerId={destServerId}
        onSelectDestServer={handleSelectDestServer}
        checkedSourceUsers={checkedSourceUsers}
        onToggleSourceUser={handleToggleSourceUser}
        checkedDestUsers={checkedDestUsers}
        onToggleDestUser={handleToggleDestUser}
        onSourceUsersChange={setSourceUsers}
        onDestUsersChange={setDestUsers}
        sourceAutoWarming={warmingServers.has(sourceServerId)}
        destAutoWarming={warmingServers.has(destServerId)}
        sourceUsersNonce={sourceUsersNonce}
        destUsersNonce={destUsersNonce}
        showDestTokenAffordance={showDestTokenAffordance}
        onSaveDestToken={(u) => setTokenModalForUser(u)}
        onManualRefresh={(side) => void handleManualRefresh(side)}
        destUserIsTransferable={destUserIsTransferable}
      />

      {/* Playlist type filter. Mirrors the "Build caches"
          Collections / Playlists checkbox pattern from Servers >
          Overview so the operator sees a consistent control across
          panels. Filters every checked source user's playlists at
          once. Hidden until at least one source user is checked so
          the panel isn't cluttered before there's anything to filter. */}
      {checkedSourceUsers.size > 0 && (
        <div
          className="panel"
          style={{
            marginTop: 10,
            display: 'flex',
            gap: 14,
            alignItems: 'center',
            flexWrap: 'wrap',
            fontSize: 12,
          }}
        >
          <strong style={{ marginRight: 4 }}>Show types:</strong>
          <label
            style={{ display: 'flex', alignItems: 'center', gap: 4 }}
            title="Include music / audio-book / spoken playlists. Plex playlistType=audio; Jellyfin/Emby MediaType=Audio."
          >
            <input
              type="checkbox"
              checked={typeFilterAudio}
              onChange={(e) => setTypeFilterAudio(e.target.checked)}
            />
            Audio
          </label>
          <label
            style={{ display: 'flex', alignItems: 'center', gap: 4 }}
            title="Include movie / TV / music-video playlists. Plex playlistType=video; Jellyfin/Emby MediaType=Video."
          >
            <input
              type="checkbox"
              checked={typeFilterVideo}
              onChange={(e) => setTypeFilterVideo(e.target.checked)}
            />
            Video
          </label>
          <label
            style={{ display: 'flex', alignItems: 'center', gap: 4 }}
            title="Include photo playlists. Plex playlistType=photo; Jellyfin/Emby MediaType=Photo."
          >
            <input
              type="checkbox"
              checked={typeFilterPhoto}
              onChange={(e) => setTypeFilterPhoto(e.target.checked)}
            />
            Photo
          </label>
          <span className="help" style={{ flex: 1, minWidth: 240, color: 'var(--text-dim)' }}>
            Untick a type to hide its playlists across every user
            below. Playlists with no type tag (older cache rows)
            always remain visible.
          </span>
        </div>
      )}

      {/* Per-checked-source-user playlists cards. End user picks
          playlists to copy from here. There are no destination-side
          cards: the dest user is a write target, not a read source,
          so listing their existing playlists here would be clutter
          that doesn't drive any decision. */}
      {Array.from(checkedSourceUsers).map((uid) => {
        const u = sourceUsers.find((x) => userKey(x) === uid);
        if (!u) return null;
        return (
          <UserPlaylistsCard
            key={`src-${uid}`}
            side="source"
            serverId={sourceServerId}
            serverLabel={sourceServerLabel}
            userId={apiUserId(u)}
            username={u.username}
            role={u.role}
            selectedPlaylistIds={selectedPlaylistIdsFor(uid)}
            onTogglePlaylist={togglePlaylistFor(u)}
            onUserNotFound={() => handleToggleSourceUser(uid)}
            playlistTypeFilter={playlistTypeFilter}
            smartMode={workflowMode === 'smart'}
          />
        );
      })}
      {/* Dest-side cards intentionally omitted per the new design. */}

      {/* Smart Playlist mode: per-playlist migration-mode picker
          (filter vs hard copy) + a "Set all" control. Hidden in copy
          mode and until at least one smart playlist is queued. */}
      {workflowMode === 'smart' && queuedArr.length > 0 && (
        <SmartMigrationModePanel
          queued={queuedArr}
          onSetMode={setQueuedMode}
          onSetAll={setAllQueuedMode}
        />
      )}

      {deployError && (
        <div className="banner error" style={{ marginTop: 8 }}>
          Deploy failed: {deployError}
        </div>
      )}

      <PlaylistMgmtDeployBar
        queued={queuedArr}
        checkedDestUsers={checkedDestUserObjects}
        submitting={submitting}
        onDeploy={() => void doDeploy()}
        mode={workflowMode === 'smart' ? 'smart' : 'copy'}
      />

      {/* Batch options. Hidden until at least one playlist is queued
          so the panel is empty on first load. Smart Playlist mode
          submits a single smart_playlist_migrate job, so the
          copy-batch knobs do not apply and the section is hidden. */}
      {queuedArr.length > 0 && workflowMode !== 'smart' && (
        <details
          className="panel"
          style={{ marginTop: 8, padding: 8, fontSize: 12 }}
        >
          <summary style={{ cursor: 'pointer', fontWeight: 600 }}>
            Batch options
            <span style={{ color: 'var(--text-dim)', fontWeight: 400, marginLeft: 6 }}>
              (parallelism / label / cross-source)
            </span>
          </summary>
          <div
            style={{
              display: 'flex',
              flexWrap: 'wrap',
              gap: 14,
              marginTop: 8,
              alignItems: 'flex-start',
            }}
          >
            <label style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
              <span>
                Parallelism override
                <span style={{ color: 'var(--text-dim)', marginLeft: 4 }}>
                  (default {batchDefaultWorkers}, max {batchMaxSize})
                </span>
              </span>
              <input
                type="number"
                min={1}
                max={batchMaxSize}
                value={batchParallelismOverride}
                onChange={(e) => setBatchParallelismOverride(e.target.value)}
                placeholder={String(batchDefaultWorkers)}
                title="Per-submit worker pool override. Leave blank to use the playlist_mgmt_batch_workers tunable."
                style={{ width: 100 }}
              />
            </label>
            <label style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
              <span>Batch label</span>
              <input
                type="text"
                value={batchLabel}
                onChange={(e) => setBatchLabel(e.target.value)}
                placeholder={`batch of ${queuedArr.length * checkedDestUserObjects.length} playlist(s)`}
                title="Optional label shown in the activity feed and the active-deploys panel header."
                style={{ width: 240 }}
              />
            </label>
            <fieldset
              style={{
                border: '1px solid var(--text-dim)',
                borderRadius: 4,
                padding: '4px 8px',
                margin: 0,
              }}
            >
              <legend style={{ padding: '0 4px' }}>Cross-source batches</legend>
              <label style={{ display: 'block' }}>
                <input
                  type="radio"
                  name="cross-source"
                  value="per_source"
                  checked={crossSourceMode === 'per_source'}
                  onChange={() => setCrossSourceMode('per_source')}
                />
                {' '}One batch per source server
                <span style={{ color: 'var(--text-dim)', marginLeft: 6 }}>
                  (recommended; each source honors its own backpressure)
                </span>
              </label>
              <label style={{ display: 'block' }}>
                <input
                  type="radio"
                  name="cross-source"
                  value="combined"
                  checked={crossSourceMode === 'combined'}
                  onChange={() => setCrossSourceMode('combined')}
                />
                {' '}One combined batch across all sources
              </label>
              <div style={{ color: 'var(--text-dim)', marginTop: 4, fontSize: 11 }}>
                No effect today — the current layout pins source to a single server.
                Wired for future multi-source queueing.
              </div>
            </fieldset>
          </div>
        </details>
      )}

      {/* Copy mode: the ActiveDeploysPanel tracks every Deploy as a
          JobRecord on the existing queue, persists across page reload,
          and exposes Clone Deploy + Dismiss + Refresh on each row.
          Smart Playlist mode: the smart_playlist_migrate job is not a
          copy job (the copy-jobs feed would not surface it), so the
          dedicated SmartMigrationProgressPanel tails smart_playlist.log
          and shows per-playlist migration records instead. */}
      {workflowMode === 'smart' ? (
        <SmartMigrationProgressPanel
          jobId={activeSmartJobId}
          expectedCount={smartExpectedCount}
        />
      ) : (
        <ActiveDeploysPanel
          submissionTick={submissionTick}
          labels={{
            sourceUsernameByApiId: Object.fromEntries(
              sourceUsers.map((u) => [apiUserId(u), u.username]),
            ),
            destUsernameByApiId: Object.fromEntries(
              destUsers.map((u) => [apiUserId(u), u.username]),
            ),
            // FEUI-03: feed from the accumulating cache (survives the
            // post-deploy queue clear), not the queue-derived map.
            playlistNameById: { ...playlistNameCacheRef.current },
            destServerLabelById: Object.fromEntries(
              servers.map((s) => [s.id, s.name]),
            ),
          }}
          onCloneDeploy={(body) => void cloneDeploy(body)}
        />
      )}

      {tokenModalForUser && (
        <PlexHomeTokenModal
          open={true}
          serverId={destServerId}
          serverLabel={destServerLabel || '(destination server)'}
          username={tokenModalForUser.username}
          userRole={tokenModalForUser.role}
          hasExistingToken={tokenModalForUser.has_token}
          cachedAdmin={cachedAdmin}
          onAdminCached={setCachedAdmin}
          onClose={() => setTokenModalForUser(null)}
          onSaved={() => {
            setTokenModalForUser(null);
            setDestUsersNonce((n) => n + 1);
          }}
        />
      )}
    </div>
  );
}
