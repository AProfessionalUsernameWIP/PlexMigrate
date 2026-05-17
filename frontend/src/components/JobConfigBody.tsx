// Shared body component for Run Job + Schedules.
//
// Renders the panel stack both pages share verbatim — WorkflowStrip,
// ModeAndServersPanel, the fieldset gate, Scope section header,
// fan-out toggle, UserFilterPanel, DirectUsersPanel, DataToMigratePanel,
// Restoration mode panel, sum-mode warning, PerRunSettingsPanel.
//
// Both pages produce a JobConfigOwner of the same shape; the body
// renders identical JSX regardless of source. Page-specific content
// is injected via three children slots:
//
//   * topSlot — rendered above WorkflowStrip (Schedules' timing panel).
//   * restoreSourceSlot — rendered between DirectUsersPanel and
//     DataToMigratePanel (Run Job's snapshot/file picker, OR
//     Schedules' input_files textarea).
//   * restorationModeExtras — rendered inside the Restoration mode
//     panel below RestoreModeSelector (Schedules' persistent
//     confirm_replace + confirm_additive_merge checkboxes; Run Job
//     passes nothing).
//
// Modals + Submit/Save buttons stay in each page's wrapper — they're
// page-specific and depend on each page's submit/save state machine.
//
// Phase E (Plan[SCHEDULES-ALIGNMENT-V2], 2026-05-16): introduced to
// dedupe the ~400 lines of panel-stack wiring that was duplicated
// across JobFormPanel and SchedulesPanel.

import type { ReactNode } from 'react';
import type { JobConfigOwner } from './JobConfigOwner';
import { WorkflowStrip } from './WorkflowStrip';
import { ModeAndServersPanel } from './ModeAndServersPanel';
import { UserFilterPanel } from './UserFilterPanel';
import { DirectUsersPanel } from './DirectUsersPanel';
import { DataToMigratePanel } from './DataToMigratePanel';
import { RestoreModeSelector } from './RestoreModeSelector';
import { PerRunSettingsPanel } from './PerRunSettingsPanel';

interface Props {
  owner: JobConfigOwner;
  // Optional: Schedules injects its timing panel here. Run Job
  // passes nothing.
  topSlot?: ReactNode;
  // Optional: each page's restore-source content. Run Job passes
  // its snapshot/file picker; Schedules passes its input_files
  // textarea. Rendered between DirectUsersPanel and
  // DataToMigratePanel.
  restoreSourceSlot?: ReactNode;
  // Optional: Schedules' persistent confirm checkboxes (Replace +
  // additive Merge) injected below RestoreModeSelector. Run Job
  // passes nothing (uses the typed-REPLACE modal at submit time).
  restorationModeExtras?: ReactNode;
}

