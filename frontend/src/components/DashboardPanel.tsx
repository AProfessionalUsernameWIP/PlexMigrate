// The live dashboard view.
//
// Mirrors the terminal panel in services/dashboard.py :: _build_dashboard:
//   * Thread pool summary (categorised counts)
//   * Run stats (completed / skipped / failed / unresolved)
//   * Match resolution stats (GUID / filepath / suffix / fuzzy)
//   * Per-library progress bars
//   * Activity feed (last several events, colour-coded)
//   * Job state badge with elapsed
//
// No ETA columns anywhere on the dashboard. An ETA would have to be
// anchored on a step-count denominator that isn't work-time-uniform
// (snapshot owner-phase steps complete in seconds; per-user fan-out
// steps take minutes per user), so extrapolation flips libraries to
// "Done" prematurely.
//
// Reads only from props - no fetch logic here. The App owns the WS
// subscription and pushes the snapshot down.

import { type ReactNode, Fragment, createContext, useContext, useEffect, useMemo, useRef, useState } from 'react';
import { ActivityEntry, ContainerResult, CurrentItem, DashboardState, FanOutDestState, JobPayload, LibraryProgress, LogFile, DashboardFrame, api } from '../api';
import { LogTailer } from './LogTailer';
import { NetworkPanel } from './NetworkPanel';
import { InfoTip } from './InfoTip';
import { useConfirm } from './ConfirmModal';
import { usePermission } from '../hooks/usePermission';
import { useNowTick } from '../hooks/useNowTick';
import { pausableInterval } from '../utils/pausableInterval';

interface Props {
  snapshot: DashboardFrame | null;
  // Connection state from App so the empty-state panel can
  // distinguish "still connecting" from "connected, no job running".
  connState?: 'connecting' | 'connected' | 'disconnected';
  // Which job's sub-tab is active. ``null`` defaults to the running
  // job. Selection is owned by App.tsx so the sub-tab strip sits at
  // the same visual layer as Run Job / Servers / Settings strips.
  // Single-job snapshots ignore this prop entirely.
  selectedJobId?: string | null;
}

// DashboardPanel-scoped config that deep-tree sub-components read. The
// values come from settings and live in DashboardPanel state, so a
// settings change re-renders every consumer; a module-level ``let``
// would mutate silently without re-rendering. Defaults match the
// backend tunable defaults.
interface DashboardConfig {
  etrColorMultiplier: number;
  restorationSummaryMaxItems: number;
}

const DashboardConfigContext = createContext<DashboardConfig>({
  etrColorMultiplier: 1.0,
  restorationSummaryMaxItems: 10,
});

export function DashboardPanel({
  snapshot,
  connState = 'connected',
  selectedJobId = null,
}: Props) {
  // The per-phase stall thresholds scale by an end user-set ETR
  // colour multiplier, and the Restoration Summary panel caps its
  // row count by a tunable. Both are component state surfaced to the
  // deep-tree consumers via DashboardConfigContext, so a settings
  // change re-renders them. A change via Settings takes effect on the
  // next DashboardPanel mount.
  const [etrColorMultiplier, setEtrColorMultiplier] = useState(1.0);
  const [restorationSummaryMaxItems, setRestorationSummaryMaxItems] = useState(10);
  useEffect(() => {
    api.getSettings()
      .then((s) => {
        const m = (s as { etr_color_multiplier?: number }).etr_color_multiplier;
        // Clamp to the [0.5, 2.0] window the backend tunable enforces.
        if (typeof m === 'number' && Number.isFinite(m)) {
          setEtrColorMultiplier(Math.min(2.0, Math.max(0.5, m)));
        }
        // Restoration Summary row cap, from
        // settings.tunables.restoration_summary_panel_max_items.
        const raw = (s as unknown as Record<string, unknown>).tunables;
        const tn = raw && typeof raw === 'object' ? (raw as Record<string, unknown>) : {};
        const cap = tn['restoration_summary_panel_max_items'];
        if (typeof cap === 'number' && Number.isFinite(cap)) {
          setRestorationSummaryMaxItems(Math.min(200, Math.max(1, Math.trunc(cap))));
        }
      })
      .catch(() => { /* non-fatal - keep the defaults */ });
  }, []);
  const dashboardConfig = useMemo<DashboardConfig>(
    () => ({ etrColorMultiplier, restorationSummaryMaxItems }),
    [etrColorMultiplier, restorationSummaryMaxItems],
  );

  const dash = snapshot?.dashboard ?? null;
  const job = snapshot?.job ?? null;
  const fanOut = snapshot?.fan_out ?? null;
  const jobs = snapshot?.jobs ?? [];

  // Multi-job selection. When two or more jobs are active / queued
  // the parent (App.tsx) renders a sub-tab strip and threads the
  // selected job id down via ``selectedJobId``. If the end user
  // hasn't picked one yet (or the prior pick has fallen off the list)
  // we default to the running job. If the end user selected a queued
  // job we render the lightweight QueuedJobPanel - there's no
  // dashboard to render until the worker picks it up.
  if (jobs.length > 1) {
    const activeJob =
      jobs.find((j) => j.job_id === selectedJobId)
      ?? jobs.find((j) => job && j.job_id === job.job_id)
      ?? jobs[0];
    const isRunningJob = !!job && activeJob.job_id === job.job_id;
    if (!isRunningJob) {
      const queuePosition = jobs.findIndex((j) => j.job_id === activeJob.job_id);
      return <QueuedJobPanel job={activeJob} queuePosition={queuePosition} />;
    }
    // Fall through into the existing single-job render path for the
    // running record - that's identical to the single-job case below.
  }

  // Unified shell: JobHeader renders exactly ONCE here, for every job
  // type, then the body is routed to one of two components:
  //   * FanOutBody    - the per-destination sub-tab layout (>1 dest)
  //   * DashboardBody - the single-job section list (snapshot /
  //     restore / direct; each fan-out destination sub-tab also
  //     reuses DashboardBody internally).
  // JobHeader has one render site here so it is never double-rendered
  // on a fan-out per-destination tab. One render site, one source of
  // truth.
  return (
    <DashboardConfigContext.Provider value={dashboardConfig}>
      <JobHeader job={job} dash={dash} />
      {fanOut && fanOut.length > 1 ? (
        <FanOutBody job={job} fanOut={fanOut} connState={connState} frame={snapshot} />
      ) : (
        <DashboardBody
          job={job}
          dash={dash}
          frame={snapshot}
          scopedDest={null}
          connState={connState}
          // Single-destination mode: the log tailer reads the job's
          // run_log_dir. Pass null so the body uses its own state.
          logRunOverride={null}
        />
      )}
    </DashboardConfigContext.Provider>
  );
}

// Placeholder rendered when the end user clicks a queued job's
// sub-tab. We don't have a DashboardState for a job that hasn't
// started yet, so show the end user the queue position and the
// job's submitted scope so they can still inspect it without having
// to wait for the worker to pick it up.
function QueuedJobPanel({ job, queuePosition }: { job: JobPayload; queuePosition: number }) {
  const params = (job.params as Record<string, unknown>) ?? {};
  const libs = Array.isArray(params.libraries) ? (params.libraries as unknown[]).map(String) : [];
  const inputFiles = Array.isArray(params.input_files) ? (params.input_files as unknown[]).map(String) : [];
  const src = typeof params.source_server_name === 'string' ? params.source_server_name : '';
  const dests = Array.isArray(params.dest_server_names)
    ? (params.dest_server_names as unknown[]).map(String)
    : (typeof params.dest_server_name === 'string' ? [params.dest_server_name] : []);
  // The "source" of a restore is a snapshot file, not a server, so
  // the input_files block is labelled "Snapshot used" and the
  // destination block is "Restoring to".
  const isRestore = job.mode === 'restore';
  return (
    <div className="panel">
      <h2 style={{ marginTop: 0 }}>
        {job.mode.toUpperCase()} job - {job.state}
        <span style={{ marginLeft: 12, fontSize: 13, color: 'var(--text-dim)' }}>
          queue position #{queuePosition}
        </span>
      </h2>
      <SectionHint>
        This job is waiting for the worker. It will start automatically when the
        currently-running job (and any earlier queued jobs) finish.
      </SectionHint>
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 16, fontSize: 14, marginTop: 6 }}>
        {isRestore ? (
          inputFiles.length > 0 && (
            <div>
              <span className="label">Snapshot used:</span>{' '}
              <strong>
                {inputFiles.length} export file{inputFiles.length === 1 ? '' : 's'}
              </strong>
            </div>
          )
        ) : (
          src && (
            <div><span className="label">Source:</span> <strong>{src}</strong></div>
          )
        )}
        {dests.length > 0 && (
          <div>
            <span className="label">
              {isRestore
                ? (dests.length > 1 ? 'Restoring to:' : 'Restoring to:')
                : (dests.length > 1 ? 'Destinations:' : 'Destination:')}
            </span>{' '}
            <strong>{dests.join(', ')}</strong>
          </div>
        )}
        {libs.length > 0 && (
          <div>
            <span className="label">Libraries:</span> {libs.join(', ')}
          </div>
        )}
        {!isRestore && inputFiles.length > 0 && (
          <div>
            <span className="label">Export files:</span> {inputFiles.length}
          </div>
        )}
      </div>
    </div>
  );
}

// The per-snapshot panel body. JobHeader is not rendered here; the
// unified shell in DashboardPanel owns it. This component is purely
// the ordered section list plus the empty / connecting /
// job-starting placeholder.
//
// ``logRunOverride`` lets the fan-out per-destination sub-tab point
// the inline log tail at the destination's own run_log_dir rather
// than the global job's. ``null`` means "use the job's run dir."

// The body layout, as one ordered, declarative list. "Which panel
// shows for which job mode" lives HERE - not scattered across
// return-null guards in each component. ``modes: 'all'`` renders for
// every mode; otherwise the section renders only for the listed modes
// (and never when the job mode is unknown). Data-driven hiding (e.g.
// BatchEtrPanel when empty, ContainerSummary when no containers yet)
// still lives inside the components - only the *mode* gate is here.
type SectionCtx = {
  job: JobPayload | null;
  dash: DashboardState;
  frame: DashboardFrame | null;
  scopedDest: string | null;
  logRunOverride: string | null;
};
const DASHBOARD_SECTIONS: {
  key: string;
  modes: JobPayload['mode'][] | 'all';
  render: (c: SectionCtx) => ReactNode;
}[] = [
  // direct-only: the Tier-3 fuzzy-match review banner.
  { key: 'fuzzy', modes: ['direct'], render: (c) => <FuzzyTierWarning job={c.job} dash={c.dash} /> },
  { key: 'coverage', modes: 'all', render: (c) => <RunCoverage dash={c.dash} /> },
  { key: 'runstats', modes: 'all', render: (c) => <RunStats dash={c.dash} /> },
  // Match resolution only means something when matching against a
  // target server - a snapshot resolves nothing, so this panel would
  // render all-zeros on a snapshot. Restore / direct only.
  { key: 'match', modes: ['restore', 'direct'], render: (c) => <MatchStats dash={c.dash} /> },
  // restore-only: the per-container restoration summary.
  { key: 'container', modes: ['restore'], render: (c) => <ContainerSummary dash={c.dash} job={c.job} /> },
  { key: 'batch', modes: 'all', render: (c) => <BatchEtrPanel dash={c.dash} /> },
  { key: 'libraries', modes: 'all', render: (c) => <LibraryList libs={c.dash.libraries} job={c.job} /> },
  { key: 'threadpool', modes: 'all', render: (c) => <ThreadPool dash={c.dash} /> },
  { key: 'current', modes: 'all', render: (c) => <CurrentlyProcessing items={c.dash.current_items ?? []} /> },
  { key: 'activity', modes: 'all', render: (c) => <ActivityFeed entries={c.dash.activity} /> },
  { key: 'network', modes: 'all', render: (c) => <NetworkPanel frame={c.frame} scopedDest={c.scopedDest} /> },
  { key: 'log', modes: 'all', render: (c) => <DashboardLogTail job={c.job} logRunOverride={c.logRunOverride} /> },
];

