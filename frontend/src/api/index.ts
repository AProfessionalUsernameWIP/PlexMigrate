// Barrel for the per-resource API modules. Phase 3c rebuilds the same
// public surface that ``frontend/src/api.ts`` used to expose directly:
//
//   * Every type/interface/class from ``./types``.
//   * The HTTP/auth core (``http`` itself stays internal but the
//     token + handler API is re-exported).
//   * The two WebSocket clients + their singletons.
//   * The flat ``api`` const composed by spreading each per-resource
//     object so legacy ``api.listServers()`` / ``api.devConsoleItems()``
//     / etc. calls keep resolving unchanged.
//
// Consumers continue to import ``from '../api'`` because
// ``frontend/src/api.ts`` re-exports everything in this module.

// ── Types + structured error ─────────────────────────────────────────
export type {
  DashboardState,
  ContainerResult,
  CurrentItem,
  LibraryProgress,
  ActivityEntry,
  JobPayload,
  FanOutDestState,
  ServerNetworkState,
  DashboardFrame,
  UserIdentityMap,
  SettingsView,
  LibraryWalkRow,
  LibraryWalkStatus,
  PrunePreview,
  PruneResult,
  LeafCounts,
  LibraryDescriptor,
  ServerView,
  TokenCaptureSummary,
  PinMigrationSuggestion,
  ProbeFallbackOffer,
  ProbeUnsavedResult,
  ServerUser,
  ServerUsersResponse,
  PingResult,
  ServerIn,
  ServerDeleteSummary,
  ServerCascadePreview,
  Schedule,
  LogRun,
  LogFile,
  LogFileContent,
  DbStats,
  DevTestFailedTest,
  DevTestXfailTest,
  DevTestRunSummary,
  RuntimeRunSummary,
  RuntimeEntry,
  RecentRunRow,
  ServerTime,
  ExportArchive,
  Snapshot,
  LibraryMetricsRow,
  LibraryMetricsMap,
  Role,
  Permission,
  AuthStatus,
  AuthUser,
  AuthSession,
  MeResponse,
  ManagedUser,
  UserPermissionsResponse,
  ManagedUserService,
  ManagedUserKind,
  ManagedUserHiddenScope,
  ServerManagedUser,
  GlobalTombstone,
  DatabaseCardinality,
  DatabaseSensitivity,
  DatabaseTypeSummary,
  DatabaseInstance,
  DatabaseInstanceMetadata,
  DatabaseColumn,
  DatabaseIndex,
  DatabaseTable,
  DatabaseInstanceSchema,
  DatabaseCell,
  DatabaseTableRowsPage,
  CppProposedResolution,
  CppOverallVerdict,
  CppSourceRole,
  CppDestRole,
  CppResolutionAction,
  CppResolutionsStatus,
  CppUserRowCounts,
  CppDestUserOption,
  CppUserResolution,
  CppLibraryTypeNote,
  CppTombstoneNote,
  CppZeroRowSkip,
  CrossPlatformPreflightReport,
  PreflightResponse,
  CppUserResolutionDecision,
  CrossPlatformPreflightAck,
  InlineCreateUserBody,
  InlineCreateUserResponse,
  ScheduleResolutionsPatch,
  PlaylistSpec,
  PlaylistItem,
  PlaylistDetail,
  PlaylistCopyIn,
  PlaylistCopyResult,
  PlaylistCacheStatus,
  PlaylistCacheRefreshResult,
  PlaylistMgmtUser,
  PlaylistMgmtUsersResponse,
  PlaylistCopyJob,
  PlaylistCopyBatchIn,
  PlaylistCopyBatchItemResult,
  PlaylistCopyBatchResult,
  PlaylistMgmtPlaylistsResponse,
  PlaylistMgmtCacheStatusResponse,
  PlaylistMgmtBulkRefreshResponse,
  SmartFilterClause,
  SmartFilterGroup,
  SmartFilterNode,
  SmartFilterDict,
  SmartPlaylistPreview,
  SmartMigrateItem,
  SmartMigrationRecord,
  DevConsoleServer,
  DevConsoleLibrary,
  DevConsoleUser,
  DevConsolePerUserState,
  DevConsoleItem,
  DevConsoleServerDetail,
  DevConsoleItemsResponse,
  DevConsoleRelation,
  DevConsoleItemDetail,
  DevConsoleCommandResult,
  DevConsoleStagedChange,
  DevConsoleStagedResponse,
  DevConsoleSyncStatus,
  DevConsolePlaylist,
  DevConsoleCollection,
  DevConsoleEvent,
  DevConsoleGroup,
  DevConsoleGroupsResponse,
  DevConsoleSmartFilterField,
  DevConsoleSmartFieldsResponse,
  DevConsoleItemFilters,
  LibraryMappingLib,
  LibraryMappingCandidate,
  LibraryMappingRow,
  SyncType,
  ConflictPolicy,
  SyncSubscription,
  SyncWriteRow,
  SyncPlaylistSelection,
} from './types';

export { PlaylistMgmtStructuredError } from './types';

// ── Core auth/HTTP plumbing ──────────────────────────────────────────
export {
  setAccessToken,
  getAccessToken,
  decodeJwtPayload,
  onUnauthorized,
  onElevationRequired,
  ELEVATION_REQUIRED_DETAIL_MARKER,
} from './core';

// ── WebSocket helpers ────────────────────────────────────────────────
export { buildWsUrl } from './ws-base';
export { DashboardWsClient, dashboardWsClient } from './dashboard-ws';
export { DevConsoleWsClient, devConsoleWsClient } from './dev-console-ws';

// ── Per-resource REST modules ────────────────────────────────────────
import { authApi } from './auth';
import { collectionCacheApi } from './collection-cache';
import { databaseApi } from './database';
import { devConsoleApi } from './dev-console';
import { jobsApi } from './jobs';
import { libraryMappingApi } from './library-mapping';
import { libraryWalkApi } from './library-walk';
import { logsApi } from './logs';
import { managedUsersApi } from './managed-users';
import { playlistMgmtApi } from './playlist-mgmt';
import { schedulesApi } from './schedules';
import { serverMirrorApi } from './server-mirror';
import { serversApi } from './servers';
import { settingsApi } from './settings';
import { snapshotsApi } from './snapshots';
import { syncApi } from './sync';
import { systemApi } from './system';

// ── Composed flat ``api`` const ──────────────────────────────────────
// Legacy consumers call ``api.listServers()``, ``api.devConsoleItems()``,
// etc. directly off this object. Spreading each per-resource object
// keeps the surface flat and identical to what the monolithic
// ``api`` const looked like before Phase 3c.
export const api = {
  ...authApi,
  ...collectionCacheApi,
  ...databaseApi,
  ...devConsoleApi,
  ...jobsApi,
  ...libraryMappingApi,
  ...libraryWalkApi,
  ...logsApi,
  ...managedUsersApi,
  ...playlistMgmtApi,
  ...schedulesApi,
  ...serverMirrorApi,
  ...serversApi,
  ...settingsApi,
  ...snapshotsApi,
  ...syncApi,
  ...systemApi,
};
