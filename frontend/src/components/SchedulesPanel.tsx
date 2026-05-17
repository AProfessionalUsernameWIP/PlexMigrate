// Scheduled snapshots - list, create, edit, delete.
//
// Each schedule fires an snapshot run on a recurring trigger. The
// scheduler lives server-side (see server/schedules.py); this panel
// is just a thin CRUD form over /api/schedules. The list at the top
// shows the next firing time so the user can confirm their schedule
// is wired correctly.

import { useEffect, useRef, useState } from 'react';
import { api, LibraryDescriptor, Schedule, ServerTime, ServerUser, ServerView } from '../api';
import { usePermission } from '../hooks/usePermission';
import { serverSupportsFastCollections } from '../utils/plexVersion';
import { InfoTip } from './InfoTip';
import { RestoreModeSelector } from './RestoreModeSelector';
import { UserFilterPanel, EMPTY_FILTER } from './UserFilterPanel';
import type { UserFilterCriteria } from './UserFilterPanel';
import { DirectUsersPanel } from './DirectUsersPanel';
import { PerRunSettingsPanel } from './PerRunSettingsPanel';
import type { WatchRatingsStrategy } from './PerRunSettingsPanel';
import { PinPreflightModal } from './PinPreflightModal';
import { SchedulePreflightStep } from './SchedulePreflightStep';
import { ScheduleResolutionEditor } from './ScheduleResolutionEditor';
import type { PreflightResponse, CrossPlatformPreflightAck } from '../api';
import { JobConfigBody } from './JobConfigBody';
import type { JobConfigOwner } from './JobConfigOwner';
import { EMPTY_FILTER as _EMPTY_FILTER_FOR_TYPES } from './UserFilterPanel';
void _EMPTY_FILTER_FOR_TYPES;
import { LibraryMetricsMatrix } from './LibraryMetricsMatrix';
import type { LibraryMetricsMap } from './LibraryMetricsMatrix';
import { WorkflowStrip } from './WorkflowStrip';
import type { WorkflowMode, BackendOrCross, RateMode } from './WorkflowStrip';
import { ModeAndServersPanel } from './ModeAndServersPanel';
import { DataToMigratePanel } from './DataToMigratePanel';
import { type BackendType, backendCounts, serversForBackend } from './BackendTabStrip';
import type { PingResult } from '../api';

