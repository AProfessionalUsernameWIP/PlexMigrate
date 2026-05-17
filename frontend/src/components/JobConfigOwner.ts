// Shared owner interface for JobConfigBody.
//
// Both JobFormPanel (Run Job) and SchedulesPanel (Schedules) build an
// owner of this shape and hand it to JobConfigBody. JobFormPanel's
// owner reads from useState hooks + computes derived values inline.
// SchedulesPanel's owner reads from the `schedule` object via getter
// fields + writes via `set('field', value)` callbacks. The body
// component renders identical JSX regardless of source.
//
// Phase E (Plan[SCHEDULES-ALIGNMENT-V2], 2026-05-16): introduced to
// dedupe the ~400 lines of panel-stack wiring that was duplicated
// across the two pages.

import type {
  LibraryDescriptor,
  PingResult,
  ServerUser,
  ServerView,
  Snapshot,
} from '../api';
import type { LibraryMetricsMap } from './LibraryMetricsMatrix';
import type { Mode } from './ModeAndServersPanel';
import type {
  BackendOrCross,
  RateMode,
  WorkflowMode,
} from './WorkflowStrip';
import type {
  PerRunSubTab,
  WatchRatingsStrategy,
} from './PerRunSettingsPanel';
import type {
  MergeWatchStrategy,
  RestoreMode,
} from './RestoreModeSelector';
import type { UserFilterCriteria } from './UserFilterPanel';

export interface JobConfigOwner {
  // ── Identity ────────────────────────────────────────────────────
  // Disambiguates radio name attributes when the body is mounted in
  // two places on the same document. Run Job passes 'job'; Schedules
  // passes 'sched-${id || "new"}'.
  idScope: string;

  // ── Mode ────────────────────────────────────────────────────────
  mode: Mode;
  setMode: (m: Mode) => void;

  // ── Set Backend (WorkflowStrip) ────────────────────────────────
  workflowMode: WorkflowMode;
  setWorkflowMode: (m: WorkflowMode) => void;
  rateMode: RateMode;
  setRateMode: (m: RateMode) => void;
  rateThreshold: string;
  setRateThreshold: (v: string) => void;
  // D-OWNER trigger button was removed in Phase D (subsumed by the
  // InlineCreateUserForm inside CrossPlatformPreflightModal), but the
  // WorkflowStrip prop interface still carries these two for back-
  // compat. Run Job passes the real values + onOpen; Schedules
  // passes 0 + a no-op.
  userCreateSpecCount: number;
  onOpenUserCreateModal: () => void;

  // ── Servers (ModeAndServersPanel) ──────────────────────────────
  servers: ServerView[];
  pings: Record<string, PingResult>;
  // ServerPicker keys by id; the owner translates name <-> id at the
  // boundary on Schedules.
  sourceServerId: string;
  setSourceServerId: (id: string) => void;
  destServerIds: Set<string>;
  setDestServerIds: (next: Set<string>) => void;

  // ── Derived backend identity (consumed by WorkflowStrip + the
  //     cross-backend sub-card). Pages compute these from picked
  //     servers' service_type.
  sourceBackend: BackendOrCross;
  destBackend: BackendOrCross;
  isCrossBackend: boolean;

  // ── Server-list filtering (consumed by ModeAndServersPanel) ─────
  // Pre-computed by the owner-builder so JobConfigBody doesn't need
  // to know about backend filtering rules.
  workflowServers: ServerView[];
  populatedBackendsCount: number;
  showWorkflowTabs: boolean;

  // ── Mode-specific selection-ready gate ──────────────────────────
  // True when the active mode's source/dest selection prerequisites
  // are met. Drives the fieldset disabled gate inside JobConfigBody.
  serversReady: boolean;

  // ── Scope: Users ────────────────────────────────────────────────
  includeManagedUsers: boolean;
  setIncludeManagedUsers: (v: boolean) => void;
  sourceUsers: ServerUser[] | null;
  destUsers: ServerUser[] | null;
  includedUsers: Set<string>;
  setIncludedUsers: (s: Set<string>) => void;
  userFilteredIds: Set<string>;
  setUserFilteredIds: (s: Set<string>) => void;
  userFilterCriteria: UserFilterCriteria;
  setUserFilterCriteria: (c: UserFilterCriteria) => void;
  userFilterActive: boolean;
  usersError: string | null;

