// Active-deploys panel for Playlist Management.
//
// Deploy on the Playlist Management surface submits each copy as a
// job to the existing JobQueue. This panel tracks those jobs:
//   * Polls /api/playlist-mgmt/copy-jobs every 2s while at least one
//     job is queued / running. Stops polling when everything is in a
//     terminal state, then resumes on the next Deploy.
//   * Each row shows job state, per-pair label (source → dest), live
//     counters (written / skipped / failed) once results are in, and
//     the per-item miss reasons inline-collapsible.
//   * Local-only dismiss persisted to localStorage so dismissed jobs
//     don't keep re-rendering across page reloads. The job itself
//     stays in the server's history; we just hide it from this panel.
//   * Clone-deploy: a button on every finished row that re-submits
//     the same params as a NEW job (no copy-job-id reuse so each
//     submission has its own audit trail).
//   * Manual Refresh button next to the panel header forces an
//     immediate poll.
//
// The panel is the source of truth for the "I just deployed something
// and want to see how it's going" UX. It survives page reload because
// the server holds the job state; on mount we hydrate from
// /copy-jobs and re-render whatever isn't dismissed.

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { api } from '../api';
import { errorText } from '../utils/format';
import { pausableInterval } from '../utils/pausableInterval';
import type {
  PlaylistCopyIn,
  PlaylistCopyJob,
  PlaylistCopyBatchItemResult,
} from '../api';

const POLL_INTERVAL_MS = 2000;
const DISMISSED_KEY = 'playlistMgmt.dismissedJobIds';

function loadDismissed(): Set<string> {
  try {
    const raw = window.localStorage.getItem(DISMISSED_KEY);
    if (!raw) return new Set();
    const parsed = JSON.parse(raw);
    if (Array.isArray(parsed)) {
      return new Set(parsed.filter((x) => typeof x === 'string'));
    }
  } catch {
    /* fall through */
  }
  return new Set();
}

function saveDismissed(s: Set<string>): void {
  try {
    window.localStorage.setItem(DISMISSED_KEY, JSON.stringify(Array.from(s)));
  } catch {
    /* localStorage full / quota / disabled — non-fatal */
  }
}

function isTerminal(state: string): boolean {
  return state === 'completed'
    || state === 'completed_with_errors'
    || state === 'failed'
    || state === 'cancelled';
}

function stateLabel(state: string): { label: string; color: string } {
  switch (state) {
    case 'queued':
      return { label: 'Queued', color: 'var(--text-dim)' };
    case 'running':
      return { label: 'Running', color: 'var(--accent, #4a7afc)' };
    case 'completed':
      return { label: 'Succeeded', color: 'var(--success, #16a34a)' };
    case 'completed_with_errors':
      return { label: 'Completed (with errors)', color: 'var(--warn, #f5a623)' };
    case 'failed':
      return { label: 'Failed', color: 'var(--bad, #ef4444)' };
    case 'cancelled':
      return { label: 'Cancelled', color: 'var(--text-dim)' };
    case 'stopping':
      return { label: 'Stopping…', color: 'var(--warn, #f5a623)' };
    default:
      return { label: state, color: 'var(--text-dim)' };
  }
}

interface JobLabelLookup {
  // Map params → end user-readable username strings. Source server's
  // user list (parent state) gives us source-user names; dest list
  // gives us dest-user names. Empty / missing usernames fall back to
  // the raw user_id we submitted.
  sourceUsernameByApiId: Record<string, string>;
  destUsernameByApiId: Record<string, string>;
  // Playlist names by source playlist_id, so each row can show the
  // playlist label instead of just an id.
  playlistNameById: Record<string, string>;
  destServerLabelById: Record<string, string>;
}

interface Props {
  // Bumped by the parent right after each Deploy submission so the
  // panel kicks off a fresh poll immediately (instead of waiting up
  // to POLL_INTERVAL_MS).
  submissionTick: number;
  // Labels for prettier row rendering. Kept loose — anything missing
  // falls back to the raw API id from the job's params.
  labels: JobLabelLookup;
  // Clone-deploy submits a fresh copy job with the same params; the
  // parent owns the submission so it can also tick `submissionTick`
  // to nudge the poller.
  onCloneDeploy: (body: PlaylistCopyIn) => void;
}

