// Settings > Application Logs.
//
// Houses APP-LEVEL logs only: database access audit, auth events,
// network reachability, debug traces. Per-run JOB logs (snapshot,
// restore, direct-transfer) now live under Servers > Logs and are
// not surfaced here.
//
// The viewer matches the run-log viewer's behaviours so end users
// see a consistent log surface across the app:
//
//   * Live tail (2 s poll, ?since=offset for incremental fetches).
//   * Toggleable live tail (checkbox in the header).
//   * Manual Reload (resets the offset and re-reads the tail).
//   * Sticky-bottom scroll: pinned to the bottom when the end user
//     hasn't manually scrolled up; releases the pin when they scroll
//     away so reading older lines is uninterrupted.
//   * Case-insensitive substring filter. Empty filter renders the raw
//     blob; non-empty switches to per-line render with the matched
//     substring highlighted.
//   * Truncation banner when the server reports the read started past
//     a file boundary (rotation or sub-tail-bytes file end).
//
// Categories are stable across releases. A category may have no
// backing log writer yet (auth / network / debug today); the backend
// returns an empty content string + a note that this panel surfaces
// as an "info" banner.

import { useEffect, useMemo, useRef, useState } from 'react';
import { api } from '../api';
import { errorText, formatBytes } from '../utils/format';
import { pausableInterval } from '../utils/pausableInterval';
import { renderHighlighted } from '../utils/highlight';

type AppLogCategory = 'app' | 'db-access' | 'playlist-cache' | 'sync' | 'user-activity' | 'auth' | 'network' | 'debug';

interface CategoryDef {
  key: AppLogCategory;
  label: string;
  description: string;
}

const CATEGORIES: CategoryDef[] = [
  {
    key: 'app',
    label: 'App',
    description:
      'Every plexmigrate.* server-side log line at INFO+ level. Covers the user-token capture flow (Refresh users, server-add), the owner-token mirror decisions, scheduler ticks, and every other event the per-run job logs do not capture. The right place to look when something happened "outside" a snapshot or restore run.',
  },
  {
    key: 'db-access',
    label: 'Database Access',
    description:
      'Every read / write against media.db and snapshots.db, plus credential fetches. Toggled via the db_admin-gated audit-log switch in Settings.',
  },
  {
    key: 'playlist-cache',
    label: 'Playlist Cache',
    description:
      'Every playlist-cache refresh attempt — bulk per-server (triggered by Servers ▸ Refresh and by the Playlist Transfer column Refresh button) and per-user. Each line records server, action, durations, and per-user success/error counts.',
  },
  {
    key: 'sync',
    label: 'Sync Activity',
    description:
      'Sync engine activity from the polling worker: per-cycle reconcile traces, watch-count math, playlist merge decisions, and any sync-initiated playlist copies. Routed here (not into runtime.log) so it never bleeds into a running job’s per-run log. Manual / operator-initiated playlist copies continue to write to runtime.log.',
  },
  {
    key: 'user-activity',
    label: 'User Activity Sweep',
    description:
      'Auth-health probe results from the user-activity sweeper. Each line records (server, user, probe result) and any auto-tombstone decisions. Surfaced here so an operator can audit which users have been failing auth and which were tombstoned automatically. The sweeper itself is off by default; turn it on under Settings > Tunables > Polling > User Activity, then enable per-server auto-tombstone under each server\'s edit panel.',
  },
  {
    key: 'auth',
    label: 'Auth Events',
    description:
      'Login / logout / refresh / elevation / password change / role change. Scaffolded surface; backing writer ships in a follow-up scope.',
  },
  {
    key: 'network',
    label: 'Network',
    description:
      'Per-server ping latency, reachability transitions, retry budget exhaustion. Scaffolded surface; backing writer ships in a follow-up scope.',
  },
  {
    key: 'debug',
    label: 'Debug',
    description:
      'Verbose internal traces from background tasks (sidecar sweep, library walk scheduler, run_timings flush). Scaffolded surface; backing writer ships in a follow-up scope.',
  },
];

const TAIL_POLL_MS = 2000;




interface BackupEntry {
  filename: string;
  size_bytes: number;
}

