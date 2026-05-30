// Live progress for a running Smart Playlist Migration job. Rendered
// below the deploy bar in Playlist Transfer's Smart Playlist mode
// after a migration is submitted. Tails the dedicated
// smart_playlist.log for live feedback and reads the migration-record
// DB for the per-playlist results. Driven purely by the active job
// id.

import { useEffect, useRef, useState } from 'react';
import { api } from '../api';
import type { SmartMigrationRecord } from '../api';
import { SmartFilterTree } from './SmartFilterTree';
import { pausableInterval } from '../utils/pausableInterval';

function statusColor(status: string): string {
  if (status === 'success') return 'var(--good)';
  if (status === 'partial') return 'var(--warn)';
  return 'var(--bad)';
}

interface Props {
  // The active smart-migration job id, or null when none has run yet.
  jobId: string | null;
  // How many items were submitted, so polling knows when the run is
  // complete (records written == items submitted).
  expectedCount: number;
}

export function SmartMigrationProgressPanel({ jobId, expectedCount }: Props) {
  const [migrations, setMigrations] = useState<SmartMigrationRecord[]>([]);
  const [logText, setLogText] = useState('');
  const [polling, setPolling] = useState(false);
  const [expandedResultId, setExpandedResultId] = useState<number | null>(null);
  const logOffsetRef = useRef(0);
  const graceRef = useRef(0);
  const logBoxRef = useRef<HTMLPreElement>(null);
  const lastJobRef = useRef<string | null>(null);

  // New job -> seed the log cursor at the shared log's current end so
  // the view shows only this job's lines, reset state, start polling.
  useEffect(() => {
    if (!jobId || jobId === lastJobRef.current) return;
    lastJobRef.current = jobId;
    let cancelled = false;
    setLogText('');
    setMigrations([]);
    setExpandedResultId(null);
    graceRef.current = 0;
    api.readAppLog('smart-playlist', 1, 0)
      .then((seed) => { if (!cancelled) logOffsetRef.current = seed.next_offset; })
      .catch(() => { logOffsetRef.current = 0; })
      .finally(() => { if (!cancelled) setPolling(true); });
    return () => { cancelled = true; };
  }, [jobId]);

  // Poll migration records + tail smart_playlist.log while running.
  // Polling continues a few cycles past the last record so the
  // trailing "done" log line is captured.
  useEffect(() => {
    if (!jobId || !polling) return;
    let cancelled = false;
    const tick = async () => {
      let done = false;
      try {
        const r = await api.listSmartMigrations(jobId);
        if (cancelled) return;
        setMigrations(r.migrations);
        done = r.migrations.length >= expectedCount;
      } catch {
        /* transient; keep polling */
      }
      try {
        const lr = await api.readAppLog('smart-playlist', 0, logOffsetRef.current);
        if (cancelled) return;
        if (lr.content) {
          const prefix = lr.rotated_during_poll ? '\n--- log rotated ---\n' : '';
          setLogText((prev) => prev + prefix + lr.content);
        }
        logOffsetRef.current = lr.next_offset;
      } catch {
        /* transient; keep polling */
      }
      if (cancelled) return;
      if (done) {
        graceRef.current += 1;
        if (graceRef.current >= 3) setPolling(false);
      }
    };
    void tick();
    const stopPoll = pausableInterval(() => void tick(), 2000);
    return () => { cancelled = true; stopPoll(); };
  }, [jobId, polling, expectedCount]);

  // Keep the live log scrolled to the newest line.
  useEffect(() => {
    const el = logBoxRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [logText]);

  if (!jobId) return null;

  return (
    <>
      <div className="panel" style={{ marginTop: 12 }}>
        <h3 style={{ margin: '0 0 6px' }}>
          Smart migration log
          {polling && (
            <span style={{ color: 'var(--accent)', fontSize: 12, marginLeft: 8 }}>
              running…
            </span>
          )}
        </h3>
        <pre
          ref={logBoxRef}
          style={{
            maxHeight: 260,
            overflow: 'auto',
            margin: 0,
            padding: 8,
            fontSize: 11,
            lineHeight: 1.4,
            background: 'var(--bg-alt, #0d1117)',
            borderRadius: 4,
            whiteSpace: 'pre-wrap',
            wordBreak: 'break-word',
          }}
        >
          {logText || '(waiting for log output…)'}
        </pre>
      </div>

      <div className="panel" style={{ marginTop: 10 }}>
        <h3 style={{ margin: '0 0 6px' }}>Migration results</h3>
        {migrations.length === 0 ? (
          <p className="help">
            No migration records yet for this run. Completed playlists
            appear here as the job runs.
          </p>
        ) : (
          <ul style={{ listStyle: 'none', margin: 0, padding: 0 }}>
            {migrations.map((m) => {
              const expanded = expandedResultId === m.id;
              return (
                <li
                  key={m.id}
                  style={{
                    borderTop: '1px solid var(--border, #2a2f37)',
                    padding: '6px 0',
                  }}
                >
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
                    <span
                      style={{
                        color: statusColor(m.status),
                        border: `1px solid ${statusColor(m.status)}`,
                        borderRadius: 3,
                        fontSize: 10,
                        padding: '1px 6px',
                        textTransform: 'uppercase',
                      }}
                    >
                      {m.status || 'unknown'}
                    </span>
                    <span style={{ fontWeight: 600 }}>
                      {m.source_playlist_name || '(unnamed)'}
                    </span>
                    <span style={{ color: 'var(--text-dim)' }}>
                      → {m.dest_backend || '?'} ({m.mode || '?'})
                    </span>
                    {m.portable_filter && (
                      <button
                        type="button"
                        onClick={() => setExpandedResultId(expanded ? null : m.id)}
                      >
                        {expanded ? 'Hide filter' : 'Show filter'}
                      </button>
                    )}
                  </div>
                  {m.warning && (
                    <div style={{ fontSize: 11, color: 'var(--warn)', marginTop: 2 }}>
                      {m.warning}
                    </div>
                  )}
                  {m.unresolved && m.unresolved.length > 0 && (
                    <div style={{ fontSize: 11, color: 'var(--bad)', marginTop: 2 }}>
                      Unresolved: {m.unresolved.join('; ')}
                    </div>
                  )}
                  {expanded && m.portable_filter && (
                    <div
                      style={{
                        margin: '6px 0 4px 8px',
                        padding: 8,
                        background: 'var(--bg-alt, rgba(127,127,127,0.08))',
                        borderRadius: 4,
                      }}
                    >
                      <div style={{ fontSize: 12, marginBottom: 4 }}>
                        <strong>{m.portable_filter.description}</strong>
                      </div>
                      {m.portable_filter.root ? (
                        <SmartFilterTree node={m.portable_filter.root} />
                      ) : (
                        <span className="help">No filter clauses.</span>
                      )}
                    </div>
                  )}
                </li>
              );
            })}
          </ul>
        )}
      </div>
    </>
  );
}
