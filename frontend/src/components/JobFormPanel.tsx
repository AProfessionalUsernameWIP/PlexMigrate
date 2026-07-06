// Job submission form.
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
// Every engine job parameter has a clearly labelled form
// control. The form does not submit the Plex URL or token directly
//  those live in the registry the user manages from the Servers tab.

import { useEffect, useMemo, useState } from 'react';
import { api, ExportArchive, LeafCounts, LibraryDescriptor, ServerManagedUser, ServerUser, ServerView, DashboardFrame, Snapshot } from '../api';
import { BackendType, backendCounts, serversForBackend } from './BackendTabStrip';
import { useBackendTint, type BackendTint } from '../contexts/BackendTintContext';
import { serverSupportsFastCollections } from '../utils/plexVersion';
import { usePingPoller } from '../hooks/usePingPoller';
import { RestoreModeSelector, RestoreMode, MergeWatchStrategy } from './RestoreModeSelector';
import { ReplaceConfirmModal } from './ReplaceConfirmModal';
import { InfoTip } from './InfoTip';
import { PinPreflightModal } from './PinPreflightModal';
import { CrossPlatformPreflightModal } from './CrossPlatformPreflightModal';
import type { PreflightResponse, CrossPlatformPreflightAck } from '../api';
import { UserFilterPanel, EMPTY_FILTER } from './UserFilterPanel';
import type { UserFilterCriteria } from './UserFilterPanel';
import { UserCreationModal } from './UserCreationModal';
import type { ProposedUser, UserCreateSpec as ModalUserCreateSpec } from './UserCreationModal';
import { LibraryMetricsMatrix } from './LibraryMetricsMatrix';
import type { LibraryMetricsMap, LibraryMetricsRow } from './LibraryMetricsMatrix';
import { ServerPicker } from './ServerPicker';
import { SnapshotPicker } from './SnapshotPicker';
import { DirectUsersPanel } from './DirectUsersPanel';
import { WorkflowStrip } from './WorkflowStrip';
import type { WorkflowMode, BackendOrCross, RateMode } from './WorkflowStrip';
import { ModeAndServersPanel } from './ModeAndServersPanel';
import type { Mode } from './ModeAndServersPanel';
import { LibrariesPanel } from './LibrariesPanel';
import { DataToMigratePanel } from './DataToMigratePanel';
import { JobConfigBody } from './JobConfigBody';
import { RunJobLibraryMappingPanel } from './RunJobLibraryMappingPanel';
import type { JobConfigOwner } from './JobConfigOwner';
import { PerRunSettingsPanel } from './PerRunSettingsPanel';
import type {
  WatchRatingsStrategy,
  MixedMediaBehavior,
  MixedMediaVideoRouting,
  MixedMediaLogging,
  MixedMediaCollisionHandling,
} from './PerRunSettingsPanel';

// Live status indicator polling cadence for the server pickers.
const PING_INTERVAL_MS = 30_000;


// The user picker reads from the local managed_users DB instead of
// hitting the live Plex API on every job-form visit. Shape adapter:
// DB rows carry ``username`` and ``kind`` directly; the picker UI
// expects the live API's ``ServerUser`` shape with ``plex_id`` /
// ``raw_name``. Translating here keeps the downstream render code
// working against one shape.
function dbUserToPickerUser(u: ServerManagedUser): ServerUser {
  return {
    kind: u.kind,
    plex_id: u.username,
    raw_name: u.username,
    display_name: u.display_name || '',
  };
}

// Fetch DB-backed users for one server with a one-shot cold-start
// recovery: if the DB has no rows yet (the user hasn't re-tested
// their existing servers since the managed_users cache was added),
// fire a single sync and re-fetch. Failures swallow into an empty
// list - the downstream effect surfaces a single combined error if
// multiple servers fail.
async function fetchPickerUsers(serverId: string): Promise<ServerUser[]> {
  let res = await api.listServerManagedUsers(serverId);
  if (res.users.length === 0) {
    try {
      await api.syncServerManagedUsers(serverId);
      res = await api.listServerManagedUsers(serverId);
    } catch {
      // Sync 502'd (server unreachable or token rejected). Fall
      // through with whatever the DB has, including the empty list -
      // the picker shows "no users" and the end user can fix the
      // server in the Servers tab.
    }
  }
  // Share-state filter. The local managed_users cache may
  // still carry rows for users the end user un-shared on Plex.tv;
  // the engine drops them at run-time but they shouldn't be selectable
  // here either. Owners are always kept (admin token covers them
  // regardless of the per-user share check).
  return res.users
    .filter((u) => u.kind === 'owner' || u.active_share !== false)
    .map(dbUserToPickerUser);
}

interface Props {
  snapshot: DashboardFrame | null;
}