function sectionVisible(
  modes: JobPayload['mode'][] | 'all',
  mode: JobPayload['mode'] | null,
): boolean {
  if (modes === 'all') return true;
  return mode !== null && modes.includes(mode);
}

function DashboardBody({
  job,
  dash,
  frame,
  scopedDest,
  connState,
  logRunOverride,
}: {
  job: JobPayload | null;
  dash: DashboardState | null;
  // Frame is threaded down so the inline NetworkPanel can read
  // ``servers_network`` (process-lifetime, server-keyed telemetry).
  frame: DashboardFrame | null;
  // When set, NetworkPanel narrows to just (source +) this one
  // destination - used inside fan-out per-dest sub-tabs.
  scopedDest: string | null;
  connState: 'connecting' | 'connected' | 'disconnected';
  logRunOverride: string | null;
}) {
  const mode = job?.mode ?? null;
  return (
    <>
      {dash ? (
        <>
          {/* Surface dash.finalizing prominently. After every library
              shows "Library complete" the engine still has to close
              logs, finalize the run dir, write the snapshot .db file,
              and run the mirror writethrough - all of which can take
              10-60s on a large run. Without this banner the dashboard
              goes silent and the operator reasonably wonders whether
              the job hung. The label is set by server/jobs.py at each
              finalize step ("closing run logs" -> "writing snapshot
              to database" -> "writing mirror"). */}
          {dash.finalizing && (
            <div
              className="banner info"
              style={{ marginBottom: 12, display: 'flex', alignItems: 'center', gap: 8 }}
              role="status"
            >
              <span className="spinner" aria-hidden="true" />
              <strong>Finalizing run:</strong>
              <span>{dash.finalizing}…</span>
              <span style={{ marginLeft: 'auto', fontSize: 12, opacity: 0.7 }}>
                Run is not hung — libraries are complete; the engine is writing artifacts.
              </span>
            </div>
          )}
          {DASHBOARD_SECTIONS
            .filter((s) => sectionVisible(s.modes, mode))
            .map((s) => (
              <Fragment key={s.key}>
                {s.render({ job, dash, frame, scopedDest, logRunOverride })}
              </Fragment>
            ))}
        </>
      ) : (
        <div className="panel">
          {connState === 'connecting' && (
            <div className="empty">Connecting to the server…</div>
          )}
          {connState === 'disconnected' && (
            <div className="empty">
              Disconnected from the server. The page will reconnect automatically.
            </div>
          )}
          {connState === 'connected' && (
            (() => {
              // If a job exists and is in flight, the placeholder
              // DashboardState from JobQueue should have populated
              // `dash` by now. We hit this branch only if the
              // pre-flight DashboardState hasn't arrived yet (very
              // brief, but possible right after submit). Use the job
              // state to render the right copy.
              const s = job?.state;
              if (s === 'queued' || s === 'running' || s === 'stopping') {
                return (
                  <div className="empty">
                    <strong>Job starting…</strong>
                    <div style={{ marginTop: 6, fontSize: 12 }}>
                      Connecting to Plex and preparing the dashboard. On a large server
                      with many home users this can take 10–60 seconds the first time.
                    </div>
                  </div>
                );
              }
              return (
                <div className="empty" data-testid="dashboard-no-active-jobs">No job is running. Start one from the <strong>Run Job</strong> tab.</div>
              );
            })()
          )}
        </div>
      )}
      {!dash && job && <DashboardLogTail job={job} logRunOverride={logRunOverride} />}
    </>
  );
}

// Yellow review banner. Only fires during direct-transfer runs
// where Tier 3 (fuzzy title matching) actually produced a match - the
// gate ships OFF by default, so a non-zero count means the end user
// explicitly opted in via Settings → Transfer Resolution and we want
// to flag the result for spot-check.
function FuzzyTierWarning({
  job,
  dash,
}: {
  job: JobPayload | null;
  dash: DashboardState | null;
}) {
  if (!job || job.mode !== 'direct') return null;
  const fuzzy = dash?.tier_counts?.fuzzy ?? 0;
  if (fuzzy <= 0) return null;
  return (
    <div
      className="banner"
      style={{
        background: 'rgba(217, 119, 6, 0.12)',
        border: '1px solid var(--warn, #d97706)',
        color: 'var(--warn, #d97706)',
        padding: '8px 12px',
        borderRadius: 6,
        marginBottom: 8,
        fontSize: 13,
      }}
    >
      <strong>Fuzzy title match used ({fuzzy})</strong> - Tier 3 produced{' '}
      {fuzzy === 1 ? 'a match' : 'matches'} for this transfer. Tier 3 can
      pair items by title alone and may produce incorrect matches; review
      recommended.
    </div>
  );
}

// Per-container restoration summary. Renders a table of every
// playlist + collection touched by the active import (or the just-
// completed import) showing "restored / total" plus an expandable
// "X items unavailable" breakdown. Hidden entirely on snapshot /
// direct-transfer runs and when the importer hasn't recorded anything
// yet (live frames before the first container's merge step finishes).
function ContainerSummary({
  dash,
  job,
}: {
  dash: DashboardState;
  job: JobPayload | null;
}) {
  // Only relevant for import runs.
  if (!job || job.mode !== 'restore') return null;
  const playlists = dash.container_summary?.playlists ?? [];
  const collections = dash.container_summary?.collections ?? [];
  if (playlists.length === 0 && collections.length === 0) return null;
  return (
    <div className="panel">
      <h2>Restoration Summary</h2>
      <SectionHint>
        Per-container restore results. Items not available on the
        destination are skipped, not failed - the container is still
        created with whatever resolved.
      </SectionHint>
      {playlists.length > 0 && (
        <ContainerSummaryTable kind="Playlists" rows={playlists} />
      )}
      {collections.length > 0 && (
        <ContainerSummaryTable kind="Collections" rows={collections} />
      )}
    </div>
  );
}