  // ── Scope: Libraries + matrix ───────────────────────────────────
  libraries: LibraryDescriptor[];
  librariesError: string | null;
  selectedLibs: Set<string>;
  libraryMetrics: LibraryMetricsMap | null;
  setLibraryMetrics: (m: LibraryMetricsMap) => void;
  atLeastOneType: boolean;

  // ── Restore-from-snapshot context (for DataToMigratePanel) ──────
  // restoreSource defaults to 'snapshot'. Schedules pin this to
  // 'snapshot' since they don't support file mode; Run Job toggles
  // between snapshot picker / JSON file picker.
  restoreSource: 'snapshot' | 'file';
  selectedSnapshot: Snapshot | null;
  snapshotHasWatchHistory: boolean;
  snapshotHasRatings: boolean;
  snapshotHasPlaylists: boolean;
  snapshotHasCollections: boolean;

  // ── Restoration mode (selector + sub-strategy) ──────────────────
  restoreMode: RestoreMode;
  setRestoreMode: (m: RestoreMode) => void;
  mergeWatchStrategy: MergeWatchStrategy;
  setMergeWatchStrategy: (s: MergeWatchStrategy) => void;
  autoCaptureBeforeReplace: boolean;
  setAutoCaptureBeforeReplace: (v: boolean) => void;

  // ── Per-Run Settings (collapse + sub-tab + 12 fields) ───────────
  advancedOpen: boolean;
  setAdvancedOpen: (v: boolean) => void;
  perRunSubTab: PerRunSubTab;
  setPerRunSubTab: (t: PerRunSubTab) => void;
  workers: string;
  setWorkers: (v: string) => void;
  scrobbleWorkers: string;
  setScrobbleWorkers: (v: string) => void;
  strictMatch: boolean;
  setStrictMatch: (v: boolean) => void;
  overwritePlaylists: boolean;
  setOverwritePlaylists: (v: boolean) => void;
  prebuildJsonSidecar: boolean;
  setPrebuildJsonSidecar: (v: boolean) => void;
  verbose: boolean;
  setVerbose: (v: boolean) => void;
  outputDir: string;
  setOutputDir: (v: string) => void;
  logDir: string;
  setLogDir: (v: string) => void;
  remapOld: string;
  setRemapOld: (v: string) => void;
  remapNew: string;
  setRemapNew: (v: string) => void;
  skipPlaylistPrebuild: boolean;
  setSkipPlaylistPrebuild: (v: boolean) => void;
  fastCollectionDetection: boolean;
  setFastCollectionDetection: (v: boolean) => void;
  watchRatingsStrategy: WatchRatingsStrategy;
  setWatchRatingsStrategy: (v: WatchRatingsStrategy) => void;

  // ── Mixed-media playlist per-run overrides ─────────────────────
  // Plan[MIXED-MEDIA-PLAYLISTS]-2026-05-16. All five inherit the
  // global tunable when set to '' (string fields) or '' (numeric
  // field — empty string in the input). The owner-builder converts
  // '' to null at submit time. UI lives in the General sub-tab of
  // PerRunSettingsPanel, gated to restore/direct modes (snapshot
  // doesn't write playlists, so the override is a no-op there).
  mixedMediaBehavior: '' | 'skip' | 'dominant' | 'split';
  setMixedMediaBehavior: (v: '' | 'skip' | 'dominant' | 'split') => void;
  mixedMediaDominanceThreshold: string;
  setMixedMediaDominanceThreshold: (v: string) => void;
  mixedMediaVideoRouting: '' | 'library_agnostic' | 'library_dominant';
  setMixedMediaVideoRouting: (v: '' | 'library_agnostic' | 'library_dominant') => void;
  mixedMediaLogging: '' | 'full' | 'decisions_only' | 'off';
  setMixedMediaLogging: (v: '' | 'full' | 'decisions_only' | 'off') => void;
  mixedMediaCollisionHandling: '' | 'duplicate' | 'suffix' | 'skip';
  setMixedMediaCollisionHandling: (v: '' | 'duplicate' | 'suffix' | 'skip') => void;
}
