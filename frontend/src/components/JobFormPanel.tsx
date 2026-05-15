// Job submission form (v0.9.0).
//
// Three modes:
//   * Snapshot   pick source server, pick libraries, write JSON files.
//   * Restore   pick destination server, pick existing export files,
//               merge into Plex.
//   * Direct   pick source AND destination servers side-by-side,
//               pick libraries, transfer in memory without an
//               intermediate file. Source and destination must
//               be different.
//
// Every CLI flag from plexmigrate.py has a clearly labelled form
// control. The form does not submit the Plex URL or token directly
//  those live in the registry the user manages from the Servers tab.

import { useEffect, useRef, useState } from 'react';
import { api, ExportArchive, LibraryDescriptor, PingResult, ServerManagedUser, ServerUser, ServerView, DashboardFrame, Snapshot } from '../api';
import { serverSupportsFastCollections } from '../utils/plexVersion';
import { RestoreModeSelector, RestoreMode, MergeWatchStrategy } from './RestoreModeSelector';
import { ReplaceConfirmModal } from './ReplaceConfirmModal';

// v0.9.1: live status indicator polling cadence for the server pickers.
const PING_INTERVAL_MS = 30_000;


// PR-11 - the user picker reads from the local managed_users DB
// instead of hitting the live Plex API on every job-form visit.
// Shape adapter: DB rows carry ``username`` and ``kind`` directly;
// the picker UI was originally built against the live API's
// ``ServerUser`` shape with ``plex_id`` / ``raw_name``. Translating
// here keeps the downstream render code unchanged.
function dbUserToPickerUser(u: ServerManagedUser): ServerUser {
  return {
    kind: u.kind,
    plex_id: u.username,
    raw_name: u.username,
    display_name: u.display_name || '',
  };
}

// Fetch DB-backed users for one server with a one-shot cold-start
// recovery: if the DB has no rows yet (the operator just installed
// PR-11 without re-testing their existing servers), fire a single
// sync and re-fetch. Failures swallow into an empty list - the
// downstream effect surfaces a single combined error if multiple
// servers fail.
async function fetchPickerUsers(serverId: string): Promise<ServerUser[]> {
  let res = await api.listServerManagedUsers(serverId);
  if (res.users.length === 0) {
    try {
      await api.syncServerManagedUsers(serverId);
      res = await api.listServerManagedUsers(serverId);
    } catch {
      // Sync 502'd (server unreachable or token rejected). Fall
      // through with whatever the DB has, including the empty list -
      // the picker shows "no users" and the operator can fix the
      // server in the Servers tab.
    }
  }
  return res.users.map(dbUserToPickerUser);
}

interface Props {
  snapshot: DashboardFrame | null;
}

type Mode = 'snapshot' | 'restore' | 'direct';