export function JobConfigBody({
  owner,
  topSlot,
  restoreSourceSlot,
  restorationModeExtras,
}: Props) {
  const {
    idScope,
    mode,
    setMode,
    workflowMode,
    setWorkflowMode,
    rateMode,
    setRateMode,
    rateThreshold,
    setRateThreshold,
    userCreateSpecCount,
    onOpenUserCreateModal,
    servers,
    pings,
    sourceServerId,
    setSourceServerId,
    destServerIds,
    setDestServerIds,
    sourceBackend,
    destBackend,
    isCrossBackend,
    workflowServers,
    populatedBackendsCount,
    showWorkflowTabs,
    serversReady,
    includeManagedUsers,
    setIncludeManagedUsers,
    sourceUsers,
    destUsers,
    includedUsers,
    setIncludedUsers,
    userFilteredIds,
    setUserFilteredIds,
    setUserFilterCriteria,
    userFilterActive,
    usersError,
    libraries,
    librariesError,
    selectedLibs,
    libraryMetrics,
    setLibraryMetrics,
    atLeastOneType,
    restoreSource,
    selectedSnapshot,
    snapshotHasWatchHistory,
    snapshotHasRatings,
    snapshotHasPlaylists,
    snapshotHasCollections,
    restoreMode,
    setRestoreMode,
    mergeWatchStrategy,
    setMergeWatchStrategy,
    autoCaptureBeforeReplace,
    setAutoCaptureBeforeReplace,
    advancedOpen,
    setAdvancedOpen,
    perRunSubTab,
    setPerRunSubTab,
    workers,
    setWorkers,
    scrobbleWorkers,
    setScrobbleWorkers,
    strictMatch,
    setStrictMatch,
    overwritePlaylists,
    setOverwritePlaylists,
    prebuildJsonSidecar,
    setPrebuildJsonSidecar,
    verbose,
    setVerbose,
    outputDir,
    setOutputDir,
    logDir,
    setLogDir,
    remapOld,
    setRemapOld,
    remapNew,
    setRemapNew,
    skipPlaylistPrebuild,
    setSkipPlaylistPrebuild,
    fastCollectionDetection,
    setFastCollectionDetection,
    watchRatingsStrategy,
    setWatchRatingsStrategy,
    mixedMediaBehavior,
    setMixedMediaBehavior,
    mixedMediaDominanceThreshold,
    setMixedMediaDominanceThreshold,
    mixedMediaVideoRouting,
    setMixedMediaVideoRouting,
    mixedMediaLogging,
    setMixedMediaLogging,
    mixedMediaCollisionHandling,
    setMixedMediaCollisionHandling,
  } = owner;

  // Mode-gates used by several sub-panels below. The "user-section
  // gate" hides UserFilter + DirectUsers when the end user opted out
  // of managed-user capture (fan-out toggle off) or when the mode
  // doesn't support per-user filtering (e.g. restore-from-file).
  const userSectionMode =
    mode === 'direct'
    || mode === 'snapshot'
    || (mode === 'restore' && restoreSource === 'snapshot');

  const sourceServer = servers.find((s) => s.id === sourceServerId) ?? null;

  return (
    <>
      {topSlot}

      {showWorkflowTabs && (
        <WorkflowStrip
          servers={servers}
          workflowMode={workflowMode}
          onWorkflowChange={setWorkflowMode}
          populatedBackendsCount={populatedBackendsCount}
          sourceBackend={sourceBackend}
          destBackend={destBackend}
          isCrossBackend={isCrossBackend}
          rateMode={rateMode}
          onRateModeChange={setRateMode}
          rateThreshold={rateThreshold}
          onRateThresholdChange={setRateThreshold}
          userCreateSpecCount={userCreateSpecCount}
          onOpenUserCreateModal={onOpenUserCreateModal}
        />
      )}

      <ModeAndServersPanel
        mode={mode}
        onModeChange={setMode}
        sourceServerName={sourceServerId}
        onSourceServerChange={setSourceServerId}
        destServerNames={destServerIds}
        onDestServerNamesChange={setDestServerIds}
        workflowServers={workflowServers}
        pings={pings}
        serversReady={serversReady}
      />

      {/* Fieldset gate: greys out everything below until the active
          mode's source/dest selection prerequisites are met. */}
      <fieldset
        className="job-form-gate"
        disabled={!serversReady}
        style={{
          border: 'none', padding: 0, margin: 0, minWidth: 0,
          opacity: serversReady ? 1 : 0.5,
          pointerEvents: serversReady ? 'auto' : 'none',
        }}
      >
        <div className="section-header">
          <h2 style={{ marginBottom: 4 }}>Scope - what to migrate</h2>
          <span className="help" style={{ color: 'var(--text-dim)', fontSize: 12 }}>
            Pick libraries, data types, and (for direct transfer) which managed users move.
            Defaults are everything checked.
          </span>
        </div>

        {/* libraries error banner. Each page populates owner.librariesError
            as appropriate; the body just renders it. */}
        {librariesError && (
          <div className="panel">
            <div className="banner error">Could not list libraries: {librariesError}.</div>
          </div>
        )}

        {/* Per-user fan-out toggle. Hidden in modes where it doesn't
            apply (restore-from-file). */}
        {userSectionMode && (
          <div className="panel" style={{ padding: '10px 14px' }}>
            <label style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 13, cursor: 'pointer' }}>
              <input
                type="checkbox"
                checked={includeManagedUsers}
                onChange={(e) => setIncludeManagedUsers(e.target.checked)}
              />
              <span><strong>Include managed users in capture</strong></span>
              <span style={{ fontSize: 11, color: 'var(--text-dim)', marginLeft: 8 }}>
                Capture each managed user's per-user state in addition to the owner's.
                Unchecking captures owner-only.
              </span>
            </label>
          </div>
        )}

        {/* UserFilterPanel (when fan-out is on AND user lists have
            loaded) OR owner-only hint (when fan-out is off).
            Restore-from-snapshot mode: the credential filters apply
            to the DESTINATION server(s) — the engine has to
            authenticate as each user on every destination to write
            their per-user data — so we resolve dest names to ids and
            pass those as credentialServerIds. Snapshot / direct mode
            falls through to the default (= [sourceServerId]). */}
        {includeManagedUsers && userSectionMode && sourceUsers !== null && destUsers !== null && (
          <div className="panel">
            <UserFilterPanel
              sourceUsers={sourceUsers}
              sourceServerId={sourceServerId || null}
              mode={mode === 'restore' ? 'restore' : 'capture'}
              credentialServerIds={
                mode === 'restore'
                  ? Array.from(destServerIds)
                      .map((name) => servers.find((s) => s.name === name)?.id)
                      .filter((x): x is string => !!x)
                  : undefined
              }
              disabled={mode === 'restore' ? destServerIds.size === 0 : !sourceServerId}
              onFilteredChange={(ids, criteria) => {
                setUserFilteredIds(ids);
                setUserFilterCriteria(criteria);
              }}
            />
          </div>
        )}
        {!includeManagedUsers && userSectionMode && (
          <div className="panel" style={{ padding: '8px 14px', fontSize: 12, color: 'var(--text-dim)' }}>
            Managed users excluded by the "Include managed users in capture" checkbox above.
            Owner-only capture; user filter + picker hidden.
          </div>
        )}

        {/* DirectUsersPanel for the user selection grid. */}
        {includeManagedUsers && userSectionMode && sourceUsers !== null && destUsers !== null && (
          <DirectUsersPanel
            mode={mode}
            sourceUsers={userFilterActive
              ? sourceUsers.filter((u) => userFilteredIds.has(u.plex_id))
              : sourceUsers}
            destUsers={destUsers}
            included={includedUsers}
            onToggle={(plex_id) => {
              const next = new Set(includedUsers);
              if (next.has(plex_id)) next.delete(plex_id);
              else next.add(plex_id);
              setIncludedUsers(next);
            }}
            onAll={() => {
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

        {/* Restore source slot (page-specific): Run Job's snapshot/file
            picker, OR Schedules' input_files textarea. Rendered between
            user pickers and DataToMigratePanel. */}
        {restoreSourceSlot}

        <DataToMigratePanel
          mode={mode}
          libraries={libraries}
          selectedLibs={selectedLibs}
          restoreSource={restoreSource}
          selectedSnapshot={selectedSnapshot}
          snapshotHasWatchHistory={snapshotHasWatchHistory}
          snapshotHasRatings={snapshotHasRatings}
          snapshotHasPlaylists={snapshotHasPlaylists}
          snapshotHasCollections={snapshotHasCollections}
          libraryMetrics={libraryMetrics}
          onLibraryMetricsChange={setLibraryMetrics}
          atLeastOneType={atLeastOneType}
        />

        {/* Restoration mode panel + page-specific extras (Schedules'
            persistent confirm checkboxes). */}
        {(mode === 'restore' || mode === 'direct') && (
          <div className="panel">
            <h2 style={{ marginTop: 0 }}>Restoration mode</h2>
            <RestoreModeSelector
              mode={restoreMode}
              autoCaptureBeforeReplace={autoCaptureBeforeReplace}
              mergeWatchStrategy={mergeWatchStrategy}
              onMergeWatchStrategyChange={setMergeWatchStrategy}
              onModeChange={setRestoreMode}
              onAutoCaptureChange={setAutoCaptureBeforeReplace}
              idPrefix={`${idScope}-restore`}
            />
            {restorationModeExtras}
          </div>
        )}

        {/* Sum-mode non-idempotency warning, rendered as a sibling
            of the Restoration mode panel. Same trigger logic both
            pages used. Schedules supplements with a stronger
            additive-merge warning inside restorationModeExtras. */}
        {(mode === 'restore' || mode === 'direct')
          && restoreMode === 'merge'
          && mergeWatchStrategy === 'sum' && (
          <div
            className="banner"
            style={{
              background: 'rgba(245, 166, 35, 0.10)',
              border: '1px solid var(--warn, #f5a623)',
              color: 'var(--text, inherit)',
              marginTop: 8,
              padding: '10px 12px',
              borderRadius: 6,
              fontSize: 12,
            }}
          >
            <strong style={{ color: 'var(--warn, #f5a623)' }}>
              Combine totals is not idempotent.
            </strong>{' '}
            Each run adds the snapshot's stored view counts on top of the
            destination's current counts. Re-running the same snapshot doubles
            the contribution. Use only when the snapshot represents activity
            that should accumulate alongside the destination's own plays.
          </div>
        )}

        <PerRunSettingsPanel
          mode={mode}
          sourceServer={sourceServer}
          idScope={idScope}
          open={advancedOpen}
          onOpenChange={setAdvancedOpen}
          subTab={perRunSubTab}
          onSubTabChange={setPerRunSubTab}
          workers={workers}
          onWorkersChange={setWorkers}
          scrobbleWorkers={scrobbleWorkers}
          onScrobbleWorkersChange={setScrobbleWorkers}
          strictMatch={strictMatch}
          onStrictMatchChange={setStrictMatch}
          overwritePlaylists={overwritePlaylists}
          onOverwritePlaylistsChange={setOverwritePlaylists}
          prebuildJsonSidecar={prebuildJsonSidecar}
          onPrebuildJsonSidecarChange={setPrebuildJsonSidecar}
          verbose={verbose}
          onVerboseChange={setVerbose}
          outputDir={outputDir}
          onOutputDirChange={setOutputDir}
          logDir={logDir}
          onLogDirChange={setLogDir}
          remapOld={remapOld}
          onRemapOldChange={setRemapOld}
          remapNew={remapNew}
          onRemapNewChange={setRemapNew}
          skipPlaylistPrebuild={skipPlaylistPrebuild}
          onSkipPlaylistPrebuildChange={setSkipPlaylistPrebuild}
          fastCollectionDetection={fastCollectionDetection}
          onFastCollectionDetectionChange={setFastCollectionDetection}
          watchRatingsStrategy={watchRatingsStrategy}
          onWatchRatingsStrategyChange={setWatchRatingsStrategy}
          mixedMediaBehavior={mixedMediaBehavior}
          onMixedMediaBehaviorChange={setMixedMediaBehavior}
          mixedMediaDominanceThreshold={mixedMediaDominanceThreshold}
          onMixedMediaDominanceThresholdChange={setMixedMediaDominanceThreshold}
          mixedMediaVideoRouting={mixedMediaVideoRouting}
          onMixedMediaVideoRoutingChange={setMixedMediaVideoRouting}
          mixedMediaLogging={mixedMediaLogging}
          onMixedMediaLoggingChange={setMixedMediaLogging}
          mixedMediaCollisionHandling={mixedMediaCollisionHandling}
          onMixedMediaCollisionHandlingChange={setMixedMediaCollisionHandling}
        />
      </fieldset>
    </>
  );
}
