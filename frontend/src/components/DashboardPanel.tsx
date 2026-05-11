// The live dashboard view.
//
// Mirrors the terminal panel in services/dashboard.py :: _build_dashboard:
//   * Thread pool summary (categorised counts)
//   * Run stats (completed / skipped / failed / unresolved)
//   * Match resolution stats (GUID / filepath / suffix / fuzzy)
//   * Per-library progress bars with ETA
//   * Activity feed (last several events, colour-coded)
//   * Job state badge with elapsed + ETA
//
// Reads only from props — no fetch logic here. The App owns the WS
// subscription and pushes the snapshot down.

import { type ReactNode, useEffect, useMemo, useState } from 'react';
import { ActivityEntry, CurrentItem, DashboardSnapshot, JobPayload, LibraryProgress, LogFile, SnapshotMessage, api } from '../api';
import { LogTailer } from './LogTailer';

interface Props {
  snapshot: SnapshotMessage | null;
  // v0.9.3: connection state from App so the empty-state panel can
  // distinguish "still connecting" from "connected, no job running".
  connState?: 'connecting' | 'connected' | 'disconnected';
}

export function DashboardPanel({ snapshot, connState = 'connected' }: Props) {
  // Re-render every second so the elapsed-time strings tick even when
  // no new snapshot has arrived (e.g. job idle but page still open).
  const [, force] = useState(0);
  useEffect(() => {
    const t = window.setInterval(() => force((x) => x + 1), 1000);
    return () => window.clearInterval(t);
  }, []);

  const dash = snapshot?.dashboard ?? null;
  const job = snapshot?.job ?? null;

  return (
    <>
      <JobHeader job={job} dash={dash} />
      {dash ? (
        <>
          <RunCoverage dash={dash} />
          <RunStats dash={dash} />
          <MatchStats dash={dash} />
          <ThreadPool dash={dash} />
          <CurrentlyProcessing items={dash.current_items ?? []} />
          <LibraryList libs={dash.libraries} now={Date.now() / 1000} />
          <ActivityFeed entries={dash.activity} />
          <DashboardLogTail job={job} />
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
                <div className="empty">No job is running. Start one from the <strong>Run Job</strong> tab.</div>
              );
            })()
          )}
        </div>
      )}
      {!dash && job && <DashboardLogTail job={job} />}
    </>
  );
}

// ── Dashboard live-log tail (v0.9.4) ──────────────────────────────────────────
// Embedded under the JobHeader so users can tail any log file from the
// current run without switching to the Logs tab. The file dropdown
// shows everything in the run dir (runtime.log, errors.log, media.log,
// + per-library success/fail logs once they appear).
//
// File-list refresh: we re-fetch the dropdown every 5 s while the job
// is running because per-library success/fail logs appear *after* each
// library finishes — without a refresh the dropdown would never show
// them mid-run.

function DashboardLogTail({ job }: { job: JobPayload | null }) {
  const [files, setFiles] = useState<LogFile[]>([]);
  const [selected, setSelected] = useState<string>('runtime.log');
  const [listError, setListError] = useState<string | null>(null);

  // Derive the run name from the absolute path in job.run_log_dir.
  // The backend writes paths with the OS separator (Linux in Docker,
  // Windows on a native install) so we normalise both styles.
  const runName = useMemo(() => {
    if (!job?.run_log_dir) return null;
    const parts = job.run_log_dir.replace(/\\/g, '/').split('/').filter(Boolean);
    return parts[parts.length - 1] || null;
  }, [job?.run_log_dir]);

  // The job's run_log_dir field still points at the *pre-rename* path
  // because _finalise_run_dir renames the directory at job end but
  // never updates the JobRecord. When the job state transitions to
  // completed/failed/cancelled we freeze the tail rather than chase
  // the suffixed path — the Logs tab is the right place for post-mortem.
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
          // Swallow that specific case — the tail is about to freeze
          // anyway and a banner error is just noise.
          if (isRunGoneError(e)) { setListError(null); return; }
          setListError(String(e));
        });
    };
    load();
    // Stop refreshing once the job is done — the file set is final.
    if (!jobIsLive) return () => { cancelled = true; };
    const tick = window.setInterval(load, 5000);
    return () => { cancelled = true; window.clearInterval(tick); };
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