export function JobFormPanel({ snapshot }: Props) {
  const [mode, setMode] = useState<Mode>('snapshot');

  // Registry-aware server selection.
  // v0.10.0  destinations are now a Set so direct-transfer and import
  // jobs can target multiple servers in one job (fan-out). Membership
  // is order-insensitive; the rendered picker is a multi-select grid.
  // The source is still a single string  fan-out is one source many
  // destinations, never the reverse.
  const [servers, setServers] = useState<ServerView[]>([]);
  const [sourceServerName, setSourceServerName] = useState<string>('');
  const [destServerNames, setDestServerNames] = useState<Set<string>>(new Set());
  const [serversError, setServersError] = useState<string | null>(null);

  // v0.9.1: live ping results keyed by server id. The selectors below
  // read this to render a status dot and latency next to each option.
  // Kept separate from ``servers`` so a ping refresh doesn't trigger
  // the library-fetch effect (which depends on ``servers``).
  const [pings, setPings] = useState<Record<string, PingResult>>({});
  const pollTimerRef = useRef<number | null>(null);

  // Library picker (snapshot + direct).
  const [libraries, setLibraries] = useState<LibraryDescriptor[]>([]);
  const [selectedLibs, setSelectedLibs] = useState<Set<string>>(new Set());
  const [librariesError, setLibrariesError] = useState<string | null>(null);

  // Export file picker (import only).
  const [snapshots, setExports] = useState<ExportArchive[]>([]);
  const [selectedFiles, setSelectedFiles] = useState<Set<string>>(new Set());
  // Restore source toggle. Defaults to 'snapshot' (registered .db) since
  // that's the post-PR-13 storage shape; legacy JSON archives still work
  // via the 'file' option.
  const [restoreSource, setRestoreSource] = useState<'snapshot' | 'file'>('snapshot');
  const [registeredSnapshots, setRegisteredSnapshots] = useState<Snapshot[]>([]);
  const [selectedSnapshotId, setSelectedSnapshotId] = useState<string | null>(null);
  const [snapshotsLoadError, setSnapshotsLoadError] = useState<string | null>(null);

  // Common engine flags.
  const [workers, setWorkers] = useState<string>('');
  const [scrobbleWorkers, setScrobbleWorkers] = useState<string>('');
  const [verbose, setVerbose] = useState(false);
  const [outputDir, setOutputDir] = useState<string>('');
  const [logDir, setLogDir] = useState<string>('');

  // Restore-only flags.
  const [strictMatch, setStrictMatch] = useState(true);
  const [overwritePlaylists, setOverwritePlaylists] = useState(false);
  const [fastCollectionDetection, setFastCollectionDetection] = useState(false);
  const [skipPlaylistPrebuild, setSkipPlaylistPrebuild] = useState(false);

  // v0.13.x Restore mode (Merge / Replace). Shared between the
  // ``restore`` and ``direct`` operations - both end up in the same
  // engine path. Defaults to ``merge`` (the safe, additive, current
  // behaviour); Replace is opt-in and gated by the typed-confirmation
  // modal below.
  const [restoreMode, setRestoreMode] = useState<RestoreMode>('merge');
  const [autoCaptureBeforeReplace, setAutoCaptureBeforeReplace] = useState(true);
  // v0.13.x: Merge-mode sub-toggle for the watch-count math. Defaults
  // to "higher" (the legacy idempotent behaviour) so existing job
  // submissions are wire-identical until the operator opts into the
  // additive variant.
  const [mergeWatchStrategy, setMergeWatchStrategy] = useState<MergeWatchStrategy>('higher');
  // Gate for the typed-REPLACE modal. Submit() flips it true when the
  // operator hits Submit with mode=replace; the modal's onConfirm
  // calls ``submitConfirmed()`` which actually fires the API request.
  const [replaceModalOpen, setReplaceModalOpen] = useState(false);
  // PR-3 / Phase D - four-flag data-type filter. Replaces the two old
  // skip_* checkboxes (skip_collections / skip_playlists). Applies to
  // every job mode (snapshot / import / direct) so the operator can
  // pick exactly which data types to migrate.
  const [includeWatchHistory, setIncludeWatchHistory] = useState(true);
  const [includeRatings, setIncludeRatings] = useState(true);
  const [includePlaylists, setIncludePlaylists] = useState(true);
  const [includeCollections, setIncludeCollections] = useState(true);
  // Snapshot-only: render a .plexexport.json sidecar at the end of the
  // run so the first Exports-tab download is instant. Off by default
  // (extra wall-clock cost); operators opt in per job.
  const [prebuildJsonSidecar, setPrebuildJsonSidecar] = useState(false);
  const atLeastOneType =
    includeWatchHistory || includeRatings || includePlaylists || includeCollections;

  // Snapshot-aware gating for the include_* toggles. When the operator
  // picks "From registered snapshot" in import mode AND a row is
  // selected, gate the include_* toggles by what the run *actually
  // gathered* (``captured_types``), not by what's in the cumulative
  // .db (``row_counts``).
  //
  // ``row_counts`` reflects the state of media.db at capture time,
  // which accumulates across runs - a playlists-only run still
  // surfaces non-zero ``watch_events`` from a previous capture for
  // the same server. ``captured_types`` is the explicit subset of
  // {watch_history, ratings, playlists, collections} this run
  // touched, matching the operator's mental model.
  //
  // Fallback: rows captured before ``captured_types`` was added to
  // the registry schema have ``captured_types === null``. For those
  // we walk back to the row_counts heuristic so the UI stays useful
  // for legacy entries.
  const _selectedSnapshot =
    mode === 'restore' && restoreSource === 'snapshot' && selectedSnapshotId
      ? registeredSnapshots.find((s) => s.id === selectedSnapshotId)
      : undefined;
  const gatingFromSnapshot = !!_selectedSnapshot;
  const _capturedTypes = _selectedSnapshot?.captured_types ?? null;
  const _rc = _selectedSnapshot?.row_counts || {};
  // Strict preference for captured_types when present; fall back to
  // row_counts otherwise.
  const _hasType = (type: string, rcKey: keyof typeof _rc): boolean => {
    if (!gatingFromSnapshot) return true;
    if (_capturedTypes !== null) return _capturedTypes.includes(type);
    return (_rc[rcKey] ?? 0) > 0;
  };
  const snapshotHasWatchHistory = _hasType('watch_history', 'watch_events');
  const snapshotHasRatings      = _hasType('ratings', 'ratings');
  const snapshotHasPlaylists    = _hasType('playlists', 'playlists');
  const snapshotHasCollections  = _hasType('collections', 'collections');

  const [remapOld, setRemapOld] = useState('');
  const [remapNew, setRemapNew] = useState('');

  // v0.9.6 Feature 4: per-side managed-user lists for direct transfer.
  // Loaded in parallel as soon as both source + destination are
  // chosen. ``null`` = not loaded yet for that side; an array (even
  // empty) means the fetch completed. ``sourceUsers === null ||
  // destUsers === null`` gates the Users section's rendering so it
  // doesn't flash an empty intersection during the fetch window.
  const [sourceUsers, setSourceUsers] = useState<ServerUser[] | null>(null);
  const [destUsers, setDestUsers] = useState<ServerUser[] | null>(null);
  const [usersError, setUsersError] = useState<string | null>(null);
  // Operator's set of included managed-user identifiers. Auto-initialised
  // to the full transferable intersection (all checked by default) and
  // then mutated by per-row checkbox toggles. Reset whenever either
  // server selection changes.
  const [includedUsers, setIncludedUsers] = useState<Set<string>>(new Set());

  // Submission state.
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [submitOk, setSubmitOk] = useState<string | null>(null);

  // v0.13 form-layout refactor: the Run-Job form is now grouped into
  // three sections - Mode & Servers (always visible) → Scope (always
  // visible) → Per-Run Settings (collapsed by default). The Scope
  // card holds the controls that answer "what data moves" (libraries,
  // data types, users, export files for import). The Per-Run Settings
  // card holds everything else (engine tuning, retry behaviour, path
  // remap, output / log dirs, watch+ratings strategy override).
  // 90%+ of operators never touch this section so collapsing it
  // keeps the form scannable.
  const [advancedOpen, setAdvancedOpen] = useState(false);

  // Per-Run Settings sub-tab. ``general`` holds the common knobs
  // (workers, scrobble_workers, strict_match, sidecar toggle, verbose);
  // ``advanced`` holds the deeper / migration-specific knobs
  // (output_dir, log_dir, path remap, skip prebuild, fast collection
  // detection, watch+ratings strategy override). Workspace state —
  // not persisted across submissions.
  const [perRunSubTab, setPerRunSubTab] = useState<'general' | 'advanced'>('general');

  // Per-job watch+ratings filter strategy override. Top of the
  // resolution chain (per-job → per-server → global default → "smart").
  // ``''`` means "inherit" (no override sent to backend).
  const [watchRatingsStrategy, setWatchRatingsStrategy] = useState<
    '' | 'smart' | 'force_bulk' | 'force_server_side'
  >('');

  // Load registered servers on mount.
  // v0.9.1 change: do NOT pre-select source/destination. The previous
  // code auto-selected the first registered server, which is exactly
  // the failure mode the user reported  operations silently used a
  // server the user never picked. The new flow forces the user to
  // make a deliberate selection (and the gated lower panels make this
  // visible).
  useEffect(() => {
    setServersError(null);
    api.listServers()
      .then((rows) => {
        setServers(rows);
      })
      .catch((e) => setServersError(String(e)));
  }, []);

  // v0.9.1: poll each registered server's status every 30 s so the
  // dots in the source/destination selectors stay current. Pings are
  // independent and cheap (single HTTP GET against /identity), so we
  // run them all in parallel each tick.
  useEffect(() => {
    if (servers.length === 0) {
      if (pollTimerRef.current !== null) {
        window.clearInterval(pollTimerRef.current);
        pollTimerRef.current = null;
      }
      return;
    }
    const pingAll = async () => {
      const tasks = servers.map(async (s) => {
        try {
          const result = await api.pingServer(s.id);
          setPings((prev) => ({ ...prev, [s.id]: result }));
        } catch {
          // Drop silently  the next tick retries.
        }
      });
      await Promise.allSettled(tasks);
    };
    pingAll();
    if (pollTimerRef.current !== null) window.clearInterval(pollTimerRef.current);
    pollTimerRef.current = window.setInterval(pingAll, PING_INTERVAL_MS);
    return () => {
      if (pollTimerRef.current !== null) {
        window.clearInterval(pollTimerRef.current);
        pollTimerRef.current = null;
      }
    };
  }, [servers]);

  // Whenever the source server selection changes (and we're in a mode
  // that reads from source) refresh the library list against that
  // server's cache. We hit /api/servers/{id}/libraries which forces a
  // live re-probe; the cached result on the registry row is the same
  // value but might be stale if the server was added a long time ago.
  useEffect(() => {
    if (mode === 'restore') return;
    const srv = servers.find((s) => s.name === sourceServerName);
    if (!srv) {
      setLibraries([]);
      return;
    }
    setLibrariesError(null);
    api.listServerLibraries(srv.id)
      .then((libs) => setLibraries(libs))
      .catch((e) => setLibrariesError(String(e)));
  }, [sourceServerName, mode, servers]);

  // Load existing snapshot files when import mode is active.
  // PR-13: the registry-backed listSnapshots() shape isn't compatible
  // with the JSON-file-picker UI here (snapshots are now ``.db``
  // files; the operator can't ingest them directly through this
  // picker until the importer learns to read DB snapshots). For now
  // the picker lists the legacy ``.plexexport.json`` archives moved
  // to ``snapshots/legacy/`` by the PR-13 migration - those remain
  // ingestible by the existing JSON-based importer.
  useEffect(() => {
    if (mode !== 'restore') return;
    api.listLegacySnapshots().then(setExports).catch(() => setExports([]));
    // Registered snapshots from snapshots.db - the primary import
    // source post-PR-13. The importer reads JSON; the route handler
    // for /api/job/import-from-snapshot materialises the sidecar
    // before forwarding, so the engine path stays unchanged.
    setSnapshotsLoadError(null);
    api.listSnapshots()
      .then((r) => setRegisteredSnapshots(r.snapshots))
      .catch((e) => {
        setRegisteredSnapshots([]);
        setSnapshotsLoadError(String(e));
      });
  }, [mode]);

  // Snapshot defaults from Settings. Loaded once on mount; the
  // per-server overrides map is cached so subsequent source-server
  // picks don't re-hit /api/settings.
  const [perServerSnapshotDefaults, setPerServerSnapshotDefaults] = useState<
    Record<string, Record<string, boolean | undefined>>
  >({});
  useEffect(() => {
    api.getSettings()
      .then((s) => {
        if (s.prebuild_json_sidecar_default === true) {
          setPrebuildJsonSidecar(true);
        }
        const raw = (s as unknown as Record<string, unknown>).snapshot_defaults_per_server;
        if (raw && typeof raw === 'object') {
          setPerServerSnapshotDefaults(raw as Record<string, Record<string, boolean | undefined>>);
        }
      })
      .catch(() => { /* defaults stay at built-in */ });
  }, []);

  // When the operator picks a different source server, layer in any
  // per-server overrides that exist for it. Non-destructive: fields
  // the operator already changed stay changed unless the new server
  // has an explicit override for them. Resolution order documented
  // in dbschema.md - this implements step (2) for ad-hoc jobs.
  useEffect(() => {
    if (mode !== 'snapshot' && mode !== 'direct') return;
    if (!sourceServerName) return;
    const srv = servers.find((s) => s.name === sourceServerName);
    if (!srv) return;
    const o = perServerSnapshotDefaults[srv.id];
    if (!o) return;
    if (typeof o.prebuild_json_sidecar === 'boolean') setPrebuildJsonSidecar(o.prebuild_json_sidecar);
    if (typeof o.include_watch_history === 'boolean') setIncludeWatchHistory(o.include_watch_history);
    if (typeof o.include_ratings === 'boolean') setIncludeRatings(o.include_ratings);
    if (typeof o.include_playlists === 'boolean') setIncludePlaylists(o.include_playlists);
    if (typeof o.include_collections === 'boolean') setIncludeCollections(o.include_collections);
    if (typeof o.skip_playlist_prebuild === 'boolean') setSkipPlaylistPrebuild(o.skip_playlist_prebuild);
    if (typeof o.fast_collection_detection === 'boolean') setFastCollectionDetection(o.fast_collection_detection);
  }, [sourceServerName, perServerSnapshotDefaults, servers, mode]);

  // v0.14 — Fast Collection Detection auto-defaulting based on Plex
  // version. Runs after the per-server-override effect above so an
  // explicit per-server value wins. When the source server's
  // ``plex_version`` is >= 1.32, default the toggle ON; otherwise
  // force it OFF (the engine would auto-fall-back anyway, but
  // surfacing the forced-off state in the UI is clearer than letting
  // operators tick a box that silently does nothing).
  //
  // Re-runs every time the source server changes — switching from a
  // supported server to an unsupported one drops the flag back to
  // OFF automatically, so a stale "on" can't leak into a job aimed
  // at an old Plex. There's no separate "user touched it" guard:
  // each new server-pick resets the toggle to the version-driven
  // default. If the operator wants a different value after that,
  // their click stays until the next server-pick.
  useEffect(() => {
    if (mode !== 'snapshot' && mode !== 'direct') return;
    if (!sourceServerName) return;
    const srv = servers.find((s) => s.name === sourceServerName);
    if (!srv) return;
    // Per-server explicit override on this field wins outright —
    // don't touch the toggle in that case (the previous effect
    // already applied it). Skip rule mirrors the override effect's
    // ``typeof === 'boolean'`` test exactly.
    const o = perServerSnapshotDefaults[srv.id];
    if (o && typeof o.fast_collection_detection === 'boolean') return;
    setFastCollectionDetection(serverSupportsFastCollections(srv.plex_version));
  }, [sourceServerName, perServerSnapshotDefaults, servers, mode]);

  // When the operator picks a snapshot as the import source (or
  // switches between snapshots), reseed the four include_* toggles
  // so "checked == this type was actually captured by the run".
  // Prefers ``captured_types`` (authoritative) and falls back to
  // ``row_counts`` for legacy rows that pre-date that column.
  useEffect(() => {
    if (mode !== 'restore') return;
    if (restoreSource !== 'snapshot') return;
    if (!selectedSnapshotId) return;
    const snap = registeredSnapshots.find((s) => s.id === selectedSnapshotId);
    if (!snap) return;
    if (snap.captured_types !== null) {
      const types = snap.captured_types;
      setIncludeWatchHistory(types.includes('watch_history'));
      setIncludeRatings(types.includes('ratings'));
      setIncludePlaylists(types.includes('playlists'));
      setIncludeCollections(types.includes('collections'));
    } else {
      const rc = snap.row_counts || {};
      setIncludeWatchHistory((rc.watch_events ?? 0) > 0);
      setIncludeRatings((rc.ratings ?? 0) > 0);
      setIncludePlaylists((rc.playlists ?? 0) > 0);
      setIncludeCollections((rc.collections ?? 0) > 0);
    }
  }, [selectedSnapshotId, registeredSnapshots, mode, restoreSource]);

  // v0.9.6 Feature 4: load users from BOTH servers in direct mode so
  // the form can compute the transferable intersection. Reset state
  // on every selection change so we never show a stale list. The
  // includedUsers default ("all checked") is set once after the
  // fetch resolves so the operator only needs to *un*check to
  // exclude  matching the spec.
  // v0.10.0  destinations are a set, so the per-user transferable
  // intersection now spans the source plus *every* selected
  // destination. A managed user must exist on every side to be
  // included; missing on any one destination drops them from the
  // default-checked set. The owner is treated the same way.
  // ``destServerNames`` is included in the dep list as a stable
  // string (sorted, joined) so React only re-runs the effect on
  // actual membership changes, not on every render.
  const destNamesKey = Array.from(destServerNames).sort().join('|');
  useEffect(() => {
    setSourceUsers(null);
    setDestUsers(null);
    setIncludedUsers(new Set());
    setUsersError(null);
    // v0.14 — Snapshot mode loads ONLY the source server's users
    // (no intersection needed; snapshot is one-way capture). We treat
    // ``destUsers`` as a mirror of ``sourceUsers`` so the shared
    // ``DirectUsersPanel`` renders the source list as "transferable"
    // (no greyed-out rows). Default-include everyone.
    if (mode === 'snapshot') {
      if (!sourceServerName) return;
      const src = servers.find((s) => s.name === sourceServerName);
      if (!src) return;
      let cancelled = false;
      fetchPickerUsers(src.id)
        .then((srcList) => {
          if (cancelled) return;
          setSourceUsers(srcList);
          setDestUsers(srcList);   // mirror so intersection = full list
          setIncludedUsers(new Set(srcList.map((u) => u.plex_id)));
        })
        .catch((e) => {
          if (cancelled) return;
          setUsersError(`source: ${String(e)}`);
          setSourceUsers([]);
          setDestUsers([]);
        });
      return () => { cancelled = true; };
    }
    if (mode !== 'direct') return;
    if (!sourceServerName || destServerNames.size === 0) return;
    if (destServerNames.has(sourceServerName)) return;
    const src = servers.find((s) => s.name === sourceServerName);
    const dsts = Array.from(destServerNames)
      .map((n) => servers.find((s) => s.name === n))
      .filter((s): s is ServerView => !!s);
    if (!src || dsts.length === 0) return;
    let cancelled = false;
    // PR-11 - the user picker reads from the local managed_users DB
    // (no live Plex round-trip during job setup). ``fetchPickerUsers``
    // handles the cold-DB case by firing a one-shot sync and re-
    // fetching, so a server whose table was never warmed paints the
    // picker without forcing the operator to visit User Management
    // first.
    Promise.allSettled([
      fetchPickerUsers(src.id),
      ...dsts.map((d) => fetchPickerUsers(d.id)),
    ]).then((results) => {
      if (cancelled) return;
      const sres = results[0];
      const dResults = results.slice(1);
      const srcOk = sres.status === 'fulfilled';
      const srcList = srcOk && sres.status === 'fulfilled' ? sres.value : [];
      // For multi-destination, the "destUsers" surface shown in the
      // UI's "on destination" column is the *intersection* across
      // destinations  that's the set of users a fan-out can actually
      // carry to every target. The Sets approach makes that easy.
      const perDestLists: ServerUser[][] = dResults.map((r) =>
        r.status === 'fulfilled' ? r.value : []
      );
      let destIntersection: ServerUser[] = perDestLists[0] ?? [];
      for (let i = 1; i < perDestLists.length; i += 1) {
        const ids = new Set(perDestLists[i].map((u) => u.plex_id));
        destIntersection = destIntersection.filter((u) => ids.has(u.plex_id));
      }
      setSourceUsers(srcList);
      setDestUsers(destIntersection);
      const dstIds = new Set(destIntersection.map((u) => u.plex_id));
      const intersection = srcList
        .filter((u) => dstIds.has(u.plex_id))
        .map((u) => u.plex_id);
      setIncludedUsers(new Set(intersection));
      const errors: string[] = [];
      if (!srcOk) errors.push(`source: ${String((sres as PromiseRejectedResult).reason)}`);
      dResults.forEach((r, i) => {
        if (r.status !== 'fulfilled') {
          errors.push(`${dsts[i].name}: ${String((r as PromiseRejectedResult).reason)}`);
        }
      });
      if (errors.length) setUsersError(errors.join(' · '));
    });
    return () => { cancelled = true; };
  }, [mode, sourceServerName, destNamesKey, servers]);

  // v0.14 — Restore mode user picker. Loads the snapshot's user list
  // (from snapshot_users in the .db) plus every destination's user
  // list, intersects them, and exposes the result through the same
  // ``sourceUsers`` / ``destUsers`` state the rest of the form reads.
  //
  // Owner-row normalisation: the snapshot stores the owner with
  // ``plex_id = ""`` (it never knew the destination's owner email at
  // capture time). The destination stores the owner with
  // ``plex_id = <owner_email>``. To make ``DirectUsersPanel``'s
  // plex_id-keyed intersection work, we rewrite the snapshot's owner
  // row to carry the FIRST destination's owner email before storing.
  // Multi-destination fan-out where destinations have different owner
  // emails is rare in practice (most operators run the same Plex
  // account across servers); the user-filter resolution catches a
  // mismatch by simply excluding owner from that destination.
  //
  // Only fires when restoring from a registered snapshot. File-based
  // restore (loose .plexexport.json picks) doesn't get a user picker
  // — the file would need to be parsed; we treat that as a future
  // refinement and leave the filter empty (= all users).
  useEffect(() => {
    if (mode !== 'restore') return;
    if (restoreSource !== 'snapshot') return;
    if (!selectedSnapshotId) return;
    if (destServerNames.size === 0) return;
    const dsts = Array.from(destServerNames)
      .map((n) => servers.find((s) => s.name === n))
      .filter((s): s is ServerView => !!s);
    if (dsts.length === 0) return;
    let cancelled = false;
    // Snapshot users + destination users have DIFFERENT response
    // shapes (snapshot returns the wrapped ``{users, error}``;
    // ``fetchPickerUsers`` already unwraps to ServerUser[]), so we
    // run them as two separate Promise.allSettled groups to keep the
    // result types straight.
    const snapPromise = Promise.allSettled([api.listSnapshotUsers(selectedSnapshotId)]);
    const destPromise = Promise.allSettled(dsts.map((d) => fetchPickerUsers(d.id)));
    Promise.all([snapPromise, destPromise]).then(([snapRs, destResults]) => {
      if (cancelled) return;
      const snapRes = snapRs[0];
      const snapUsers = (snapRes.status === 'fulfilled' && snapRes.value && Array.isArray(snapRes.value.users))
        ? snapRes.value.users
        : [];
      const perDestLists: ServerUser[][] = destResults.map((r) =>
        r.status === 'fulfilled' ? r.value : []
      );
      let destIntersection: ServerUser[] = perDestLists[0] ?? [];
      for (let i = 1; i < perDestLists.length; i += 1) {
        const ids = new Set(perDestLists[i].map((u) => u.plex_id));
        destIntersection = destIntersection.filter((u) => ids.has(u.plex_id));
      }
      // Owner normalisation: rewrite the snapshot's owner row
      // (plex_id="") to carry the first destination's owner email so
      // the intersection by plex_id picks owner up correctly.
      const firstDestOwner = (perDestLists[0] || []).find((u) => u.kind === 'owner');
      const normalisedSnapUsers: ServerUser[] = snapUsers.map((u) => {
        if (u.kind === 'owner' && firstDestOwner) {
          return { ...u, plex_id: firstDestOwner.plex_id };
        }
        return u;
      });
      setSourceUsers(normalisedSnapUsers);
      setDestUsers(destIntersection);
      const dstIds = new Set(destIntersection.map((u) => u.plex_id));
      const intersection = normalisedSnapUsers
        .filter((u) => dstIds.has(u.plex_id))
        .map((u) => u.plex_id);
      setIncludedUsers(new Set(intersection));
      const errors: string[] = [];
      if (snapRes.status !== 'fulfilled') {
        errors.push(`snapshot users: ${String((snapRes as PromiseRejectedResult).reason)}`);
      }
      destResults.forEach((r, i) => {
        if (r.status !== 'fulfilled') {
          errors.push(`${dsts[i].name}: ${String((r as PromiseRejectedResult).reason)}`);
        }
      });
      if (errors.length) setUsersError(errors.join(' · '));
    });
    return () => { cancelled = true; };
  }, [mode, restoreSource, selectedSnapshotId, destNamesKey, servers]);

  const jobRunning = !!snapshot?.job && snapshot.job.state === 'running';

  // ── Submit handler ────────────────────────────────────────────────
  //
  // Two entry points share one body:
  //   - submit() is the button click. For restore/direct in Replace
  //     mode it intercepts and pops the typed-REPLACE modal instead
  //     of immediately firing the API request.
  //   - submitConfirmed() is what the modal's onConfirm calls (and
  //     what snapshot/Merge submissions flow through directly). It
  //     does the actual API work.
  const submit = async () => {
    if ((mode === 'restore' || mode === 'direct') && restoreMode === 'replace') {
      setSubmitError(null);
      setSubmitOk(null);
      setReplaceModalOpen(true);
      return;
    }
    await submitConfirmed();
  };

  const submitConfirmed = async () => {
    setSubmitError(null);
    setSubmitOk(null);
    setSubmitting(true);
    try {
      if (mode === 'snapshot') {
        if (!sourceServerName) throw new Error('Pick a source server first.');
        const payload: Record<string, unknown> = {
          source_server_name: sourceServerName,
          libraries: Array.from(selectedLibs),
        };
        if (outputDir) payload.output_dir = outputDir;
        if (workers) payload.workers = Number(workers);
        if (scrobbleWorkers) payload.scrobble_workers = Number(scrobbleWorkers);
        if (logDir) payload.log_dir = logDir;
        payload.verbose = verbose;
        if (fastCollectionDetection) payload.fast_collection_detection = true;
        if (skipPlaylistPrebuild) payload.skip_playlist_prebuild = true;
        // PR-3 / Phase D - four-flag data-type filter. Always sent so
        // the server has an explicit value rather than relying on a
        // model default. Defaults are all true so this is a no-op
        // when the operator hasn't unchecked anything.
        payload.include_watch_history = includeWatchHistory;
        payload.include_ratings = includeRatings;
        payload.include_playlists = includePlaylists;
        payload.include_collections = includeCollections;
        payload.prebuild_json_sidecar = prebuildJsonSidecar;
        // Per-job watch+ratings strategy override (top of resolution
        // chain). Empty string means "inherit" → omit field so backend
        // falls through to per-server / global / default.
        if (watchRatingsStrategy) payload.watch_ratings_filter_strategy = watchRatingsStrategy;
        // v0.14 — per-snapshot user filter. Send the explicit list
        // when the operator has picked a subset; omit entirely when
        // every available user is checked (= historical "all users"
        // default at the backend).
        if (sourceUsers !== null && destUsers !== null) {
          const dstIds = new Set(destUsers.map((u) => u.plex_id));
          const transferable = sourceUsers.filter((u) => dstIds.has(u.plex_id));
          if (includedUsers.size < transferable.length) {
            payload.user_filter = Array.from(includedUsers);
          }
        }
        const r = await api.submitSnapshot(payload);
        setSubmitOk(`Snapshot job ${r.job_id} queued.`);
      } else if (mode === 'restore') {
        if (destServerNames.size === 0) throw new Error('Pick at least one destination server first.');
        const destList = Array.from(destServerNames);
        const payload: Record<string, unknown> = {
          dest_server_names: destList,
          strict_match: strictMatch,
          overwrite_playlists: overwritePlaylists,
          mode: restoreMode,
          auto_capture_before_replace: autoCaptureBeforeReplace,
          confirm_replace: restoreMode === 'replace',
          merge_watch_strategy: mergeWatchStrategy,
        };
        if (workers) payload.workers = Number(workers);
        if (scrobbleWorkers) payload.scrobble_workers = Number(scrobbleWorkers);
        if (logDir) payload.log_dir = logDir;
        payload.verbose = verbose;
        if (remapOld && remapNew) {
          payload.remap_old = remapOld;
          payload.remap_new = remapNew;
        }
        payload.include_watch_history = includeWatchHistory;
        payload.include_ratings = includeRatings;
        payload.include_playlists = includePlaylists;
        payload.include_collections = includeCollections;
        // v0.14 — per-restore user filter. Only applicable to the
        // snapshot-based restore path (file-based restore doesn't
        // surface a user picker yet — would require parsing the
        // file). Send the explicit list when the operator picked a
        // subset; omit entirely when every intersectable user is
        // selected (= historical "all users" default at the backend).
        if (
          restoreSource === 'snapshot'
          && sourceUsers !== null
          && destUsers !== null
        ) {
          const dstIds = new Set(destUsers.map((u) => u.plex_id));
          const transferable = sourceUsers.filter((u) => dstIds.has(u.plex_id));
          if (includedUsers.size < transferable.length) {
            payload.user_filter = Array.from(includedUsers);
          }
        }

        let r;
        if (restoreSource === 'snapshot') {
          if (!selectedSnapshotId) throw new Error('Pick a registered snapshot first.');
          payload.snapshot_id = selectedSnapshotId;
          r = await api.submitRestoreFromSnapshot(payload);
        } else {
          if (selectedFiles.size === 0) throw new Error('Pick at least one export file.');
          payload.input_files = Array.from(selectedFiles);
          r = await api.submitRestore(payload);
        }
        setSubmitOk(
          destList.length > 1
            ? `Fan-out import ${r.job_id} queued - ${destList.length} destinations.`
            : `Restore job ${r.job_id} queued.`,
        );
      } else {
        // direct
        if (!sourceServerName) throw new Error('Pick a source server.');
        if (destServerNames.size === 0) throw new Error('Pick at least one destination server.');
        if (destServerNames.has(sourceServerName)) {
          throw new Error('Source and destination must be different.');
        }
        const destList = Array.from(destServerNames);
        const payload: Record<string, unknown> = {
          source_server_name: sourceServerName,
          dest_server_names: destList,
          libraries: Array.from(selectedLibs),
          strict_match: strictMatch,
          mode: restoreMode,
          auto_capture_before_replace: autoCaptureBeforeReplace,
          confirm_replace: restoreMode === 'replace',
          merge_watch_strategy: mergeWatchStrategy,
        };
        if (workers) payload.workers = Number(workers);
        if (scrobbleWorkers) payload.scrobble_workers = Number(scrobbleWorkers);
        if (logDir) payload.log_dir = logDir;
        payload.verbose = verbose;
        if (fastCollectionDetection) payload.fast_collection_detection = true;
        if (remapOld && remapNew) {
          payload.remap_old = remapOld;
          payload.remap_new = remapNew;
        }
        // PR-3 / Phase D - four-flag data-type filter on direct too.
        payload.include_watch_history = includeWatchHistory;
        payload.include_ratings = includeRatings;
        payload.include_playlists = includePlaylists;
        payload.include_collections = includeCollections;
        if (watchRatingsStrategy) payload.watch_ratings_filter_strategy = watchRatingsStrategy;
        // v0.9.6 Feature 4 / v0.9.7 Item 7: send ``user_filter``
        // whenever the Users section rendered AND at least one
        // transferable entry exists (owner OR managed). If both
        // servers report no users we omit the field so the
        // backend's "None = include all" default applies. Owner is
        // included in the intersection check now  unchecking the
        // owner is how the operator skips library-level data.
        if (sourceUsers !== null && destUsers !== null) {
          const dstIds = new Set(destUsers.map((u) => u.plex_id));
          const hasIntersection = sourceUsers.some((u) => dstIds.has(u.plex_id));
          if (hasIntersection) {
            payload.user_filter = Array.from(includedUsers);
          }
        }
        const r = await api.submitDirect(payload);
        setSubmitOk(
          destList.length > 1
            ? `Fan-out transfer ${r.job_id} queued - 1 source → ${destList.length} destinations.`
            : `Direct transfer ${r.job_id} queued.`,
        );
      }
    } catch (e) {
      setSubmitError(String(e));
    } finally {
      setSubmitting(false);
    }
  };

  const toggleLib = (name: string) => {
    setSelectedLibs((prev) => {
      const next = new Set(prev);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      return next;
    });
  };
  const selectAllLibs = () => setSelectedLibs(new Set(libraries.map((l) => l.name)));
  const clearLibs = () => setSelectedLibs(new Set());

  const toggleFile = (name: string) => {
    setSelectedFiles((prev) => {
      const next = new Set(prev);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      return next;
    });
  };

  return (
    <>
      {submitError && <div className="banner error">{submitError}</div>}
      {submitOk && <div className="banner good">{submitOk}</div>}
      {jobRunning && (
        <div className="banner info">
          A job is currently running. Submitting will queue this job to run after the current one finishes.
        </div>
      )}
      {serversError && (
        <div className="banner error">Could not load servers: {serversError}</div>
      )}
      {servers.length === 0 && !serversError && (
        <div className="banner info">
          No Plex servers registered yet. Open the <strong>Servers</strong> tab to add one before submitting a job.
        </div>
      )}

      {/* ── Server selection area (v0.9.1) ───────────────────────────
            Per spec, the server selectors live at the top of the page
            and gate everything below. The selectors are side-by-side
            with status indicators next to each option; unreachable
            servers still appear but are visually marked as offline.
            For snapshot-only jobs the destination selector is hidden
            and replaced with a note pointing at the output directory.

            All sections below this panel are wrapped in a fieldset
            that goes disabled until the selection prerequisites are
            satisfied for the current mode.                            */}
      <div className="panel">
        <h2>Mode &amp; Servers</h2>
        <label className="field">
          <span className="label">Operation</span>
          <span className="help">
            <strong>Snapshot</strong> writes JSON files. <strong>Restore</strong> reads JSON files into Plex.
            <strong> Direct transfer</strong> reads from one Plex and writes straight to another with no
            intermediate file (with automatic chained fallback if the direct path is unavailable).
          </span>
          <select value={mode} onChange={(e) => setMode(e.target.value as Mode)}>
            <option value="snapshot">Snapshot - save data from a Plex server</option>
            <option value="restore">Restore - restore data to a Plex server</option>
            <option value="direct">Direct transfer - read from one server, write to another</option>
          </select>
        </label>

        <div className="grid-2">
          {/* ── Source selector (left) ──────────────────────────── */}
          {mode === 'restore' ? (
            <div className="field">
              <span className="label">Source Server</span>
              <span className="help">
                Not applicable for import - the source is the export file(s) you pick below.
              </span>
            </div>
          ) : (
            <div className="field">
              <span className="label">Source Server</span>
              <span className="help">
                The Plex server this operation will read from. Status indicators are refreshed every 30 seconds.
              </span>
              <ServerPicker
                value={sourceServerName}
                onChange={(name) => setSourceServerName(name)}
                servers={servers}
                pings={pings}
                excludeNames={mode === 'direct' ? destServerNames : undefined}
              />
            </div>
          )}

          {/* ── Destination selector (right) ─────────────────────── */}
          {mode === 'snapshot' ? (
            <div className="field">
              <span className="label">Destination Server</span>
              <span className="help">
                Not applicable for snapshot - the snapshot will be saved to the configured output directory
                (see <strong>Settings</strong> or override below).
              </span>
            </div>
          ) : (
            <div className="field">
              <span className="label">
                Destination Server{destServerNames.size > 1 ? 's (Fan-out)' : 's'}
              </span>
              <span className="help">
                {/* v0.10.0: multi-select destinations enable fan-out  one source → many
                    destinations in a single job. Pick one for the classic single-destination
                    flow; pick two or more to fan-out (each destination runs sequentially
                    in this release, with its own dashboard card). */}
                The Plex server(s) this operation will write into. Additive merge rules apply
                - nothing on a destination is ever deleted or reduced. Select two or more to
                fan-out: one job that writes into every destination.
              </span>
              <ServerPicker
                multi
                values={destServerNames}
                onMultiChange={setDestServerNames}
                servers={servers}
                pings={pings}
                excludeNames={mode === 'direct' && sourceServerName
                  ? new Set([sourceServerName])
                  : undefined}
              />
              {destServerNames.size > 1 && (
                <div className="banner info" style={{ marginTop: 8 }}>
                  Fan-out enabled - this job will write to <strong>{destServerNames.size}</strong> destinations in parallel.
                  Each destination has its own dashboard card, run-log directory, and error tracking.
                </div>
              )}
            </div>
          )}
        </div>

        {/* Selection-state indicator helps the user understand why the
            lower panels are disabled when they're disabled.            */}
        {!serversReady(mode, sourceServerName, destServerNames) && (
          <div className="banner info" style={{ marginTop: 8 }}>
            {mode === 'snapshot' && 'Pick a Source Server to continue.'}
            {mode === 'restore' && 'Pick at least one Destination Server to continue.'}
            {mode === 'direct' && 'Pick a Source Server and at least one Destination Server to continue.'}
          </div>
        )}
      </div>

      {/* ── Gate everything below until the selectors are satisfied ───
           A disabled <fieldset> non-interactively greys out every form
           control inside it without otherwise changing the layout. */}
      <fieldset
        className="job-form-gate"
        disabled={!serversReady(mode, sourceServerName, destServerNames)}
        style={{
          border: 'none', padding: 0, margin: 0, minWidth: 0,
          opacity: serversReady(mode, sourceServerName, destServerNames) ? 1 : 0.5,
          pointerEvents: serversReady(mode, sourceServerName, destServerNames) ? 'auto' : 'none',
        }}
      >
      {/* ── Scope section header ─────────────────────────────────────
            v0.13 form-layout refactor: every control under here answers
            "what data moves through this job" - libraries, data types,
            users, or (in import mode) which export files to read. They
            are visually grouped under one header so the operator can
            see the scope of the run at a glance without scanning for
            the relevant controls scattered between engine knobs. */}
      <div className="section-header">
        <h2 style={{ marginBottom: 4 }}>Scope - what to migrate</h2>
        <span className="help" style={{ color: 'var(--text-dim)', fontSize: 12 }}>
          Pick libraries, data types, and (for direct transfer) which managed users move.
          Defaults are everything checked.
        </span>
      </div>

      {/* ── Library / file picker ──────────────────────────────────── */}
      {(mode === 'snapshot' || mode === 'direct') && (
        <div className="panel">
          <h2>Libraries</h2>
          {librariesError ? (
            <div className="banner error">Could not list libraries: {librariesError}.</div>
          ) : (
            <>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 6 }}>
                <span className="label">Libraries to {mode === 'snapshot' ? 'snapshot' : 'transfer'}</span>
                <div className="row-buttons">
                  <button onClick={selectAllLibs}>All</button>
                  <button onClick={clearLibs}>None</button>
                </div>
              </div>
              <span className="help" style={{ display: 'block', color: 'var(--text-dim)', fontSize: 12, marginBottom: 6 }}>
                Empty selection = {mode === 'snapshot' ? 'every library on the source' : 'every library present on both servers'}.
                Mirrors <code>--libraries "Movies,TV Shows,Music"</code>.
              </span>
              <div className="checkbox-grid">
                {libraries.length === 0 ? (
                  <div className="empty">Pick a server above to load its libraries.</div>
                ) : libraries.map((lib) => (
                  <label key={lib.name} className="switch">
                    <input type="checkbox" checked={selectedLibs.has(lib.name)} onChange={() => toggleLib(lib.name)} />
                    <span>{lib.name}</span>
                    <span className="help">({lib.type}, {lib.count.toLocaleString()})</span>
                  </label>
                ))}
              </div>
            </>
          )}
        </div>
      )}

      {(mode === 'direct'
        || mode === 'snapshot'
        || (mode === 'restore' && restoreSource === 'snapshot')) &&
        sourceUsers !== null && destUsers !== null && (
        <DirectUsersPanel
          mode={mode}
          sourceUsers={sourceUsers}
          destUsers={destUsers}
          included={includedUsers}
          onToggle={(plex_id) => {
            setIncludedUsers((prev) => {
              const next = new Set(prev);
              if (next.has(plex_id)) next.delete(plex_id);
              else next.add(plex_id);
              return next;
            });
          }}
          onAll={() => {
            // v0.9.7 Item 7: include both owner and managed in the
            // intersection  owner is selectable too.
            const dstIds = new Set(destUsers.map((u) => u.plex_id));
            setIncludedUsers(new Set(
              sourceUsers
                .filter((u) => dstIds.has(u.plex_id))
                .map((u) => u.plex_id),
            ));
          }}
          onNone={() => setIncludedUsers(new Set())}
          loadError={usersError}
        />
      )}

      {mode === 'restore' && (
        <div className="panel">
          <h2>Restore source</h2>
          <div className="row-buttons" style={{ marginBottom: 12 }}>
            <button
              type="button"
              className={restoreSource === 'snapshot' ? 'primary' : ''}
              onClick={() => setRestoreSource('snapshot')}
            >
              From registered snapshot
            </button>
            <button
              type="button"
              className={restoreSource === 'file' ? 'primary' : ''}
              onClick={() => setRestoreSource('file')}
            >
              From JSON archive
            </button>
          </div>

          {restoreSource === 'snapshot' ? (
            <>
              <span className="help" style={{ display: 'block', color: 'var(--text-dim)', fontSize: 12, marginBottom: 6 }}>
                Reads directly from a snapshot registered in <code>snapshots.db</code>.
                The server materialises a JSON sidecar from the snapshot's <code>.db</code> on first use and caches it.
                {destServerNames.size > 0 && ' Rows are ranked by how many of their captured libraries overlap with the chosen destination(s).'}
              </span>
              {snapshotsLoadError && (
                <div className="banner error" style={{ marginBottom: 8 }}>
                  Could not load snapshots: {snapshotsLoadError}
                </div>
              )}
              {registeredSnapshots.length === 0 ? (
                <div className="empty">
                  No snapshots registered yet. Run a snapshot job first, or switch to <em>From JSON archive</em>.
                </div>
              ) : (
                <SnapshotPicker
                  rows={registeredSnapshots}
                  selectedId={selectedSnapshotId}
                  onSelect={setSelectedSnapshotId}
                  destLibraries={destLibraryNames(servers, destServerNames)}
                />
              )}
            </>
          ) : (
            <>
              <span className="help" style={{ display: 'block', color: 'var(--text-dim)', fontSize: 12, marginBottom: 6 }}>
                Restore from a <code>.plexexport.json</code> file on disk. Use this when
                you don't have the snapshot <code>.db</code> registered locally - either
                a JSON copy you downloaded from another install (the
                <strong> Exports</strong> tab's Download button produces these), or an
                archive from before the <code>.db</code>-based capture pipeline. Files
                listed here come from the server's <code>snapshots/legacy/</code>
                directory; drop a JSON in there to make it pickable.
              </span>
              <div className="checkbox-grid">
                {snapshots.length === 0 ? (
                  <div className="empty">
                    No JSON archives found in <code>snapshots/legacy/</code>. Drop a
                    <code> .plexexport.json</code> file in that directory and refresh.
                  </div>
                ) : snapshots.map((f) => (
                  <label key={f.name} className="switch">
                    <input type="checkbox" checked={selectedFiles.has(f.name)} onChange={() => toggleFile(f.name)} />
                    <span>{formatRestoreLabel(f)}</span>
                    <span className="help">{f.name}</span>
                  </label>
                ))}
              </div>
            </>
          )}
        </div>
      )}

      {/* ── Data to migrate (Scope card 2/2) ─────────────────────────
            v0.13 form-layout refactor: promoted out of the
            "Resolution & Performance" panel into its own card at the
            top of the form. This is a *scope* decision, not engine
            tuning, and the operator should see it next to the
            Libraries / Users controls.

            Snapshot-aware gating: when the operator picks a registered
            snapshot in import mode, the four toggles below are gated
            by the snapshot's row_counts. Types that aren't in the
            snapshot are disabled (greyed) and forced off; types that
            are present are checked by default but can still be
            unchecked. */}
      <div className="panel">
        <h2>Data to migrate</h2>
        <span className="help" style={{ display: 'block', color: 'var(--text-dim)', fontSize: 12, marginBottom: 8 }}>
          Pick which data types this job moves. Defaults are everything checked.
          Unchecking a type skips both the source gather and the destination merge for that type.
          {gatingFromSnapshot && (
            <>
              {' '}<strong>Filtered to the contents of the selected snapshot</strong> -
              greyed-out types are not present in this snapshot's <code>.db</code>.
            </>
          )}
        </span>
        <label className="switch" style={!snapshotHasWatchHistory ? { opacity: 0.5 } : undefined}>
          <input
            type="checkbox"
            checked={includeWatchHistory && snapshotHasWatchHistory}
            disabled={!snapshotHasWatchHistory}
            onChange={(e) => setIncludeWatchHistory(e.target.checked)}
          />
          <span>Watch history{!snapshotHasWatchHistory && gatingFromSnapshot && <em style={{ color: 'var(--text-dim)', fontSize: 11, marginLeft: 6 }}>(not in snapshot)</em>}</span>
        </label>
        <label className="switch" style={!snapshotHasRatings ? { opacity: 0.5 } : undefined}>
          <input
            type="checkbox"
            checked={includeRatings && snapshotHasRatings}
            disabled={!snapshotHasRatings}
            onChange={(e) => setIncludeRatings(e.target.checked)}
          />
          <span>Ratings{!snapshotHasRatings && gatingFromSnapshot && <em style={{ color: 'var(--text-dim)', fontSize: 11, marginLeft: 6 }}>(not in snapshot)</em>}</span>
        </label>
        <label className="switch" style={!snapshotHasPlaylists ? { opacity: 0.5 } : undefined}>
          <input
            type="checkbox"
            checked={includePlaylists && snapshotHasPlaylists}
            disabled={!snapshotHasPlaylists}
            onChange={(e) => setIncludePlaylists(e.target.checked)}
          />
          <span>Playlists{!snapshotHasPlaylists && gatingFromSnapshot && <em style={{ color: 'var(--text-dim)', fontSize: 11, marginLeft: 6 }}>(not in snapshot)</em>}</span>
        </label>
        <label className="switch" style={!snapshotHasCollections ? { opacity: 0.5 } : undefined}>
          <input
            type="checkbox"
            checked={includeCollections && snapshotHasCollections}
            disabled={!snapshotHasCollections}
            onChange={(e) => setIncludeCollections(e.target.checked)}
          />
          <span>Collections{!snapshotHasCollections && gatingFromSnapshot && <em style={{ color: 'var(--text-dim)', fontSize: 11, marginLeft: 6 }}>(not in snapshot)</em>}</span>
        </label>
        {!atLeastOneType && (
          <div className="banner error" style={{ marginTop: 8 }}>
            At least one data type must be selected - otherwise the job has nothing to do.
          </div>
        )}
      </div>

      {/* ── Restoration mode (Merge / Replace) ───────────────────────
            v0.13.x: shown for any job that writes into a destination
            (restore + direct transfer). Snapshot jobs don't write into
            Plex so the selector is hidden in snapshot mode. The selector
            is intentionally placed close to the bottom of the form, just
            above the submit button, so the operator's last decision
            before submit is the destructive-vs-additive choice. */}
      {(mode === 'restore' || mode === 'direct') && (
        <div className="panel">
          <h2 style={{ marginTop: 0 }}>Restoration mode</h2>
          <span className="help" style={{ display: 'block', color: 'var(--text-dim)', fontSize: 12, marginBottom: 8 }}>
            <strong>Merge</strong> is the default - additive and safe to re-run.
            <strong> Replace</strong> overwrites the destination to match the snapshot
            exactly. Replace asks you to type <code>REPLACE</code> on submit, and
            (with the safety belt on) auto-captures the destination first so you
            have a rollback point.
          </span>
          <RestoreModeSelector
            mode={restoreMode}
            autoCaptureBeforeReplace={autoCaptureBeforeReplace}
            mergeWatchStrategy={mergeWatchStrategy}
            onMergeWatchStrategyChange={setMergeWatchStrategy}
            onModeChange={setRestoreMode}
            onAutoCaptureChange={setAutoCaptureBeforeReplace}
            idPrefix={mode === 'restore' ? 'jobform-restore' : 'jobform-direct'}
          />
        </div>
      )}

      {/* ── Advanced options (collapsible) ───────────────────────────
            v0.13 form-layout refactor: every knob below is engine
            tuning, retry behaviour, path remapping, output paths, or
            logging - things the average operator never touches. The
            section starts collapsed; clicking the header toggles it.
            Renamed from "Advanced options" to "Per-Run Settings" with
            General / Advanced sub-tabs in v0.14 — the new layout
            separates common operator-level knobs (workers, strict
            match, sidecar) from migration-specific deep knobs
            (output dir, path remap, engine tuning, watch+ratings
            strategy override). */}
      <div className="panel">
        <button
          type="button"
          onClick={() => setAdvancedOpen((o) => !o)}
          aria-expanded={advancedOpen}
          style={{
            background: 'none', border: 'none', padding: 0,
            font: 'inherit', color: 'inherit', cursor: 'pointer',
            width: '100%', textAlign: 'left',
            display: 'flex', alignItems: 'center', gap: 8,
          }}
        >
          <span style={{ fontSize: 14, color: 'var(--text-dim)' }}>
            {advancedOpen ? '▾' : '▸'}
          </span>
          <h2 style={{ margin: 0 }}>Per-Run Settings</h2>
        </button>
        <span className="help" style={{ display: 'block', color: 'var(--text-dim)', fontSize: 12, marginTop: 6 }}>
          Override what's set under <strong>Servers ▸ Run Defaults</strong> for this one
          run only. Defaults are right for most operators — start here only if a run
          misbehaves, you need cross-platform path translation, or you're tuning
          per-job for an unusual server.
        </span>

        {advancedOpen && (
          <div style={{ marginTop: 12 }}>
            {/* Sub-tab strip. Switching tabs is workspace state only —
                doesn't reset any field values. */}
            <nav className="tabs sub-tabs" style={{ marginBottom: 12 }}>
              <button
                type="button"
                className={perRunSubTab === 'general' ? 'active' : ''}
                onClick={() => setPerRunSubTab('general')}
              >
                General
              </button>
              <button
                type="button"
                className={perRunSubTab === 'advanced' ? 'active' : ''}
                onClick={() => setPerRunSubTab('advanced')}
              >
                Advanced
              </button>
            </nav>

            {perRunSubTab === 'general' && (
              <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
                {/* ── Resolution & performance ─ */}
                <div>
                  <h3 style={{ marginTop: 0 }}>Resolution &amp; performance</h3>
                  {mode === 'direct' && (
                    <div className="banner info">
                      Direct transfers hit two Plex servers simultaneously. Start with about <strong>half</strong> the default worker count
                      and watch the Failed counter on the dashboard - if it climbs, lower workers further.
                    </div>
                  )}
                  <div className="grid-2">
                    <label className="field">
                      <span className="label">Worker threads</span>
                      <span className="help">How many threads run in parallel. Mirrors <code>--workers</code>. Blank = use default from Run Defaults.</span>
                      <input type="number" min={1} max={128} value={workers} onChange={(e) => setWorkers(e.target.value)} placeholder="(default)" />
                    </label>
                    <label className="field">
                      <span className="label">Scrobble workers</span>
                      <span className="help">Max simultaneous view-count writes during import or direct transfer. Mirrors <code>--scrobble-workers</code>.</span>
                      <input type="number" min={1} max={64} value={scrobbleWorkers} onChange={(e) => setScrobbleWorkers(e.target.value)} placeholder="(default)" />
                    </label>
                  </div>
                  {(mode === 'restore' || mode === 'direct') && (
                    <>
                      <label className="switch">
                        <input type="checkbox" checked={strictMatch} onChange={(e) => setStrictMatch(e.target.checked)} />
                        <span>Strict match</span>
                        <span className="help">Require exactly one fuzzy title match (default). Unchecking is equivalent to <code>--no-strict-match</code>.</span>
                      </label>
                      {mode === 'restore' && (
                        <label className="switch">
                          <input type="checkbox" checked={overwritePlaylists} onChange={(e) => setOverwritePlaylists(e.target.checked)} />
                          <span>Overwrite playlists</span>
                          <span className="help">Mirrors <code>--overwrite-playlists</code>. No-op for backward compat - all imports are additive since v0.2.0.</span>
                        </label>
                      )}
                    </>
                  )}
                  {mode === 'snapshot' && (
                    <label className="switch">
                      <input
                        type="checkbox"
                        checked={prebuildJsonSidecar}
                        onChange={(e) => setPrebuildJsonSidecar(e.target.checked)}
                      />
                      <span>Save JSON copy after snapshot</span>
                      <span className="help">
                        Writes a <code>.plexexport.json</code> file next to the snapshot
                        <code>.db</code> at the end of the run. Useful when you want a
                        portable text-format archive ready to download immediately. Off
                        by default - the JSON is otherwise rendered on first
                        <strong> Download</strong> click in the Exports tab and cached
                        from that point on. Adds wall-clock time to the run.
                      </span>
                    </label>
                  )}
                </div>

                {/* ── Logging ─ */}
                <div>
                  <h3 style={{ marginTop: 0 }}>Logging</h3>
                  <label className="switch">
                    <input type="checkbox" checked={verbose} onChange={(e) => setVerbose(e.target.checked)} />
                    <span>Verbose logging</span>
                    <span className="help">DEBUG-level output. Mirrors <code>--verbose</code>.</span>
                  </label>
                </div>
              </div>
            )}

            {perRunSubTab === 'advanced' && (
              <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
                {/* ── Output location ─ */}
                {mode === 'snapshot' && (
                  <div>
                    <h3 style={{ marginTop: 0 }}>Output location</h3>
                    <label className="field">
                      <span className="label">Output directory</span>
                      <span className="help">Where the <code>.plexexport.json</code> files will be written. Mirrors <code>--output-dir</code>. Leave blank to use the default from Run Defaults.</span>
                      <input type="text" value={outputDir} onChange={(e) => setOutputDir(e.target.value)} placeholder="./snapshots" />
                    </label>
                    <label className="field">
                      <span className="label">Log directory</span>
                      <span className="help">Where per-run log subdirectories are created. Mirrors <code>--log-dir</code>. Blank = use default from Run Defaults.</span>
                      <input type="text" value={logDir} onChange={(e) => setLogDir(e.target.value)} placeholder="./plex_logs" />
                    </label>
                  </div>
                )}
                {mode !== 'snapshot' && (
                  <div>
                    <h3 style={{ marginTop: 0 }}>Log location</h3>
                    <label className="field">
                      <span className="label">Log directory</span>
                      <span className="help">Where per-run log subdirectories are created. Mirrors <code>--log-dir</code>. Blank = use default from Run Defaults.</span>
                      <input type="text" value={logDir} onChange={(e) => setLogDir(e.target.value)} placeholder="./plex_logs" />
                    </label>
                  </div>
                )}

                {/* ── Path remap ─ */}
                {(mode === 'restore' || mode === 'direct') && (
                  <div>
                    <h3 style={{ marginTop: 0 }}>Path remap (cross-platform migrations)</h3>
                    <span className="help" style={{ display: 'block', color: 'var(--text-dim)', fontSize: 12, marginBottom: 8 }}>
                      Only needed when the media root path on the destination server differs from the stored path
                      (e.g. exporting from Windows <code>C:\Media</code>, importing on Linux <code>/mnt/plex</code>).
                      Suffix matching handles most cases automatically. Mirrors <code>--remap-path OLD NEW</code>.
                    </span>
                    <div className="grid-2">
                      <label className="field">
                        <span className="label">Old root prefix</span>
                        <input type="text" value={remapOld} onChange={(e) => setRemapOld(e.target.value)} placeholder="C:\Media\" />
                      </label>
                      <label className="field">
                        <span className="label">New root prefix</span>
                        <input type="text" value={remapNew} onChange={(e) => setRemapNew(e.target.value)} placeholder="/mnt/plex/" />
                      </label>
                    </div>
                  </div>
                )}

                {/* ── Engine tuning (snapshot/direct) ─ */}
                {(mode === 'snapshot' || mode === 'direct') && (
                  <div>
                    <h3 style={{ marginTop: 0 }}>Engine tuning</h3>
                    <label className="switch">
                      <input type="checkbox" checked={skipPlaylistPrebuild} onChange={(e) => setSkipPlaylistPrebuild(e.target.checked)} />
                      <span>Skip playlist pre-building</span>
                      <span className="help">
                        Skip the parallel upfront fetch that loads all playlists and their items
                        before snapshot starts. Playlists still snapshot correctly - the data is fetched
                        lazily the first time each server needs it, and the result is shared so each
                        server is still only fetched once per run. Use this to eliminate the
                        "Warming playlist cache" stall at job start without losing any playlist data.
                      </span>
                    </label>
                    {(() => {
                      const srv = servers.find((s) => s.name === sourceServerName);
                      const ver = srv?.plex_version ?? '';
                      const supported = serverSupportsFastCollections(ver);
                      const unknown = !ver;
                      const disabled = !supported;
                      return (
                        <label
                          className="switch"
                          style={{ opacity: disabled ? 0.55 : 1 }}
                          title={
                            disabled
                              ? unknown
                                ? 'Plex version unknown for this server — refresh it from the Servers tab to enable this option.'
                                : `Requires Plex Media Server ≥ 1.32. Source server reports ${ver}.`
                              : `Plex ${ver} supports librarySectionUserID — fast detection is available.`
                          }
                        >
                          <input
                            type="checkbox"
                            checked={disabled ? false : fastCollectionDetection}
                            disabled={disabled}
                            onChange={(e) => setFastCollectionDetection(e.target.checked)}
                          />
                          <span>
                            Fast collection detection
                            {disabled && (
                              <span className="tag failed" style={{ marginLeft: 8, fontSize: 10 }}>
                                {unknown ? 'unknown version' : 'unsupported'}
                              </span>
                            )}
                            {!disabled && (
                              <span className="tag done" style={{ marginLeft: 8, fontSize: 10 }}>
                                Plex {ver}
                              </span>
                            )}
                          </span>
                          <span className="help">
                            Use Plex's <code>librarySectionUserID</code> attribute to distinguish
                            library-wide from personal collections without a set lookup. Measurably
                            faster on large libraries (300+ collections, 10+ users). Requires
                            Plex Media Server ≥ 1.32; greyed out below that. Defaults ON when the
                            source server supports it.
                          </span>
                        </label>
                      );
                    })()}
                  </div>
                )}

                {/* ── Watch+Ratings strategy override (snapshot/direct) ─ */}
                {(mode === 'snapshot' || mode === 'direct') && (
                  <div>
                    <h3 style={{ marginTop: 0 }}>Watch+Ratings capture strategy</h3>
                    <span className="help" style={{ display: 'block', color: 'var(--text-dim)', fontSize: 12, marginBottom: 8 }}>
                      Overrides the per-server / Run-Defaults strategy for this run only.
                      Useful when a server is having a 429-storm today (force bulk) or when
                      you specifically want smaller payloads back from Plex (force server-side).
                    </span>
                    <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                      <label className="switch" title="Use the per-server override → Run Defaults → built-in 'smart' value. Recommended unless you have a specific reason to override.">
                        <input type="radio" name="wr-strategy-job" checked={watchRatingsStrategy === ''} onChange={() => setWatchRatingsStrategy('')} />
                        <span>Inherit <em>(recommended)</em></span>
                        <span className="help">Use the per-server override from Servers ▸ Advanced Settings, or the global default from Run Defaults.</span>
                      </label>
                      <label className="switch" title="Engine picks per library — bulk-fetch when both watch+ratings wanted, server-side filter when only one.">
                        <input type="radio" name="wr-strategy-job" checked={watchRatingsStrategy === 'smart'} onChange={() => setWatchRatingsStrategy('smart')} />
                        <span>Smart</span>
                        <span className="help">Engine picks per library. Equivalent to the global default behaviour.</span>
                      </label>
                      <label className="switch" title="Always fetch the full library and filter locally. Best for rate-limited Plex servers — fewer API calls, larger payloads.">
                        <input type="radio" name="wr-strategy-job" checked={watchRatingsStrategy === 'force_bulk'} onChange={() => setWatchRatingsStrategy('force_bulk')} />
                        <span>Force bulk</span>
                        <span className="help">Always bulk-fetch + filter locally. Best for rate-limited / 429-prone Plex servers.</span>
                      </label>
                      <label className="switch" title="Always let Plex filter on its side. Best when wire-traffic back from Plex is the constraint.">
                        <input type="radio" name="wr-strategy-job" checked={watchRatingsStrategy === 'force_server_side'} onChange={() => setWatchRatingsStrategy('force_server_side')} />
                        <span>Force server-side</span>
                        <span className="help">Always use server-side filter scans. Smaller payloads, more API calls.</span>
                      </label>
                    </div>
                  </div>
                )}
              </div>
            )}
          </div>
        )}
      </div>

      <div className="panel">
        <div className="row-buttons">
          <button
            className="primary"
            disabled={submitting || servers.length === 0 || !atLeastOneType}
            onClick={submit}
          >
            {submitting ? 'Submitting…' :
              mode === 'direct' ? (restoreMode === 'replace' ? 'Submit Direct Transfer (Replace)' : 'Submit Direct Transfer') :
              mode === 'snapshot' ? 'Submit Snapshot Job' :
              restoreMode === 'replace' ? 'Submit Replace Restore' :
              'Submit Restore Job'}
          </button>
        </div>
      </div>
      </fieldset>

      <ReplaceConfirmModal
        open={replaceModalOpen}
        targetLabel={
          mode === 'restore'
            ? destServerNames.size > 1
              ? `${destServerNames.size} destinations`
              : Array.from(destServerNames)[0] || 'destination'
            : destServerNames.size > 1
              ? `${destServerNames.size} destinations`
              : Array.from(destServerNames)[0] || 'destination'
        }
        autoCaptureBeforeReplace={autoCaptureBeforeReplace}
        onCancel={() => setReplaceModalOpen(false)}
        onConfirm={() => {
          setReplaceModalOpen(false);
          void submitConfirmed();
        }}
      />
    </>
  );
}