export function JobFormPanel({ snapshot }: Props) {
  const [mode, setMode] = useState<Mode>('snapshot');

  // Workflow selector. Adds a backend-class layer above the mode
  // tabs so end users explicitly pick which kind of job they're
  // running:
  //
  //   * 'plex' / 'jellyfin' / 'emby' = same-backend workflow. The
  //     server pickers below are filtered to servers of that backend
  //     only, making it impossible to accidentally send a Plex source
  //     to a Jellyfin destination by clicking the wrong row.
  //   * 'cross' = cross-platform workflow. Pickers render every
  //     registered server regardless of backend. Used for Plex->Jellyfin
  //     or any other backend-to-backend transfer.
  //
  // The strip is auto-hidden when only one backend has registered
  // servers (Plex-only install sees zero UX change). When 2+ backend
  // types exist, the Cross-platform tab also becomes available.
  //
  // Tabs are visible for every backend regardless of engine support;
  // the Submit button below is gated for workflows whose backend
  // support isn't ready, with a banner explaining why.
  const [workflowMode, setWorkflowMode] = useState<WorkflowMode>('plex');

  // Registry-aware server selection.
  // Destinations are a Set so direct-transfer and import jobs can
  // target multiple servers in one job (fan-out). Membership is
  // order-insensitive; the rendered picker is a multi-select grid.
  // The source is a single string - fan-out is one source many
  // destinations, never the reverse.
  const [servers, setServers] = useState<ServerView[]>([]);
  // This state holds the registry's stable server id, not the
  // friendly name. The same friendly name can exist across backends
  // (Plex "Jade.TV" and Emby "Jade.TV" are different servers);
  // keying on id is the only way to identify the server
  // unambiguously. The variable names below keep the "Name" suffix
  // because the API field is ``source_server_name``; we convert
  // id -> name at submit time only.
  const [sourceServerName, setSourceServerName] = useState<string>('');
  const [destServerNames, setDestServerNames] = useState<Set<string>>(new Set());

  // Helpers: convert the id-typed selection state to the name
  // strings the backend API currently expects. Returns '' for an
  // unknown id (caller should treat that as "no selection").
  const _idToName = (id: string): string => {
    const row = servers.find((s) => s.id === id);
    return row?.name || '';
  };
  const _idsToNames = (ids: Iterable<string>): string[] => {
    const out: string[] = [];
    for (const id of ids) {
      const n = _idToName(id);
      if (n) out.push(n);
    }
    return out;
  };

  // Workflow-tab derived data + presentational helpers.
  // ``populatedBackends`` is the set of backends that have at least
  // one registered server. ``showWorkflowTabs`` gates rendering: a
  // single-backend install (most installs today) sees no workflow
  // strip, identical UX to before this feature. Two or more backend
  // types triggers the strip - and unlocks the Cross-platform tab.
  const populatedBackends = useMemo(() => {
    const counts = backendCounts(servers);
    return (['plex', 'jellyfin', 'emby'] as BackendType[])
      .filter((b) => counts[b] > 0);
  }, [servers]);
  const showWorkflowTabs = populatedBackends.length >= 2;

  // Servers visible inside the active workflow. The pickers below
  // pass this filtered list down to ServerPicker. Cross-platform
  // workflow sees every registered server.
  const workflowServers = useMemo(() => {
    if (workflowMode === 'cross') return servers;
    return serversForBackend(servers, workflowMode);
  }, [servers, workflowMode]);

  // Source / dest backend are derived from the actually-picked
  // source / destination server's service_type, NOT from a separate
  // chip state. The backend tabs above filter both pickers'
  // available lists; the
  // cross-platform tab unlocks an "open selection" mode where the
  // end user can pick servers from any backend and the actual
  // cross-backend-ness comes from the picker choice.
  const sourceBackend = useMemo<BackendOrCross>(() => {
    if (!sourceServerName) {
      // No source picked yet; fall back to the active tab so the
      // sub-card label has a reasonable default.
      return workflowMode === 'cross' ? 'plex' : workflowMode;
    }
    const srv = servers.find((s) => s.id === sourceServerName);
    const t = (srv as unknown as { service_type?: string } | undefined)?.service_type;
    return (t === 'jellyfin' || t === 'emby') ? t : 'plex';
  }, [sourceServerName, servers, workflowMode]);
  const destBackend = useMemo<BackendOrCross>(() => {
    // For fan-out across destinations, prefer the FIRST destination's
    // backend for the sub-card label; isCrossBackend below catches
    // the case where any destination's backend differs from source.
    if (destServerNames.size === 0) return sourceBackend;
    const firstId = Array.from(destServerNames)[0];
    const srv = servers.find((s) => s.id === firstId);
    const t = (srv as unknown as { service_type?: string } | undefined)?.service_type;
    return (t === 'jellyfin' || t === 'emby') ? t : 'plex';
  }, [destServerNames, servers, sourceBackend]);
  const isCrossBackend = useMemo(() => {
    // True when ANY picked destination's backend differs from the
    // source's backend. Same-backend fan-out keeps this false even
    // across multiple destinations.
    if (!sourceServerName || destServerNames.size === 0) return false;
    const srcSrv = servers.find((s) => s.id === sourceServerName);
    const srcType = (srcSrv as unknown as { service_type?: string } | undefined)?.service_type || 'plex';
    for (const id of destServerNames) {
      const dst = servers.find((s) => s.id === id);
      const dstType = (dst as unknown as { service_type?: string } | undefined)?.service_type || 'plex';
      if (dstType !== srcType) return true;
    }
    return false;
  }, [sourceServerName, destServerNames, servers]);

  // Publish the chameleon backend tint based on the current source +
  // destination picks. Cross-backend routes (source and dest differ)
  // publish 'mixed' so the "you're running across backends" warning
  // signal carries into the chrome too. Same-backend routes publish
  // the source backend so the Run button + selection highlights take
  // on that backend's identity. This is the JobFormPanel-equivalent
  // of BackendTabStrip's tint hook (Run Job doesn't render a strip
  // because the picker selection is the source of truth here).
  const jobFormTint: BackendTint = useMemo(() => {
    if (sourceServerName && destServerNames.size > 0 && isCrossBackend) return 'mixed';
    if (sourceBackend === 'plex' || sourceBackend === 'jellyfin' || sourceBackend === 'emby') {
      return sourceBackend;
    }
    return null;
  }, [sourceServerName, destServerNames, isCrossBackend, sourceBackend]);
  useBackendTint(jobFormTint);

  // D-RATE mode. End user's per-job rating-mapping policy in the
  // cross-backend sub-card. Default
  // matches the backend (favorite_threshold=5.0); 'tunable' lets the
  // end user pick their own threshold; 'numeric_only' uses the 11.0
  // sentinel to disable IsFavorite writes entirely.
  const [rateMode, setRateMode] = useState<RateMode>('default');
  const [rateThreshold, setRateThreshold] = useState<string>('5.0');

  // Per-user fan-out toggle. Default ON matches the backend's
  // include_managed_users=True kwarg.
  const [includeManagedUsers, setIncludeManagedUsers] = useState<boolean>(true);

  // D-OWNER end user-confirmed user-creation spec list. Empty until
  // the modal is approved.
  type UserCreateSpec = {
    source_user_handle: string;
    target_username: string;
    temp_password: string;
    target_user_policy?: Record<string, unknown> | null;
  };
  const [userCreateSpecs, setUserCreateSpecs] = useState<UserCreateSpec[]>([]);
  const [userCreateModalOpen, setUserCreateModalOpen] = useState<boolean>(false);
  const [userCreateConfirmed, setUserCreateConfirmed] = useState<boolean>(false);

  // Cross-backend submission is driven by the
  // CrossPlatformPreflightModal's verdict: the preflight endpoint
  // computes per-destination decisions, and the modal locks Submit
  // only when a row has a blocking verdict the end user hasn't
  // resolved. Same-backend routes skip the modal entirely (verdict=ok).
  //
  // ``workflowEngineReady`` is a constant true, kept as a hook for
  // any non-preflight readiness check that may want it in the future;
  // the gate that reads it is a no-op.
  const workflowEngineReady = true;
  // First-time-enablement notice for Jellyfin / Emby. The engine is
  // implemented and contract-tested, but this is the first release
  // where same-backend Jellyfin / Emby jobs are end user-callable, so
  // we surface a softer informational banner asking end users to
  // report adapter bugs. Removed once these backends have soaked.
  const showFirstRunNotice = workflowMode === 'jellyfin' || workflowMode === 'emby';

  // Auto-correct workflowMode when its target backend has zero
  // registered servers AND another backend does. Same pattern as
  // BackendTabStrip on the Servers panel: prevents the end user from
  // staring at an empty picker forever.
  useEffect(() => {
    if (servers.length === 0) return;
    if (workflowMode === 'cross') {
      // Cross requires at least two distinct backend types; demote
      // to the first populated backend otherwise.
      if (populatedBackends.length < 2) {
        setWorkflowMode(populatedBackends[0] || 'plex');
      }
      return;
    }
    const counts = backendCounts(servers);
    if (counts[workflowMode] === 0) {
      setWorkflowMode(populatedBackends[0] || 'plex');
    }
  }, [servers, workflowMode, populatedBackends]);

  // When the workflow changes, ensure the in-state selection still
  // points at a server that's visible inside the new workflow.
  // Otherwise switching from Plex to Jellyfin would leave a Plex
  // server id selected silently - which the ServerPicker would
  // simply not render, but the submit logic would still pick up.
  useEffect(() => {
    if (sourceServerName) {
      const stillVisible = workflowServers.some((s) => s.id === sourceServerName);
      if (!stillVisible) setSourceServerName('');
    }
    if (destServerNames.size > 0) {
      const stillVisibleDests = new Set(
        Array.from(destServerNames).filter((id) =>
          workflowServers.some((s) => s.id === id),
        ),
      );
      if (stillVisibleDests.size !== destServerNames.size) {
        setDestServerNames(stillVisibleDests);
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [workflowServers]);

  const [serversError, setServersError] = useState<string | null>(null);

  // v0.9.1: live ping results keyed by server id. The selectors below
  // read this to render a status dot and latency next to each option.
  // usePingPoller manages the cadence, in-flight pings, and timer
  // cleanup; it returns the per-id PingResult map directly so this
  // file no longer needs the explicit useState/useRef pair that used
  // to drive the inline useEffect block.
  const pings = usePingPoller(servers, PING_INTERVAL_MS);

  // Library picker (snapshot + direct).
  const [libraries, setLibraries] = useState<LibraryDescriptor[]>([]);
  const [selectedLibs, setSelectedLibs] = useState<Set<string>>(new Set());
  const [librariesError, setLibrariesError] = useState<string | null>(null);

  // Export file picker (import only).
  const [snapshots, setExports] = useState<ExportArchive[]>([]);
  const [selectedFiles, setSelectedFiles] = useState<Set<string>>(new Set());
  // Restore source toggle. Defaults to 'snapshot' (registered .db),
  // the current storage shape; legacy JSON archives still work via
  // the 'file' option.
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

  // Restore mode (Merge / Replace). Shared between the ``restore``
  // and ``direct`` operations - both end up in the same engine
  // path. Defaults to ``merge`` (the safe, additive behaviour);
  // Replace is opt-in and gated by the typed-confirmation modal
  // below.
  const [restoreMode, setRestoreMode] = useState<RestoreMode>('merge');
  const [autoCaptureBeforeReplace, setAutoCaptureBeforeReplace] = useState(true);
  // Merge-mode sub-toggle for the watch-count math. Defaults to
  // "higher" (the idempotent behaviour); the end user opts into the
  // additive variant.
  const [mergeWatchStrategy, setMergeWatchStrategy] = useState<MergeWatchStrategy>('higher');

  // Per-run override that bypasses the saved library mapping table.
  // Default false (mappings apply). When true, the engine falls back
  // to exact-name matching between source + destination libraries.
  // The checkbox is surfaced as a first-class control next to Submit
  // (Replace mode users see the explicit cross-backend safety belt
  // as a banner instead).
  const [ignoreLibraryMapping, setIgnoreLibraryMapping] = useState<boolean>(false);

  // Per-run library mapping override map. Keys are source library
  // names; values are destination library names (empty string for
  // explicit per-run skip). Edits here NEVER touch the shared
  // library_mappings table - they only travel with this one job
  // submission. The engine reads the map BEFORE the saved table, so
  // an entry here is the highest-precedence routing decision short
  // of the ignore_library_mapping flag.
  const [libraryMappingOverrides, setLibraryMappingOverrides] = useState<Record<string, string>>({});

  // Per-library counts read from the selected snapshot's .db file.
  // Populated when source is a snapshot so the Run Job > Library
  // Mapping panel renders rich counts (movies / shows / seasons /
  // episodes / tracks / etc.) for the source side, matching the
  // destination side. Null until fetched; empty array when the
  // snapshot file is missing.
  //
  // media_type_counts is the authoritative per-libtype breakdown -
  // Plex stores 'artist', 'album', and 'track' as separate items,
  // all in server_items. Counting server_items rows without
  // splitting by media_type gives a misleading union (e.g. "12,005
  // artists" for a library with 1,100 artists + 7,400 tracks). The
  // split is carried through to leaf_counts so the renderer puts
  // each count on the right line.
  const [snapshotLibraryCounts, setSnapshotLibraryCounts] = useState<Record<string, {
    section_type: string;
    item_count: number;
    media_type_counts: Record<string, number>;
    hierarchy_counts: Record<string, number>;
    watch_events: number;
    ratings: number;
    playlists: number;
    collections: number;
  }> | null>(null);

  useEffect(() => {
    if (mode !== 'restore' || restoreSource !== 'snapshot' || !selectedSnapshotId) {
      setSnapshotLibraryCounts(null);
      return;
    }
    let cancelled = false;
    api.snapshotLibraryCounts(selectedSnapshotId).then(
      (r) => {
        if (cancelled) return;
        const idx: Record<string, {
          section_type: string; item_count: number;
          media_type_counts: Record<string, number>;
          hierarchy_counts: Record<string, number>;
          watch_events: number; ratings: number;
          playlists: number; collections: number;
        }> = {};
        for (const lib of r.libraries || []) {
          idx[lib.section_title] = {
            section_type: lib.section_type,
            item_count: lib.item_count,
            media_type_counts: lib.media_type_counts || {},
            hierarchy_counts: lib.hierarchy_counts || {},
            watch_events: lib.watch_events,
            ratings: lib.ratings,
            playlists: lib.playlists,
            collections: lib.collections,
          };
        }
        setSnapshotLibraryCounts(idx);
      },
      () => { if (!cancelled) setSnapshotLibraryCounts(null); },
    );
    return () => { cancelled = true; };
  }, [mode, restoreSource, selectedSnapshotId]);

  // Preflight result from POST /api/library-mapping/preflight.
  // Fetched whenever (source, dest, libs, mode, ignore_mapping)
  // changes so the operator sees the warning before Submit.
  const [mappingPreflight, setMappingPreflight] = useState<{
    same_server: boolean;
    cross_backend_replace_refusal: string | null;
    library_warnings: Array<{
      library: string;
      status: 'unmapped' | 'operator_skip' | 'auto_unconfirmed' | 'per_run_route' | 'per_run_skip';
      detail: string;
    }>;
    any_unmapped: boolean;
  } | null>(null);
  // Gate for the typed-REPLACE modal. Submit() flips it true when the
  // end user hits Submit with mode=replace; the modal's onConfirm
  // calls ``submitConfirmed()`` which actually fires the API request.
  const [replaceModalOpen, setReplaceModalOpen] = useState(false);
  // PIN preflight modal state. Filled by ``submit()`` from the
  // ``/api/job/preflight-pin-check`` response; ``pinPreflightAck`` is
  // set when the end user clicks Continue anyway and is stamped onto
  // the next ``api.submit*`` payload via ``stampPreflight``.
  const [pinPreflightOpen, setPinPreflightOpen] = useState(false);
  const [pinPreflightAtRisk, setPinPreflightAtRisk] = useState<string[]>([]);
  const [pinPreflightAck, setPinPreflightAck] = useState(false);

  // Cross-platform preflight state. The modal opens between
  // PinPreflight and ReplaceConfirm when the route is cross-backend
  // AND the backend's aggregate verdict is not 'ok'. On Continue, the
  // end user's per-destination decisions go into the submit payload
  // as ``cross_platform_resolutions``, read by engine-side
  // enforcement at write time.
  const [cppOpen, setCppOpen] = useState(false);
  const [cppResponse, setCppResponse] = useState<PreflightResponse | null>(null);
  const [cppAcks, setCppAcks] = useState<Record<string, CrossPlatformPreflightAck>>({});
  // Four-flag data-type filter. Applies to every job mode (snapshot
  // / import / direct) so the end user can pick exactly which data
  // types to migrate.
  //
  // The four include_* booleans below are not surfaced as standalone
  // checkboxes. The per-library LibraryMetricsMatrix is the end
  // user's only metric control. The booleans are kept (a) so submit
  // payload code populates ``include_watch_history`` etc., (b) so
  // the ``atLeastOneType`` validation gates Submit, and (c) so
  // preflight / saved-settings paths that set them still work. A
  // useEffect below derives them from libraryMetrics on every
  // change so the matrix stays the source of truth.
  const [includeWatchHistory, setIncludeWatchHistory] = useState(true);
  const [includeRatings, setIncludeRatings] = useState(true);
  const [includePlaylists, setIncludePlaylists] = useState(true);
  const [includeCollections, setIncludeCollections] = useState(true);
  // Snapshot-only: render a .plexexport.json sidecar at the end of the
  // run so the first Exports-tab download is instant. Off by default
  // (extra wall-clock cost); end users opt in per job.
  const [prebuildJsonSidecar, setPrebuildJsonSidecar] = useState(false);
  const atLeastOneType =
    includeWatchHistory || includeRatings || includePlaylists || includeCollections;

  // Snapshot-aware gating for the include_* toggles. When the end user
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
  // touched, matching the end user's mental model.
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

  // Per-side managed-user lists for direct transfer. Loaded in
  // parallel as soon as both source + destination are chosen.
  // ``null`` = not loaded yet for that side; an array (even empty)
  // means the fetch completed. ``sourceUsers === null || destUsers
  // === null`` gates the Users section's rendering so it doesn't
  // flash an empty intersection during the fetch window.
  const [sourceUsers, setSourceUsers] = useState<ServerUser[] | null>(null);
  const [destUsers, setDestUsers] = useState<ServerUser[] | null>(null);
  const [usersError, setUsersError] = useState<string | null>(null);
  // End user's set of included managed-user identifiers. Auto-initialised
  // to the full transferable intersection (all checked by default) and
  // then mutated by per-row checkbox toggles. Reset whenever either
  // server selection changes.
  const [includedUsers, setIncludedUsers] = useState<Set<string>>(new Set());

  // User filter criteria narrow the user-selection grid by
  // attributes like stored PIN/token + cross-server presence. The
  // UserFilterPanel below pushes the matching plex_id set back here.
  const [userFilteredIds, setUserFilteredIds] = useState<Set<string>>(new Set());
  const [userFilterCriteria, setUserFilterCriteria] = useState<UserFilterCriteria>(EMPTY_FILTER);

  // Source users not present on the destination, used as the
  // D-OWNER modal's proposed list. Case-insensitive match on the
  // username; missing-on-destination = "this user needs to be
  // created."
  const proposedUserCreates = useMemo<ProposedUser[]>(() => {
    if (!isCrossBackend) return [];
    if (sourceUsers === null || destUsers === null) return [];
    const destHandles = new Set(
      destUsers.map((u) => (u.plex_id || u.raw_name || '').toLowerCase()),
    );
    return sourceUsers
      .filter((u) => {
        if (u.kind === 'owner') return false;  // owners don't get auto-created
        const handle = (u.plex_id || u.raw_name || '').toLowerCase();
        if (!handle) return false;
        return !destHandles.has(handle);
      })
      .map((u) => ({
        source_user_handle: u.plex_id || u.raw_name || '',
        source_display_name: u.display_name || u.raw_name || u.plex_id || '',
        suggested_target_username: u.display_name || u.plex_id || u.raw_name || '',
      }));
  }, [isCrossBackend, sourceUsers, destUsers]);
  const userFilterActive = (Object.values(userFilterCriteria) as boolean[]).some(Boolean);

  // FECORE-04: when the attribute filter is active, the picker is fed
  // a narrowed list (sourceUsers ∩ userFilteredIds), but includedUsers
  // is the set actually submitted as user_filter. Prune includedUsers
  // down to ids the filter still admits so filtered-out users can't
  // linger in the submitted selection while invisible in the picker.
  // When the filter is inactive, leave includedUsers untouched.
  useEffect(() => {
    if (!userFilterActive) return;
    setIncludedUsers((prev) => {
      const pruned = new Set<string>();
      for (const id of prev) {
        if (userFilteredIds.has(id)) pruned.add(id);
      }
      if (pruned.size === prev.size) return prev;
      return pruned;
    });
  }, [userFilterActive, userFilteredIds]);

  // Per-library metric override map. When the end user fills any
  // cell here, the submit path sends ``library_metrics`` and the
  // backend's engine applies it per library. When the matrix is
  // empty, the four global include_* flags above are sent and the
  // backend's validator expands them into library_metrics at parse
  // time.
  const [libraryMetrics, setLibraryMetrics] = useState<LibraryMetricsMap | null>(null);

  // The four global include_* flags are derived from libraryMetrics.
  // Whenever the end user toggles a cell in the matrix, this effect
  // re-computes whether each metric is on for ANY visible library
  // and stamps the corresponding global. Submit logic sends both
  // ``include_*`` AND ``library_metrics`` (backend uses
  // library_metrics as authoritative; the globals are a fallback
  // AND drive the atLeastOneType submit gate banner).
  //
  // The some()-checks must walk the full ``libraries`` list, not
  // just Object.values(libraryMetrics): libraries the user hasn't
  // touched have no entry but default to all-on per the matrix's
  // contract (and render that way). Treat missing rows as ALL_ON so
  // the globals match what the user actually sees in the matrix.
  useEffect(() => {
    if (!libraryMetrics || libraries.length === 0) return;
    const ALL_ON: LibraryMetricsRow = {
      watch_history: true,
      ratings: true,
      playlists: true,
      collections: true,
    };
    const rows = libraries.map((lib) => libraryMetrics[lib.name] ?? ALL_ON);
    setIncludeWatchHistory(rows.some((r) => r.watch_history));
    setIncludeRatings(rows.some((r) => r.ratings));
    setIncludePlaylists(rows.some((r) => r.playlists));
    setIncludeCollections(rows.some((r) => r.collections));
  }, [libraryMetrics, libraries]);

  // selectedLibs is auto-synced from libraryMetrics (there is no
  // separate Libraries checkbox panel). A library is "selected" if
  // (a) it has no entry in libraryMetrics (default all-on per
  // LibraryMetricsMatrix's fallback behaviour) OR (b) it has an
  // entry with at least one true flag. Excluding a library is done
  // by clicking its row "none" button in the matrix - that zeroes
  // the entry and the library drops out of selectedLibs on the next
  // tick.
  useEffect(() => {
    if (libraries.length === 0) return;
    const next = new Set<string>();
    for (const lib of libraries) {
      const row = libraryMetrics?.[lib.name];
      if (!row) {
        next.add(lib.name); // missing entry = default all-on
        continue;
      }
      if (row.watch_history || row.ratings || row.playlists || row.collections) {
        next.add(lib.name);
      }
    }
    setSelectedLibs(next);
  }, [libraries, libraryMetrics]);

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
  // 90%+ of end users never touch this section so collapsing it
  // keeps the form scannable.
  const [advancedOpen, setAdvancedOpen] = useState(false);

  // Per-Run Settings sub-tab. ``general`` holds the common knobs
  // (workers, scrobble_workers, strict_match, sidecar toggle, verbose);
  // ``advanced`` holds the deeper / migration-specific knobs
  // (output_dir, log_dir, path remap, skip prebuild, fast collection
  // detection, watch+ratings strategy override). Workspace state -
  // not persisted across submissions.
  const [perRunSubTab, setPerRunSubTab] = useState<'general' | 'advanced'>('general');

  // Per-job watch+ratings filter strategy override. Top of the
  // resolution chain (per-job → per-server → global default → "smart").
  // ``''`` means "inherit" (no override sent to backend).
  const [watchRatingsStrategy, setWatchRatingsStrategy] = useState<WatchRatingsStrategy>('');

  // Mixed-media playlist per-run overrides. All five default to ''
  // (inherit) so the backend uses the global tunable. Owner-builder
  // converts '' to null at submit time.
  const [mixedMediaBehavior, setMixedMediaBehavior] = useState<MixedMediaBehavior>('');
  const [mixedMediaDominanceThreshold, setMixedMediaDominanceThreshold] = useState('');
  const [mixedMediaVideoRouting, setMixedMediaVideoRouting] = useState<MixedMediaVideoRouting>('');
  const [mixedMediaLogging, setMixedMediaLogging] = useState<MixedMediaLogging>('');
  const [mixedMediaCollisionHandling, setMixedMediaCollisionHandling] = useState<MixedMediaCollisionHandling>('');

  // Load registered servers on mount.
  // Do NOT pre-select source/destination. Auto-selecting the first
  // registered server lets operations silently run against a server
  // the user never picked; forcing a deliberate selection (and the
  // gated lower panels) makes the choice visible.
  useEffect(() => {
    setServersError(null);
    api.listServers()
      .then((rows) => {
        setServers(rows);
      })
      .catch((e) => setServersError(String(e)));
  }, []);

  // Ping poll for the selector status dots is handled by
  // usePingPoller (declared with the live ping state above).

  // Whenever the source server selection changes (and we're in a mode
  // that reads from source) refresh the library list against that
  // server's cache. We hit /api/servers/{id}/libraries which forces a
  // live re-probe; the cached result on the registry row is the same
  // value but might be stale if the server was added a long time ago.
  useEffect(() => {
    if (mode === 'restore') return;
    const srv = servers.find((s) => s.id === sourceServerName);
    if (!srv) {
      setLibraries([]);
      return;
    }
    // FECORE-13: cancellation guard so a stale listServerLibraries
    // response can't clobber a newer one on rapid server switching.
    let cancelled = false;
    setLibrariesError(null);
    api.listServerLibraries(srv.id)
      .then((libs) => { if (!cancelled) setLibraries(libs); })
      .catch((e) => { if (!cancelled) setLibrariesError(String(e)); });
    return () => { cancelled = true; };
  }, [sourceServerName, mode, servers]);

  // Load existing snapshot files when import mode is active.
  // The registry-backed listSnapshots() shape isn't compatible with
  // the JSON-file-picker UI here (registered snapshots are ``.db``
  // files; the end user can't ingest them directly through this
  // picker until the importer learns to read DB snapshots). The
  // picker lists the legacy ``.plexexport.json`` archives under
  // ``snapshots/legacy/`` - those remain ingestible by the existing
  // JSON-based importer.
  useEffect(() => {
    if (mode !== 'restore') return;
    api.listLegacySnapshots().then(setExports).catch(() => setExports([]));
    // Registered snapshots from snapshots.db - the primary import
    // source. The importer reads JSON; the route handler for
    // /api/job/import-from-snapshot materialises the sidecar before
    // forwarding, so the engine path stays unchanged.
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

  // When the end user picks a different source server, layer in any
  // per-server overrides that exist for it. Non-destructive: fields
  // the end user already changed stay changed unless the new server
  // has an explicit override for them. Resolution order documented
  // in dbschema.md - this implements step (2) for ad-hoc jobs.
  // Fire the library-mapping preflight whenever the inputs that
  // affect it change. The endpoint short-circuits to a "no warnings"
  // reply for same-server jobs + snapshot mode (snapshot doesn't
  // write to a destination, so mappings don't apply). Throttled by
  // React's effect dependency check; no debouncing needed because
  // the inputs only change on operator action.
  useEffect(() => {
    // Snapshot mode never writes to a destination; no mapping needed.
    if (mode === 'snapshot') {
      setMappingPreflight(null);
      return;
    }
    // Derive the effective source server id per mode. For
    // restore-from-snapshot the source is the snapshot's captured
    // server_id (the source server may be offline); direct +
    // restore-from-file use the form's source picker.
    const effectiveSourceId =
      mode === 'restore' && _selectedSnapshot
        ? _selectedSnapshot.server_id
        : sourceServerName;
    // Single destination check covers the most common direct +
    // restore cases. Fan-out runs check each destination separately
    // server-side, but the form gate uses just the first.
    if (!effectiveSourceId || destServerNames.size === 0) {
      setMappingPreflight(null);
      return;
    }
    const firstDest = Array.from(destServerNames)[0];
    const libs = Array.from(selectedLibs);
    if (libs.length === 0) {
      setMappingPreflight(null);
      return;
    }
    let cancelled = false;
    api.libraryMappingPreflight({
      source_server_id: effectiveSourceId,
      dest_server_id: firstDest,
      library_names: libs,
      mode: restoreMode,
      ignore_library_mapping: ignoreLibraryMapping,
      library_mapping_overrides: libraryMappingOverrides,
    }).then(
      (r) => { if (!cancelled) setMappingPreflight(r); },
      () => { if (!cancelled) setMappingPreflight(null); },
    );
    return () => { cancelled = true; };
  }, [
    mode, sourceServerName, destServerNames, selectedLibs,
    restoreMode, ignoreLibraryMapping, libraryMappingOverrides,
    _selectedSnapshot,
  ]);

  useEffect(() => {
    if (mode !== 'snapshot' && mode !== 'direct') return;
    if (!sourceServerName) return;
    const srv = servers.find((s) => s.id === sourceServerName);
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

  // v0.14 - Fast Collection Detection auto-defaulting based on Plex
  // version. Runs after the per-server-override effect above so an
  // explicit per-server value wins. When the source server's
  // ``plex_version`` is >= 1.32, default the toggle ON; otherwise
  // force it OFF (the engine would auto-fall-back anyway, but
  // surfacing the forced-off state in the UI is clearer than letting
  // end users tick a box that silently does nothing).
  //
  // Re-runs every time the source server changes - switching from a
  // supported server to an unsupported one drops the flag back to
  // OFF automatically, so a stale "on" can't leak into a job aimed
  // at an old Plex. There's no separate "user touched it" guard:
  // each new server-pick resets the toggle to the version-driven
  // default. If the end user wants a different value after that,
  // their click stays until the next server-pick.
  useEffect(() => {
    if (mode !== 'snapshot' && mode !== 'direct') return;
    if (!sourceServerName) return;
    const srv = servers.find((s) => s.id === sourceServerName);
    if (!srv) return;
    // Per-server explicit override on this field wins outright -
    // don't touch the toggle in that case (the previous effect
    // already applied it). Skip rule mirrors the override effect's
    // ``typeof === 'boolean'`` test exactly.
    const o = perServerSnapshotDefaults[srv.id];
    if (o && typeof o.fast_collection_detection === 'boolean') return;
    setFastCollectionDetection(serverSupportsFastCollections(srv.plex_version));
  }, [sourceServerName, perServerSnapshotDefaults, servers, mode]);

  // When the end user picks a snapshot as the import source (or
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

  // Load users from BOTH servers in direct mode so the form can
  // compute the transferable intersection. Reset state on every
  // selection change so we never show a stale list. The
  // includedUsers default ("all checked") is set once after the
  // fetch resolves so the end user only needs to *un*check to
  // exclude.
  //
  // Destinations are a set, so the per-user transferable
  // intersection spans the source plus *every* selected
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
    // v0.14 - Snapshot mode loads ONLY the source server's users
    // (no intersection needed; snapshot is one-way capture). We treat
    // ``destUsers`` as a mirror of ``sourceUsers`` so the shared
    // ``DirectUsersPanel`` renders the source list as "transferable"
    // (no greyed-out rows). Default-include everyone.
    if (mode === 'snapshot') {
      if (!sourceServerName) return;
      const src = servers.find((s) => s.id === sourceServerName);
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
    const src = servers.find((s) => s.id === sourceServerName);
    // destServerNames contains server IDs despite the misleading
    // "Names" suffix (the state setter is wired through
    // setDestServerIds in JobConfigBody). Look them up by id, not
    // ``s.name``: a name lookup returns undefined, which leaves
    // ``dsts`` empty, early-returns, and the user picker never
    // populates.
    const dsts = Array.from(destServerNames)
      .map((id) => servers.find((s) => s.id === id))
      .filter((s): s is ServerView => !!s);
    if (!src || dsts.length === 0) return;
    let cancelled = false;
    // The user picker reads from the local managed_users DB (no live
    // Plex round-trip during job setup). ``fetchPickerUsers`` handles
    // the cold-DB case by firing a one-shot sync and re-fetching, so
    // a server whose table was never warmed paints the picker without
    // forcing the end user to visit User Management first.
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

  // Restore mode user picker. Loads the snapshot's user list (from
  // snapshot_users in the .db) plus every destination's user list,
  // intersects them, and exposes the result through the same
  // ``sourceUsers`` / ``destUsers`` state the rest of the form reads.
  //
  // Owner-row normalisation: the snapshot stores the owner with
  // ``plex_id = ""`` (it never knew the destination's owner email at
  // capture time). The destination stores the owner with
  // ``plex_id = <owner_email>``. To make ``DirectUsersPanel``'s
  // plex_id-keyed intersection work, we rewrite the snapshot's owner
  // row to carry the FIRST destination's owner email before storing.
  // Multi-destination fan-out where destinations have different owner
  // emails is rare in practice (most end users run the same Plex
  // account across servers); the user-filter resolution catches a
  // mismatch by simply excluding owner from that destination.
  //
  // Only fires when restoring from a registered snapshot. File-based
  // restore (loose .plexexport.json picks) doesn't get a user picker
  // - the file would need to be parsed; we treat that as a future
  // refinement and leave the filter empty (= all users).
  useEffect(() => {
    if (mode !== 'restore') return;
    if (restoreSource !== 'snapshot') return;
    if (!selectedSnapshotId) return;
    if (destServerNames.size === 0) return;
    // destServerNames carries server IDs (see comment in the
    // direct-mode effect above). Look them up by id, not ``s.name``:
    // a name lookup leaves ``dsts`` empty and the restore-mode user
    // picker never appears.
    const dsts = Array.from(destServerNames)
      .map((id) => servers.find((s) => s.id === id))
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
    // Restore-mode parity: also fetch managed_users for every
    // destination so the default "checked" set excludes users who
    // can't actually be impersonated on every destination. Owner is
    // always defaulted in (admin token). Users without credentials
    // remain VISIBLE in the picker (unchecked) so the end user can
    // still pick them and supply credentials later.
    const muPromise = Promise.allSettled(
      dsts.map((d) => api.listServerManagedUsers(d.id, true)),
    );
    Promise.all([snapPromise, destPromise, muPromise]).then(([snapRs, destResults, muResults]) => {
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
      // Build a per-destination "user -> hasCredential" map. We AND
      // across destinations: a user is default-checked only when EVERY
      // destination has either a token or a PIN for them.
      const credByDest: Record<string, Set<string>> = {};
      muResults.forEach((r, i) => {
        const destId = dsts[i].id;
        if (r.status !== 'fulfilled') {
          credByDest[destId] = new Set();
          return;
        }
        const usernames = new Set<string>();
        for (const row of r.value.users || []) {
          if (row.has_token || row.has_pin) usernames.add(row.username);
        }
        credByDest[destId] = usernames;
      });
      const destIds = dsts.map((d) => d.id);
      const dstIds = new Set(destIntersection.map((u) => u.plex_id));
      const defaultChecked = normalisedSnapUsers
        .filter((u) => dstIds.has(u.plex_id))
        .filter((u) => {
          if (u.kind === 'owner') return true; // admin token covers owner
          // Require credentials on EVERY destination. raw_name matches
          // the managed_users.username field (the same key
          // UserFilterPanel uses).
          return destIds.every((sid) => (credByDest[sid] || new Set()).has(u.raw_name));
        })
        .map((u) => u.plex_id);
      setIncludedUsers(new Set(defaultChecked));
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
  // Stamp the end user's preflight acknowledgement onto a submission
  // payload. No-op if the end user did not see the modal. Captured
  // as a closure so each ``api.submit*`` call site only needs
  // ``stampPreflight(payload)`` right before it fires.
  const stampPreflight = (payload: Record<string, unknown>) => {
    if (pinPreflightAck) {
      payload.pin_preflight_acknowledged = true;
      payload.pin_preflight_at_risk = pinPreflightAtRisk;
    }
    // Bundle the end user's cross-platform preflight decisions when
    // the modal was shown + Continue was clicked. Backend reads this
    // at submit time for engine-side enforcement. Same key the
    // schedule row uses.
    if (Object.keys(cppAcks).length > 0) {
      payload.cross_platform_resolutions = cppAcks;
    }
  };

  // Cross-platform preflight gate. Run between PinPreflight and
  // ReplaceConfirm. When the route is cross-backend, fire the
  // backend's preflight and let the verdict drive the modal/Submit
  // relationship:
  //   * verdict='ok' -> fall through to the Replace modal / submit
  //     directly (no modal shown).
  //   * verdict='ack_required' | 'blocked' -> open the modal; submit
  //     resumes via the modal's onContinue callback.
  // Network failure on the preflight call is soft: fall through to
  // submit (the engine's belt-and-braces enforcement still runs).
  const crossPlatformGate = async () => {
    if (!isCrossBackend) {
      await replaceOrSubmit();
      return;
    }
    // Build a preflight body that matches the backend's
    // CrossPlatformPreflightJobIn shape. Supports snapshot_id /
    // input_files + destinations; direct-transfer preflight is a v2
    // follow-up.
    const body: Record<string, unknown> = {
      // id-keyed destinations.
      dest_server_ids: Array.from(destServerNames),
      dest_server_names: _idsToNames(destServerNames),
    };
    if (mode === 'restore' && restoreSource === 'snapshot' && selectedSnapshotId) {
      body.snapshot_id = selectedSnapshotId;
    } else if (mode === 'restore' && selectedFiles.size > 0) {
      body.input_files = Array.from(selectedFiles);
    } else {
      // Direct or snapshot mode: preflight v1 doesn't cover these
      // (no snapshot file to inspect). Fall through to submit; the
      // engine's belt-and-braces enforcement catches block-class
      // configurations at write time.
      await replaceOrSubmit();
      return;
    }
    body.include_watch_history = includeWatchHistory;
    body.include_ratings = includeRatings;
    body.include_playlists = includePlaylists;
    body.include_collections = includeCollections;
    body.include_managed_users = includeManagedUsers;
    if (includedUsers.size > 0) {
      body.user_filter = Array.from(includedUsers);
    }
    try {
      const r = await api.jobsCrossPlatformPreflight(body);
      if (r.aggregate_verdict === 'ok') {
        await replaceOrSubmit();
        return;
      }
      setCppResponse(r);
      setCppOpen(true);
      // Modal's onContinue will set acks and call replaceOrSubmit().
    } catch {
      // Network failure - don't block submit. Engine enforcement
      // still applies.
      await replaceOrSubmit();
    }
  };

  // Post-preflight continuation. Pops the Replace modal at the right
  // moment when the preflight is clear (or after Continue anyway).
  // ``afterPreflight`` is an alias kept for existing call sites.
  const replaceOrSubmit = async () => {
    if ((mode === 'restore' || mode === 'direct') && restoreMode === 'replace') {
      setSubmitError(null);
      setSubmitOk(null);
      setReplaceModalOpen(true);
      return;
    }
    await submitConfirmed();
  };
  const afterPreflight = async () => {
    // After PIN preflight passes, run the cross-platform preflight
    // gate. It chains to replaceOrSubmit() either directly (verdict=ok
    // or non-cross-backend) or via the modal's Continue callback
    // (verdict=ack_required / blocked).
    await crossPlatformGate();
  };

  const submit = async () => {
    // D-OWNER intercept: when a cross-backend route would create
    // users on the destination AND the end user hasn't already
    // confirmed the modal, open it now and bail. The modal's
    // onConfirm flips userCreateConfirmed true and re-fires submit().
    if (
      isCrossBackend
      && proposedUserCreates.length > 0
      && !userCreateConfirmed
    ) {
      setUserCreateModalOpen(true);
      return;
    }

    // Ask the backend whether any in-scope managed user is
    // PIN-protected with no credentials on file. ``restore`` mode
    // always returns ``checked: false`` so this naturally skips the
    // modal for file-mediated runs. Preflight network failure is
    // soft: we proceed without the modal rather than blocking the
    // submit (the engine still falls back to admin-token
    // impersonation).
    let atRisk: string[] = [];
    try {
      const r = await api.preflightPinCheck({
        mode,
        source_server_name: _idToName(sourceServerName) || null,
        dest_server_names: _idsToNames(destServerNames),
        user_filter: includedUsers.size > 0 ? Array.from(includedUsers) : null,
      });
      if (r.checked && r.at_risk_users.length > 0) {
        atRisk = r.at_risk_users;
      }
    } catch {
      // Graceful degrade: the end user can still submit, the engine
      // handles missing creds at the per-user level as before.
    }

    if (atRisk.length > 0) {
      setPinPreflightAtRisk(atRisk);
      setPinPreflightAck(false);
      setPinPreflightOpen(true);
      return;
    }

    await afterPreflight();
  };

  // Mutate the payload to add the 5 mixed-media override fields.
  // Empty strings (the "inherit" sentinel) are omitted so the
  // backend uses the global tunable.
  const stampMixedMedia = (payload: Record<string, unknown>) => {
    if (mixedMediaBehavior) payload.mixed_media_behavior = mixedMediaBehavior;
    if (mixedMediaDominanceThreshold) {
      const n = Number(mixedMediaDominanceThreshold);
      if (Number.isFinite(n)) payload.mixed_media_dominance_threshold = n;
    }
    if (mixedMediaVideoRouting) payload.mixed_media_video_routing = mixedMediaVideoRouting;
    if (mixedMediaLogging) payload.mixed_media_logging = mixedMediaLogging;
    if (mixedMediaCollisionHandling) payload.mixed_media_collision_handling = mixedMediaCollisionHandling;
  };

  const submitConfirmed = async () => {
    setSubmitError(null);
    setSubmitOk(null);
    setSubmitting(true);
    try {
      if (mode === 'snapshot') {
        if (!sourceServerName) throw new Error('Pick a source server first.');
        const payload: Record<string, unknown> = {
          // Send the stable server id; backend prefers it over the
          // friendly name. Name is sent too for any legacy code
          // paths that still route by name.
          source_server_id: sourceServerName,
          source_server_name: _idToName(sourceServerName),
          libraries: Array.from(selectedLibs),
        };
        if (outputDir) payload.output_dir = outputDir;
        if (workers) payload.workers = Number(workers);
        if (scrobbleWorkers) payload.scrobble_workers = Number(scrobbleWorkers);
        if (logDir) payload.log_dir = logDir;
        payload.verbose = verbose;
        if (fastCollectionDetection) payload.fast_collection_detection = true;
        if (skipPlaylistPrebuild) payload.skip_playlist_prebuild = true;
        // Four-flag data-type filter. Always sent so the server has
        // an explicit value rather than relying on a model default.
        // Defaults are all true so this is a no-op when the end user
        // hasn't unchecked anything.
        payload.include_watch_history = includeWatchHistory;
        payload.include_ratings = includeRatings;
        payload.include_playlists = includePlaylists;
        payload.include_collections = includeCollections;
        // When the end user filled in any per-library cell, send the
        // explicit per-library map instead of letting the backend
        // expand the global flags. The backend treats
        // library_metrics as the source of truth when set.
        if (libraryMetrics && Object.keys(libraryMetrics).length > 0) {
          payload.library_metrics = libraryMetrics;
        }
        payload.prebuild_json_sidecar = prebuildJsonSidecar;
        // Per-user fan-out toggle. Snapshot mode only cares about
        // this (no destination writes). Default ON; explicit OFF
        // tells the engine to skip the managed-user gather pass.
        payload.include_managed_users = includeManagedUsers;
        // Per-job watch+ratings strategy override (top of resolution
        // chain). Empty string means "inherit" → omit field so backend
        // falls through to per-server / global / default.
        if (watchRatingsStrategy) payload.watch_ratings_filter_strategy = watchRatingsStrategy;
        // v0.14 - per-snapshot user filter. Send the explicit list
        // when the end user has picked a subset; omit entirely when
        // every available user is checked (= historical "all users"
        // default at the backend).
        if (sourceUsers !== null && destUsers !== null) {
          const dstIds = new Set(destUsers.map((u) => u.plex_id));
          const transferable = sourceUsers.filter((u) => dstIds.has(u.plex_id));
          if (includedUsers.size < transferable.length) {
            payload.user_filter = Array.from(includedUsers);
          }
        }
        stampMixedMedia(payload);
        stampPreflight(payload);
        const r = await api.submitSnapshot(payload);
        setSubmitOk(`Snapshot job ${r.job_id} queued.`);
      } else if (mode === 'restore') {
        if (destServerNames.size === 0) throw new Error('Pick at least one destination server first.');
        const destList = _idsToNames(destServerNames);
        const payload: Record<string, unknown> = {
          // id-keyed destinations.
          dest_server_ids: Array.from(destServerNames),
          dest_server_names: destList,
          strict_match: strictMatch,
          overwrite_playlists: overwritePlaylists,
          mode: restoreMode,
          auto_capture_before_replace: autoCaptureBeforeReplace,
          confirm_replace: restoreMode === 'replace',
          merge_watch_strategy: mergeWatchStrategy,
          // Per-run bypass for the saved mapping table. Default
          // false so the mapping table is always consulted; flipping
          // the checkbox makes the engine fall back to exact-name
          // match only.
          ignore_library_mapping: ignoreLibraryMapping,
          // Per-run library name overrides. Omitted entirely when no
          // entries are set so the engine sees None and goes
          // straight to the saved table consult. When entries ARE
          // set, the engine reads them BEFORE the saved table.
          ...(Object.keys(libraryMappingOverrides).length > 0
            ? { library_mapping_overrides: libraryMappingOverrides }
            : {}),
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
        // When the end user filled in any per-library cell, send the
        // explicit per-library map. Engine override at
        // restore_export_file honours it per-library.
        if (libraryMetrics && Object.keys(libraryMetrics).length > 0) {
          payload.library_metrics = libraryMetrics;
        }
        // Cross-backend payload additions on the restore path.
        // include_managed_users always sent; the other two are gated
        // on cross-backend routes where they actually apply (the
        // backend ignores them on same-backend routes).
        payload.include_managed_users = includeManagedUsers;
        if (isCrossBackend) {
          if (rateMode === 'tunable') {
            const n = parseFloat(rateThreshold);
            payload.favorite_threshold = Number.isFinite(n) ? n : 5.0;
          } else if (rateMode === 'numeric_only') {
            payload.favorite_threshold = 11.0;
          }
          // rateMode === 'default' omits the field; backend uses 5.0.
          if (userCreateSpecs.length > 0) {
            payload.user_create_specs = userCreateSpecs;
          }
        }
        // Per-restore user filter. Only applicable to the
        // snapshot-based restore path (file-based restore doesn't
        // surface a user picker yet - would require parsing the
        // file). Send the explicit list when the end user picked a
        // subset; omit entirely when every intersectable user is
        // selected (= "all users" default at the backend).
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

        stampMixedMedia(payload);
        let r;
        if (restoreSource === 'snapshot') {
          if (!selectedSnapshotId) throw new Error('Pick a registered snapshot first.');
          payload.snapshot_id = selectedSnapshotId;
          stampPreflight(payload);
          r = await api.submitRestoreFromSnapshot(payload);
        } else {
          if (selectedFiles.size === 0) throw new Error('Pick at least one export file.');
          payload.input_files = Array.from(selectedFiles);
          stampPreflight(payload);
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
        const destList = _idsToNames(destServerNames);
        const payload: Record<string, unknown> = {
          // id-keyed source + destinations.
          source_server_id: sourceServerName,
          source_server_name: _idToName(sourceServerName),
          dest_server_ids: Array.from(destServerNames),
          dest_server_names: destList,
          libraries: Array.from(selectedLibs),
          strict_match: strictMatch,
          mode: restoreMode,
          auto_capture_before_replace: autoCaptureBeforeReplace,
          confirm_replace: restoreMode === 'replace',
          merge_watch_strategy: mergeWatchStrategy,
          // Per-run bypass for the saved mapping table. Default
          // false so the mapping table is always consulted; flipping
          // the checkbox makes the engine fall back to exact-name
          // match only.
          ignore_library_mapping: ignoreLibraryMapping,
          // Per-run library name overrides. Omitted entirely when no
          // entries are set so the engine sees None and goes
          // straight to the saved table consult. When entries ARE
          // set, the engine reads them BEFORE the saved table.
          ...(Object.keys(libraryMappingOverrides).length > 0
            ? { library_mapping_overrides: libraryMappingOverrides }
            : {}),
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
        // Four-flag data-type filter on direct too.
        payload.include_watch_history = includeWatchHistory;
        payload.include_ratings = includeRatings;
        payload.include_playlists = includePlaylists;
        payload.include_collections = includeCollections;
        // Per-library metric map. _transfer_one_library applies the
        // override at the top so every downstream gather path sees
        // the per-library include_* booleans.
        if (libraryMetrics && Object.keys(libraryMetrics).length > 0) {
          payload.library_metrics = libraryMetrics;
        }
        // Cross-backend payload additions on the direct-transfer
        // path. Same shape as on the restore path.
        payload.include_managed_users = includeManagedUsers;
        if (isCrossBackend) {
          if (rateMode === 'tunable') {
            const n = parseFloat(rateThreshold);
            payload.favorite_threshold = Number.isFinite(n) ? n : 5.0;
          } else if (rateMode === 'numeric_only') {
            payload.favorite_threshold = 11.0;
          }
          if (userCreateSpecs.length > 0) {
            payload.user_create_specs = userCreateSpecs;
          }
        }
        if (watchRatingsStrategy) payload.watch_ratings_filter_strategy = watchRatingsStrategy;
        // Send ``user_filter`` whenever the Users section rendered
        // AND at least one transferable entry exists (owner OR
        // managed). If both servers report no users we omit the
        // field so the backend's "None = include all" default
        // applies. Owner is included in the intersection check;
        // unchecking the owner is how the end user skips
        // library-level data.
        if (sourceUsers !== null && destUsers !== null) {
          const dstIds = new Set(destUsers.map((u) => u.plex_id));
          const hasIntersection = sourceUsers.some((u) => dstIds.has(u.plex_id));
          if (hasIntersection) {
            payload.user_filter = Array.from(includedUsers);
          }
        }
        stampMixedMedia(payload);
        stampPreflight(payload);
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
      // Clear the ack so the next Run click triggers a fresh
      // preflight check. Without this, a re-submit after a failure
      // would silently re-use the prior acknowledgement.
      setPinPreflightAck(false);
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

  // Build the JobConfigOwner from local state + derived values.
  // JobConfigBody renders the shared panel stack from this owner;
  // both Run Job and Schedules build their own owner of the same
  // shape.
  const _serversReady = serversReady(mode, sourceServerName, destServerNames);
  const owner: JobConfigOwner = {
    idScope: 'job',
    mode, setMode,
    workflowMode, setWorkflowMode,
    rateMode, setRateMode,
    rateThreshold, setRateThreshold,
    userCreateSpecCount: userCreateSpecs.length,
    onOpenUserCreateModal: () => setUserCreateModalOpen(true),
    servers, pings,
    sourceServerId: sourceServerName,
    setSourceServerId: setSourceServerName,
    destServerIds: destServerNames,
    setDestServerIds: setDestServerNames,
    sourceBackend, destBackend, isCrossBackend,
    workflowServers,
    populatedBackendsCount: populatedBackends.length,
    showWorkflowTabs,
    serversReady: _serversReady,
    includeManagedUsers, setIncludeManagedUsers,
    sourceUsers, destUsers,
    includedUsers, setIncludedUsers,
    userFilteredIds, setUserFilteredIds,
    userFilterCriteria, setUserFilterCriteria,
    userFilterActive,
    usersError,
    libraries,
    librariesError,
    selectedLibs,
    libraryMetrics, setLibraryMetrics,
    atLeastOneType,
    restoreSource,
    selectedSnapshot: _selectedSnapshot ?? null,
    snapshotHasWatchHistory,
    snapshotHasRatings,
    snapshotHasPlaylists,
    snapshotHasCollections,
    restoreMode, setRestoreMode,
    mergeWatchStrategy, setMergeWatchStrategy,
    autoCaptureBeforeReplace, setAutoCaptureBeforeReplace,
    advancedOpen, setAdvancedOpen,
    perRunSubTab, setPerRunSubTab,
    workers, setWorkers,
    scrobbleWorkers, setScrobbleWorkers,
    strictMatch, setStrictMatch,
    overwritePlaylists, setOverwritePlaylists,
    prebuildJsonSidecar, setPrebuildJsonSidecar,
    verbose, setVerbose,
    outputDir, setOutputDir,
    logDir, setLogDir,
    remapOld, setRemapOld,
    remapNew, setRemapNew,
    skipPlaylistPrebuild, setSkipPlaylistPrebuild,
    fastCollectionDetection, setFastCollectionDetection,
    watchRatingsStrategy, setWatchRatingsStrategy,
    mixedMediaBehavior, setMixedMediaBehavior,
    mixedMediaDominanceThreshold, setMixedMediaDominanceThreshold,
    mixedMediaVideoRouting, setMixedMediaVideoRouting,
    mixedMediaLogging, setMixedMediaLogging,
    mixedMediaCollisionHandling, setMixedMediaCollisionHandling,
  };

  // Restore source slot — Run Job's snapshot picker + JSON file picker.
  // Rendered inside JobConfigBody between user pickers and the
  // DataToMigratePanel. Schedules passes its own version (textarea).
  const restoreSourceSlot = mode === 'restore' ? (
    <div className="panel">
      <h2>Restore source</h2>
      <div className="row-buttons" style={{ marginBottom: 12 }}>
        <button
          data-testid="job-restore-source-snapshot"
          type="button"
          className={restoreSource === 'snapshot' ? 'primary' : ''}
          onClick={() => setRestoreSource('snapshot')}
        >
          From registered snapshot
        </button>
        <button
          data-testid="job-restore-source-file"
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
  ) : null;

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
      <JobConfigBody
        owner={owner}
        restoreSourceSlot={restoreSourceSlot}
        libraryMappingSlot={
          /* Per-run library mapping editor. JobConfigBody renders
              this between DataToMigratePanel and PerRunSettingsPanel
              - same cluster as "what's leaving / where's it going".
              Collapsed by default; expands to a two-column
              click-to-link editor (mirrors Server Syncing >
              Library Mapping's UX) that writes pairs to LOCAL
              STATE only (never persists to the shared mapping
              table).

              Source-side data is derived per mode:
                * restore-from-snapshot: snapshot.server_id +
                  snapshot.libraries (the snapshot's captured
                  library list - the source server may be offline
                  and we wouldn't be able to query it live)
                * direct: sourceServerName + the form's live
                  libraries list (already loaded by the form's
                  per-mode effect) */
          <RunJobLibraryMappingPanel
            enabled={mode === 'restore' || mode === 'direct'}
            sourceServerId={
              mode === 'restore' && _selectedSnapshot
                ? _selectedSnapshot.server_id
                : sourceServerName
            }
            destServerIds={Array.from(destServerNames)}
            selectedLibraryNames={Array.from(selectedLibs)}
            sourceLibraries={
              mode === 'restore' && _selectedSnapshot
                ? (_selectedSnapshot.libraries || []).map((n) => {
                    // Enrich each snapshot library with
                    // counts pulled from the snapshot.db join
                    // (server_items + items). Two count families are
                    // used:
                    //   media_type_counts -> COUNT of leaf rows by
                    //     items.media_type (track, episode, movie ...)
                    //   hierarchy_counts  -> COUNT(DISTINCT ...) over
                    //     the hierarchy columns (artist, show_title,
                    //     show_title+season_index, album). This is
                    //     what makes "number of artists" / "number of
                    //     series" accurate even though the snapshot
                    //     only stores leaf rows.
                    // Note: these counts reflect WHAT THE SNAPSHOT
                    // CAPTURED, not the live library's current state.
                    // A snapshot that captured only watched tracks
                    // reports tracks=N + artists=(distinct artists of
                    // those tracks) rather than the library total.
                    // That's correct behaviour for the panel's job
                    // ("show me what's available to move"); the copy
                    // below in the panel surfaces a reminder so
                    // operators don't read smaller counts as a bug.
                    const ct = snapshotLibraryCounts?.[n];
                    if (!ct) {
                      return { name: n, type: '', key: n, count: 0 };
                    }
                    const t = (ct.section_type || '').toLowerCase();
                    const mt = ct.media_type_counts || {};
                    const hc = ct.hierarchy_counts || {};
                    const leaf: LeafCounts = {};
                    let topCount = ct.item_count;
                    if (t === 'show') {
                      // top-level unit is the series; seasons and
                      // episodes drop to leaf rows.
                      topCount = hc.series || mt.show || ct.item_count;
                      if (hc.seasons) leaf.seasons = hc.seasons;
                      if (mt.episode) leaf.episodes = mt.episode;
                    } else if (t === 'artist') {
                      // top-level unit is the artist; albums and
                      // tracks drop to leaf rows.
                      topCount = hc.artists || mt.artist || ct.item_count;
                      if (hc.albums) leaf.albums = hc.albums;
                      if (mt.track) leaf.tracks = mt.track;
                    } else if (t === 'movie') {
                      topCount = mt.movie || ct.item_count;
                      if (ct.collections > 0) {
                        leaf.collections = ct.collections;
                      }
                    }
                    if (ct.playlists > 0) leaf.playlists = ct.playlists;
                    return {
                      name: n,
                      type: ct.section_type,
                      key: n,
                      count: topCount,
                      leaf_counts: leaf,
                    };
                  })
                : libraries
            }
            ignoreLibraryMapping={ignoreLibraryMapping}
            onIgnoreLibraryMappingChange={setIgnoreLibraryMapping}
            libraryMappingOverrides={libraryMappingOverrides}
            onLibraryMappingOverridesChange={setLibraryMappingOverrides}
            mappingPreflight={mappingPreflight}
            restoreMode={restoreMode}
            sourceIsSnapshot={mode === 'restore' && restoreSource === 'snapshot'}
          />
        }
      />

      {/* Placeholder fieldset wrapping the Submit-button block,
          which lives in this page's wrapper rather than the shared
          JobConfigBody. The disabled attribute lines up with
          owner.serversReady; visual greying happens via inline
          style. After this block closes, the remaining JSX
          (sum-mode warning, Submit row) renders. */}
      <fieldset
        className="job-form-gate"
        disabled={!_serversReady}
        style={{
          border: 'none', padding: 0, margin: 0, minWidth: 0,
          opacity: _serversReady ? 1 : 0.5,
          pointerEvents: _serversReady ? 'auto' : 'none',
        }}
      >

      <div className="panel">
        <div className="row-buttons">
          <button
            data-testid="job-submit"
            className="primary"
            disabled={
              submitting
              || servers.length === 0
              || !atLeastOneType
              // FECORE-01: when a user filter is active, gate on the
              // SUBMITTED set (includedUsers), not userFilteredIds (the
              // attribute-match set). The two diverge once the operator
              // unchecks users, and the job submits includedUsers - so
              // an empty included set with a filter active means the
              // engine has nothing to do for managed-user content.
              || (userFilterActive && includedUsers.size === 0)
              // Cross-backend Submit is verdict-driven by the
              // CrossPlatformPreflightModal. The line below is a
              // no-op (workflowEngineReady is always true), kept so
              // the disabled-expression structure stays intact in
              // case a future readiness check re-enters here.
              || !workflowEngineReady
              // Refuse cross-backend Replace pre-submit when the
              // server-side guard would refuse it too. Same error
              // copy renders in the banner above.
              || Boolean(mappingPreflight?.cross_backend_replace_refusal)
            }
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
              : _idsToNames(destServerNames)[0] || 'destination'
            : destServerNames.size > 1
              ? `${destServerNames.size} destinations`
              : _idsToNames(destServerNames)[0] || 'destination'
        }
        autoCaptureBeforeReplace={autoCaptureBeforeReplace}
        onCancel={() => setReplaceModalOpen(false)}
        onConfirm={() => {
          setReplaceModalOpen(false);
          void submitConfirmed();
        }}
      />

      {/* PIN preflight warning. Sits between submit() and the
          Replace modal / submitConfirmed() so the end user confirms
          before any commit. Cancel aborts; Continue anyway stamps the
          ack on the next payload and proceeds to ``afterPreflight``. */}
      <PinPreflightModal
        open={pinPreflightOpen}
        atRiskUsers={pinPreflightAtRisk}
        onCancel={() => {
          setPinPreflightOpen(false);
          setPinPreflightAtRisk([]);
        }}
        onContinue={() => {
          setPinPreflightOpen(false);
          setPinPreflightAck(true);
          void afterPreflight();
        }}
      />

      {/* Cross-platform preflight modal. Opens between PinPreflight
          and ReplaceConfirm when the actively-picked source /
          destination backends differ AND the backend's preflight
          returns aggregate_verdict !== 'ok'. End user decisions
          persist into the submit payload via stampPreflight
          (cross_platform_resolutions field). */}
      <CrossPlatformPreflightModal
        open={cppOpen}
        response={cppResponse}
        onCancel={() => {
          setCppOpen(false);
          setCppResponse(null);
        }}
        onContinue={(acks: Record<string, CrossPlatformPreflightAck>) => {
          setCppOpen(false);
          setCppResponse(null);
          setCppAcks(acks);
          // Defer one tick so setCppAcks commits before stampPreflight
          // reads it.
          window.setTimeout(() => void replaceOrSubmit(), 0);
        }}
      />

      {/* D-OWNER user-creation modal. Opens from two paths: (a) the
          end user clicks "Review users to create" in the
          cross-backend sub-card; (b) Submit fires while a
          cross-backend route has proposed users to create and the
          end user has not yet confirmed. onConfirm saves the spec
          list, marks userCreateConfirmed, then immediately re-fires
          submit() so the deferred submit completes without a second
          click. */}
      <UserCreationModal
        open={userCreateModalOpen}
        proposed={proposedUserCreates}
        initialSpecs={userCreateSpecs as ModalUserCreateSpec[]}
        onClose={() => setUserCreateModalOpen(false)}
        onConfirm={(specs) => {
          setUserCreateSpecs(specs);
          setUserCreateConfirmed(true);
          setUserCreateModalOpen(false);
          // If the modal was opened by Submit-intercept, re-fire
          // submit now that the end user has confirmed. The flag
          // above prevents another intercept loop.
          void submit();
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
 * Build a readable short-form label for an export file in the
 * import picker. Prefers ``{library}  {source_server}
 * (short date)`` when both library and source_server are populated;
 * falls back to whatever's available without the dashes / parens so
 * older exports (no source_server, no captured_at) still render
 * cleanly. The full filename stays in the ``help`` row underneath
 * so end users can still copy-paste it when needed.
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
function destLibraryNames(servers: ServerView[], destIds: Set<string>): Set<string> {
  const out = new Set<string>();
  if (destIds.size === 0) return out;
  for (const s of servers) {
    if (!destIds.has(s.id)) continue;
    for (const lib of s.last_libraries || []) {
      const n = (lib.name || '').trim();
      if (n) out.add(n);
    }
  }
  return out;
}