// ── Job header ────────────────────────────────────────────────────────────────

function JobHeader({ job, dash }: { job: JobPayload | null; dash: DashboardSnapshot | null }) {
  const elapsed = dash ? secondsToHMS(Date.now() / 1000 - dash.start_time) : '—';
  // v0.9.2 fix for "Current Job shows 0/N and no ETA" bugs.
  // Both totalCount and completedCount now read from
  // ``dash.libraries[].{total,completed}`` — the exact same fields
  // the Libraries section below already reads correctly. The previous
  // build read completedCount from ``dash.completed/skipped/failed``,
  // which are the engine's resolution-pipeline counters (incremented
  // by _record_success/_record_failure on matched items). Those are a
  // different concept from per-library progress (advanced by
  // advance_library at phase boundaries), so the header always lagged
  // behind the per-library rows. With both summands now from the same
  // source, the ETA formula below also starts producing a value
  // because completedCount > 0 lifts pct above the 2% threshold.
  const totalCount = dash ? dash.libraries.reduce((acc, l) => acc + l.total, 0) : 0;
  const completedCount = dash ? dash.libraries.reduce((acc, l) => acc + l.completed, 0) : 0;
  const pct = totalCount > 0 ? completedCount / totalCount : 0;
  // Global ETA: extrapolate from current rate.
  const eta = dash && pct > 0.02 && pct < 1
    ? secondsToHMS(((Date.now() / 1000 - dash.start_time) / pct) * (1 - pct))
    : '—';

  // Pull multi-server names out of the job params dict the server
  // populates per /api/job request. We render them as a "Plex1 →
  // Plex2" badge so the user always knows where data is flowing.
  const params = (job?.params ?? {}) as Record<string, string | undefined>;
  const sourceServer = params['source_server_name'];
  const destServer = params['dest_server_name'];

  const onStop = async () => {
    try { await api.stopJob(); } catch { /* surface elsewhere */ }
  };
  // The Stop button accepts a single click while running and then
  // shows "Stopping…" + disables until the engine actually returns.
  // Without this label change the user couldn't tell their click was
  // received (the engine may take seconds-to-minutes to wind down
  // because it finishes the current library before exiting — see
  // services/exporter.py :: run_export's stop semantics).
  const stopping = job?.state === 'stopping';
  const stopDisabled = !job || (job.state !== 'running' && !stopping);
  const stopLabel = stopping ? 'Stopping…' : 'Stop Job';

  return (
    <div className="panel">
      <div className="row" style={{ alignItems: 'center' }}>
        <div className="col">
          <h2 style={{ margin: 0 }}>Current Job</h2>
          <div style={{ marginTop: 6, fontSize: 20, fontWeight: 600 }}>
            {job ? (
              <>
                <span>{job.mode.toUpperCase()}</span>
                <span style={{ marginLeft: 12 }}><JobBadge state={job.state} /></span>
              </>
            ) : (
              <span style={{ color: 'var(--text-dim)' }}>Idle</span>
            )}
          </div>
          {(sourceServer || destServer) && (
            <div style={{ marginTop: 8, fontSize: 13, color: 'var(--text-dim)' }}>
              {job?.mode === 'direct' && sourceServer && destServer ? (
                <>
                  <span>Source: <strong style={{ color: 'var(--text)' }}>{sourceServer}</strong></span>
                  <span style={{ margin: '0 8px' }}>→</span>
                  <span>Destination: <strong style={{ color: 'var(--text)' }}>{destServer}</strong></span>
                </>
              ) : sourceServer ? (
                <span>Source: <strong style={{ color: 'var(--text)' }}>{sourceServer}</strong></span>
              ) : (
                <span>Destination: <strong style={{ color: 'var(--text)' }}>{destServer}</strong></span>
              )}
            </div>
          )}
        </div>
        <div className="col">
          <div className="kv">
            <div className="k">Elapsed</div><div className="v">{elapsed}</div>
            <div className="k">Estimated remaining</div><div className="v">{eta}</div>
            <div className="k">Progress</div><div className="v">{completedCount.toLocaleString()} / {totalCount.toLocaleString()} items ({(pct * 100).toFixed(1)}%)</div>
            {job?.run_log_dir && (<>
              <div className="k">Run log directory</div><div className="v">{job.run_log_dir}</div>
            </>)}
            {job?.error && (<>
              <div className="k">Error</div><div className="v" style={{ color: 'var(--bad)' }}>{job.error}</div>
            </>)}
          </div>
        </div>
        <div className="col" style={{ display: 'flex', justifyContent: 'flex-end', alignItems: 'flex-start' }}>
          <button className="danger" disabled={stopDisabled} onClick={onStop}>{stopLabel}</button>
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
    failed: 'error',
    cancelled: 'failed',
  };
  return <span className={`tag ${map[state]}`}>{state.toUpperCase()}</span>;
}