export function ApplicationLogsPanel() {
  const [category, setCategory] = useState<AppLogCategory>('db-access');
  const [content, setContent] = useState<string>('');
  const [note, setNote] = useState<string>('');
  const [path, setPath] = useState<string>('');
  const [sizeBytes, setSizeBytes] = useState<number>(0);
  // Two distinct truncation signals from the backend. ``headOmitted``
  // is normal-for-large-files (initial read fetched the last N bytes
  // of a bigger file; older bytes still live in the same file).
  // ``rotatedDuringPoll`` is the discontinuity case (polling cursor
  // landed past EOF because logrotate fired; older bytes are now in
  // a .1 backup, surfaced in ``backups``).
  const [headOmitted, setHeadOmitted] = useState<boolean>(false);
  const [rotatedDuringPoll, setRotatedDuringPoll] = useState<boolean>(false);
  const [backups, setBackups] = useState<BackupEntry[]>([]);
  // End user-selected backup file. Empty string = read the active log;
  // a filename like "db_access.log.1" = read that specific archive.
  // Live tail is silently disabled when a backup is selected (the
  // file never grows after rotation).
  const [selectedBackup, setSelectedBackup] = useState<string>('');
  const [error, setError] = useState<string | null>(null);
  const [liveTail, setLiveTail] = useState<boolean>(true);
  const [lastPolledAt, setLastPolledAt] = useState<number | null>(null);
  const [filter, setFilter] = useState<string>('');

  // Offset for the next ?since=... poll. Mirrors LogTailer's pattern.
  const offsetRef = useRef<number>(0);
  // Sticky-bottom: pinned to the bottom unless the end user has
  // scrolled away. Released on manual scroll up; re-acquired when
  // the scroll reaches the bottom again.
  const bodyRef = useRef<HTMLDivElement | null>(null);
  const stickyRef = useRef<boolean>(true);

  // Full reload (since=0). Called on initial mount, on category
  // change, on backup-file change, and on end user-clicked Reload.
  const reload = async (key: AppLogCategory, backup = selectedBackup) => {
    setError(null);
    offsetRef.current = 0;
    try {
      const r = await api.readAppLog(key, 0, 0, backup);
      setContent(r.content);
      setNote(r.note);
      setPath(r.path);
      setSizeBytes(r.size_bytes);
      setHeadOmitted(r.head_omitted);
      setRotatedDuringPoll(false);
      setBackups(r.backups);
      offsetRef.current = r.next_offset;
      setLastPolledAt(Date.now());
      stickyRef.current = true;
      requestAnimationFrame(() => {
        if (bodyRef.current) bodyRef.current.scrollTop = bodyRef.current.scrollHeight;
      });
    } catch (e) {
      setError(errorText(e));
      setContent('');
      setNote('');
      setPath('');
      setSizeBytes(0);
      setHeadOmitted(false);
      setRotatedDuringPoll(false);
      setBackups([]);
    }
  };

  // Initial mount + category change. Reset the backup selection so a
  // category swap always lands on the active log first.
  useEffect(() => {
    setSelectedBackup('');
    void reload(category, '');
    /* eslint-disable-next-line react-hooks/exhaustive-deps */
  }, [category]);

  // Backup-file change. Re-read the selected target.
  useEffect(() => {
    void reload(category, selectedBackup);
    /* eslint-disable-next-line react-hooks/exhaustive-deps */
  }, [selectedBackup]);

  // Poll loop. Appends only the bytes since the last next_offset.
  // Disabled when the end user is viewing a rotated backup (those
  // files don't grow), and disabled for the placeholder categories
  // that always return empty.
  useEffect(() => {
    if (!liveTail) return;
    if (note) return;
    if (selectedBackup) return;
    // ``cancelled`` guards a stale in-flight poll: a category or
    // backup switch tears this effect down, but a readAppLog request
    // already dispatched still resolves. Without the guard its bytes
    // and next_offset would splice another log's tail into the
    // freshly-switched view (offsetRef is shared across categories).
    let cancelled = false;
    const stop = pausableInterval(async () => {
      try {
        const r = await api.readAppLog(category, 0, offsetRef.current);
        if (cancelled) return;
        // rotated_during_poll means the file shrank between polls
        // (logrotate fired). Reset the body to the new tail and
        // surface the banner so the end user knows older bytes
        // moved into a .1 backup.
        if (r.rotated_during_poll && offsetRef.current > 0) {
          setContent(r.content);
          setRotatedDuringPoll(true);
        } else if (r.content.length > 0) {
          setContent((prev) => prev + r.content);
        }
        setSizeBytes(r.size_bytes);
        setBackups(r.backups);
        offsetRef.current = r.next_offset;
        setLastPolledAt(Date.now());
      } catch (e) {
        if (cancelled) return;
        setError(errorText(e));
      }
    }, TAIL_POLL_MS);
    return () => { cancelled = true; stop(); };
  }, [liveTail, category, note, selectedBackup]);

  // Keep the viewport pinned to the bottom when new content arrives
  // and the end user hasn't scrolled away.
  useEffect(() => {
    if (!bodyRef.current || !stickyRef.current) return;
    bodyRef.current.scrollTop = bodyRef.current.scrollHeight;
  }, [content]);

  const onBodyScroll = () => {
    const el = bodyRef.current;
    if (!el) return;
    // Threshold matches LogTailer (40 px slack so a few pixels of
    // sub-line scroll don't pop the pin).
    stickyRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
  };

  // Per-line filter. Empty filter renders the raw blob in a <pre>;
  // non-empty splits + filters case-insensitively. Cost is paid only
  // when the end user is actively filtering.
  const filteredLines = useMemo(() => {
    if (!filter) return null;
    const needle = filter.toLowerCase();
    const out: string[] = [];
    for (const line of content.split('\n')) {
      if (line.toLowerCase().includes(needle)) out.push(line);
    }
    return out;
  }, [content, filter]);

  const def = CATEGORIES.find((c) => c.key === category);
  const statusLabel = note
    ? 'No log source yet'
    : selectedBackup
      ? `Viewing backup ${selectedBackup}`
      : liveTail
        ? `Live · last polled ${lastPolledAt ? new Date(lastPolledAt).toLocaleTimeString() : '-'}`
        : 'Paused';

  return (
    <div className="panel">
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <h2 style={{ margin: 0 }}>Application Logs</h2>
        <div style={{ display: 'flex', gap: 12, alignItems: 'center' }}>
          <span style={{ fontSize: 11, color: 'var(--text-dim)' }}>{statusLabel}</span>
          {!note && !selectedBackup && (
            <label style={{ display: 'inline-flex', alignItems: 'center', gap: 4, fontSize: 12 }}>
              <input
                type="checkbox"
                checked={liveTail}
                onChange={(e) => setLiveTail(e.target.checked)}
              />
              Live tail
            </label>
          )}
          <button onClick={() => void reload(category, selectedBackup)}>Reload</button>
        </div>
      </div>
      <p style={{ color: 'var(--text-dim)', fontSize: 12, marginTop: 4 }}>
        Application-level logs about the Hestia-MediaManager app itself. Per-job
        run logs (snapshot / restore / direct transfer) live under{' '}
        <strong>Servers &gt; Logs</strong> grouped by source server.
      </p>

      {/* Category sub-tab strip. Matches LibraryCataloguesPanel + the
          refactored ServerLogsPanel; click selects, body renders in
          place below. */}
      <nav className="tabs sub-tabs" style={{ marginBottom: 12 }}>
        {CATEGORIES.map((c) => (
          <button
            key={c.key}
            className={category === c.key ? 'active' : ''}
            onClick={() => setCategory(c.key)}
          >
            {c.label}
          </button>
        ))}
      </nav>

      {def && (
        <>
          <h3 style={{ fontSize: 13, margin: '0 0 6px' }}>{def.label}</h3>
          <p style={{ color: 'var(--text-dim)', fontSize: 12, marginBottom: 6 }}>
            {def.description}
          </p>
          {path && (
            <div
              style={{
                color: 'var(--text-dim)',
                fontSize: 11,
                marginBottom: 10,
                display: 'flex',
                alignItems: 'center',
                gap: 6,
              }}
              title="Resolved on-disk path. Useful for tailing from a shell (e.g. `tail -f <path>`)."
            >
              <span>File:</span>
              <code style={{ userSelect: 'all' }}>{path}</code>
              <button
                type="button"
                onClick={() => {
                  if (path) void navigator.clipboard?.writeText(path);
                }}
                style={{ fontSize: 11, padding: '0 6px' }}
                title="Copy path to clipboard"
              >
                Copy
              </button>
            </div>
          )}
        </>
      )}

      {error && (
        <div className="banner error" style={{ marginBottom: 8 }}>{error}</div>
      )}

      {note && (
        <div className="banner info" style={{ fontSize: 12, marginBottom: 8 }}>
          {note}
        </div>
      )}

      {/* Two distinct cases, each with its own banner so the end user
          knows what they're looking at. ``rotatedDuringPoll`` is the
          real "logrotate fired" signal; ``headOmitted`` is the
          "this file is bigger than the read cap, you're seeing the
          tail" case (normal for large active logs). */}
      {rotatedDuringPoll && (
        <div className="banner info" style={{ fontSize: 12, marginBottom: 6 }}>
          Log rotation fired during this session. Older bytes have moved
          to a backup file (pick one below to read it); new lines continue
          to append to the active log.
        </div>
      )}
      {headOmitted && !selectedBackup && (
        <div style={{ color: 'var(--text-dim)', fontSize: 11, marginBottom: 6 }}>
          Showing the tail of the active log because the file exceeds
          the read cap (<code>log_read_max_bytes</code> in Tunables).
          Increase the cap to see more, or open a backup file below
          for the rolled-over history.
        </div>
      )}

      {/* Rotated-backup picker. Empty selection = active log. When
          one is picked, live tail is suppressed (the backup file
          doesn't grow) and the body shows the backup's tail. */}
      {backups.length > 0 && (
        <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 6, fontSize: 12 }}>
          <span style={{ color: 'var(--text-dim)' }}>Source:</span>
          <select
            value={selectedBackup}
            onChange={(e) => setSelectedBackup(e.target.value)}
            style={{ minWidth: 200 }}
            title="Pick the active log or a rotated backup. Backups are read-only and never grow; live tail is automatically suppressed when viewing one."
          >
            <option value="">Active log</option>
            {backups.map((b) => (
              <option key={b.filename} value={b.filename}>
                {b.filename} · {formatBytes(b.size_bytes)}
              </option>
            ))}
          </select>
          {selectedBackup && (
            <span style={{ color: 'var(--text-dim)', fontSize: 11 }}>
              (read-only · live tail disabled)
            </span>
          )}
        </div>
      )}

      {sizeBytes > 0 && (
        <div style={{ display: 'flex', gap: 12, fontSize: 11, color: 'var(--text-dim)', marginBottom: 6 }}>
          <span>Size: {formatBytes(sizeBytes)}</span>
        </div>
      )}

      {/* Case-insensitive substring filter. Same shape as LogTailer's
          filter input. */}
      <div style={{
        display: 'flex', alignItems: 'center', gap: 6,
        marginBottom: 6, fontSize: 12,
      }}>
        <span style={{ color: 'var(--text-dim)' }}>Filter:</span>
        <input
          type="text"
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          placeholder="case-insensitive substring…"
          disabled={!!note}
          style={{ flex: 1, fontFamily: 'var(--mono, monospace)' }}
        />
        {filter && (
          <button onClick={() => setFilter('')} style={{ fontSize: 11 }}>
            Clear
          </button>
        )}
        {filter && filteredLines && (
          <span style={{ color: 'var(--text-dim)', fontSize: 11 }}>
            {filteredLines.length} match{filteredLines.length === 1 ? '' : 'es'}
          </span>
        )}
      </div>

      {!error && (
        <div
          ref={bodyRef}
          onScroll={onBodyScroll}
          style={{
            background: 'var(--bg-panel)',
            border: '1px solid var(--border)',
            borderRadius: 4,
            padding: 8,
            fontSize: 11,
            fontFamily: 'monospace',
            whiteSpace: 'pre-wrap',
            wordBreak: 'break-word',
            minHeight: 200,
            maxHeight: 480,
            overflowY: 'auto',
          }}
        >
          {filteredLines !== null ? (
            filteredLines.length === 0 ? (
              <span style={{ color: 'var(--text-dim)' }}>
                (no lines match the current filter)
              </span>
            ) : (
              filteredLines.map((line, i) => (
                <div key={`${i}-${line.slice(0, 32)}`}>{renderHighlighted(line, filter)}</div>
              ))
            )
          ) : (
            content || (note ? '' : '(no log entries to show)')
          )}
        </div>
      )}
    </div>
  );
}
