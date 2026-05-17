// Top-level Playlist Management surface (Jobs ▸ Playlist Management).
//
// 2026-05-16 redesign (end user request): the prior two-column single-
// user-pick shape was scrapped in favour of a multi-select layout that
// mirrors Run Job's Mode-and-Servers panel:
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
// Auto-warm cache on first server selection (W2) and the Plex Home
// per-user-token UI (W4 — Save Token affordance + DEST_USER_TOKEN_MISSING
// error handling) carry over from the prior shape.

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
} from '../api';
import { BackendTabStrip, type BackendType, serversForBackend } from './BackendTabStrip';
import { PlaylistMgmtServersAndUsersPanel } from './PlaylistMgmtServersAndUsersPanel';
import { UserPlaylistsCard } from './UserPlaylistsCard';
import { PlaylistMgmtDeployBar, type QueuedPlaylist } from './PlaylistMgmtDeployBar';
import { ActiveDeploysPanel } from './ActiveDeploysPanel';
import { PlexHomeTokenModal, type AdminCreds } from './PlexHomeTokenModal';

type WorkflowMode = 'plex' | 'jellyfin' | 'emby' | 'cross';

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
  // Mirror of the users list each child Side reports up; lets us
  // resolve user_id -> username/role for the playlists cards below.
  const [sourceUsers, setSourceUsers] = useState<PlaylistMgmtUser[]>([]);
  const [destUsers, setDestUsers] = useState<PlaylistMgmtUser[]>([]);

  // ── Playlist multi-select (source side) ───────────────────────
  // Key = `${sourceUserId}::${playlist_id}` so two users with the
  // same playlist name don't collide. Value is the full QueuedEntry.
  const [queued, setQueued] = useState<Map<string, QueuedEntry>>(new Map());

  // ── Result state ──────────────────────────────────────────────
  // 2026-05-16 (end user request): the prior single-target dropdown was
  // dropped in favour of cartesian fan-out — every queued source
  // playlist gets copied to every checked dest user. No explicit
  // target state needed.
  const [submitting, setSubmitting] = useState(false);
  // Bumped every time the end user clicks Deploy (or Clone Deploy)
  // so the ActiveDeploysPanel kicks an immediate poll for the freshly-
  // queued jobs instead of waiting up to 2s for its next interval.
  const [submissionTick, setSubmissionTick] = useState(0);

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
  const workflowServers = useMemo(() => {
    if (workflowMode === 'cross') return servers;
    return serversForBackend(servers, workflowMode);
  }, [servers, workflowMode]);

  // ── Initial load: servers + auth_mode tunable ─────────────────
  useEffect(() => {
    let cancelled = false;
    api.listServers()
      .then((r) => { if (!cancelled) setServers(r); })
      .catch((e) => { if (!cancelled) setServersError(String(e instanceof Error ? e.message : e)); });
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

  // Transferability predicate (2026-05-16 end user request,
  // 2026-05-17 fix). A user is shown in the destination picker only
  // when a copy could authenticate as them.
  //
  // The owner / admin case is ABSOLUTE: admin tokens can always write
  // to or as the owner (it IS the owner's token). The prior version
  // hid Plex owners under strict + per_user_token mode because they
  // typically have ``has_token=false`` (their token is the admin one,
  // not a per-user-saved one). That was wrong — the owner is always
  // a writable target. End user's "I can't transfer to myself" bug
  // report 2026-05-17 traces back to that miss.
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
  // 2026-05-16 (end user request): the prior gate skipped the rebuild
  // when cache_status already had rows (even stale ones from a prior
  // session). The end user's mental model is "picking a server should
  // build/refresh the cache" — so we now fire on every first-selection
  // regardless of existing rows. The `warmedServersRef` guard still
  // prevents repeated rebuilds within one session.
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
    const visible = new Set(workflowServers.map((s) => s.id));
    if (sourceServerId && !visible.has(sourceServerId)) {
      setSourceServerId('');
      setCheckedSourceUsers(new Set());
      setSourceUsers([]);
      setQueued(new Map());
    }
    if (destServerId && !visible.has(destServerId)) {
      setDestServerId('');
      setCheckedDestUsers(new Set());
      setDestUsers([]);
    }
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

  // Source and dest selections are independent (2026-05-17 end user
  // correction: an earlier revision auto-checked the matching same-
  // identity user on the dest side when a source user was checked.
  // The operator explicitly does NOT want that — picking a source
  // user encodes nothing about the dest target, and vice versa. The
  // only side-effect of unchecking a source user is dropping any of
  // their queued playlists.
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
    if (p.is_smart) return;
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

  const queuedArr = useMemo(() => Array.from(queued.values()), [queued]);

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
  // 2026-05-17 (end user request, Option A): every queued (source
  // playlist, dest user) pair now becomes its own JobRecord on the
  // existing JobQueue. The Deploy click submits the batch quickly
  // (one POST per pair, in parallel) and the ActiveDeploysPanel takes
  // over for visibility + persistence. The panel polls
  // /api/playlist-mgmt/copy-jobs every 2s, survives page reload, and
  // exposes Clone Deploy + Dismiss / Refresh on each row.
  const doDeploy = async () => {
    if (queuedArr.length === 0 || checkedDestUserObjects.length === 0 || submitting) return;
    setSubmitting(true);
    const submissions: PlaylistCopyIn[] = [];
    for (const targetUser of checkedDestUserObjects) {
      const destApiUserId = apiUserId(targetUser);
      for (const entry of queuedArr) {
        submissions.push({
          source_server_id: sourceServerId,
          source_user_id: entry.sourceApiUserId,
          source_playlist_id: entry.playlist.playlist_id,
          dest_server_id: destServerId,
          dest_user_id: destApiUserId,
        });
      }
    }
    // Fire submissions in parallel; the queue worker still runs jobs
    // serially (Plex's API doesn't like concurrent same-user writes),
    // but submitting in parallel is cheap and gets every row onto the
    // active-deploys panel immediately.
    await Promise.all(submissions.map(async (body) => {
      try {
        await api.submitPlaylistCopyJob(body);
      } catch (e) {
        // A submit failure is rare (the queue accepts everything); log
        // and continue so other submissions still go through.
        // eslint-disable-next-line no-console
        console.warn('submitPlaylistCopyJob failed', body, e);
      }
    }));
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
    <>
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
        activeBackend={workflowMode === 'cross' ? 'plex' : (workflowMode as BackendType)}
        onChange={(b) => setWorkflowMode(b as WorkflowMode)}
      />

      <PlaylistMgmtServersAndUsersPanel
        servers={workflowServers}
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

      {/* Per-checked-source-user playlists cards. End user picks
          playlists to copy from here. 2026-05-16 (end user request):
          destination-side cards were removed — the dest user is a
          write target, not a read source, so listing their existing
          playlists here was clutter that didn't drive any decision. */}
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
          />
        );
      })}
      {/* Dest-side cards intentionally omitted per the new design. */}

      <PlaylistMgmtDeployBar
        queued={queuedArr}
        checkedDestUsers={checkedDestUserObjects}
        submitting={submitting}
        onDeploy={() => void doDeploy()}
      />

      {/* 2026-05-17 — replaces the prior synchronous copy-result panel.
          The ActiveDeploysPanel tracks every Deploy as a JobRecord on
          the existing queue, persists across page reload, and exposes
          Clone Deploy + Dismiss + Refresh on each row. */}
      <ActiveDeploysPanel
        submissionTick={submissionTick}
        labels={{
          sourceUsernameByApiId: Object.fromEntries(
            sourceUsers.map((u) => [apiUserId(u), u.username]),
          ),
          destUsernameByApiId: Object.fromEntries(
            destUsers.map((u) => [apiUserId(u), u.username]),
          ),
          playlistNameById: Object.fromEntries(
            queuedArr.map((q) => [q.playlist.playlist_id, q.playlist.name]),
          ),
          destServerLabelById: Object.fromEntries(
            servers.map((s) => [s.id, s.name]),
          ),
        }}
        onCloneDeploy={(body) => void cloneDeploy(body)}
      />

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
    </>
  );
}