// ── Run coverage (v0.9.3) ─────────────────────────────────────────────────────
// "How big is this run?" — counts the run's *scope* (users covered,
// items being touched per category) rather than the per-disposition
// counters in RunStats. The fields come from DashboardState's
// set_user_count and inc_watch / inc_playlist / inc_collection /
// inc_rating, populated by the engine's gather and process loops.

function RunCoverage({ dash }: { dash: DashboardSnapshot }) {
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

// ── Currently Processing (v0.9.3) ─────────────────────────────────────────────
// One row per active worker thread, showing exactly what each worker
// is doing right now. Updated at the 4 Hz WS tick — transient items
// flash by but multi-second items stay long enough to read.

function CurrentlyProcessing({ items }: { items: CurrentItem[] }) {
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
              <th style={{ width: '18%' }}>Library</th>
              <th style={{ width: '11%' }}>Type</th>
              <th style={{ width: '12%' }}>Phase</th>
              <th>Title</th>
              <th style={{ width: '10%' }}>Age</th>
            </tr>
          </thead>
          <tbody>
            {visible.map((ci, i) => (
              <tr key={i}>
                <td style={{ color: 'var(--text-dim)' }}>{ci.library}</td>
                <td className="mono">{ci.type}</td>
                <td>
                  {ci.phase
                    ? <span className={`tag ${PHASE_TAG[ci.phase] ?? 'phase'}`}>{ci.phase}</span>
                    : <span style={{ color: 'var(--text-dim)' }}>—</span>}
                </td>
                <td>{ci.title}</td>
                <td className="mono">{secondsToHMS(Math.max(0, now - ci.started_at))}</td>
              </tr>
            ))}
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

// Phase → tag-style class. Reuses the existing tag colour vocabulary
// (the same one ActivityFeed uses) so the dashboard stays visually
// consistent without inventing new CSS classes.
const PHASE_TAG: Record<string, string> = {
  resolving:  'phase',
  scrobbling: 'merged',
  rating:     'rated',
  merging:    'appended',
  exporting:  'started',
  indexing:   'phase',
  fetching:   'phase',
};

// ── Run stats ─────────────────────────────────────────────────────────────────

function RunStats({ dash }: { dash: DashboardSnapshot }) {
  return (
    <div className="panel">
      <h2>Run Stats</h2>
      <SectionHint>Outcome counts as items move through the pipeline.</SectionHint>
      <div className="grid-4">
        <Stat label="Completed" value={dash.completed} variant="good" />
        <Stat label="Skipped"   value={dash.skipped} />
        <Stat label="Failed"    value={dash.failed} variant="bad" />
        <Stat label="Unresolved" value={dash.unresolved} variant="warn" />
      </div>
    </div>
  );
}

// ── Match resolution ──────────────────────────────────────────────────────────

function MatchStats({ dash }: { dash: DashboardSnapshot }) {
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

function ThreadPool({ dash }: { dash: DashboardSnapshot }) {
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
            // v0.9.2: render the category label + count followed by a
            // plain-English description so a user unfamiliar with the
            // pipeline stages can tell what each group of threads is
            // currently doing. The pill itself still flows inline
            // (existing layout); only the text inside it grew.
            <span key={lbl} className="thread-pill" title={THREAD_DESCRIPTIONS[lbl] ?? ''}>
              <span className="thread-pill-label">{lbl} × {n}</span>
              {THREAD_DESCRIPTIONS[lbl] && (
                <span className="thread-pill-desc"> — {THREAD_DESCRIPTIONS[lbl]}</span>
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
  export: 'Exporting',
};

// v0.9.2: plain-English descriptions for each thread category.
// Keyed by the *display label* produced by THREAD_LABELS above so
// the lookup in the JSX uses the same string the user sees.
const THREAD_DESCRIPTIONS: Record<string, string> = {
  'Watched':     'Reading or writing TV / movie view counts and resume positions',
  'Play Count':  'Reading or writing music track play counts',
  'Playlists':   'Reading or merging playlists and their items',
  'Collections': 'Reading or merging collections and their members',
  'Ratings':     'Reading or writing star ratings on items',
  'Scan Cache':  'Building the file-path lookup table used by matching tiers 2 and 3',
  'Home User':   'Fetching or applying data for one Plex Home managed user',
  'Exporting':   'Reading and serialising one library’s data',
};

// ── Per-library progress ──────────────────────────────────────────────────────

function LibraryList({ libs, now }: { libs: LibraryProgress[]; now: number }) {
  return (
    <div className="panel">
      <h2>Libraries</h2>
      <SectionHint>Per-library progress with item counts, current phase, and an ETA.</SectionHint>
      {libs.length === 0 ? (
        <div className="empty">No libraries in this run.</div>
      ) : (
        <table className="list">
          <thead>
            <tr>
              <th style={{ width: '20%' }}>Library</th>
              <th style={{ width: '40%' }}>Progress</th>
              <th style={{ width: '12%' }}>Items</th>
              <th style={{ width: '18%' }}>Phase</th>
              <th style={{ width: '10%' }}>ETA</th>
            </tr>
          </thead>
          <tbody>
            {libs.map((lib) => {
              const total = lib.total || 1;
              const pct = total > 0 ? lib.completed / total : 0;
              const eta = computeETA(lib, now);
              return (
                <tr key={lib.name}>
                  <td>{lib.name} <StatusDot status={lib.status} /></td>
                  <td>
                    <div className={`bar-outer ${lib.status === 'error' ? 'error' : ''} ${lib.status === 'done' ? 'done' : ''}`}>
                      <div className="bar-inner" style={{ width: `${Math.min(100, pct * 100)}%` }} />
                      <div className="bar-text">{(pct * 100).toFixed(0)}%</div>
                    </div>
                  </td>
                  <td className="num">{lib.completed.toLocaleString()} / {lib.total.toLocaleString()}</td>
                  <td>{lib.phase || '—'}</td>
                  <td className="mono">{eta}</td>
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

function computeETA(lib: LibraryProgress, now: number): string {
  if (lib.status === 'done') return 'Done';
  if (lib.status === 'error') return 'Error';
  if (lib.status === 'queued') return 'Queued';
  if (lib.start_time <= 0) return 'starting…';
  const elapsed = now - lib.start_time;
  const pct = lib.total > 0 ? lib.completed / lib.total : 0;
  if (pct <= 0.02 || elapsed <= 0) return 'starting…';
  return secondsToHMS((elapsed / pct) * (1 - pct));
}

// ── Activity feed ─────────────────────────────────────────────────────────────

function ActivityFeed({ entries }: { entries: ActivityEntry[] }) {
  // Newest at top — the underlying deque is oldest-first, so reverse.
  const ordered = [...entries].reverse();
  return (
    <div className="panel">
      <h2>Activity Feed</h2>
      <SectionHint>The most recent pipeline events, newest at the top.</SectionHint>
      {ordered.length === 0 ? (
        <div className="empty">No events yet.</div>
      ) : (
        <div className="feed">
          {ordered.map((e, i) => (
            <div className="row" key={i}>
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
  if (!isFinite(s) || s < 0) return '—';
  const sec = Math.floor(s);
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const ss = sec % 60;
  if (h > 0) return `${h}:${pad2(m)}:${pad2(ss)}`;
  return `${m}:${pad2(ss)}`;
}
function pad2(n: number): string { return n < 10 ? `0${n}` : `${n}`; }
