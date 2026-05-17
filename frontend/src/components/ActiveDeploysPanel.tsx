// Active-deploys panel for Playlist Management.
//
// 2026-05-17 (end user request): Deploy on the Playlist Management
// surface now submits each copy as a job to the existing JobQueue.
// This panel tracks those jobs:
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
import type { PlaylistCopyIn, PlaylistCopyJob } from '../api';

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
  const pollTimerRef = useRef<number | null>(null);

  const fetchJobs = useCallback(async () => {
    try {
      const r = await api.listPlaylistCopyJobs();
      setJobs(r.jobs || []);
      setError(null);
    } catch (e) {
      setError(String(e instanceof Error ? e.message : e));
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
        window.clearInterval(pollTimerRef.current);
        pollTimerRef.current = null;
      }
      return;
    }
    if (pollTimerRef.current !== null) return;
    pollTimerRef.current = window.setInterval(() => {
      void fetchJobs();
    }, POLL_INTERVAL_MS);
    return () => {
      if (pollTimerRef.current !== null) {
        window.clearInterval(pollTimerRef.current);
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
          <DeployRow
            key={j.job_id}
            job={j}
            labels={labels}
            onDismiss={() => dismissOne(j.job_id)}
            onClone={() => onCloneDeploy(buildCloneBody(j.params))}
          />
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