function ContainerSummaryTable({
  kind,
  rows,
}: {
  kind: 'Playlists' | 'Collections';
  rows: ContainerResult[];
}) {
  const memberWord = kind === 'Playlists' ? 'items' : 'members';
  // Cap on-screen rows + scroll the rest so the Restoration Summary
  // panel doesn't unbounded-grow on libraries with hundreds of
  // playlists. Cap source is the
  // ``restoration_summary_panel_max_items`` tunable (default 10),
  // surfaced through DashboardConfigContext so a change re-renders.
  // Approx row height is ~32px; we set maxHeight at cap × row + header
  // padding so the cap value × row height bounds the visible window.
  const cap = useContext(DashboardConfigContext).restorationSummaryMaxItems;
  const scrollable = rows.length > cap;
  const APPROX_ROW_HEIGHT = 32;
  const maxHeightPx = scrollable ? cap * APPROX_ROW_HEIGHT + 4 : undefined;
  return (
    <div style={{ marginBottom: 12 }}>
      <h3 style={{ fontSize: 13, margin: '6px 0', color: 'var(--text-dim)' }}>
        {kind} ({rows.length})
        {scrollable && (
          <span style={{ fontWeight: 400, marginLeft: 8, fontSize: 11 }}>
            — showing {cap} of {rows.length}, scroll for more
          </span>
        )}
      </h3>
      <div
        style={{
          maxHeight: maxHeightPx,
          overflowY: scrollable ? 'auto' : undefined,
          border: scrollable ? '1px solid var(--border, #2a3146)' : undefined,
          borderRadius: scrollable ? 4 : undefined,
        }}
      >
        <table style={{ width: '100%', fontSize: 12, borderCollapse: 'collapse' }}>
          <thead style={{ position: scrollable ? 'sticky' : undefined, top: 0, background: 'var(--bg-panel, #131722)' }}>
            <tr style={{ textAlign: 'left', color: 'var(--text-dim)' }}>
              <th style={{ padding: '4px 6px' }}>Name</th>
              <th style={{ padding: '4px 6px' }}>Library</th>
              <th style={{ padding: '4px 6px', textAlign: 'right' }}>Restored</th>
              <th style={{ padding: '4px 6px' }}>Status</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r, i) => (
              <ContainerSummaryRow key={`${r.library}/${r.name}/${i}`} row={r} memberWord={memberWord} />
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function ContainerSummaryRow({
  row,
  memberWord,
}: {
  row: ContainerResult;
  memberWord: 'items' | 'members';
}) {
  const [open, setOpen] = useState(false);
  const hasSkipped = row.skipped > 0 || (row.skipped_items?.length ?? 0) > 0;
  const isFullyRestored = !row.smart && row.total > 0 && row.restored === row.total;
  const isWhollyMissing = !row.smart && row.total > 0 && row.restored === 0;
  const statusColor = row.smart
    ? 'var(--warn, #d97706)'
    : isWhollyMissing
      ? 'var(--err, #dc2626)'
      : isFullyRestored
        ? 'var(--ok, #16a34a)'
        : 'var(--warn, #d97706)';
  const statusLabel = row.smart
    ? 'Smart playlist - manual'
    : isFullyRestored
      ? 'Fully restored'
      : isWhollyMissing
        ? 'No members available'
        : `${row.skipped} not available`;
  return (
    <>
      <tr
        style={{
          borderTop: '1px solid var(--border, #2a2a2a)',
          cursor: hasSkipped ? 'pointer' : 'default',
        }}
        onClick={() => { if (hasSkipped) setOpen((x) => !x); }}
      >
        <td style={{ padding: '4px 6px' }}>
          {hasSkipped && (
            <span style={{ marginRight: 4, color: 'var(--text-dim)' }}>
              {open ? '▾' : '▸'}
            </span>
          )}
          {row.name}
        </td>
        <td style={{ padding: '4px 6px', color: 'var(--text-dim)' }}>{row.library || '-'}</td>
        <td style={{ padding: '4px 6px', textAlign: 'right', fontVariantNumeric: 'tabular-nums' }}>
          {row.smart ? '-' : `${row.restored} / ${row.total}`}
        </td>
        <td style={{ padding: '4px 6px', color: statusColor }}>{statusLabel}</td>
      </tr>
      {open && hasSkipped && (
        <tr>
          <td colSpan={4} style={{
            padding: '6px 12px 10px 24px',
            background: 'rgba(0,0,0,0.18)',
            color: 'var(--text-dim)',
            fontSize: 11,
          }}>
            {row.reason && (
              <div style={{ marginBottom: 4 }}><em>{row.reason}</em></div>
            )}
            {(row.skipped_items?.length ?? 0) > 0 && (
              <>
                <div style={{ marginBottom: 4 }}>
                  Unavailable {memberWord}:
                </div>
                <ul style={{ margin: 0, paddingLeft: 16 }}>
                  {row.skipped_items.map((item, i) => (
                    <li key={i}>
                      <strong>{item.title}</strong>
                      {item.type ? ` (${item.type})` : ''}
                      {item.reason ? ` - ${item.reason}` : ''}
                    </li>
                  ))}
                </ul>
                {row.skipped_items_truncated && row.skipped_items_truncated > 0 && (
                  <div style={{ marginTop: 4, fontStyle: 'italic' }}>
                    … and {row.skipped_items_truncated} more (see run log).
                  </div>
                )}
              </>
            )}
          </td>
        </tr>
      )}
    </>
  );
}

// ── Dashboard live-log tail ───────────────────────────────────────────────────
// Embedded under the JobHeader so users can tail any log file from the
// current run without switching to the Logs tab. The file dropdown
// shows everything in the run dir (runtime.log, errors.log, media.log,
// + per-library success/fail logs once they appear).
//
// File-list refresh: we re-fetch the dropdown every 5 s while the job
// is running because per-library success/fail logs appear *after* each
// library finishes - without a refresh the dropdown would never show
// them mid-run.

function DashboardLogTail({
  job,
  logRunOverride = null,
}: {
  job: JobPayload | null;
  // Fan-out per-destination sub-tabs pass their own log dir basename
  // here so the tail attaches to that destination's run directory
  // instead of the parent job's. ``null`` = use the job's
  // run_log_dir (single-destination behaviour).
  logRunOverride?: string | null;
}) {
  const [files, setFiles] = useState<LogFile[]>([]);
  const [selected, setSelected] = useState<string>('runtime.log');
  const [listError, setListError] = useState<string | null>(null);

  // Derive the run name. Override wins when present; otherwise fall
  // back to the job's run_log_dir basename. The backend writes paths
  // with the OS separator (Linux in Docker, Windows on a native
  // install) so we normalise both styles.
  const runName = useMemo(() => {
    const raw = logRunOverride ?? job?.run_log_dir ?? null;
    if (!raw) return null;
    const parts = raw.replace(/\\/g, '/').split('/').filter(Boolean);
    return parts[parts.length - 1] || null;
  }, [logRunOverride, job?.run_log_dir]);

  // The job's run_log_dir field still points at the *pre-rename* path
  // because _finalise_run_dir renames the directory at job end but
  // never updates the JobRecord. When the job state transitions to
  // completed/failed/cancelled we freeze the tail rather than chase
  // the suffixed path - the Logs tab is the right place for post-mortem.
  const jobIsLive =
    !!job && (job.state === 'queued' || job.state === 'running' || job.state === 'stopping');

  // Periodically refresh the dropdown so per-library .log files
  // appearing mid-run become selectable without a page reload.
  useEffect(() => {
    if (!runName) {
      setFiles([]);
      return;
    }
    let cancelled = false;
    const load = () => {
      api.listLogFiles(runName)
        .then((fs) => { if (!cancelled) { setFiles(fs); setListError(null); } })
        .catch((e) => {
          if (cancelled) return;
          // The run directory is renamed (..._PASS / _FAIL) when the
          // job finishes, so the original runName 404s at that moment.
          // Swallow that specific case - the tail is about to freeze
          // anyway and a banner error is just noise.
          if (isRunGoneError(e)) { setListError(null); return; }
          setListError(String(e));
        });
    };
    load();
    // Stop refreshing once the job is done - the file set is final.
    if (!jobIsLive) return () => { cancelled = true; };
    const stop = pausableInterval(load, 5000);
    return () => { cancelled = true; stop(); };
  }, [runName, jobIsLive]);

  // Default selection: prefer runtime.log if present; otherwise first
  // file in the dropdown. Resets when the run changes.
  useEffect(() => {
    if (files.length === 0) return;
    const names = files.map((f) => f.name);
    if (names.includes(selected)) return;
    setSelected(names.includes('runtime.log') ? 'runtime.log' : names[0]);
  }, [files, selected]);

  if (!job) {
    return (
      <div className="panel">
        <h2>Live Log</h2>
        <div className="empty">No active run. Start a job from the <strong>Run Job</strong> tab to tail its log here.</div>
      </div>
    );
  }
  if (!runName) {
    return (
      <div className="panel">
        <h2>Live Log</h2>
        <div className="empty">Log file not yet available initialising…</div>
      </div>
    );
  }

  return (
    <div className="panel">
      <div style={{
        display: 'flex', justifyContent: 'space-between', alignItems: 'center',
        marginBottom: 8, gap: 12, flexWrap: 'wrap',
      }}>
        <h2 style={{ margin: 0 }}>Live Log</h2>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <label style={{ fontSize: 12, color: 'var(--text-dim)' }}>File:</label>
          <select
            value={selected}
            onChange={(e) => setSelected(e.target.value)}
            disabled={files.length === 0}
          >
            {files.length === 0 && <option value="">(no files yet)</option>}
            {files.map((f) => (
              <option key={f.name} value={f.name}>{f.name}</option>
            ))}
          </select>
        </div>
      </div>
      <SectionHint>Tail of any log file in the current run directory, pick a file from the dropdown above.</SectionHint>
      {listError && <div className="banner error" style={{ marginBottom: 6 }}>{listError}</div>}
      {selected && files.some((f) => f.name === selected) ? (
        <LogTailer
          runName={runName}
          fileName={selected}
          height={220}
          externalFreeze={!jobIsLive}
        />
      ) : (
        <div className="empty" style={{ fontSize: 12 }}>
          Waiting for log files to appear in the run directory…
        </div>
      )}
    </div>
  );
}

// ── Fan-out sub-tab system ────────────────────────────────────────────────────
//
// When the WS payload carries more than one destination, the Dashboard
// tab grows a row of nested sub-tabs:
//
//   [ Server A ] [ Server B ] ... [ Overview ] [ Logs ]
//
// Each per-destination sub-tab reuses ``DashboardBody`` - the same
// component that renders single-destination jobs - with the
// destination's snapshot slice as props. No separate FanOutLayout.
//
// The Overview sub-tab is a compact read-only status table summarising
// every destination at a glance. The Logs sub-tab is a unified log
// viewer with one bubble per server (source + destinations); selecting
// a bubble points the embedded LogTailer at that server's run dir.
// Multi-server merged tailing is intentionally out of scope here -
// the bubbles act as a switcher today and can become a merged view
// in a follow-up.

type FanOutTabKey = string;  // dest_name | "__overview__" | "__logs__"

// FanOutBody - the per-destination sub-tab layout. Parallel to
// DashboardBody (the single-job body); the unified shell in
// DashboardPanel picks one or the other and renders JobHeader above
// whichever it picks. JobHeader is rendered by the shell, not here,
// so it is never double-rendered on a per-dest tab.
function FanOutBody({
  job,
  fanOut,
  connState,
  frame,
}: {
  job: JobPayload | null;
  fanOut: FanOutDestState[];
  connState: 'connecting' | 'connected' | 'disconnected';
  // Threaded through to FanOutPerDest → DashboardBody → NetworkPanel so
  // each destination's sub-tab can render its per-server HTTP telemetry
  // from frame.servers_network.
  frame: DashboardFrame | null;
}) {
  // Track selection by tab key. Default to the first destination.
  const [active, setActive] = useState<FanOutTabKey>(fanOut[0]?.dest_name ?? '__overview__');

  // If the active destination disappeared (rare - end user-side state
  // change between renders), fall back to overview rather than blank.
  useEffect(() => {
    if (active === '__overview__' || active === '__logs__') return;
    if (!fanOut.some((d) => d.dest_name === active)) {
      setActive('__overview__');
    }
  }, [fanOut, active]);

  const tabs: { key: FanOutTabKey; label: string; dest?: FanOutDestState }[] = [
    ...fanOut.map((d) => ({ key: d.dest_name, label: d.dest_name, dest: d })),
    { key: '__overview__', label: 'Overview' },
    { key: '__logs__', label: 'Logs' },
  ];

  return (
    <>
      <div className="panel" style={{ paddingBottom: 8 }}>
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
          {tabs.map((t) => {
            const isActive = active === t.key;
            const stateClass = t.dest ? `fanout-tab-${t.dest.state}` : '';
            return (
              <button
                key={t.key}
                onClick={() => setActive(t.key)}
                className={`fanout-tab ${stateClass}`}
                style={{
                  padding: '6px 12px',
                  borderRadius: 999,
                  border: '1px solid var(--border, #2a3146)',
                  background: isActive ? 'var(--accent, #4a7afc)' : 'var(--panel-alt, #1b2233)',
                  color: isActive ? '#fff' : 'var(--text)',
                  cursor: 'pointer',
                  fontSize: 13,
                }}
              >
                {t.dest && (
                  <span
                    style={{
                      display: 'inline-block',
                      width: 8,
                      height: 8,
                      borderRadius: '50%',
                      marginRight: 6,
                      background: badgeColorForState(t.dest.state),
                    }}
                  />
                )}
                {t.label}
              </button>
            );
          })}
        </div>
        {connState !== 'connected' && (
          <div className="banner info" style={{ marginTop: 8 }}>
            {connState === 'connecting' ? 'Connecting to the server…' : 'Reconnecting…'}
          </div>
        )}
      </div>

      {active === '__overview__' && <FanOutOverview job={job} fanOut={fanOut} />}
      {active === '__logs__' && <FanOutUnifiedLogs job={job} fanOut={fanOut} />}
      {active !== '__overview__' && active !== '__logs__' && (() => {
        const dest = fanOut.find((d) => d.dest_name === active);
        if (!dest) return null;
        return <FanOutPerDest dest={dest} job={job} connState={connState} frame={frame} />;
      })()}
    </>
  );
}

// One destination's sub-tab content. Reuses ``DashboardBody`` so the
// per-destination view shows the exact same panels as a normal
// single-destination job - progress, ETA, activity, match stats,
// HTTP telemetry, embedded log tail. The destination's own run dir
// is passed as ``logRunOverride`` so the embedded tail reads that
// destination's runtime.log rather than the parent job's.
function FanOutPerDest({
  dest,
  job,
  connState,
  frame,
}: {
  dest: FanOutDestState;
  job: JobPayload | null;
  connState: 'connecting' | 'connected' | 'disconnected';
  // Frame for NetworkPanel's per-server telemetry. The destination
  // name is passed as ``scopedDest`` so this sub-tab only shows
  // (source +) that one destination.
  frame: DashboardFrame | null;
}) {
  return (
    <>
      <div className="panel" style={{ display: 'flex', alignItems: 'baseline', gap: 12, flexWrap: 'wrap' }}>
        <h2 style={{ margin: 0 }}>{dest.dest_name}</h2>
        <span className={`badge badge-${dest.state}`}>{dest.state}</span>
        {dest.error && (
          <span className="banner error" style={{ margin: 0, padding: '4px 8px' }}>
            {dest.error}
          </span>
        )}
        {dest.started_at && (
          <span className="label" style={{ fontSize: 12 }}>
            started {new Date(dest.started_at * 1000).toLocaleTimeString()}
          </span>
        )}
        {dest.finished_at && (
          <span className="label" style={{ fontSize: 12 }}>
            finished {new Date(dest.finished_at * 1000).toLocaleTimeString()}
          </span>
        )}
      </div>
      <DashboardBody
        job={job}
        dash={dest.dashboard}
        frame={frame}
        scopedDest={dest.dest_name}
        connState={connState}
        logRunOverride={dest.log_dir || null}
      />
    </>
  );
}

// Compact, read-only status table - one row per destination.
function FanOutOverview({
  job,
  fanOut,
}: {
  job: JobPayload | null;
  fanOut: FanOutDestState[];
}) {
  const sourceName = (() => {
    const raw = (job?.params as Record<string, unknown> | undefined)?.source_server_name;
    return typeof raw === 'string' ? raw : '';
  })();
  const counts = useMemo(() => {
    const acc = { queued: 0, running: 0, completed: 0, failed: 0, cancelled: 0 };
    for (const d of fanOut) {
      if (d.state in acc) (acc as Record<string, number>)[d.state] += 1;
    }
    return acc;
  }, [fanOut]);
  return (
    <>
      <div className="panel">
        <h2>Fan-out - {fanOut.length} destinations</h2>
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 16, alignItems: 'baseline' }}>
          {sourceName && (
            <div>
              <span className="label">Source:</span>{' '}
              <strong>{sourceName}</strong>
            </div>
          )}
          <div><span className="label">Running:</span> {counts.running}</div>
          <div><span className="label">Completed:</span> {counts.completed}</div>
          <div><span className="label">Failed:</span> {counts.failed}</div>
          <div><span className="label">Queued:</span> {counts.queued}</div>
          {counts.cancelled > 0 && (
            <div><span className="label">Cancelled:</span> {counts.cancelled}</div>
          )}
        </div>
        <SectionHint>
          Each destination runs in parallel on its own thread with its own dashboard,
          log directory, and error tracking. A failed destination doesn't affect its siblings.
        </SectionHint>
      </div>
      <div className="panel">
        <h2>Per-destination status</h2>
        <table className="list" style={{ width: '100%' }}>
          <thead>
            {/* No ETA column. An ETA would be inconsistent across
                job phases - it would mark "done" and flip the bar
                green at ~71% because the underlying estimator
                anchors on a different step count than the visible
                progress. Progress + last log line carry the
                load instead. */}
            <tr>
              <th>Server</th>
              <th>Status</th>
              <th>Current library</th>
              <th>Progress</th>
              <th>Last log line</th>
              <th>Avg latency</th>
            </tr>
          </thead>
          <tbody>
            {fanOut.map((d) => {
              const dash = d.dashboard;
              const activeLib = dash?.libraries.find((l) => l.status === 'active') ?? null;
              const totalCompleted = (dash?.libraries ?? []).reduce((a, l) => a + l.completed, 0);
              const totalTotal = (dash?.libraries ?? []).reduce((a, l) => a + l.total, 0);
              const pct = totalTotal > 0 ? Math.round((totalCompleted / totalTotal) * 100) : 0;
              const lastLog = dash?.activity?.[dash.activity.length - 1];
              const lastLogText = lastLog
                ? `${lastLog.timestamp} ${lastLog.action_type.toUpperCase()} ${lastLog.title}`
                : '-';
              const errBadge = d.error || (d.state === 'failed');
              return (
                <tr key={d.dest_name}>
                  <td>
                    {errBadge && <span className="badge badge-failed" style={{ marginRight: 6 }}>!</span>}
                    {d.dest_name}
                  </td>
                  <td>
                    <span className={`badge badge-${d.state}`}>{d.state}</span>
                  </td>
                  <td>{activeLib?.name || '-'}</td>
                  <td>{totalTotal > 0 ? `${pct}%` : '-'}</td>
                  <td style={{ maxWidth: 320, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                    {lastLogText}
                  </td>
                  <td>-</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </>
  );
}

// Unified Logs sub-tab. Bubbles are multi-select and act as a
// client-side filter on top of a merged log view that pulls each
// destination's runtime.log, tags every line with its server, and
// orders chronologically. All bubbles default-on. Toggling a bubble
// off hides that server's lines without affecting the merge.
//
// The ``LogTailer`` component is not modified. The merge happens
// inside ``FanOutMergedLogTailer``, which owns its own polling for
// multiple files - the per-file fetch mechanism (``api.readLogFile``)
// is the same primitive ``LogTailer`` uses.
function FanOutUnifiedLogs({
  job,
  fanOut,
}: {
  job: JobPayload | null;
  fanOut: FanOutDestState[];
}) {
  const palette = ['#4a7afc', '#e58a3e', '#3ba56d', '#c854c0', '#d4b13e', '#3ec0d0'];
  // One bubble per destination. Source-side log lines naturally land
  // in each destination's runtime.log (the engine logs through the
  // destination thread's context), so there's no distinct source
  // stream to fetch - the source bubble would be cosmetic only and
  // we omit it for now.
  const bubbles = fanOut.map((d, i) => ({
    key: d.dest_name,
    label: d.dest_name,
    dest: d,
    color: palette[i % palette.length],
  }));
  // Default all bubbles visible; toggling acts as a hide-only filter.
  const [visible, setVisible] = useState<Set<string>>(() => new Set(bubbles.map((b) => b.key)));
  // Re-arm visible set when the fan-out destination list itself
  // changes (a new destination spinning up mid-run, or stale state
  // from a prior fan-out). Preserve any existing user selections that
  // are still relevant.
  useEffect(() => {
    setVisible((prev) => {
      const next = new Set<string>();
      for (const b of bubbles) {
        if (prev.size === 0 || prev.has(b.key)) next.add(b.key);
      }
      return next;
    });
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fanOut.map((d) => d.dest_name).join('|')]);

  const toggle = (key: string) => {
    setVisible((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };

  // Build the run names we need to merge. ``runNameFromLogDir``
  // extracts the last path component (matches what ``DashboardLogTail``
  // does internally).
  const sources = bubbles
    .filter((b) => !!b.dest?.log_dir)
    .map((b) => ({
      key: b.key,
      label: b.label,
      color: b.color,
      runName: runNameFromPath(b.dest!.log_dir),
    }));

  return (
    <>
      <div className="panel">
        <h2>Unified logs</h2>
        <SectionHint>
          One bubble per destination in this fan-out. All on by default - click a bubble
          to hide that destination's lines from the merged view below. The merged
          chronological feed shows every server's <code>runtime.log</code> interleaved by
          timestamp, each line tagged with its server's colour. The underlying log files
          are not modified.
        </SectionHint>
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, marginTop: 8 }}>
          {bubbles.map((b) => {
            const isOn = visible.has(b.key);
            const hasErr = !!b.dest?.error || b.dest?.state === 'failed';
            const isLive = b.dest?.state === 'running';
            return (
              <button
                key={b.key}
                onClick={() => toggle(b.key)}
                title={`${b.dest?.state ?? 'idle'} - click to ${isOn ? 'hide' : 'show'} lines from this destination`}
                style={{
                  padding: '4px 12px',
                  borderRadius: 999,
                  border: `2px solid ${b.color}`,
                  background: isOn ? b.color : 'transparent',
                  color: isOn ? '#fff' : b.color,
                  cursor: 'pointer',
                  fontSize: 12,
                  opacity: isOn ? 1 : 0.55,
                  display: 'inline-flex',
                  alignItems: 'center',
                  gap: 6,
                }}
              >
                {isLive && (
                  <span
                    style={{
                      width: 6, height: 6, borderRadius: '50%',
                      background: isOn ? '#fff' : b.color,
                    }}
                  />
                )}
                {b.label}
                {hasErr && (
                  <span style={{
                    background: 'var(--bad, #d05050)', color: '#fff',
                    borderRadius: 8, padding: '0 6px', fontSize: 11,
                  }}>!</span>
                )}
              </button>
            );
          })}
        </div>
      </div>
      <FanOutMergedLogTailer
        sources={sources}
        visibleKeys={visible}
        jobIsLive={isJobLive(job)}
      />
    </>
  );
}

// Pull the last path component from an absolute or relative path,
// normalising both POSIX and Windows separators. Same approach the
// existing DashboardLogTail uses internally.
function runNameFromPath(p: string): string {
  const parts = p.replace(/\\/g, '/').split('/').filter(Boolean);
  return parts[parts.length - 1] || '';
}

function isJobLive(job: JobPayload | null): boolean {
  if (!job) return false;
  return job.state === 'queued' || job.state === 'running' || job.state === 'stopping';
}

// Multi-source merged log viewer for the fan-out Unified Logs
// sub-tab. Polls each destination's ``runtime.log`` in parallel via
// the same ``api.readLogFile`` primitive ``LogTailer`` uses, merges
// the line streams by their leading ``[YYYY-MM-DD HH:MM:SS]`` prefix,
// and renders them inline with a server-colour-coded prefix per line.
// Hidden via the ``visibleKeys`` filter - the merge runs on the full
// dataset so toggling a bubble doesn't cause flicker, only re-renders
// which lines are shown.
type MergedLogSource = { key: string; label: string; color: string; runName: string };

function FanOutMergedLogTailer({
  sources,
  visibleKeys,
  jobIsLive,
}: {
  sources: MergedLogSource[];
  visibleKeys: Set<string>;
  jobIsLive: boolean;
}) {
  // One body buffer per source, keyed by source.key. Updated by the
  // poll loop below; the merge is recomputed off these whenever any
  // body changes or the visibility filter changes.
  const [bodies, setBodies] = useState<Record<string, string>>({});
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [filter, setFilter] = useState<string>('');
  const offsetsRef = useRef<Record<string, number>>({});
  const bodyRef = useRef<HTMLDivElement | null>(null);
  const stickyRef = useRef<boolean>(true);

  // Reset offsets and bodies when the set of source run dirs changes.
  // We key the effect on a stable concatenated string of (key, runName)
  // pairs to detect actual changes - sources is rebuilt every render.
  const sourcesSig = sources.map((s) => `${s.key}:${s.runName}`).join('|');
  useEffect(() => {
    offsetsRef.current = {};
    setBodies({});
    setErrors({});
  }, [sourcesSig]);

  // Poll loop - fetch each source in parallel every 2 s. Freezes when
  // the job is no longer live so a finished run doesn't keep hammering
  // ``/api/logs/.../runtime.log`` (consistent with LogTailer's
  // ``externalFreeze`` behaviour).
  useEffect(() => {
    if (sources.length === 0) return;
    let cancelled = false;
    const TAIL_POLL_MS = 2000;

    const pollOne = async (s: MergedLogSource) => {
      if (!s.runName) return;
      const off = offsetsRef.current[s.key] ?? 0;
      try {
        const r = await api.readLogFile(s.runName, 'runtime.log', off);
        if (cancelled) return;
        offsetsRef.current[s.key] = r.next_offset;
        // Append (or replace if first read) - match LogTailer semantics.
        setBodies((prev) => ({
          ...prev,
          [s.key]: (off === 0 ? '' : prev[s.key] || '') + r.content,
        }));
        setErrors((prev) => {
          if (!(s.key in prev)) return prev;
          const { [s.key]: _, ...rest } = prev;
          return rest;
        });
      } catch (e) {
        if (cancelled) return;
        // Suppress 404 (the run dir gets renamed _PASS / _FAIL when
        // the destination finishes - the tail freeze below picks
        // that up via jobIsLive on the next tick).
        const msg = String(e);
        if (msg.includes('404')) return;
        setErrors((prev) => ({ ...prev, [s.key]: msg }));
      }
    };

    const tick = () => {
      for (const s of sources) void pollOne(s);
    };
    tick();
    if (!jobIsLive) return () => { cancelled = true; };
    const stop = pausableInterval(tick, TAIL_POLL_MS);
    return () => {
      cancelled = true;
      stop();
    };
  }, [sourcesSig, jobIsLive, sources]);

  // Merge: split each source body into lines, tag with source key,
  // parse the leading ``[YYYY-MM-DD HH:MM:SS]`` timestamp, sort by
  // timestamp ascending. Lines without a timestamp (rare - Python
  // tracebacks etc.) sort just after the preceding timestamped line
  // they belong to via stable sort + carrying the previous timestamp.
  const mergedLines = useMemo(() => {
    type Line = { key: string; ts: string; text: string };
    const out: Line[] = [];
    for (const s of sources) {
      const body = bodies[s.key];
      if (!body) continue;
      let lastTs = '';
      for (const raw of body.split('\n')) {
        if (!raw) continue;
        const m = raw.match(/^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]/);
        if (m) lastTs = m[1];
        out.push({ key: s.key, ts: lastTs, text: raw });
      }
    }
    out.sort((a, b) => (a.ts < b.ts ? -1 : a.ts > b.ts ? 1 : 0));
    return out;
  }, [bodies, sourcesSig]);

  // Apply both the visibility filter (bubble multi-select) and the
  // optional keyword filter (search box). Keyword filter is the same
  // case-insensitive substring match LogTailer uses.
  const visibleLines = useMemo(() => {
    const kw = filter.trim().toLowerCase();
    return mergedLines.filter((l) => {
      if (!visibleKeys.has(l.key)) return false;
      if (kw && !l.text.toLowerCase().includes(kw)) return false;
      return true;
    });
  }, [mergedLines, visibleKeys, filter]);

  // Sticky-bottom scroll behaviour mirrors LogTailer's.
  useEffect(() => {
    if (!stickyRef.current || !bodyRef.current) return;
    bodyRef.current.scrollTop = bodyRef.current.scrollHeight;
  }, [visibleLines.length]);

  const onScroll = () => {
    const el = bodyRef.current;
    if (!el) return;
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 8;
    stickyRef.current = atBottom;
  };

  const colorFor = (key: string) =>
    sources.find((s) => s.key === key)?.color ?? 'var(--text)';
  const labelFor = (key: string) =>
    sources.find((s) => s.key === key)?.label ?? key;
  const errorBanner = Object.entries(errors).filter(([, msg]) => !!msg);

  return (
    <div className="panel">
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8 }}>
        <h2 style={{ margin: 0 }}>Live merged log</h2>
        <input
          type="text"
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          placeholder="Filter lines (case-insensitive)"
          style={{ width: 260, fontSize: 12 }}
        />
      </div>
      {errorBanner.length > 0 && (
        <div className="banner error" style={{ marginBottom: 8, fontSize: 12 }}>
          Some log files failed to load: {errorBanner.map(([k, m]) => `${labelFor(k)}: ${m}`).join('; ')}
        </div>
      )}
      <div
        ref={bodyRef}
        onScroll={onScroll}
        className="mono"
        style={{
          height: 480,
          overflowY: 'auto',
          background: 'var(--panel-alt, #0f1422)',
          padding: '8px 12px',
          borderRadius: 6,
          fontSize: 12,
          lineHeight: 1.5,
          whiteSpace: 'pre-wrap',
        }}
      >
        {visibleLines.length === 0 ? (
          <div className="empty" style={{ padding: 0 }}>
            {sources.length === 0
              ? 'No destination log directories yet - waiting for the fan-out to start.'
              : visibleKeys.size === 0
                ? 'No destinations selected - toggle a bubble above to see its lines.'
                : 'No log entries match the current filter.'}
          </div>
        ) : (
          visibleLines.map((l, i) => (
            <div key={i}>
              <span style={{ color: colorFor(l.key), fontWeight: 600 }}>
                [{labelFor(l.key)}]
              </span>{' '}
              <span>{l.text}</span>
            </div>
          ))
        )}
      </div>
    </div>
  );
}

function badgeColorForState(state: string): string {
  switch (state) {
    case 'running':   return '#4a7afc';
    case 'completed': return '#3ba56d';
    case 'failed':    return '#d05050';
    case 'cancelled': return '#888';
    case 'queued':    return '#d4b13e';
    default:          return '#888';
  }
}


// ── Job header ────────────────────────────────────────────────────────────────

// Source / destination endpoints derived from a job's params dict.
// One helper so JobHeader (the mode chip) and <JobEndpoints> (the
// line under the title) always agree on what counts as a fan-out.
function deriveEndpoints(job: JobPayload | null) {
  const params = (job?.params ?? {}) as Record<string, unknown>;
  const sourceServer =
    typeof params['source_server_name'] === 'string' ? params['source_server_name'] : undefined;
  const destServer =
    typeof params['dest_server_name'] === 'string' ? params['dest_server_name'] : undefined;
  const destServersList: string[] = Array.isArray(params['dest_server_names'])
    ? (params['dest_server_names'] as unknown[]).filter((x): x is string => typeof x === 'string')
    : [];
  const inputFiles = Array.isArray(params['input_files'])
    ? (params['input_files'] as unknown[]).filter((x) => typeof x === 'string').length
    : 0;
  // Restore-from-snapshot path. ``snapshot_id`` is the registered
  // snapshot's stable id; the dashboard looks the row up via
  // api.listSnapshots() so the restore line can show "Snapshot from
  // <server>" with the captured-at date underneath.
  //
  // The submit handler in server/app.py pops ``snapshot_id`` off the
  // request body and re-stamps it as ``_imported_from_snapshot_id``
  // before queueing the run record (line ~2641 in app.py). Both keys
  // are checked here so the lookup resolves regardless of which
  // submit path queued the run.
  const snapshotId =
    typeof params['snapshot_id'] === 'string'
      ? params['snapshot_id']
      : typeof params['_imported_from_snapshot_id'] === 'string'
        ? params['_imported_from_snapshot_id']
        : undefined;
  const isFanOut = destServersList.length > 1;
  return { sourceServer, destServer, destServersList, inputFiles, snapshotId, isFanOut };
}

// The source / destination line under the job title. ONE component
// for all four job shapes:
//   snapshot -> "Source: X"
//   restore  -> "Snapshot used: N file(s) -> Restoring to: Y"
//   direct   -> "Source: X -> Destination: Y"
//   fan-out  -> "Source: X -> Destinations (N): Y, Z, ..."
//
// The "source" of a restore is a snapshot file, not a server, so the
// restore shape surfaces "Snapshot used" + the export file count
// rather than a "Destination · N export files" rendering.
function JobEndpoints({ job }: { job: JobPayload | null }) {
  const { sourceServer, destServer, destServersList, inputFiles, snapshotId, isFanOut } =
    deriveEndpoints(job);
  // Look up the source snapshot's metadata so the restore line can
  // show "Snapshot from <server>" + captured-at date rather than an
  // "N export file(s)" count. Fetches once when the snapshot_id
  // changes (per current-job switch).
  const [snapshotMeta, setSnapshotMeta] = useState<
    {
      server_name: string;
      captured_at: number;
      snapshot_name: string;
      file_path: string;
      libraries: string[];
    } | null
  >(null);
  useEffect(() => {
    if (!snapshotId) {
      setSnapshotMeta(null);
      return;
    }
    let cancelled = false;
    api.listSnapshots()
      .then((r) => {
        if (cancelled) return;
        const match = (r.snapshots || []).find((s) => s.id === snapshotId);
        if (match) {
          setSnapshotMeta({
            server_name: match.server_name,
            captured_at: match.captured_at,
            snapshot_name: match.snapshot_name,
            file_path: match.file_path,
            libraries: Array.isArray(match.libraries) ? match.libraries : [],
          });
        } else {
          setSnapshotMeta(null);
        }
      })
      .catch(() => {
        if (!cancelled) setSnapshotMeta(null);
      });
    return () => { cancelled = true; };
  }, [snapshotId]);
  if (!sourceServer && !destServer && !isFanOut) return null;
  return (
    <div style={{ marginTop: 8, fontSize: 13, color: 'var(--text-dim)' }}>
      {isFanOut ? (
        <>
          {sourceServer && (
            <>
              <span>Source: <strong style={{ color: 'var(--text)' }}>{sourceServer}</strong></span>
              <span style={{ margin: '0 8px' }}>→</span>
            </>
          )}
          <span>Destinations ({destServersList.length}):{' '}</span>
          {destServersList.map((d, i) => (
            <span key={d}>
              <strong style={{ color: 'var(--text)' }}>{d}</strong>
              {i < destServersList.length - 1 && <span>, </span>}
            </span>
          ))}
        </>
      ) : job?.mode === 'direct' && sourceServer && destServer ? (
        <>
          <span>Source: <strong style={{ color: 'var(--text)' }}>{sourceServer}</strong></span>
          <span style={{ margin: '0 8px' }}>→</span>
          <span>Destination: <strong style={{ color: 'var(--text)' }}>{destServer}</strong></span>
        </>
      ) : job?.mode === 'restore' ? (
        <div style={{ display: 'flex', flexWrap: 'wrap', alignItems: 'flex-start', gap: '0 8px' }}>
          <div style={{ display: 'flex', flexDirection: 'column' }}>
            <span style={{ display: 'inline-flex', flexWrap: 'wrap', alignItems: 'center', gap: 6 }}>
              <span>Snapshot from:</span>
              <strong
                style={{ color: 'var(--text)' }}
                title={
                  snapshotMeta
                    ? `${snapshotMeta.snapshot_name}\n${snapshotMeta.file_path}\ncaptured ${new Date(snapshotMeta.captured_at * 1000).toLocaleString()}`
                    : (inputFiles > 0 ? `${inputFiles} export file${inputFiles === 1 ? '' : 's'} on disk` : '')
                }
              >
                {snapshotMeta?.server_name
                  ?? (inputFiles > 0
                    ? `${inputFiles} export file${inputFiles === 1 ? '' : 's'}`
                    : '(none specified)')}
              </strong>
              {/* Library badges: surface the libraries captured in the
                  snapshot so the operator can confirm scope at a glance.
                  Renders inline next to the server name. */}
              {snapshotMeta?.libraries && snapshotMeta.libraries.length > 0 && (
                snapshotMeta.libraries.map((lib) => (
                  <span
                    key={lib}
                    className="tag phase"
                    style={{ fontSize: 10, color: 'var(--text)' }}
                    title={`Library captured in this snapshot: ${lib}`}
                  >
                    {lib}
                  </span>
                ))
              )}
            </span>
            {snapshotMeta?.captured_at && (
              <span style={{ fontSize: 11, color: 'var(--text-dim)', marginTop: 2 }}>
                captured {new Date(snapshotMeta.captured_at * 1000).toLocaleString()}
              </span>
            )}
          </div>
          {destServer && (
            <>
              <span style={{ alignSelf: 'center' }}>→</span>
              <span style={{ alignSelf: 'center' }}>
                Restoring to: <strong style={{ color: 'var(--text)' }}>{destServer}</strong>
              </span>
            </>
          )}
        </div>
      ) : sourceServer ? (
        <span>Source: <strong style={{ color: 'var(--text)' }}>{sourceServer}</strong></span>
      ) : (
        <>
          <span>Destination: <strong style={{ color: 'var(--text)' }}>{destServer}</strong></span>
          {inputFiles > 0 && (
            <span style={{ marginLeft: 8 }}>
              · {inputFiles} export file{inputFiles === 1 ? '' : 's'}
            </span>
          )}
        </>
      )}
    </div>
  );
}

// Self-ticking elapsed-time display. Renders secondsToHMS(ref - start)
// and, while the job is still in flight (endSec === null), re-renders
// only itself once a second. Isolating the tick in this leaf keeps the
// live elapsed counter from re-rendering the whole dashboard panel.
function ElapsedClock({ startSec, endSec }: { startSec: number; endSec: number | null }) {
  useNowTick(1000, endSec === null);
  const ref = endSec ?? Date.now() / 1000;
  return <>{secondsToHMS(ref - startSec)}</>;
}

function JobHeader({ job, dash }: { job: JobPayload | null; dash: DashboardState | null }) {
  // Detect "run is over" so elapsed freezes at the final duration
  // instead of climbing while the post-finish snapshot is retained
  // on-screen, and ETA returns to '-' (extrapolating a remaining
  // time after the run finished is meaningless).
  const runIsOver = !!job && (
    job.state === 'completed' ||
    job.state === 'completed_with_errors' ||
    job.state === 'failed' ||
    job.state === 'cancelled'
  );
  // Both totalCount and completedCount read from
  // ``dash.libraries[].{total,completed}`` - the exact same fields
  // the Libraries section below reads. They must NOT read
  // completedCount from ``dash.completed/skipped/failed``, which are
  // the engine's resolution-pipeline counters (incremented by
  // _record_success/_record_failure on matched items): those are a
  // different concept from per-library progress (advanced by
  // advance_library at phase boundaries) and would make the header
  // lag behind the per-library rows. With both summands from the
  // same source, the header progress stays in step with the
  // per-library table.
  const totalCount = dash ? dash.libraries.reduce((acc, l) => acc + l.total, 0) : 0;
  const completedCount = dash ? dash.libraries.reduce((acc, l) => acc + l.completed, 0) : 0;
  const pct = totalCount > 0 ? completedCount / totalCount : 0;
  // ── Top-level ETA ──
  //
  // The dashboard shows no live "Estimated remaining" row. The Run
  // Job form's pre-submit prediction is the only ETA shown to the
  // end user. A live-updating remaining-time number during the run
  // is too noisy to be useful - completion fraction is item-weighted,
  // not work-time-weighted, so a quick early phase craters the
  // displayed value. The backend still computes predicted_etr_seconds
  // and surfaces it on the wire for any consumer that wants it (logs,
  // future telemetry, etc.); the dashboard just does not display it.

  // Source / destination endpoints come from the job params dict.
  // ``deriveEndpoints`` is the single source of truth, shared with
  // the <JobEndpoints> line rendered below.
  const { isFanOut } = deriveEndpoints(job);
  // Display label for the mode chip. ``direct`` (single) keeps the
  // existing label; multi-dest direct or import surfaces as FAN-OUT.
  const modeLabel = job
    ? (isFanOut ? 'FAN-OUT' : job.mode.toUpperCase())
    : '';

  // Header context.
  const queuedLibs = extractQueuedLibraries(job);
  // Active libraries are derived from the dashboard's per-library
  // statuses: there is no separate ``current_library`` field because
  // multiple libraries can be active at once under ``lib_pool``
  // concurrency, and the libraries[] array already models that.
  const activeLibs = dash ? dash.libraries.filter((l) => l.status === 'active').map((l) => l.name) : [];
  // Current user is the raw identifier from the engine. The display
  // name lookup is purely a rendering concern  backend logs and
  // engine logic always use the raw identifier.
  const rawCurrentUser = dash?.current_user ?? null;
  const currentUserDisplay = rawCurrentUser
    ? (dash?.user_display_names?.[rawCurrentUser] || rawCurrentUser)
    : null;

  const confirm = useConfirm();
  const onStop = async () => {
    // Stop now (a) signals the running job to halt at the next
    // item-level checkpoint and (b) clears every queued job. Both are
    // hard to undo (a half-finished run, a wiped queue), so confirm.
    const ok = await confirm({
      body:
        'Stop will halt the running job at the next safe checkpoint ' +
        'and remove every queued job. The running job ends as ' +
        'Cancelled; anything captured so far is kept.\n\n' +
        'Continue?',
      danger: true,
    });
    if (!ok) return;
    try { await api.stopJob(false); } catch { /* surface elsewhere */ }
  };
  const onHardStop = async () => {
    // Confirm because this is a destructive operation (in-flight
    // items become failures) and there's no undo.
    const ok = await confirm({
      body:
        'Hard stop will tear down the HTTP session to the Plex server immediately ' +
        'and clear every queued job. Any items currently being processed will be ' +
        'recorded as failures in this run\'s logs. Use this only when the regular ' +
        'Stop has been pending too long.\n\n' +
        'Continue?',
      danger: true,
    });
    if (!ok) return;
    try { await api.stopJob(true); } catch { /* surface elsewhere */ }
  };
  // The Stop button accepts a single click while running and then
  // shows "Stopping…" + disables until the engine actually returns.
  // The engine checks the stop signal at item-level checkpoints, so a
  // soft Stop now winds down promptly (seconds) rather than running to
  // the next library boundary  see services/snapshotter.py ::
  // snapshot_watch_history's stop_event checkpoint.
  //
  // Stop / Hard Stop are gated on ``jobs.stop``. Viewer and end user
  // have no Stop permission; their buttons render disabled so the
  // layout stays consistent across roles. The backend
  // (``require_role('manager')`` on /api/job/stop) is the
  // authoritative gate - this is UX-only.
  const canStopJobs = usePermission('jobs.stop');
  const stopping = job?.state === 'stopping';
  const stopDisabled = !canStopJobs || !job || (job.state !== 'running' && !stopping);
  const stopLabel = stopping ? 'Stopping…' : 'Stop Job';
  const hardStopDisabled = !canStopJobs || !job || (job.state !== 'running' && !stopping);

  return (
    <div className="panel">
      <div className="row" style={{ alignItems: 'center' }}>
        <div className="col">
          <h2 style={{ margin: 0 }}>Current Job</h2>
          <div style={{ marginTop: 6, fontSize: 20, fontWeight: 600 }}>
            {job ? (
              <>
                <span>{modeLabel}</span>
                <span style={{ marginLeft: 12 }}><JobBadge state={job.state} /></span>
              </>
            ) : (
              <span style={{ color: 'var(--text-dim)' }}>Idle</span>
            )}
          </div>
          <JobEndpoints job={job} />
        </div>
        <div className="col">
          <div className="kv">
            {/* Start / End time stamps. The ``started_at`` field is
                set by the worker thread at the moment the engine
                actually begins; ``finished_at`` is stamped on
                terminal states (completed / failed / cancelled).
                Both are unix seconds on the wire and rendered in the
                end user's local timezone here. */}
            <div className="k">Start time</div>
            <div className="v">{formatAbsoluteTimestamp(job?.started_at ?? null)}</div>
            <div className="k">End time</div>
            <div className="v">
              {runIsOver
                ? formatAbsoluteTimestamp(job?.finished_at ?? null)
                : <span style={{ color: 'var(--text-dim)' }}>--:--:--</span>}
            </div>
            <div className="k">Elapsed</div><div className="v">{dash ? <ElapsedClock startSec={dash.start_time} endSec={runIsOver && job?.finished_at ? job.finished_at : null} /> : '-'}</div>
            {/* "steps", not "items": totalCount/completedCount sum each
                library's gather-phase count (4 owner data-type gathers +
                one per home user), advanced at phase boundaries by
                advance_library() - they are NOT a media-item tally. The
                real per-type item counts live in the Libraries table's
                Items column. */}
            <div className="k">Progress</div>
            <div className="v" title="Gather-phase steps completed across all libraries (4 data-type gathers + one per home user, per library) - not a media-item count.">
              {completedCount.toLocaleString()} / {totalCount.toLocaleString()} steps ({(pct * 100).toFixed(1)}%)
            </div>
            {/* Queued libraries (from job params), currently-active
                libraries (derived from per-library status), and
                current user (raw id resolved through the cached
                display-name map). Each row is hidden entirely when
                it has nothing to show. */}
            {queuedLibs.length > 0 && (<>
              <div className="k">Libraries queued</div>
              <div className="v">
                {queuedLibs.map((lib) => (
                  <span key={lib} className="tag phase" style={{ marginRight: 4 }}>{lib}</span>
                ))}
              </div>
            </>)}
            {activeLibs.length > 0 && (<>
              <div className="k">Currently processing</div>
              <div className="v">
                {activeLibs.map((lib) => (
                  <span key={lib} className="tag started" style={{ marginRight: 4 }}>{lib}</span>
                ))}
              </div>
            </>)}
            {currentUserDisplay && (<>
              <div className="k">Current user</div>
              <div className="v" title={rawCurrentUser || ''}>{currentUserDisplay}</div>
            </>)}
            {job?.run_log_dir && (<>
              <div className="k">Run log directory</div><div className="v">{job.run_log_dir}</div>
            </>)}
            {job?.error && (<>
              <div className="k">Error</div><div className="v" style={{ color: 'var(--bad)' }}>{job.error}</div>
            </>)}
          </div>
        </div>
        <div
          className="col"
          style={{
            display: 'flex',
            flexDirection: 'column',
            justifyContent: 'flex-start',
            alignItems: 'flex-end',
            gap: 6,
          }}
        >
          <button className="danger" disabled={stopDisabled} onClick={onStop}>{stopLabel}</button>
          {/* Hard Stop. Sits directly under the soft Stop so it's
              discoverable when the soft Stop is hung but isn't the
              default click target. Smaller / dimmer to telegraph
              "this is the escalation, not the normal action". */}
          <button
            className="danger"
            disabled={hardStopDisabled}
            onClick={onHardStop}
            title="Force the running job off by tearing down the HTTP session. In-flight items become failures. Use only when the regular Stop is hung."
            style={{ fontSize: 11, padding: '4px 8px', opacity: 0.85 }}
          >
            Hard Stop (force)
          </button>
        </div>
      </div>
    </div>
  );
}

function JobBadge({ state }: { state: JobPayload['state'] }) {
  const map: Record<JobPayload['state'], string> = {
    idle: 'skipped',
    queued: 'phase',
    running: 'phase',
    stopping: 'unresolved',
    completed: 'done',
    // Amber chip for partial-success - distinct from the green
    // ``done`` chip and the red ``error`` chip. Uses the same
    // ``unresolved`` class the stopping state uses (amber/yellow).
    completed_with_errors: 'unresolved',
    failed: 'error',
    cancelled: 'failed',
  };
  // Friendly label so the UI doesn't shout "COMPLETED_WITH_ERRORS";
  // every other state's display name is its raw string, but this one
  // is long enough to deserve a tweaked rendering.
  const label = state === 'completed_with_errors'
    ? 'COMPLETED WITH ERRORS'
    : state.toUpperCase();
  return <span className={`tag ${map[state]}`}>{label}</span>;
}

// ── Run coverage ──────────────────────────────────────────────────────────────
// "How big is this run?"  counts the run's *scope* (users covered,
// items being touched per category) rather than the per-disposition
// counters in RunStats. The fields come from DashboardState's
// set_user_count and inc_watch / inc_playlist / inc_collection /
// inc_rating, populated by the engine's gather and process loops.

function RunCoverage({ dash }: { dash: DashboardState }) {
  return (
    <div className="panel">
      <h2>Run Coverage</h2>
      <SectionHint>How big this run is the user count and the totals for each category of data being touched.</SectionHint>
      <div className="grid-4">
        <Stat label="Users (incl. owner)" value={dash.home_user_count ?? 0} />
        <Stat label="Watched items" value={dash.watch_count ?? 0} />
        <Stat label="Playlists" value={dash.playlist_count ?? 0} />
        <Stat label="Collections" value={dash.collection_count ?? 0} />
        <Stat label="Ratings" value={dash.rating_count ?? 0} />
      </div>
    </div>
  );
}

// ── Currently Processing ──────────────────────────────────────────────────────
// One row per active worker thread, showing exactly what each worker
// is doing right now. Updated at the 4 Hz WS tick  transient items
// flash by but multi-second items stay long enough to read.

function CurrentlyProcessing({ items }: { items: CurrentItem[] }) {
  const { etrColorMultiplier } = useContext(DashboardConfigContext);
  // Self-tick once a second so each row's phase-age + stall colour
  // stay live. Scoped to this component so the rest of the dashboard
  // doesn't re-render on the tick; only runs while items are in flight.
  useNowTick(1000, items.length > 0);
  // Cap visible rows; very large worker pools (32+) would otherwise
  // push the rest of the dashboard off-screen.
  const MAX_ROWS = 10;
  const visible = items.slice(0, MAX_ROWS);
  const hiddenCount = items.length - visible.length;
  const now = Date.now() / 1000;

  return (
    <div className="panel">
      <h2>Currently Processing</h2>
      {items.length === 0 ? (
        <div className="empty">No items in flight.</div>
      ) : (
        <table className="list">
          <thead>
            <tr>
              <th style={{ width: '16%' }}>Library</th>
              <th style={{ width: '10%' }}>Type</th>
              <th style={{ width: '12%' }}>Phase</th>
              <th>Title</th>
              <th style={{ width: '10%' }}>Phase age</th>
            </tr>
          </thead>
          <tbody>
            {visible.map((ci) => {
              const phaseAge = ci.phase_started_at
                ? Math.max(0, now - ci.phase_started_at)
                : null;
              const health = phaseAge !== null && ci.phase
                ? getPhaseHealth(ci.phase, phaseAge, etrColorMultiplier)
                : 'normal';
              const phaseClass = health === 'normal'
                ? `tag ${PHASE_TAG[ci.phase ?? ''] ?? 'phase'}`
                : `tag stall-${health}`;
              const phaseAgeColor = health === 'red'
                ? 'var(--err, #dc2626)'
                : health === 'amber'
                  ? 'var(--warn, #d97706)'
                  : 'var(--text-dim)';
              return (
                // FECORE-11: stable composite key derived from the row's own fields
                <tr key={`${ci.library}|${ci.title}|${ci.started_at}`}>
                  <td style={{ color: 'var(--text-dim)' }}>{ci.library}</td>
                  <td className="mono">{ci.type}</td>
                  <td>
                    {ci.phase
                      ? <span className={phaseClass}>{ci.phase}</span>
                      : <span style={{ color: 'var(--text-dim)' }}>-</span>}
                  </td>
                  <td>{ci.title}</td>
                  <td className="mono" style={{ color: phaseAgeColor }}>
                    {phaseAge !== null ? secondsToHMS(phaseAge) : '-'}
                  </td>
                </tr>
              );
            })}
            {hiddenCount > 0 && (
              <tr>
                <td colSpan={5} style={{ color: 'var(--text-dim)', textAlign: 'center' }}>
                  +{hiddenCount} more worker{hiddenCount === 1 ? '' : 's'} not shown
                </td>
              </tr>
            )}
          </tbody>
        </table>
      )}
    </div>
  );
}

// Per-phase stall thresholds. Normal phase durations vary by orders
// of magnitude (a rating write is fast, an
// indexing pass on a big music library can legitimately take 90s),
// so the colour escalation has to be per-phase rather than a single
// global threshold.
const STALL_THRESHOLDS: Record<string, { amber: number; red: number }> = {
  fetching:   { amber: 45,  red: 120 },
  capturing:  { amber: 30,  red: 60  },
  indexing:   { amber: 45,  red: 90  },
  resolving:  { amber: 30,  red: 75  },
  scrobbling: { amber: 20,  red: 45  },
  rating:     { amber: 15,  red: 30  },
  merging:    { amber: 30,  red: 60  },
};

function getPhaseHealth(
  phase: string, ageSeconds: number, multiplier: number,
): 'normal' | 'amber' | 'red' {
  const base = STALL_THRESHOLDS[phase] ?? { amber: 60, red: 120 };
  const t = {
    amber: base.amber * multiplier,
    red: base.red * multiplier,
  };
  if (ageSeconds >= t.red) return 'red';
  if (ageSeconds >= t.amber) return 'amber';
  return 'normal';
}

// Phase → tag-style class. Reuses the existing tag colour vocabulary
// (the same one ActivityFeed uses) so the dashboard stays visually
// consistent without inventing new CSS classes. ``capturing`` has its
// own violet swatch (see --color-capturing in styles.css) so a glance
// at Currently Processing distinguishes snapshot-time work from
// import-time work.
const PHASE_TAG: Record<string, string> = {
  capturing:  'capturing',
  resolving:  'phase',
  scrobbling: 'merged',
  rating:     'rated',
  merging:    'appended',
  indexing:   'phase',
  fetching:   'phase',
};

// ── Run stats ─────────────────────────────────────────────────────────────────

function RunStats({ dash }: { dash: DashboardState }) {
  return (
    <div className="panel">
      <h2>Run Stats</h2>
      <SectionHint>Outcome counts as items move through the pipeline.</SectionHint>
      <div className="grid-4">
        <Stat label="Completed" value={dash.completed} variant="good" />
        <Stat label="Skipped"   value={dash.skipped} />
        <Stat label="Failed"    value={dash.failed} variant="bad" />
        {/* "Unresolved" intentionally lives ONLY in Match Resolution
            - it's a resolution-tier outcome (restore / direct), not
            a universal pipeline count. A snapshot has no "unresolved"
            concept, and Match Resolution is hidden on snapshot runs,
            so the count simply doesn't surface there. */}
      </div>
    </div>
  );
}

// ── Per-batch ETR ─────────────────────────────────────────────────────────────
//
// One row per leaf-type the engine has reported on. Each batch has its
// own independent rolling tracker on the backend, so the values here
// don't pollute each other (playlist work and movie work have very
// different per-item cost). Hides itself entirely when the engine
// hasn't pushed any batch data, so older snapshot / restore runs that
// don't plumb the timing engine render the dashboard unchanged.

function BatchEtrPanel({ dash }: { dash: DashboardState }) {
  const batches = dash.batch_etrs ?? {};
  const entries = Object.entries(batches);
  if (entries.length === 0) return null;

  // Sort by progress descending so the most-active batch reads first.
  entries.sort((a, b) => {
    const aPct = a[1].total > 0 ? a[1].completed / a[1].total : 0;
    const bPct = b[1].total > 0 ? b[1].completed / b[1].total : 0;
    return bPct - aPct;
  });

  return (
    <div className="panel">
      <h2>Process List</h2>
      <span className="help" style={{ display: 'block', color: 'var(--text-dim)', fontSize: 12, marginBottom: 8 }}>
        Per-batch progress. Each batch is enumerated as the engine
        discovers real work, so totals grow until every item type has
        been counted.
      </span>
      <table className="list" style={{ width: '100%' }}>
        <thead>
          <tr>
            <th style={{ width: '20%' }}>Batch</th>
            <th style={{ width: '20%' }}>Progress</th>
            <th>Bar</th>
            <th style={{ width: '15%' }}>Complete</th>
          </tr>
        </thead>
        <tbody>
          {entries.map(([kind, info]) => {
            const total = info.total || 0;
            const completed = info.completed || 0;
            const pct = total > 0 ? Math.min(1, completed / total) : 0;
            // The total grows as the engine enumerates real work
            // (discover-don't-predict). Until any work for this batch
            // has been discovered, ``total`` is 0  there's no
            // denominator to show and "x / 0" would read as broken, so
            // we show just the running completed count.
            const hasTotal = total > 0;
            return (
              <tr key={kind}>
                <td className="mono">{kind}</td>
                <td className="mono">
                  {hasTotal ? (
                    `${completed.toLocaleString()} / ${total.toLocaleString()}`
                  ) : (
                    <>
                      {completed.toLocaleString()}
                      <span style={{ color: 'var(--text-dim)', marginLeft: 6 }}>
                        discovering
                      </span>
                    </>
                  )}
                </td>
                <td>
                  {hasTotal ? (
                    <div style={{
                      height: 8, borderRadius: 4,
                      background: 'var(--panel-alt, #1b2233)',
                      overflow: 'hidden',
                    }}>
                      <div style={{
                        width: `${pct * 100}%`,
                        height: '100%',
                        background: 'var(--color-phase, #58a6ff)',
                        transition: 'width 0.4s ease-out',
                      }} />
                    </div>
                  ) : null}
                </td>
                <td className="mono">{hasTotal ? `${(pct * 100).toFixed(1)}%` : '-'}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}


// ── Match resolution ──────────────────────────────────────────────────────────

function MatchStats({ dash }: { dash: DashboardState }) {
  return (
    <div className="panel">
      <h2>Match Resolution</h2>
      <SectionHint>How items are matched between source and destination.</SectionHint>
      <div className="grid-4">
        <Stat label="GUID"      value={dash.guid_hits} />
        <Stat label="Filepath"  value={dash.filepath_hits} />
        <Stat label="Suffix"    value={dash.suffix_hits} />
        <Stat label="Fuzzy"     value={dash.fuzzy_hits} />
        <Stat label="Unresolved" value={dash.unresolved} variant="bad" />
      </div>
    </div>
  );
}

// ── Thread pool ───────────────────────────────────────────────────────────────

function ThreadPool({ dash }: { dash: DashboardState }) {
  // Aggregate thread → category counts, identical to _build_dashboard.
  const cats: Record<string, number> = {};
  for (const cat of Object.values(dash.threads)) {
    const label = THREAD_LABELS[cat] ?? cat;
    cats[label] = (cats[label] ?? 0) + 1;
  }
  const entries = Object.entries(cats).sort();
  const activeWorkers = Object.keys(dash.threads).length;

  return (
    <div className="panel">
      <h2>Thread Pool</h2>
      <SectionHint>Worker threads currently active, grouped by what they're doing.</SectionHint>
      <div className="kv" style={{ marginBottom: 8 }}>
        <div className="k">Active workers</div><div className="v">{activeWorkers}</div>
        <div className="k">Status</div><div className="v">{dash.paused ? 'PAUSED' : 'Running'}</div>
      </div>
      {entries.length > 0 ? (
        <div>
          {entries.map(([lbl, n]) => (
            // Render the category label + count followed by a
            // plain-English description so a user unfamiliar with the
            // pipeline stages can tell what each group of threads is
            // currently doing. The pill flows inline.
            <span key={lbl} className="thread-pill" title={THREAD_DESCRIPTIONS[lbl] ?? ''}>
              <span className="thread-pill-label">{lbl} × {n}</span>
              {THREAD_DESCRIPTIONS[lbl] && (
                <span className="thread-pill-desc"> - {THREAD_DESCRIPTIONS[lbl]}</span>
              )}
            </span>
          ))}
        </div>
      ) : (
        <div className="empty">No worker threads active.</div>
      )}
    </div>
  );
}

const THREAD_LABELS: Record<string, string> = {
  watched: 'Watched',
  play_count: 'Play Count',
  playlists: 'Playlists',
  collections: 'Collections',
  ratings: 'Ratings',
  scan_cache: 'Scan Cache',
  home_user: 'Home User',
  snapshot: 'Capturing snapshot',
};

// Plain-English descriptions for each thread category. Keyed by the
// *display label* produced by THREAD_LABELS above so the lookup in
// the JSX uses the same string the user sees.
const THREAD_DESCRIPTIONS: Record<string, string> = {
  'Watched':     'Reading or writing TV / movie view counts and resume positions',
  'Play Count':  'Reading or writing music track play counts',
  'Playlists':   'Reading or merging playlists and their items',
  'Collections': 'Reading or merging collections and their members',
  'Ratings':     'Reading or writing star ratings on items',
  'Scan Cache':  'Building the file-path lookup table used by matching tiers 2 and 3',
  'Home User':   'Fetching or applying data for one Plex Home managed user',
  'Capturing snapshot':   'Reading and serialising one library’s data',
};

// ── Per-library progress ──────────────────────────────────────────────────────

// Per-snapshot-library the "total" the engine emits is structured as
// ``4 + n_user_tasks`` - four owner-phase gathers (watch history,
// ratings, playlists, collections) plus one per managed user. Threaded
// out here so the LibraryList cell can show the two-axis breakdown
// ("phases 4/4 · users 8/12") that makes the formula self-documenting,
// instead of a bare "12/16" the operator has to puzzle out.
const SNAPSHOT_OWNER_PHASES = 4;

function LibraryList({ libs, job }: { libs: LibraryProgress[]; job: JobPayload | null }) {
  // Lightweight stop-state: when the operator stops the run,
  // libraries that didn't reach 100% read "Stopped" (red) instead of a
  // misleading percentage; libraries that never started read "Skipped"
  // (neutral); libraries that genuinely completed BEFORE the stop stay
  // green. job.state === 'cancelled' is the canonical signal - same one
  // the job status badge already uses.
  const jobStopped = job?.state === 'cancelled';
  // "Steps" column: the per-library count is 4 owner-phase gathers +
  // N user gathers in snapshot / direct-transfer mode. The breakdown
  // is meaningful only on those modes; restore counts items and the
  // cell falls back to the "completed / total" shape.
  const stepsModeBreakdown = job?.mode === 'snapshot' || job?.mode === 'direct';
  return (
    <div className="panel">
      <h2>Libraries</h2>
      <SectionHint>Per-library progress with step counts and current phase.</SectionHint>
      {libs.length === 0 ? (
        <div className="empty">No libraries in this run.</div>
      ) : (
        <table className="list">
          <thead>
            <tr>
              <th style={{ width: '20%' }}>Library</th>
              <th style={{ width: '36%' }}>Progress</th>
              {/* "Steps" column with a formula tooltip. Each
                  per-library step is either one of the four
                  owner-phase gathers (watch history / ratings /
                  playlists / collections) or one managed-user gather,
                  so "8/16" on a snapshot run means "8 of (4 owner
                  phases + 12 user gathers) done." On restore /
                  direct-transfer-import, steps are items being
                  processed - end user-visible breakdown adapts per
                  mode via the cell content below. */}
              <th style={{ width: '16%', textAlign: 'right' }}>
                Steps
                <InfoTip>
                  Snapshot / direct-transfer:{' '}
                  <strong>4 owner-phase gathers</strong> (watch history, ratings, playlists,
                  collections) plus{' '}
                  <strong>one step per managed user</strong>{' '}
                  whose tokens are active.
                  <br />
                  Restore: one step per item resolved or applied.
                </InfoTip>
              </th>
              {/* No ETA column in the per-library table. An ETA
                  would be anchored on a different step-count
                  denominator than the progress bar, which would flip
                  libraries to "Done" while the visible progress was
                  still ~71% and turn the bar green prematurely. */}
              <th style={{ width: '18%' }}>Phase</th>
            </tr>
          </thead>
          <tbody>
            {libs.map((lib) => {
              const total = lib.total || 1;
              const pct = total > 0 ? lib.completed / total : 0;
              // Stop-state classification for each row. Order matters:
              // an explicit engine-side error always wins; otherwise a
              // partially-complete library on a cancelled job reads
              // "Stopped", a never-started one reads "Skipped".
              const neverStarted = jobStopped && lib.status === 'queued';
              const stoppedShort = jobStopped && !neverStarted
                && lib.status !== 'error' && pct < 1.0;
              const barClass = lib.status === 'error' || stoppedShort
                ? 'error'
                : (lib.status === 'done' ? 'done' : '');
              const barText = neverStarted
                ? 'Skipped'
                : stoppedShort
                  ? 'Stopped'
                  : `${(pct * 100).toFixed(0)}%`;
              // Two-axis cell content for snapshot / direct: derive
              // phases vs user-tasks from the engine's "4 + N" total
              // (snapshotter.py :: snapshot_library). Owner phases fill
              // before user gathers (Phase 1 / Phase 2 split), so the
              // completed-count first satisfies the 4 phases, then
              // counts towards user gathers.
              const showBreakdown = stepsModeBreakdown && lib.total >= SNAPSHOT_OWNER_PHASES;
              const userTotal = Math.max(0, lib.total - SNAPSHOT_OWNER_PHASES);
              const phasesDone = Math.min(SNAPSHOT_OWNER_PHASES, lib.completed);
              const usersDone = Math.max(0, lib.completed - SNAPSHOT_OWNER_PHASES);
              const stepsCell = showBreakdown
                ? `phases ${phasesDone}/${SNAPSHOT_OWNER_PHASES} · users ${usersDone}/${userTotal}`
                : `${lib.completed.toLocaleString()} / ${lib.total.toLocaleString()}`;
              return (
                <tr key={lib.name}>
                  <td>{lib.name} <StatusDot status={lib.status} /></td>
                  <td>
                    <div className={`bar-outer ${barClass}`}>
                      <div className="bar-inner" style={{ width: `${Math.min(100, pct * 100)}%` }} />
                      <div className="bar-text">{barText}</div>
                    </div>
                  </td>
                  <td className="num">{stepsCell}</td>
                  <td>{lib.phase || '-'}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </div>
  );
}

function StatusDot({ status }: { status: LibraryProgress['status'] }) {
  const cls = status === 'done' ? 'green' : status === 'error' ? 'red' : status === 'active' ? 'amber' : '';
  return cls ? <span className={`dot ${cls}`} style={{ marginLeft: 6 }} /> : null;
}

// ── Activity feed ─────────────────────────────────────────────────────────────

function ActivityFeed({ entries }: { entries: ActivityEntry[] }) {
  // Newest at top  the underlying deque is oldest-first, so reverse.
  const ordered = [...entries].reverse();
  return (
    <div className="panel">
      <h2>Activity Feed</h2>
      <SectionHint>The most recent pipeline events, newest at the top.</SectionHint>
      {ordered.length === 0 ? (
        <div className="empty">No events yet.</div>
      ) : (
        <div className="feed">
          {ordered.map((e) => (
            // FECORE-11: stable composite key derived from the entry's own fields
            <div className="row" key={`${e.timestamp}|${e.action_type}|${e.library}|${e.title}`}>
              <span style={{ color: 'var(--text-dim)' }}>{e.timestamp}</span>
              <span className={`tag ${TAG_FOR_ACTION[e.action_type] ?? 'phase'}`}>
                {LABEL_FOR_ACTION[e.action_type] ?? e.action_type.toUpperCase()}
              </span>
              <span style={{ color: 'var(--text-dim)' }}>{e.library}</span>
              <span>{e.title}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

const TAG_FOR_ACTION: Record<string, string> = {
  merged: 'merged',
  created: 'created',
  appended: 'appended',
  skipped: 'skipped',
  rating_set: 'rated',
  failed: 'failed',
  unresolved: 'unresolved',
  phase: 'phase',
  started: 'started',
  done: 'done',
  error: 'error',
};

const LABEL_FOR_ACTION: Record<string, string> = {
  merged: 'MERGED',
  created: 'CREATED',
  appended: 'APPENDED',
  skipped: 'SKIPPED',
  rating_set: 'RATED',
  failed: 'FAILED',
  unresolved: 'UNRESOLVED',
  phase: 'PHASE',
  started: 'STARTED',
  done: 'DONE',
  error: 'ERROR',
};

// ── Tiny shared helpers ───────────────────────────────────────────────────────

// Pull the libraries-queued list straight from the job params. The
// backend broadcasts ``job.params.libraries`` for every job mode
// (snapshot, import, direct), so no new WS field is needed.
// ``import`` mode submits ``input_files`` instead  no per-library
// names available from the form. An empty list also covers the
// legitimate "all libraries on the server" shorthand the engine
// accepts.
export function extractQueuedLibraries(job: JobPayload | null): string[] {
  if (!job) return [];
  const raw = (job.params as Record<string, unknown>)?.libraries;
  if (!Array.isArray(raw)) return [];
  return raw.filter((v): v is string => typeof v === 'string');
}

function SectionHint({ children }: { children: ReactNode }) {
  return (
    <div style={{ marginTop: -4, marginBottom: 10, fontSize: 12, color: 'var(--text-dim)' }}>
      {children}
    </div>
  );
}

function Stat({ label, value, variant }: { label: string; value: number; variant?: 'good' | 'bad' | 'warn' }) {
  return (
    <div className={`stat ${variant ?? ''}`}>
      <div className="label">{label}</div>
      <div className="value">{value.toLocaleString()}</div>
    </div>
  );
}

// True when the API returned a 404 because the run directory has
// been renamed at job end. Used to suppress a transient banner error
// in both the file-list refresh and the LogTailer poll.
export function isRunGoneError(e: unknown): boolean {
  const s = String(e);
  return s.includes('404') && /no such run/i.test(s);
}

function secondsToHMS(s: number): string {
  if (!isFinite(s) || s < 0) return '-';
  const sec = Math.floor(s);
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const ss = sec % 60;
  if (h > 0) return `${h}:${pad2(m)}:${pad2(ss)}`;
  return `${m}:${pad2(ss)}`;
}
function pad2(n: number): string { return n < 10 ? `0${n}` : `${n}`; }

// Render an absolute unix-second timestamp in the end user's local
// timezone. Returns '-' for null / undefined / non-finite inputs so
// the dashboard reads consistently when a job has been queued but
// not yet started, or when ``finished_at`` is still empty.
function formatAbsoluteTimestamp(ts: number | null | undefined): string {
  if (ts === null || ts === undefined || !isFinite(ts) || ts <= 0) return '-';
  return new Date(ts * 1000).toLocaleString();
}