export function ActiveDeploysPanel({ submissionTick, labels, onCloneDeploy }: Props) {
  const [jobs, setJobs] = useState<PlaylistCopyJob[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [dismissed, setDismissed] = useState<Set<string>>(() => loadDismissed());
  const pollTimerRef = useRef<(() => void) | null>(null);

  const fetchJobs = useCallback(async () => {
    try {
      const r = await api.listPlaylistCopyJobs();
      setJobs(r.jobs || []);
      setError(null);
    } catch (e) {
      setError(errorText(e));
    }
  }, []);

  const refresh = useCallback(async () => {
    setRefreshing(true);
    await fetchJobs();
    setRefreshing(false);
  }, [fetchJobs]);

  // Initial hydration on mount — restores the panel state across page
  // reload from the server's job history.
  useEffect(() => {
    void fetchJobs();
  }, [fetchJobs]);

  // Submission tick — kick a fresh poll right after the parent
  // submits new copies so the panel shows the new rows immediately.
  useEffect(() => {
    if (submissionTick === 0) return;
    void fetchJobs();
  }, [submissionTick, fetchJobs]);

  // Polling loop: active while any non-dismissed job is non-terminal.
  // Stops itself when everything settles; the submissionTick effect
  // re-arms it on the next deploy.
  const hasActive = useMemo(() => {
    return jobs.some((j) => !dismissed.has(j.job_id) && !isTerminal(j.state));
  }, [jobs, dismissed]);

  useEffect(() => {
    if (!hasActive) {
      if (pollTimerRef.current !== null) {
        pollTimerRef.current();
        pollTimerRef.current = null;
      }
      return;
    }
    if (pollTimerRef.current !== null) return;
    pollTimerRef.current = pausableInterval(() => {
      void fetchJobs();
    }, POLL_INTERVAL_MS);
    return () => {
      if (pollTimerRef.current !== null) {
        pollTimerRef.current();
        pollTimerRef.current = null;
      }
    };
  }, [hasActive, fetchJobs]);

  const visible = useMemo(
    () => jobs.filter((j) => !dismissed.has(j.job_id)),
    [jobs, dismissed],
  );

  const dismissOne = (jobId: string) => {
    setDismissed((prev) => {
      const next = new Set(prev);
      next.add(jobId);
      saveDismissed(next);
      return next;
    });
  };
  const dismissAllTerminal = () => {
    setDismissed((prev) => {
      const next = new Set(prev);
      for (const j of jobs) {
        if (isTerminal(j.state)) next.add(j.job_id);
      }
      saveDismissed(next);
      return next;
    });
  };
  const unhideAll = () => {
    setDismissed(() => {
      const next = new Set<string>();
      saveDismissed(next);
      return next;
    });
  };

  if (visible.length === 0 && error === null && dismissed.size === 0) {
    return null;
  }

  const activeCount = visible.filter((j) => !isTerminal(j.state)).length;
  const hiddenCount = jobs.length - visible.length;

  return (
    <div className="panel" style={{ marginTop: 12 }}>
      <div style={{ display: 'flex', alignItems: 'baseline', justifyContent: 'space-between', gap: 12, marginBottom: 8 }}>
        <h3 style={{ margin: 0 }}>
          Active deploys
          <span style={{ color: 'var(--text-dim)', fontSize: 12, fontWeight: 400, marginLeft: 8 }}>
            {activeCount > 0
              ? `${activeCount} in flight, ${visible.length - activeCount} finished`
              : `${visible.length} finished`}
            {hiddenCount > 0 ? `, ${hiddenCount} dismissed` : ''}
          </span>
        </h3>
        <div style={{ display: 'flex', gap: 6 }}>
          <button
            type="button"
            onClick={() => void refresh()}
            disabled={refreshing}
            style={{ fontSize: 11 }}
            title="Force a fresh poll of the job queue."
          >
            {refreshing ? 'Refreshing…' : 'Refresh'}
          </button>
          {visible.some((j) => isTerminal(j.state)) && (
            <button
              type="button"
              onClick={dismissAllTerminal}
              style={{ fontSize: 11 }}
              title="Hide every finished row from this list. Server history is unaffected."
            >
              Dismiss finished
            </button>
          )}
          {hiddenCount > 0 && (
            <button
              type="button"
              onClick={unhideAll}
              style={{ fontSize: 11 }}
              title="Un-hide every previously-dismissed row."
            >
              Unhide all
            </button>
          )}
        </div>
      </div>
      {error && (
        <div className="banner error" style={{ fontSize: 12, marginBottom: 8 }}>
          Could not load jobs: {error}
        </div>
      )}
      <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
        {visible.map((j) => (
          j.mode === 'playlist_copy_batch' ? (
            <BatchDeployRow
              key={j.job_id}
              job={j}
              onDismiss={() => dismissOne(j.job_id)}
              onRefresh={() => void refresh()}
            />
          ) : (
            <DeployRow
              key={j.job_id}
              job={j}
              labels={labels}
              onDismiss={() => dismissOne(j.job_id)}
              onClone={() => onCloneDeploy(buildCloneBody(j.params))}
            />
          )
        ))}
      </div>
    </div>
  );
}

function buildCloneBody(p: PlaylistCopyJob['params']): PlaylistCopyIn {
  return {
    source_server_id: p.source_server_id || '',
    source_user_id: p.source_user_id || '',
    source_playlist_id: p.source_playlist_id || '',
    dest_server_id: p.dest_server_id || '',
    dest_user_id: p.dest_user_id || '',
    dest_playlist_name: p.dest_playlist_name ?? undefined,
  };
}

interface DeployRowProps {
  job: PlaylistCopyJob;
  labels: JobLabelLookup;
  onDismiss: () => void;
  onClone: () => void;
}

function DeployRow({ job, labels, onDismiss, onClone }: DeployRowProps) {
  const [errorsOpen, setErrorsOpen] = useState(false);
  const tone = stateLabel(job.state);
  const terminal = isTerminal(job.state);

  const p = job.params;
  const sourceUser = (p.source_user_id && labels.sourceUsernameByApiId[p.source_user_id])
    || p.source_user_id
    || '(source user)';
  const destUser = (p.dest_user_id && labels.destUsernameByApiId[p.dest_user_id])
    || p.dest_user_id
    || '(dest user)';
  const playlistName = (p.source_playlist_id && labels.playlistNameById[p.source_playlist_id])
    || p.dest_playlist_name
    || '(playlist)';
  const destServerLabel = (p.dest_server_id && labels.destServerLabelById[p.dest_server_id])
    || '(dest server)';

  const result = job.result;
  const elapsed = job.finished_at && job.started_at
    ? job.finished_at - job.started_at
    : (result?.elapsed_seconds ?? null);

  return (
    <div
      style={{
        padding: 10,
        border: `1px solid ${tone.color}`,
        borderRadius: 4,
        background: terminal ? 'transparent' : 'rgba(74, 122, 252, 0.06)',
      }}
    >
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', gap: 8 }}>
        <div style={{ flex: 1 }}>
          <strong style={{ color: tone.color, fontSize: 13 }}>{tone.label}</strong>
          <span style={{ marginLeft: 8, fontSize: 13 }}>
            <strong>{playlistName}</strong>
            <span style={{ color: 'var(--text-dim)', fontSize: 11, marginLeft: 6 }}>
              ({sourceUser} → {destUser})
            </span>
          </span>
        </div>
        <div style={{ display: 'flex', gap: 6 }}>
          {terminal && (
            <>
              <button
                type="button"
                onClick={onClone}
                style={{ fontSize: 11 }}
                title="Submit a new deploy with the same source / destination / playlist."
              >
                Clone deploy
              </button>
              <button
                type="button"
                onClick={onDismiss}
                style={{ fontSize: 11 }}
                title="Hide this row from the panel. Server history is unaffected."
              >
                Dismiss
              </button>
            </>
          )}
        </div>
      </div>
      <div style={{ marginTop: 4, fontSize: 11, color: 'var(--text-dim)' }}>
        On {destServerLabel}
        {elapsed !== null ? <> · {elapsed.toFixed(1)}s</> : null}
      </div>
      {result && result.skipped && (
        <div
          style={{
            marginTop: 6,
            fontSize: 12,
            padding: '6px 8px',
            background: 'rgba(234, 179, 8, 0.10)',
            border: '1px solid rgba(234, 179, 8, 0.45)',
            borderRadius: 4,
          }}
          title={result.skip_reason || undefined}
        >
          <strong>Skipped:</strong>{' '}
          {result.skip_reason || 'source and destination resolved to the same user.'}
        </div>
      )}
      {result && !result.skipped && (
        <div style={{ marginTop: 6, fontSize: 12, display: 'flex', gap: 14 }}>
          <span>
            Written: <strong style={{ color: 'var(--success, #16a34a)' }}>{result.items_written}</strong>
          </span>
          <span>
            Skipped: <strong>{result.items_skipped_no_match}</strong>
          </span>
          <span>
            Failed:{' '}
            <strong style={{ color: result.items_failed > 0 ? 'var(--bad, #ef4444)' : undefined }}>
              {result.items_failed}
            </strong>
          </span>
        </div>
      )}
      {job.error && (
        <div style={{ marginTop: 6, fontSize: 12, color: 'var(--bad, #ef4444)' }}>
          {job.error}
        </div>
      )}
      {result && result.errors && result.errors.length > 0 && (
        <div style={{ marginTop: 6 }}>
          <button
            type="button"
            onClick={() => setErrorsOpen((p2) => !p2)}
            style={{ fontSize: 11 }}
          >
            {errorsOpen ? 'Hide' : 'Show'} {result.errors.length} per-item miss{result.errors.length === 1 ? '' : 'es'}
          </button>
          {errorsOpen && (
            <ul
              style={{
                listStyle: 'disc',
                paddingLeft: 22,
                margin: '6px 0 0',
                fontSize: 11,
                maxHeight: 160,
                overflowY: 'auto',
              }}
            >
              {result.errors.map((e, i) => (
                <li key={i} style={{ marginBottom: 2 }}>{e}</li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}


// ── Batch deploy row ──
//
// Renders a single playlist_copy_batch JobRecord. Collapsed header
// shows total/succeeded/failed/cancelled/skipped tallies; expandable
// per-item drill-down lists every result row with its index, status,
// and a Cancel button for items that haven't reached a terminal state
// yet (only meaningful while the job is running). Collapsed-by-default
// and expandable to inspect individual misses.

interface BatchRowProps {
  job: PlaylistCopyJob;
  onDismiss: () => void;
  onRefresh: () => void;
}

function BatchDeployRow({ job, onDismiss, onRefresh }: BatchRowProps) {
  const [drilldownOpen, setDrilldownOpen] = useState(false);
  // Item indices the operator clicked Cancel on but the server hasn't
  // yet reflected as terminal — kept in component state so the button
  // disables visually until the next /copy-jobs poll picks up the
  // updated state from the per-item drill-down.
  const [cancelInFlight, setCancelInFlight] = useState<Set<number>>(new Set());
  const tone = stateLabel(job.state);
  const terminal = isTerminal(job.state);
  const batch = job.batch ?? null;
  const label = batch?.label || job.params.label || '(batch)';
  const elapsed = job.finished_at && job.started_at
    ? job.finished_at - job.started_at
    : (batch?.elapsed_seconds ?? null);

  const cancelItem = async (index: number) => {
    if (cancelInFlight.has(index)) return;
    setCancelInFlight((prev) => new Set(prev).add(index));
    try {
      await api.cancelPlaylistBatchItem(job.job_id, index);
      onRefresh();
    } catch (e) {
      // eslint-disable-next-line no-console
      console.warn('cancelPlaylistBatchItem failed', { jobId: job.job_id, index, e });
      // Drop the in-flight flag so the operator can retry.
      setCancelInFlight((prev) => {
        const n = new Set(prev);
        n.delete(index);
        return n;
      });
    }
  };

  return (
    <div
      style={{
        padding: 10,
        border: `1px solid ${tone.color}`,
        borderRadius: 4,
        background: terminal ? 'transparent' : 'rgba(74, 122, 252, 0.06)',
      }}
    >
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', gap: 8 }}>
        <div style={{ flex: 1 }}>
          <strong style={{ color: tone.color, fontSize: 13 }}>{tone.label}</strong>
          <span style={{ marginLeft: 8, fontSize: 13 }}>
            <strong>Batch:</strong> {label}
            <span style={{ color: 'var(--text-dim)', fontSize: 11, marginLeft: 6 }}>
              ({batch ? `${batch.total} item(s), parallelism=${batch.parallelism}` : 'pending…'})
            </span>
          </span>
        </div>
        <div style={{ display: 'flex', gap: 6 }}>
          {terminal && (
            <button
              type="button"
              onClick={onDismiss}
              style={{ fontSize: 11 }}
              title="Hide this row from the panel. Server history is unaffected."
            >
              Dismiss
            </button>
          )}
        </div>
      </div>
      {elapsed !== null && (
        <div style={{ marginTop: 4, fontSize: 11, color: 'var(--text-dim)' }}>
          Elapsed {elapsed.toFixed(1)}s
        </div>
      )}
      {batch && (
        <div style={{ marginTop: 6, fontSize: 12, display: 'flex', gap: 14, flexWrap: 'wrap' }}>
          <span>
            Succeeded: <strong style={{ color: 'var(--success, #16a34a)' }}>{batch.succeeded}</strong>
          </span>
          <span>
            Failed:{' '}
            <strong style={{ color: batch.failed > 0 ? 'var(--bad, #ef4444)' : undefined }}>
              {batch.failed}
            </strong>
          </span>
          <span>
            Skipped: <strong>{batch.skipped}</strong>
          </span>
          <span>
            Cancelled:{' '}
            <strong style={{ color: batch.cancelled > 0 ? 'var(--warn, #f5a623)' : undefined }}>
              {batch.cancelled}
            </strong>
          </span>
          <span style={{ color: 'var(--text-dim)' }}>
            Total: {batch.total}
          </span>
        </div>
      )}
      {job.error && (
        <div style={{ marginTop: 6, fontSize: 12, color: 'var(--warn, #f5a623)' }}>
          {job.error}
        </div>
      )}
      {batch && batch.results.length > 0 && (
        <div style={{ marginTop: 6 }}>
          <button
            type="button"
            onClick={() => setDrilldownOpen((open) => !open)}
            style={{ fontSize: 11 }}
            title="Show per-item drill-down: status + per-item Cancel buttons."
          >
            {drilldownOpen ? 'Hide' : 'Show'} per-item drill-down ({batch.results.length})
          </button>
          {drilldownOpen && (
            <BatchItemList
              items={batch.results}
              jobIsRunning={!terminal}
              cancelInFlight={cancelInFlight}
              onCancelItem={(idx) => void cancelItem(idx)}
            />
          )}
        </div>
      )}
    </div>
  );
}


interface BatchItemListProps {
  items: PlaylistCopyBatchItemResult[];
  jobIsRunning: boolean;
  cancelInFlight: Set<number>;
  onCancelItem: (index: number) => void;
}

function BatchItemList({ items, jobIsRunning, cancelInFlight, onCancelItem }: BatchItemListProps) {
  return (
    <div
      style={{
        marginTop: 6,
        maxHeight: 280,
        overflowY: 'auto',
        border: '1px solid rgba(128,128,128,0.2)',
        borderRadius: 3,
        padding: 4,
      }}
    >
      {items.map((r) => {
        const statusLabel =
          r.cancelled ? 'Cancelled'
          : r.error_code ? r.error_code
          : r.skipped ? 'Skipped'
          : r.success ? 'OK'
          : 'Pending';
        const statusColor =
          r.cancelled ? 'var(--warn, #f5a623)'
          : r.error_code ? 'var(--bad, #ef4444)'
          : r.skipped ? 'var(--text-dim)'
          : r.success ? 'var(--success, #16a34a)'
          : 'var(--text-dim)';
        // A row is "done" (no Cancel button) when its result row carries
        // success=true, cancelled=true, skipped=true, or any error_code.
        // While the job is running, items that haven't yet started OR
        // that are mid-flight are eligible for cancel.
        const itemDone = r.success || r.cancelled || r.skipped || !!r.error_code;
        const showCancel = jobIsRunning && !itemDone;
        return (
          <div
            key={r.index}
            style={{
              display: 'flex',
              gap: 8,
              fontSize: 11,
              padding: '2px 4px',
              borderBottom: '1px dotted rgba(128,128,128,0.15)',
              alignItems: 'baseline',
            }}
          >
            <span style={{ color: 'var(--text-dim)', minWidth: 28 }}>#{r.index}</span>
            <span style={{ color: statusColor, minWidth: 90, fontWeight: 600 }}>
              {statusLabel}
            </span>
            <span style={{ flex: 1 }}>
              {r.success && !r.skipped && !r.cancelled && (
                <>
                  written {r.items_written}
                  {r.items_skipped_no_match > 0 ? <>, missed {r.items_skipped_no_match}</> : null}
                </>
              )}
              {r.skip_reason && <em style={{ color: 'var(--text-dim)' }}>{r.skip_reason}</em>}
              {r.errors && r.errors.length > 0 && (
                <span style={{ color: 'var(--bad, #ef4444)' }}>{r.errors[0]}</span>
              )}
            </span>
            {showCancel && (
              <button
                type="button"
                disabled={cancelInFlight.has(r.index)}
                onClick={() => onCancelItem(r.index)}
                style={{ fontSize: 10, padding: '0 4px' }}
                title="Cancel this item only; the rest of the batch continues."
              >
                {cancelInFlight.has(r.index) ? 'Cancelling…' : 'Cancel'}
              </button>
            )}
          </div>
        );
      })}
    </div>
  );
}