// ── Helpers (v0.9.1) ─────────────────────────────────────────────────────────

/**
 * Per-mode rule for whether the lower panels are interactive.
 * Snapshot needs a source server. Restore needs a destination. Direct
 * needs both, and they must be different. Mirrors the same logic
 * applied on the backend in :func:`server.jobs.JobQueue._run_*`.
 */
function serversReady(mode: Mode, src: string, dsts: Set<string>): boolean {
  if (mode === 'snapshot') return !!src;
  if (mode === 'restore') return dsts.size >= 1;
  // direct: source picked, at least one destination picked, source not
  // also in the destination set (the ServerPicker disables that option
  // visually but a stale ``destServerNames`` could still carry it).
  return !!src && dsts.size >= 1 && !dsts.has(src);
}

/**
 * v0.9.7 follow-up: build a readable short-form label for an export
 * file in the import picker. Prefers ``{library}  {source_server}
 * (short date)`` when both library and source_server are populated;
 * falls back to whatever's available without the dashes / parens so
 * older exports (no source_server, no captured_at) still render
 * cleanly. The full filename stays in the ``help`` row underneath
 * so operators can still copy-paste it when needed.
 */
function formatRestoreLabel(f: ExportArchive): string {
  const lib = (f.library ?? '').trim();
  const srv = (f.source_server ?? '').trim();
  // Date source priority: ``captured_at`` (ISO from metadata) if
  // present, else ``mtime`` (filesystem). The "short date" is just
  // YYYY-MM-DD HH:MM  locale rendering would vary between hosts;
  // a stable ISO-ish format is easier to scan in the picker.
  let when = '';
  const rawTs = f.captured_at || (f.mtime ? new Date(f.mtime * 1000).toISOString() : '');
  if (rawTs) {
    const d = new Date(rawTs);
    if (!isNaN(d.getTime())) {
      const pad = (n: number) => (n < 10 ? `0${n}` : `${n}`);
      when = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ` +
             `${pad(d.getHours())}:${pad(d.getMinutes())}`;
    }
  }
  // Compose parts conditionally so missing fields don't leave
  // stray separators in the output.
  const head = lib || f.name;
  const parts: string[] = [head];
  if (srv) parts.push(`- ${srv}`);
  if (when) parts.push(`(${when})`);
  return parts.join(' ');
}

/**
 * Union of every library name the selected destination(s) report.
 * Used by the snapshot picker to score / rank rows by overlap with
 * what the destination(s) actually have. Returns an empty set when
 * no destination is picked yet - callers should treat that as
 * "filter inactive, show everything".
 */
function destLibraryNames(servers: ServerView[], destNames: Set<string>): Set<string> {
  const out = new Set<string>();
  if (destNames.size === 0) return out;
  for (const s of servers) {
    if (!destNames.has(s.name)) continue;
    for (const lib of s.last_libraries || []) {
      const n = (lib.name || '').trim();
      if (n) out.add(n);
    }
  }
  return out;
}

// ── Sub-component: registered-snapshot picker (PR-13 follow-up) ─────────────
//
// Lists rows from snapshots.db, newest first. When a destination is
// picked, each row carries an overlap chip showing how many of its
// captured libraries also exist on the destination - rows with zero
// overlap are not hidden (the operator may have a reason to import
// anyway) but are visually dimmed and ranked last.

function SnapshotPicker({
  rows,
  selectedId,
  onSelect,
  destLibraries,
}: {
  rows: Snapshot[];
  selectedId: string | null;
  onSelect: (id: string) => void;
  destLibraries: Set<string>;
}) {
  const filterActive = destLibraries.size > 0;
  // Compute overlap once per row + sort: compatible (overlap > 0) first,
  // then by captured_at desc. When the filter isn't active, fall back
  // to pure captured_at desc.
  const enriched = rows
    .map((r) => {
      const libs = r.libraries || [];
      let overlap = 0;
      for (const lib of libs) {
        if (destLibraries.has(lib)) overlap += 1;
      }
      return { row: r, overlap, total: libs.length };
    })
    .sort((a, b) => {
      if (filterActive && (a.overlap > 0) !== (b.overlap > 0)) {
        return a.overlap > 0 ? -1 : 1;
      }
      return (b.row.captured_at || 0) - (a.row.captured_at || 0);
    });

  return (
    <div style={{ maxHeight: 360, overflowY: 'auto' }}>
      <table className="list" style={{ width: '100%' }}>
        <thead>
          <tr>
            <th></th>
            <th>Snapshot</th>
            <th>Server</th>
            <th>Captured</th>
            <th>Libraries</th>
            {filterActive && <th>Compat</th>}
          </tr>
        </thead>
        <tbody>
          {enriched.map(({ row, overlap, total }) => {
            const dim = filterActive && overlap === 0;
            return (
              <tr
                key={row.id}
                onClick={() => onSelect(row.id)}
                style={{
                  cursor: 'pointer',
                  opacity: dim ? 0.55 : 1,
                  background: row.id === selectedId ? 'var(--bg-panel)' : undefined,
                }}
              >
                <td>
                  <input
                    type="radio"
                    checked={row.id === selectedId}
                    onChange={() => onSelect(row.id)}
                  />
                </td>
                <td className="mono">{row.snapshot_name}</td>
                <td>{row.server_name}</td>
                <td>{row.captured_at ? new Date(row.captured_at * 1000).toLocaleString() : '-'}</td>
                <td>{total}</td>
                {filterActive && (
                  <td>
                    <span className={`tag ${overlap > 0 ? 'done' : 'error'}`} style={{ fontSize: 11 }}>
                      {overlap}/{total}
                    </span>
                  </td>
                )}
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

// ── Sub-component: server picker (v0.9.1) ───────────────────────────────────
//
// Each option appears as a clickable card with a status dot, friendly
// name, URL, and (when reachable) the current ping in milliseconds.
// Unreachable servers still appear in the list but are visually
// marked: red dot, "offline" instead of milliseconds, and a dimmed
// background. Clicking selects the server.
//
// The previous build used a native ``<select>`` which doesn't allow
// rich content inside <option>. The clickable-card list is required
// by the v0.9.1 spec ("live connection status indicator next to each
// option").

// v0.10.0: ServerPicker now supports both single-select (the source
// selector, the snapshot-mode destination) and multi-select (the
// import/direct destination, for fan-out). Mode is chosen by the
// caller: pass ``value`` + ``onChange`` for single, ``values`` +
// ``onMultiChange`` + ``multi`` for multi. The card layout / status
// indicators are identical between the two modes; only the toggle
// behaviour and selection state differ.
type ServerPickerSingle = {
  multi?: false;
  value: string;
  onChange: (name: string) => void;
  values?: undefined;
  onMultiChange?: undefined;
};
type ServerPickerMulti = {
  multi: true;
  values: Set<string>;
  onMultiChange: (next: Set<string>) => void;
  value?: undefined;
  onChange?: undefined;
};
type ServerPickerProps = (ServerPickerSingle | ServerPickerMulti) & {
  servers: ServerView[];
  pings: Record<string, PingResult>;
  excludeNames?: Set<string>;
};

function ServerPicker(props: ServerPickerProps) {
  const { servers, pings, excludeNames } = props;
  if (servers.length === 0) {
    return (
      <div className="empty" style={{ marginTop: 4 }}>
        No registered servers. Open the <strong>Servers</strong> tab to add one.
      </div>
    );
  }
  const isSelected = (name: string): boolean => {
    if (props.multi) return props.values.has(name);
    return props.value === name;
  };
  const handleClick = (name: string): void => {
    if (props.multi) {
      const next = new Set(props.values);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      props.onMultiChange(next);
      return;
    }
    props.onChange(name);
  };
  return (
    <div className="server-picker">
      {servers.map((s) => {
        const ping = pings[s.id];
        const status = ping?.status ?? s.last_status;
        const ms = ping?.response_ms ?? s.last_response_ms ?? null;
        const isReachable = status === 'ok';
        const isExcluded = !!excludeNames?.has(s.name);
        const selected = isSelected(s.name);
        const dotClass = status === 'ok' ? 'green'
          : status === 'auth_error' || status === 'unreachable' ? 'red'
          : 'amber';
        const metaText = isExcluded
          ? '(picked as opposite)'
          : status === 'ok' && ms !== null
            ? `${ms.toFixed(0)} ms`
            : status === 'auth_error'
              ? 'auth error'
              : status === 'unreachable'
                ? 'offline'
                : '…';
        return (
          <button
            type="button"
            key={s.id}
            className={`server-card${selected ? ' selected' : ''}${isReachable ? '' : ' offline'}`}
            disabled={isExcluded}
            onClick={() => handleClick(s.name)}
            title={ping?.detail ?? s.last_status_detail ?? ''}
            aria-pressed={selected}
          >
            <span className={`dot ${dotClass}`} />
            <span className="server-card-body">
              <span className="server-card-name">{s.name}</span>
              <span className="server-card-url">{s.url}</span>
            </span>
            <span className="server-card-meta">{metaText}</span>
          </button>
        );
      })}
    </div>
  );
}

// ── Sub-component: direct-transfer user intersection (v0.9.6 Feature 4) ─────
//
// Three groups computed by raw identifier match:
//   - Transferable : on both source AND destination → checkboxes,
//                    default checked. Operator can uncheck to exclude.
//   - Source only  : on source, missing on destination → grayed out
//                    with a "Not on destination server" note.
//   - Destination  : the spec's informational footer about inviting
//                    users via the Servers tab. No button / action.
//
// Owner rows are never rendered here  owner data always transfers,
// independent of this filter. If neither source nor destination has
// any managed users at all, the parent component still mounts this
// panel because it serves as a confirmation that there's nothing
// per-user to filter; we render a single "No managed users on
// either side" line to make that explicit.

function DirectUsersPanel(props: {
  // v0.14 — same picker, three contexts. The mode drives copy + the
  // "source only" panel's wording (the user picker re-uses the same
  // intersection logic regardless of which side is source vs dest).
  mode?: 'direct' | 'snapshot' | 'restore';
  sourceUsers: ServerUser[];
  destUsers: ServerUser[];
  included: Set<string>;
  onToggle: (plex_id: string) => void;
  onAll: () => void;
  onNone: () => void;
  loadError: string | null;
}) {
  const { mode = 'direct', sourceUsers, destUsers, included, onToggle, onAll, onNone, loadError } = props;
  // v0.9.7 Item 7: the owner is selectable alongside managed users.
  // Intersection is by raw identifier across both kinds; unchecking
  // the owner narrows the transfer so library-level data
  // (collections + the four owner-scoped blocks) is skipped.
  const dstIds = new Set(destUsers.map((u) => u.plex_id));
  const transferable = sourceUsers.filter((u) => dstIds.has(u.plex_id));
  const sourceOnly = sourceUsers.filter((u) => !dstIds.has(u.plex_id));

  const allEmpty = sourceUsers.length === 0 && destUsers.length === 0;

  // Mode-driven copy. The picker logic is identical; only the
  // operator-facing language changes.
  const copy: { help: string; missingLabel: string; missingTitle: string } = (() => {
    if (mode === 'snapshot') {
      return {
        help: "Pick which users' data the snapshot captures. The server owner's library-level data (collections + watch / playlists / ratings) is included only when the owner row is checked.",
        missingLabel: '',
        missingTitle: '',
      };
    }
    if (mode === 'restore') {
      return {
        help: "Pick which users' data to restore. Users present in the snapshot but not on the destination are greyed out — invite them to Plex Home on the destination to enable restore.",
        missingLabel: 'Not on destination',
        missingTitle: 'No matching account on the destination server.',
      };
    }
    return {
      help: "Pick which managed users' watch history, playlists, collections, and ratings travel with this direct transfer. The server owner's data always transfers regardless of what's checked here.",
      missingLabel: 'Not on destination server',
      missingTitle: 'Not on destination server',
    };
  })();

  return (
    <div className="panel">
      <h2>Users</h2>
      <span className="help" style={{ display: 'block', color: 'var(--text-dim)', fontSize: 12, marginBottom: 8 }}>
        {copy.help}
      </span>
      {loadError && (
        <div className="banner error" style={{ fontSize: 12, marginBottom: 8 }}>
          Could not fully load user lists: {loadError}
        </div>
      )}
      {allEmpty ? (
        <div className="empty" style={{ fontSize: 12 }}>
          No managed users on either server  the owner's data will transfer alone.
        </div>
      ) : (
        <>
          <div style={{ marginBottom: 10 }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 4 }}>
              <strong style={{ fontSize: 13 }}>Transferable ({transferable.length})</strong>
              <div className="row-buttons">
                <button onClick={onAll} disabled={transferable.length === 0}>All</button>
                <button onClick={onNone} disabled={transferable.length === 0}>None</button>
              </div>
            </div>
            {transferable.length === 0 ? (
              <div className="empty" style={{ fontSize: 12 }}>
                No users available on both servers.
              </div>
            ) : (
              <div className="checkbox-grid">
                {transferable.map((u) => (
                  <label key={u.plex_id} className="switch">
                    <input
                      type="checkbox"
                      checked={included.has(u.plex_id)}
                      onChange={() => onToggle(u.plex_id)}
                    />
                    <span>
                      {/* v0.9.7 follow-up: prefer the operator's
                          chosen display name (set on the Servers tab)
                          over the raw identifier. Owner's raw_name
                          is the Plex.tv email and gets noisy in this
                          list; falling back to it only when no
                          display name exists keeps the UI readable. */}
                      <strong>{u.display_name || u.raw_name}</strong>{' '}
                      {/* v0.9.7 Item 7: Owner / Managed badge so it's
                          clear the owner is a selectable target with
                          a different scope than managed users. */}
                      <span
                        className={`tag ${u.kind === 'owner' ? 'started' : 'phase'}`}
                        style={{ fontSize: 10, marginLeft: 4 }}
                      >
                        {u.kind === 'owner' ? 'Owner' : 'Managed'}
                      </span>
                    </span>
                  </label>
                ))}
              </div>
            )}
          </div>
          {/* "Source only" panel renders in direct / restore modes
              when the source carries users the destination doesn't.
              In snapshot mode we treat ``destUsers === sourceUsers``
              so this block stays empty by construction. */}
          {mode !== 'snapshot' && sourceOnly.length > 0 && (
            <div style={{ marginBottom: 10 }}>
              <strong style={{ fontSize: 13 }}>
                {mode === 'restore' ? 'Snapshot only' : 'Source only'} ({sourceOnly.length})
              </strong>
              <div className="checkbox-grid" style={{ opacity: 0.55 }}>
                {sourceOnly.map((u) => (
                  <label key={u.plex_id} className="switch" title={copy.missingTitle}>
                    <input type="checkbox" checked={false} disabled />
                    <span>
                      {/* v0.9.7 follow-up: prefer display_name same as the transferable list. */}
                      <strong>{u.display_name || u.raw_name}</strong>{' '}
                      <span style={{ color: 'var(--text-dim)', fontSize: 12 }}>
                        - {copy.missingLabel}
                      </span>
                    </span>
                  </label>
                ))}
              </div>
            </div>
          )}
          {mode !== 'snapshot' && (
            <div style={{ marginTop: 6, fontSize: 12, color: 'var(--text-dim)' }}>
              Users not on the destination can be invited via the Servers tab.
            </div>
          )}
        </>
      )}
    </div>
  );
}