export function SchedulesPanel() {
  // PR-A4 - read/write gate. ``schedules.view`` is implied to reach
  // this panel at all (App.tsx hides the sub-tab without it).
  // ``schedules.edit`` is what gates the destructive controls.
  const canEditSchedules = usePermission('schedules.edit');
  const [items, setItems] = useState<Schedule[]>([]);
  const [servers, setServers] = useState<ServerView[]>([]);
  const [libraries, setLibraries] = useState<LibraryDescriptor[]>([]);
  const [editing, setEditing] = useState<Schedule | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [serverTime, setServerTime] = useState<ServerTime | null>(null);

  const refresh = async () => {
    try {
      const [s, srv, st] = await Promise.all([
        api.listSchedules(),
        api.listServers().catch(() => [] as ServerView[]),
        api.getServerTime().catch(() => null as ServerTime | null),
      ]);
      setItems(s);
      setServers(srv);
      setServerTime(st);
    } catch (e) {
      setError(String(e));
    }
  };
  useEffect(() => { refresh(); }, []);

  // Re-fetch the server clock every 30 s so the displayed wallclock
  // doesn't drift while the panel is open.
  useEffect(() => {
    const tick = window.setInterval(() => {
      api.getServerTime().then(setServerTime).catch(() => { /* keep prior */ });
    }, 30_000);
    return () => window.clearInterval(tick);
  }, []);

  // A.10 (2026-05-16): library fetch fix - refresh per selected
  // server, handle all schedule modes, and cancel stale responses.
  // Previous behaviour was gated on editing.source_server_name only,
  // so restore-mode schedules never loaded a library list (the source
  // is input_files, not a server). The dep array was also too broad
  // (every editing-object change re-fired); narrow it to just the
  // fields that actually drive the fetch.
  //
  // Per-mode rule:
  //   snapshot / direct  -> use the source server's library list
  //   restore            -> use the FIRST destination server's libraries
  //                         (no source server in restore; the matrix
  //                         renders what the destination has)
  const _editingSource = editing?.source_server_name;
  const _editingDests = editing?.dest_server_names;
  const _editingMode = editing?.mode ?? 'snapshot';
  useEffect(() => {
    if (!editing) {
      setLibraries([]);
      return;
    }
    let targetServerName: string | undefined;
    if (_editingMode === 'restore') {
      targetServerName = (_editingDests || [])[0];
    } else {
      targetServerName = _editingSource;
    }
    if (!targetServerName) {
      setLibraries([]);
      return;
    }
    const srv = servers.find((s) => s.name === targetServerName);
    if (!srv) {
      setLibraries([]);
      return;
    }
    let cancelled = false;
    api.listServerLibraries(srv.id)
      .then((libs) => { if (!cancelled) setLibraries(libs); })
      .catch(() => { if (!cancelled) setLibraries([]); });
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [_editingMode, _editingSource, _editingDests, servers]);

  const startNew = () => setEditing({
    name: '',
    source_server_name: servers[0]?.name ?? '',
    libraries: [],
    output_dir: '',
    frequency: 'daily',
    hour: 3,
    minute: 0,
    day_of_week: 0,
    enabled: true,
    // PR-3 / Phase D - four-flag data-type filter on schedules.
    // Defaults match the Run-Job form: every data type migrated.
    include_watch_history: true,
    include_ratings: true,
    include_playlists: true,
    include_collections: true,
    prebuild_json_sidecar: false,
    // Task 2: default to the legacy snapshot-mode behaviour so the
    // form opens like it always did. End user switches mode via the
    // selector below.
    mode: 'snapshot',
    dest_server_names: null,
    input_files: null,
    restore_mode: null,
    auto_capture_before_replace: null,
    confirm_replace: false,
    merge_watch_strategy: null,
    confirm_additive_merge: false,
    remap_old: null,
    remap_new: null,
  });

  const cancelEdit = () => setEditing(null);

  const saveEdit = async () => {
    if (!editing) return;
    setError(null);
    try {
      if (editing.id) {
        await api.updateSchedule(editing.id, editing);
      } else {
        await api.createSchedule(editing);
      }
      setEditing(null);
      await refresh();
    } catch (e) {
      setError(String(e));
    }
  };

  const removeOne = async (id?: string) => {
    if (!id) return;
    if (!confirm('Delete this schedule?')) return;
    try {
      await api.deleteSchedule(id);
      await refresh();
    } catch (e) {
      setError(String(e));
    }
  };

  // Phase C: schedule whose resolutions_status === 'needs_review'
  // surfaces a top-of-page banner pointing at the resolution editor.
  const needsReviewItems = items.filter((s) => s.resolutions_status === 'needs_review');
  // Tracks which schedule's resolution editor is currently open.
  const [resolutionEditing, setResolutionEditing] = useState<Schedule | null>(null);

  return (
    <>
      {error && <div className="banner error">{error}</div>}
      {needsReviewItems.length > 0 && (
        <div
          className="banner"
          style={{
            background: 'rgba(239, 68, 68, 0.10)',
            border: '1px solid var(--bad, #ef4444)',
            color: 'var(--bad, #ef4444)',
            padding: '8px 12px',
            borderRadius: 6,
            marginBottom: 12,
            fontSize: 12,
          }}
        >
          <strong>
            {needsReviewItems.length} scheduled run
            {needsReviewItems.length === 1 ? ' has' : 's have'} a user resolution
            that no longer matches the destination.
          </strong>{' '}
          Review before the next fire. Click the red badge on the row{needsReviewItems.length === 1 ? '' : 's'} below.
        </div>
      )}
      <div className="panel">
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12 }}>
          <h2 style={{ margin: 0 }}>Scheduled Snapshots</h2>
          {canEditSchedules && (
            <button className="primary" onClick={startNew}>+ New Schedule</button>
          )}
        </div>

        {items.length === 0 ? (
          <div className="empty">No schedules yet. Click <strong>+ New Schedule</strong> to create one.</div>
        ) : (
          <table className="list">
            <thead>
              <tr>
                <th>Name</th>
                <th>Source server</th>
                <th>Libraries</th>
                <th>Frequency</th>
                <th>Time</th>
                <th>Output</th>
                <th>Next run</th>
                <th>Enabled</th>
                <th>Resolutions</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {items.map((s) => (
                <tr key={s.id}>
                  <td>{s.name}</td>
                  <td>{s.source_server_name || <span style={{ color: 'var(--bad)' }}>(not set)</span>}</td>
                  <td>{s.libraries.length === 0 ? <em>(all)</em> : s.libraries.join(', ')}</td>
                  <td>{s.frequency}{s.frequency === 'weekly' && ` (${DAYS[s.day_of_week]})`}</td>
                  <td className="mono">{pad2(s.hour)}:{pad2(s.minute)}</td>
                  <td className="mono">{s.output_dir || '(default)'}</td>
                  <td className="mono">{s.next_run_at ? formatTs(s.next_run_at) : '-'}</td>
                  <td>{s.enabled ? '✓' : '-'}</td>
                  <td>
                    {s.cross_platform_resolutions && Object.keys(s.cross_platform_resolutions).length > 0 ? (
                      <button
                        type="button"
                        onClick={() => setResolutionEditing(s)}
                        className={s.resolutions_status === 'needs_review' ? 'danger' : ''}
                        title={
                          s.resolutions_status === 'needs_review'
                            ? 'A stored decision no longer matches the destination roster. Click to review.'
                            : s.resolutions_status === 'auto_fallback'
                              ? 'This schedule relies on auto-fallback (single-admin or role-flip) decisions. Click to review.'
                              : 'Edit stored cross-platform resolutions.'
                        }
                        style={{ fontSize: 11 }}
                      >
                        {s.resolutions_status === 'needs_review' ? 'Needs review'
                          : s.resolutions_status === 'auto_fallback' ? 'Auto-fallback'
                          : 'Edit'}
                      </button>
                    ) : (
                      <span style={{ color: 'var(--text-dim)', fontSize: 11 }}>-</span>
                    )}
                  </td>
                  <td>
                    <div className="row-buttons">
                      <button onClick={() => setEditing({ ...s })}>
                        {canEditSchedules ? 'Edit' : 'View'}
                      </button>
                      {canEditSchedules && (
                        <button className="danger" onClick={() => removeOne(s.id)}>Delete</button>
                      )}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {/* Phase C: resolution editor opens when the end user clicks
          the resolutions badge on a saved schedule row. Re-runs the
          schedule preflight, opens the modal with stored decisions,
          PATCHes /api/schedules/{id}/resolutions on Continue. */}
      {resolutionEditing && (
        <ScheduleResolutionEditor
          open={!!resolutionEditing}
          schedule={resolutionEditing}
          onClose={() => setResolutionEditing(null)}
          onSaved={async () => { await refresh(); }}
        />
      )}

      {editing && (
        <ScheduleEditor
          schedule={editing}
          servers={servers}
          libraries={libraries}
          serverTime={serverTime}
          onChange={setEditing}
          onSave={saveEdit}
          onCancel={cancelEdit}
        />
      )}
    </>
  );
}

// ── Editor form ──────────────────────────────────────────────────────────────

function ScheduleEditor(props: {
  schedule: Schedule;
  servers: ServerView[];
  libraries: LibraryDescriptor[];
  serverTime: ServerTime | null;
  onChange: (s: Schedule) => void;
  onSave: () => void;
  onCancel: () => void;
}) {
  const { schedule, servers, libraries, serverTime, onChange, onSave, onCancel } = props;
  const set = <K extends keyof Schedule>(k: K, v: Schedule[K]) => onChange({ ...schedule, [k]: v });

  // Per-Run Settings collapse + sub-tab state. Workspace-only; never
  // persisted to the schedule row.
  const [perRunOpen, setPerRunOpen] = useState(false);
  const [perRunSubTab, setPerRunSubTab] = useState<'general' | 'advanced'>('general');

  // A.1 (Plan[SCHEDULES-ALIGNMENT-V2]): Schedule timing panel is a
  // collapsible "reveal as you click" panel that sits at the very
  // top of the editor and absorbs the editor h2 + Name field plus the
  // schedule-only scheduling controls (frequency, hour/minute, day-
  // of-week, enabled, output dir, server-time banner). Default-open
  // per the end user decision (both new and edit schedules).
  const [scheduleTimingOpen, setScheduleTimingOpen] = useState(true);

  // Phase B (2026-05-16): PinPreflightModal save-time intercept.
  // Schedules can't open the modal at fire time (no end user
  // present), so the end user acknowledges cross-server PIN risk
  // at SAVE time instead. The ack persists on the schedule row
  // (schedule.pin_preflight_ack, shipped in step 2) and clears
  // automatically when source_server_name changes (the ack is
  // server-specific).
  const [pinPreflightOpen, setPinPreflightOpen] = useState(false);
  const [pinPreflightAtRisk, setPinPreflightAtRisk] = useState<string[]>([]);
  // When set, save flow has been intercepted by the modal; clicking
  // Continue in the modal flips pin_preflight_ack and re-fires save.
  const [savePending, setSavePending] = useState(false);

  // Phase C: cross-platform preflight step state. Modal opens at
  // save time when the schedule is cross-backend AND the backend's
  // preflight returns aggregate_verdict !== 'ok'. On Continue the
  // end user's per-destination decisions persist into
  // schedule.cross_platform_resolutions before the schedule create /
  // edit is POSTed.
  const [cppOpen, setCppOpen] = useState(false);
  const [cppResponse, setCppResponse] = useState<PreflightResponse | null>(null);

  // Clear the ack when source_server_name actually CHANGES. A
  // ref tracks the previous value so we don't clear on first mount
  // or on unrelated re-renders.
  const prevSourceServerRef = useRef<string | undefined>(schedule.source_server_name);
  useEffect(() => {
    if (prevSourceServerRef.current !== schedule.source_server_name) {
      prevSourceServerRef.current = schedule.source_server_name;
      if (schedule.pin_preflight_ack) {
        set('pin_preflight_ack', false);
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [schedule.source_server_name]);

  // A.10 (2026-05-16): user fetch fix - refresh per selected
  // server, handle all schedule modes. Restore mode has no source
  // server (source = input_files), so the picker has to load users
  // from the FIRST destination server instead. snapshot / direct
  // use the source server as before.
  //
  // The fetch already had cancellation; this rewrite adds the mode
  // branch and narrows the dep array so changing other schedule
  // fields doesn't re-fire the fetch.
  const [sourceUsers, setSourceUsers] = useState<ServerUser[] | null>(null);
  const _userFetchTarget = (() => {
    const mode = schedule.mode ?? 'snapshot';
    if (mode === 'restore') {
      const firstDest = (schedule.dest_server_names || [])[0];
      return firstDest;
    }
    return schedule.source_server_name;
  })();
  useEffect(() => {
    if (!_userFetchTarget) {
      setSourceUsers(null);
      return;
    }
    const src = servers.find((s) => s.name === _userFetchTarget);
    if (!src) {
      setSourceUsers(null);
      return;
    }
    let cancelled = false;
    api.listServerUsers(src.id)
      .then((r) => {
        if (cancelled) return;
        setSourceUsers(r.users || []);
      })
      .catch(() => {
        if (cancelled) return;
        setSourceUsers([]);
      });
    return () => { cancelled = true; };
  }, [_userFetchTarget, servers]);

  // Set of selected user plex_ids. Default to "all selected" once the
  // user list arrives so the end user only ever needs to UNcheck to
  // exclude. ``null`` user_filter on the schedule row means "all" too.
  const selectedUsers = new Set<string>(
    Array.isArray(schedule.user_filter) ? schedule.user_filter : (sourceUsers || []).map((u) => u.plex_id),
  );

  // Phase A: user-filter criteria state. The UserFilterPanel pushes
  // a narrowed plex_id set up here whenever the end user toggles a
  // criterion. The selection grid below only renders users in the
  // filtered set, and the Save button's gate fails when the filter
  // produces zero matches.
  const [userFilteredIds, setUserFilteredIds] = useState<Set<string>>(new Set());
  const [userFilterCriteria, setUserFilterCriteria] = useState<UserFilterCriteria>(EMPTY_FILTER);
  const userFilterActive =
    (Object.values(userFilterCriteria) as boolean[]).some(Boolean);
  const toggleUser = (plex_id: string) => {
    const next = new Set(selectedUsers);
    if (next.has(plex_id)) next.delete(plex_id);
    else next.add(plex_id);
    set('user_filter', Array.from(next));
  };
  const selectAllUsers = () => {
    set('user_filter', (sourceUsers || []).map((u) => u.plex_id));
  };
  const clearAllUsers = () => {
    set('user_filter', []);
  };
  const resetUsersToAll = () => {
    // null means "all" - clears any saved subset.
    set('user_filter', null);
  };

  // v0.14 - Fast Collection Detection version gating. When the
  // schedule's source server is at Plex ≥ 1.32, default the toggle
  // ON; below 1.32 (or unknown version) force it OFF and disable
  // the input. Re-runs every time the source server changes so
  // pointing a schedule at a different server resets the toggle.
  const sourceServer = servers.find((s) => s.name === schedule.source_server_name);
  const fastSupportVersion = sourceServer?.plex_version ?? '';
  const fastSupported = serverSupportsFastCollections(fastSupportVersion);
  useEffect(() => {
    if (!schedule.source_server_name) return;
    // Don't clobber a saved value when the end user is editing an
    // existing schedule (schedule.id present) and the value the row
    // already carries matches what we'd set. The auto-default only
    // applies on fresh server picks where the field is unset.
    if (schedule.fast_collection_detection === undefined) {
      set('fast_collection_detection', fastSupported);
    } else if (!fastSupported && schedule.fast_collection_detection) {
      // Unsupported server with stale on-flag → force off.
      set('fast_collection_detection', false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [schedule.source_server_name, fastSupported]);

  const toggleLib = (name: string) => {
    const cur = new Set(schedule.libraries);
    if (cur.has(name)) cur.delete(name); else cur.add(name);
    set('libraries', Array.from(cur));
  };

  // Auto-sync schedule.libraries from library_metrics. The Libraries
  // checkbox panel has been removed (2026-05-16); the per-library
  // matrix in DataToMigratePanel is the only library selector now.
  // A library is "selected" if (a) it has no entry in library_metrics
  // (default all-on) OR (b) it has an entry with at least one true
  // flag. End users exclude a library via the matrix row's "none"
  // button. eslint-disable on the deps array because we intentionally
  // don't depend on schedule.libraries to avoid loops.
  useEffect(() => {
    if (libraries.length === 0) return;
    const next: string[] = [];
    const m = schedule.library_metrics as LibraryMetricsMap | null | undefined;
    for (const lib of libraries) {
      const row = m?.[lib.name];
      if (!row) {
        next.push(lib.name);
        continue;
      }
      if (row.watch_history || row.ratings || row.playlists || row.collections) {
        next.push(lib.name);
      }
    }
    // Skip the write when the derived list matches the current state
    // so we don't churn the schedule object on every render.
    const cur = schedule.libraries || [];
    if (cur.length === next.length && cur.every((n) => next.includes(n))) return;
    set('libraries', next);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [libraries, schedule.library_metrics]);

  // Workflow strip state (mirror of Run Job). Local to the editor;
  // not persisted on the schedule row today. Cross-backend D-RATE /
  // D-OWNER controls render in the WorkflowStrip's sub-card but are
  // UI-only on schedules until backend support lands.
  const [workflowMode, setWorkflowMode] = useState<WorkflowMode>('plex');
  // Persisted on the schedule row today via rate_mode / rate_threshold
  // (the cross-backend D-RATE controls). Initialize from the row when
  // re-editing an existing schedule.
  const [rateMode, setRateMode] = useState<RateMode>(
    (schedule.rate_mode as RateMode | null | undefined) ?? 'default',
  );
  const [rateThreshold, setRateThreshold] = useState<string>(
    schedule.rate_threshold !== null && schedule.rate_threshold !== undefined
      ? String(schedule.rate_threshold)
      : '5.0',
  );

  // Auto-pick the workflow tab from the schedule's source server.
  useEffect(() => {
    if (!schedule.source_server_name) return;
    const srv = servers.find((s) => s.name === schedule.source_server_name);
    if (!srv) return;
    const t = (srv as unknown as { service_type?: string }).service_type;
    if (t === 'jellyfin' || t === 'emby' || t === 'plex') {
      setWorkflowMode(t);
    }
  }, [schedule.source_server_name, servers]);

  const populatedBackends = (['plex', 'jellyfin', 'emby'] as BackendType[])
    .filter((b) => backendCounts(servers)[b] > 0);
  const showWorkflowTabs = populatedBackends.length >= 2;
  // 2026-05-16 Set Backend simplification: backends derived from
  // actual picked servers, not from chip state.
  const sourceBackend: BackendOrCross = (() => {
    if (!schedule.source_server_name) {
      return workflowMode === 'cross' ? 'plex' : workflowMode;
    }
    const srv = servers.find((s) => s.name === schedule.source_server_name);
    const t = (srv as unknown as { service_type?: string } | undefined)?.service_type;
    return (t === 'jellyfin' || t === 'emby') ? t : 'plex';
  })();
  const destBackend: BackendOrCross = (() => {
    const names = schedule.dest_server_names || [];
    if (names.length === 0) return sourceBackend;
    const srv = servers.find((s) => s.name === names[0]);
    const t = (srv as unknown as { service_type?: string } | undefined)?.service_type;
    return (t === 'jellyfin' || t === 'emby') ? t : 'plex';
  })();
  const isCrossBackend = (() => {
    if (!schedule.source_server_name) return false;
    const names = schedule.dest_server_names || [];
    if (names.length === 0) return false;
    const srcSrv = servers.find((s) => s.name === schedule.source_server_name);
    const srcType = (srcSrv as unknown as { service_type?: string } | undefined)?.service_type || 'plex';
    for (const n of names) {
      const dst = servers.find((s) => s.name === n);
      const dstType = (dst as unknown as { service_type?: string } | undefined)?.service_type || 'plex';
      if (dstType !== srcType) return true;
    }
    return false;
  })();
  const workflowServers = workflowMode === 'cross'
    ? servers
    : serversForBackend(servers, workflowMode);

  // Schedule state keys servers by name; ModeAndServersPanel + ServerPicker
  // key by id. Translate at the boundary.
  const _idToName = (id: string): string => servers.find((s) => s.id === id)?.name || '';
  const _nameToId = (name: string): string => servers.find((s) => s.name === name)?.id || '';
  const sourceServerId = schedule.source_server_name ? _nameToId(schedule.source_server_name) : '';
  const destServerIds = new Set(
    (schedule.dest_server_names || []).map(_nameToId).filter(Boolean),
  );

  // Pings aren't fetched on Schedules; ServerPicker falls back to
  // last_status / last_response_ms from the registry row.
  const emptyPings: Record<string, PingResult> = {};

  // Same "selectors satisfied" gate as Run Job. Drives the banner
  // inside ModeAndServersPanel; schedules' own save validation is
  // separate.
  const scheduleMode = (schedule.mode ?? 'snapshot');
  const scheduleServersReady =
    scheduleMode === 'snapshot' ? !!schedule.source_server_name
    : scheduleMode === 'restore' ? (schedule.dest_server_names?.length ?? 0) > 0
    : !!schedule.source_server_name && (schedule.dest_server_names?.length ?? 0) > 0;

  // Phase E (2026-05-16): build JobConfigOwner from the schedule
  // object + local state. Schedule fields are read directly; writes
  // go through set('field', value). JobConfigBody renders the shared
  // panel stack from this owner.
  const _atLeastOneType = (() => {
    const lm = schedule.library_metrics;
    if (!lm) return true;
    const libs = Object.keys(lm);
    if (libs.length === 0) return true;
    return libs.some((k) => {
      const r = lm[k];
      return r && (r.watch_history || r.ratings || r.playlists || r.collections);
    });
  })();
  const _librariesError =
    schedule.source_server_name && libraries.length === 0
      ? 'No libraries reachable. Pick a different source server above, or set Plex URL/token under Settings.'
      : null;

  const owner: JobConfigOwner = {
    idScope: `sched-${schedule.id || 'new'}`,
    mode: scheduleMode as Schedule['mode'] extends infer M ? M extends string ? Schedule['mode'] : never : never,
    setMode: (m) => set('mode', m),
    workflowMode, setWorkflowMode,
    rateMode, setRateMode,
    rateThreshold, setRateThreshold,
    userCreateSpecCount: 0,
    onOpenUserCreateModal: () => { /* Subsumed by CrossPlatformPreflightModal in Phase C */ },
    servers,
    pings: emptyPings,
    sourceServerId,
    setSourceServerId: (id) => {
      // 2026-05-16 (developer Emby fix): set both id-keyed and
      // name-keyed fields. Backend prefers source_server_id; name
      // stays for back-compat with any code paths still routing
      // by name.
      onChange({
        ...schedule,
        source_server_id: id || null,
        source_server_name: _idToName(id),
      });
    },
    destServerIds,
    setDestServerIds: (ids) => {
      const names: string[] = [];
      const idList: string[] = [];
      for (const id of ids) {
        const n = _idToName(id);
        if (n) {
          names.push(n);
          idList.push(id);
        }
      }
      onChange({
        ...schedule,
        dest_server_ids: idList.length > 0 ? idList : null,
        dest_server_names: names.length > 0 ? names : null,
      });
    },
    sourceBackend, destBackend, isCrossBackend,
    workflowServers,
    populatedBackendsCount: populatedBackends.length,
    showWorkflowTabs,
    serversReady: scheduleServersReady,
    includeManagedUsers: schedule.include_managed_users ?? true,
    setIncludeManagedUsers: (v) => set('include_managed_users', v),
    sourceUsers,
    destUsers: sourceUsers, // Schedules don't have a destination user list at edit time
    includedUsers: selectedUsers,
    setIncludedUsers: (s) => set('user_filter', Array.from(s)),
    userFilteredIds, setUserFilteredIds,
    userFilterCriteria, setUserFilterCriteria,
    userFilterActive,
    usersError: null,
    libraries,
    librariesError: _librariesError,
    selectedLibs: new Set(schedule.libraries),
    libraryMetrics: (schedule.library_metrics as LibraryMetricsMap | null | undefined) ?? null,
    setLibraryMetrics: (next) => set('library_metrics', next),
    atLeastOneType: _atLeastOneType,
    // Restore-from-snapshot: Schedules don't currently have a snapshot
    // picker (A.9 deferred); force restoreSource='snapshot' and pass
    // null selectedSnapshot. DataToMigratePanel falls back to
    // libraries-based rendering.
    restoreSource: 'snapshot',
    selectedSnapshot: null,
    snapshotHasWatchHistory: true,
    snapshotHasRatings: true,
    snapshotHasPlaylists: true,
    snapshotHasCollections: true,
    // Restoration mode — read from schedule with defaults; setters
    // batch updates to avoid the state-batching bug fixed in step 1.
    restoreMode: (schedule.restore_mode ?? 'merge') as 'merge' | 'replace',
    setRestoreMode: (m) => {
      const next: Schedule = { ...schedule, restore_mode: m };
      if (m !== 'replace') next.confirm_replace = false;
      onChange(next);
    },
    mergeWatchStrategy: (schedule.merge_watch_strategy ?? 'higher') as 'higher' | 'sum',
    setMergeWatchStrategy: (s) => {
      const next: Schedule = { ...schedule, merge_watch_strategy: s };
      if (s !== 'sum') next.confirm_additive_merge = false;
      onChange(next);
    },
    autoCaptureBeforeReplace: schedule.auto_capture_before_replace ?? true,
    setAutoCaptureBeforeReplace: (v) => set('auto_capture_before_replace', v),
    advancedOpen: perRunOpen,
    setAdvancedOpen: setPerRunOpen,
    perRunSubTab, setPerRunSubTab,
    workers: schedule.workers !== null && schedule.workers !== undefined ? String(schedule.workers) : '',
    setWorkers: (v) => set('workers', v === '' ? null : Number(v)),
    scrobbleWorkers: schedule.scrobble_workers !== null && schedule.scrobble_workers !== undefined ? String(schedule.scrobble_workers) : '',
    setScrobbleWorkers: (v) => set('scrobble_workers', v === '' ? null : Number(v)),
    strictMatch: schedule.strict_match ?? true,
    setStrictMatch: (v) => set('strict_match', v),
    overwritePlaylists: schedule.overwrite_playlists ?? false,
    setOverwritePlaylists: (v) => set('overwrite_playlists', v),
    prebuildJsonSidecar: schedule.prebuild_json_sidecar ?? false,
    setPrebuildJsonSidecar: (v) => set('prebuild_json_sidecar', v),
    verbose: schedule.verbose ?? false,
    setVerbose: (v) => set('verbose', v),
    outputDir: schedule.output_dir ?? '',
    setOutputDir: (v) => set('output_dir', v || null),
    logDir: schedule.log_dir ?? '',
    setLogDir: (v) => set('log_dir', v || null),
    remapOld: schedule.remap_old ?? '',
    setRemapOld: (v) => set('remap_old', v || null),
    remapNew: schedule.remap_new ?? '',
    setRemapNew: (v) => set('remap_new', v || null),
    skipPlaylistPrebuild: schedule.skip_playlist_prebuild ?? false,
    setSkipPlaylistPrebuild: (v) => set('skip_playlist_prebuild', v),
    fastCollectionDetection: schedule.fast_collection_detection ?? false,
    setFastCollectionDetection: (v) => set('fast_collection_detection', v),
    watchRatingsStrategy: (schedule.watch_ratings_filter_strategy ?? '') as never,
    setWatchRatingsStrategy: (v) => set('watch_ratings_filter_strategy', v as Schedule['watch_ratings_filter_strategy']),
    // Mixed-media playlist per-run overrides. '' sentinel ↔ null on
    // the schedule row; the dominance threshold is numeric on the row
    // but a string in the input.
    mixedMediaBehavior: (schedule.mixed_media_behavior ?? '') as '' | 'skip' | 'dominant' | 'split',
    setMixedMediaBehavior: (v) =>
      set('mixed_media_behavior', v === '' ? null : v as Schedule['mixed_media_behavior']),
    mixedMediaDominanceThreshold:
      schedule.mixed_media_dominance_threshold !== null
      && schedule.mixed_media_dominance_threshold !== undefined
        ? String(schedule.mixed_media_dominance_threshold)
        : '',
    setMixedMediaDominanceThreshold: (v) => {
      if (v === '') {
        set('mixed_media_dominance_threshold', null);
        return;
      }
      const n = Number(v);
      set('mixed_media_dominance_threshold', Number.isFinite(n) ? n : null);
    },
    mixedMediaVideoRouting: (schedule.mixed_media_video_routing ?? '') as '' | 'library_agnostic' | 'library_dominant',
    setMixedMediaVideoRouting: (v) =>
      set('mixed_media_video_routing', v === '' ? null : v as Schedule['mixed_media_video_routing']),
    mixedMediaLogging: (schedule.mixed_media_logging ?? '') as '' | 'full' | 'decisions_only' | 'off',
    setMixedMediaLogging: (v) =>
      set('mixed_media_logging', v === '' ? null : v as Schedule['mixed_media_logging']),
    mixedMediaCollisionHandling: (schedule.mixed_media_collision_handling ?? '') as '' | 'duplicate' | 'suffix' | 'skip',
    setMixedMediaCollisionHandling: (v) =>
      set('mixed_media_collision_handling', v === '' ? null : v as Schedule['mixed_media_collision_handling']),
  };

  // Schedules-only top slot: collapsible Schedule timing panel
  // containing h2 + Name + frequency + hour/min/day-of-week + enabled
  // + server-time banner.
  const topSlot = (
    <div className="panel" style={{ marginTop: 0 }}>
      <button
        type="button"
        onClick={() => setScheduleTimingOpen((o) => !o)}
        aria-expanded={scheduleTimingOpen}
        style={{
          background: 'none', border: 'none', padding: 0,
          font: 'inherit', color: 'inherit', cursor: 'pointer',
          width: '100%', textAlign: 'left',
          display: 'flex', alignItems: 'center', gap: 8,
        }}
      >
        <span style={{ fontSize: 14, color: 'var(--text-dim)' }}>
          {scheduleTimingOpen ? '▾' : '▸'}
        </span>
        <h3 style={{ margin: 0 }}>Schedule</h3>
      </button>

      {scheduleTimingOpen && (
        <div style={{ marginTop: 12 }}>
          <h2 style={{ marginTop: 0 }}>{schedule.id ? 'Edit Schedule' : 'New Schedule'}</h2>
          <label className="field">
            <span className="label">Name</span>
            <span className="help">Shown in the schedule list. Use anything descriptive - "Nightly full export", "Music Sundays", etc.</span>
            <input type="text" value={schedule.name} onChange={(e) => set('name', e.target.value)} />
          </label>

          <label className="field">
            <span className="label">
              Frequency
              <InfoTip topicId="schedule-frequency" />
            </span>
            <select value={schedule.frequency} onChange={(e) => set('frequency', e.target.value as Schedule['frequency'])}>
              <option value="hourly">Hourly</option>
              <option value="daily">Daily</option>
              <option value="weekly">Weekly</option>
            </select>
          </label>

          {serverTime && (
            <div
              className="banner"
              style={{
                background: 'var(--panel-alt, #1b2233)',
                border: '1px solid var(--border, #2a3146)',
                padding: '8px 10px',
                borderRadius: 6,
                margin: '10px 0',
                fontSize: 12,
                color: 'var(--text-dim)',
              }}
            >
              Schedule times are interpreted in the <strong>backend's</strong> timezone:{' '}
              <strong style={{ color: 'var(--text)' }}>
                {formatServerWallclock(serverTime)}
              </strong>{' '}
              <span>({serverTime.tz}{serverTime.tz_abbrev && serverTime.tz_abbrev !== serverTime.tz ? ` · ${serverTime.tz_abbrev}` : ''})</span>.
              {serverTime.tz === 'UTC' && (
                <span style={{ display: 'block', marginTop: 4, color: 'var(--warn, #d39e3c)' }}>
                  Heads up: the backend is on UTC. Set the <code>TZ</code> env var on the backend container (e.g. <code>America/Los_Angeles</code>) so this matches your local clock.
                </span>
              )}
            </div>
          )}

          <div className="grid-2">
            {schedule.frequency !== 'hourly' && (
              <label className="field">
                <span className="label">Hour (0–23, 24-hour clock)</span>
                <input type="number" min={0} max={23} value={schedule.hour} onChange={(e) => set('hour', Number(e.target.value))} />
              </label>
            )}
            <label className="field">
              <span className="label">Minute (0–59)</span>
              <input type="number" min={0} max={59} value={schedule.minute} onChange={(e) => set('minute', Number(e.target.value))} />
            </label>
            {schedule.frequency === 'weekly' && (
              <label className="field">
                <span className="label">Day of week</span>
                <select value={schedule.day_of_week} onChange={(e) => set('day_of_week', Number(e.target.value))}>
                  {DAYS.map((d, i) => <option key={i} value={i}>{d}</option>)}
                </select>
              </label>
            )}
          </div>

          <label className="switch">
            <input type="checkbox" checked={schedule.enabled} onChange={(e) => set('enabled', e.target.checked)} />
            <span>Enabled</span>
            <span className="help">Disabled schedules stay in the list but the scheduler skips them.</span>
          </label>
        </div>
      )}
    </div>
  );

  // Schedules-specific restore source slot: textarea for input_files
  // paths. Run Job uses a snapshot picker + JSON file picker; A.9
  // marks extraction of that as deferred.
  const restoreSourceSlot = (schedule.mode ?? 'snapshot') === 'restore' ? (
    <label className="field">
      <span className="label">Input files</span>
      <span className="help">
        One snapshot file path per line. Accepts container-visible <code>.plexexport.json</code> or
        <code>.db</code> paths. Each fire of this schedule reads these files.
      </span>
      <textarea
        value={(schedule.input_files || []).join('\n')}
        onChange={(e) => {
          const lines = e.target.value
            .split('\n')
            .map((s) => s.trim())
            .filter(Boolean);
          set('input_files', lines.length > 0 ? lines : null);
        }}
        rows={4}
        placeholder="/app/snapshots/Plex1_2026-05-15.plexexport.db"
        style={{ width: '100%', fontFamily: 'monospace', fontSize: 12 }}
      />
    </label>
  ) : null;

  // Schedules-specific restoration-mode extras: persistent
  // confirm_replace + confirm_additive_merge checkboxes that gate
  // Save. Run Job uses the typed-REPLACE modal at submit time
  // instead; on a schedule we need a persistent flag because every
  // fire is destructive.
  const restorationModeExtras = (
    <>
      {schedule.restore_mode === 'replace' && (
        <label className="switch" style={{ marginTop: 8 }}>
          <input
            type="checkbox"
            checked={schedule.confirm_replace ?? false}
            onChange={(e) => set('confirm_replace', e.target.checked)}
          />
          <span style={{ color: 'var(--bad)' }}>
            I understand this schedule will fire a destructive Replace on every tick
          </span>
          <span className="help">
            Required to save a Replace-mode schedule. Replace makes the destination
            match the snapshot exactly - if the snapshot is wrong, every tick
            overwrites the destination again until you disable the schedule.
          </span>
        </label>
      )}
      {(schedule.restore_mode === 'merge' || !schedule.restore_mode)
        && schedule.merge_watch_strategy === 'sum' && (
        <label className="switch" style={{ marginTop: 8 }}>
          <input
            type="checkbox"
            checked={schedule.confirm_additive_merge ?? false}
            onChange={(e) => set('confirm_additive_merge', e.target.checked)}
          />
          <span style={{ color: 'var(--bad)' }}>
            I understand each fire ADDS the stored counts; this compounds across fires
          </span>
          <span className="help">
            Required to save a schedule with Combine totals. The JobConfigBody banner
            above explains the general non-idempotency; on a schedule the effect
            compounds with every fire (after 30 fires the stored counts have been
            added 30 times).
          </span>
        </label>
      )}
    </>
  );

  return (
    <div className="panel">
      <JobConfigBody
        owner={owner}
        topSlot={topSlot}
        restoreSourceSlot={restoreSourceSlot}
        restorationModeExtras={restorationModeExtras}
      />

      <div className="row-buttons">
        <button
          className="primary"
          onClick={async () => {
            // Phase B + Phase C: chained preflight intercepts.
            //   1. PIN preflight (existing) - if at-risk users, modal opens.
            //   2. Cross-platform preflight (new) - if route is cross-backend
            //      and aggregate_verdict != 'ok', modal opens.
            //   3. Save.
            const runCrossPlatformPreflightAndSave = async () => {
              if (!isCrossBackend) {
                onSave();
                return;
              }
              try {
                const r = await api.schedulesCrossPlatformPreflight(
                  schedule as unknown as Record<string, unknown>,
                );
                if (r.aggregate_verdict === 'ok') {
                  onSave();
                  return;
                }
                setCppResponse(r);
                setCppOpen(true);
                // Save fires via the modal's onContinue callback below.
              } catch {
                // Preflight call failed - don't block save. The
                // engine's belt-and-braces enforcement still applies
                // at fire time.
                onSave();
              }
            };

            // Step 1: PIN preflight intercept (same as Phase B).
            if (schedule.pin_preflight_ack) {
              await runCrossPlatformPreflightAndSave();
              return;
            }
            const mode = schedule.mode ?? 'snapshot';
            if (mode === 'restore' || !schedule.source_server_name) {
              await runCrossPlatformPreflightAndSave();
              return;
            }
            try {
              const r = await api.preflightPinCheck({
                mode: mode as 'snapshot' | 'direct',
                source_server_name: schedule.source_server_name,
                dest_server_names: schedule.dest_server_names || null,
                user_filter: schedule.user_filter || null,
              });
              if (!r.checked || r.at_risk_users.length === 0) {
                await runCrossPlatformPreflightAndSave();
                return;
              }
              setPinPreflightAtRisk(r.at_risk_users);
              setPinPreflightOpen(true);
              setSavePending(true);
            } catch {
              // PIN probe failed - skip ahead to cross-platform preflight.
              await runCrossPlatformPreflightAndSave();
            }
          }}
          disabled={
            !schedule.name.trim() ||
            // Mode-specific source/destination requirements. Mirrors
            // the backend ScheduleIn validator so the end user sees
            // the gate up front rather than only on submit.
            ((schedule.mode ?? 'snapshot') !== 'restore' && !schedule.source_server_name) ||
            ((schedule.mode ?? 'snapshot') !== 'snapshot' &&
              (!schedule.dest_server_names || schedule.dest_server_names.length === 0)) ||
            ((schedule.mode ?? 'snapshot') === 'restore' &&
              (!schedule.input_files || schedule.input_files.length === 0)) ||
            (schedule.restore_mode === 'replace' && !schedule.confirm_replace) ||
            // Sum strategy requires explicit additive-merge confirmation
            // (the validator on the backend matches this).
            (schedule.merge_watch_strategy === 'sum' && !schedule.confirm_additive_merge) ||
            // Phase A: when user filters are active they must yield
            // at least one match. Saving a schedule with no users
            // who satisfy the filter would silently process nothing
            // on every fire, so we block the save outright per the
            // end user's stated policy.
            (userFilterActive && userFilteredIds.size === 0) ||
            // Phase C: with per-library metrics, the invariant is "at
            // least one cell in the matrix is true somewhere". When
            // library_metrics is unset or empty, default ALL_ON
            // applies, so the gate is trivially satisfied. When it's
            // explicitly set with every cell false, refuse the save.
            (() => {
              const lm = schedule.library_metrics;
              if (!lm) return false; // unset = default everything on
              const libs = Object.keys(lm);
              if (libs.length === 0) return false;
              const anyTrue = libs.some((k) => {
                const r = lm[k];
                return r && (r.watch_history || r.ratings || r.playlists || r.collections);
              });
              return !anyTrue; // disable Save when every cell is false
            })()
          }
        >
          Save
        </button>
        <button onClick={onCancel}>Cancel</button>
      </div>

      {/* Phase B: PinPreflightModal mount. Opens at Save time when
          the end user hasn't acknowledged cross-server PIN risk yet
          AND the preflight probe found at-risk managed users. On
          Continue: flip pin_preflight_ack=true and chain into the
          cross-platform preflight (Phase C). */}
      <PinPreflightModal
        open={pinPreflightOpen}
        atRiskUsers={pinPreflightAtRisk}
        onCancel={() => {
          setPinPreflightOpen(false);
          setPinPreflightAtRisk([]);
          setSavePending(false);
        }}
        onContinue={() => {
          setPinPreflightOpen(false);
          setPinPreflightAtRisk([]);
          set('pin_preflight_ack', true);
          if (savePending) {
            setSavePending(false);
            // Defer one tick so the set() state update commits, then
            // chain into the cross-platform preflight (Phase C).
            window.setTimeout(async () => {
              if (!isCrossBackend) {
                onSave();
                return;
              }
              try {
                const r = await api.schedulesCrossPlatformPreflight(
                  schedule as unknown as Record<string, unknown>,
                );
                if (r.aggregate_verdict === 'ok') {
                  onSave();
                  return;
                }
                setCppResponse(r);
                setCppOpen(true);
              } catch {
                onSave();
              }
            }, 0);
          }
        }}
      />

      {/* Phase C: cross-platform preflight modal at schedule save
          time. End user's per-destination decisions persist into
          schedule.cross_platform_resolutions before the schedule
          create / edit POSTs. */}
      <SchedulePreflightStep
        open={cppOpen}
        response={cppResponse}
        onCancel={() => {
          setCppOpen(false);
          setCppResponse(null);
        }}
        onContinue={(acks: Record<string, CrossPlatformPreflightAck>) => {
          setCppOpen(false);
          setCppResponse(null);
          set('cross_platform_resolutions', acks);
          // Defer so set() state update commits before onSave reads
          // the updated schedule.
          window.setTimeout(() => onSave(), 0);
        }}
      />
    </div>
  );
}

// ── Helpers ──────────────────────────────────────────────────────────────────

const DAYS = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'];
function pad2(n: number) { return n < 10 ? `0${n}` : String(n); }
function formatTs(ts: number) {
  const d = new Date(ts * 1000);
  return d.toLocaleString();
}

// Extract HH:MM from a backend ISO-with-offset string so we render the
// server's wallclock, not the browser's. Server sends e.g.
// "2026-05-11T14:32:11-07:00"; we want "14:32".
function formatServerWallclock(st: ServerTime): string {
  const m = st.iso.match(/T(\d{2}):(\d{2})/);
  return m ? `${m[1]}:${m[2]}` : st.iso;
}
