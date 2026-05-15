// Reusable live-tail viewer.
//
// Owns the offset tracking, sticky-bottom scroll behaviour, 2 s poll
// loop, and the body buffer for one log file inside one run dir. Used
// by:
//   * LogsPanel - full-size, manual Reload button.
//   * DashboardPanel - small height, embedded under the JobHeader so
//     the user can watch the current run without switching tabs.
//
// Always polls when ``finished`` is false; freezes when true (typically
// when the run dir has gained a _PASS / _FAIL suffix). The frontend
// recognises that suffix and stops the loop so we don't 404-spam after
// the run renames the directory.

import { useEffect, useMemo, useRef, useState } from 'react';
import { api, LogFileContent } from '../api';
import { isRunGoneError } from './DashboardPanel';

const TAIL_POLL_MS = 2000;

export interface LogTailerProps {
  runName: string;
  fileName: string;
  height?: number;          // px; defaults to a generous 480 for LogsPanel.
  liveDefault?: boolean;    // default true; pass false to start paused.
  // When true, freeze the tail (no more polls). LogsPanel passes
  // run-dir-suffix detection; DashboardPanel passes the job state.
  externalFreeze?: boolean;
}

export function LogTailer({
  runName,
  fileName,
  height = 480,
  liveDefault = true,
  externalFreeze = false,
}: LogTailerProps) {
  const [body, setBody] = useState<string>('');
  const [meta, setMeta] = useState<LogFileContent | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [liveTail, setLiveTail] = useState<boolean>(liveDefault);
  const [lastPolledAt, setLastPolledAt] = useState<number | null>(null);
  // v0.9.7 Item 8: client-side keyword filter. Empty = no filter,
  // render the raw text blob (current behaviour, zero performance
  // regression on idle viewing). Non-empty = split into lines and
  // filter case-insensitively. The cost of per-line rendering is
  // only paid when the operator is actively filtering.
  const [filter, setFilter] = useState<string>('');

  const offsetRef = useRef<number>(0);
  const bodyRef = useRef<HTMLDivElement | null>(null);
  // Sticky-bottom: pin to bottom unless the user scrolls up. When they
  // do, we pause auto-scroll so they can read older lines without the
  // viewport yanking them back every 2 seconds.
  const stickyRef = useRef<boolean>(true);

  // Initial full read whenever runName/fileName changes.
  useEffect(() => {
    if (!runName || !fileName) {
      setBody('');
      setMeta(null);
      return;
    }
    offsetRef.current = 0;
    setError(null);
    api.readLogFile(runName, fileName, 0)
      .then((r) => {
        setMeta(r);
        setBody(r.content);
        offsetRef.current = r.next_offset;
        setLastPolledAt(Date.now());
        stickyRef.current = true;
        requestAnimationFrame(() => {
          if (bodyRef.current) bodyRef.current.scrollTop = bodyRef.current.scrollHeight;
        });
      })
      .catch((e) => {
        // Suppress the transient 404 when the run directory is
        // renamed (..._PASS / _FAIL) at job end. The parent freezes
        // the tail moments later, so surfacing it would just flash a
        // banner the user can't act on.
        if (isRunGoneError(e)) return;
        setError(String(e));
      });
  }, [runName, fileName]);

  // Poll loop - appends only the new bytes via ?since=offset.
  useEffect(() => {
    if (!liveTail || externalFreeze || !runName || !fileName) return;
    const tick = window.setInterval(async () => {
      try {
        const r = await api.readLogFile(runName, fileName, offsetRef.current);
        if (r.content.length > 0) setBody((prev) => prev + r.content);
        offsetRef.current = r.next_offset;
        setMeta(r);
        setLastPolledAt(Date.now());
      } catch (e) {
        if (isRunGoneError(e)) { window.clearInterval(tick); return; }
        setError(String(e));
      }
    }, TAIL_POLL_MS);
    return () => window.clearInterval(tick);
  }, [liveTail, externalFreeze, runName, fileName]);

  // Keep the viewport pinned to the bottom whenever new bytes arrive
  // *and* the user hasn't scrolled away.
  useEffect(() => {
    if (!bodyRef.current || !stickyRef.current) return;
    bodyRef.current.scrollTop = bodyRef.current.scrollHeight;
  }, [body]);

  const onBodyScroll = () => {
    const el = bodyRef.current;
    if (!el) return;
    stickyRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
  };

  const onReload = async () => {
    if (!runName || !fileName) return;
    offsetRef.current = 0;
    setError(null);
    try {
      const r = await api.readLogFile(runName, fileName, 0);
      setBody(r.content);
      setMeta(r);
      offsetRef.current = r.next_offset;
      setLastPolledAt(Date.now());
      stickyRef.current = true;
    } catch (e) {
      setError(String(e));
    }
  };

  const statusLabel = externalFreeze
    ? 'Tail paused - run finished'
    : liveTail
      ? `Live · last polled ${lastPolledAt ? new Date(lastPolledAt).toLocaleTimeString() : '-'}`
      : 'Paused';

  // v0.9.7 Item 8: when the filter is active, split the body once
  // per (body, filter) change and render matched lines with the
  // matched substring highlighted. When the filter is empty, fall
  // through to the raw-blob render path (no split, no per-line
  // React elements) so unfiltered viewing has zero overhead.
  const filteredLines = useMemo(() => {
    if (!filter) return null;
    const needle = filter.toLowerCase();
    const out: string[] = [];
    // Split-on-newline only happens when the operator types into
    // the filter input; otherwise the raw body renders unchanged.
    for (const line of body.split('\n')) {
      if (line.toLowerCase().includes(needle)) out.push(line);
    }
    return out;
  }, [body, filter]);

  return (
    <div>
      <div style={{
        display: 'flex', justifyContent: 'flex-end', alignItems: 'center',
        gap: 12, marginBottom: 6, fontSize: 12,
      }}>
        <span style={{ color: 'var(--text-dim)' }}>{statusLabel}</span>
        {!externalFreeze && (
          <label style={{ display: 'inline-flex', alignItems: 'center', gap: 4 }}>
            <input
              type="checkbox"
              checked={liveTail}
              onChange={(e) => setLiveTail(e.target.checked)}
            />
            Live tail
          </label>
        )}
        <button onClick={onReload}>Reload</button>
      </div>
      {error && <div className="banner error" style={{ marginBottom: 6 }}>{error}</div>}
      {meta?.truncated && (
        <div style={{ color: 'var(--warn)', fontSize: 12, marginBottom: 6 }}>
          Truncated - older bytes not shown; new lines still append as they arrive.
        </div>
      )}

      {/* v0.9.7 Item 8: case-insensitive keyword filter. Empty
          input renders the raw blob below (current fast path);
          non-empty input switches to per-line filtered render
          with the matched substring highlighted. */}
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
          style={{ flex: 1, fontFamily: 'var(--mono, monospace)' }}
        />
        {filter && (
          <>
            <span style={{ color: 'var(--text-dim)', fontSize: 11 }}>
              {filteredLines?.length ?? 0} match{(filteredLines?.length ?? 0) === 1 ? '' : 'es'}
            </span>
            <button
              onClick={() => setFilter('')}
              title="Clear filter"
              style={{ padding: '2px 8px' }}
            >
              ×
            </button>
          </>
        )}
      </div>

      <div
        className="logview"
        ref={bodyRef}
        onScroll={onBodyScroll}
        style={{ height, maxHeight: height }}
      >
        {filteredLines === null ? (
          // Fast path: raw text blob, browser-native rendering.
          // Zero overhead vs. pre-v0.9.7.
          body || 'Loading…'
        ) : filteredLines.length === 0 ? (
          <span style={{ color: 'var(--text-dim)' }}>
            No lines match {JSON.stringify(filter)}.
          </span>
        ) : (
          filteredLines.map((line, i) => (
            <div key={i}>{renderHighlighted(line, filter)}</div>
          ))
        )}
      </div>
    </div>
  );
}

// ── Helpers (v0.9.7 Item 8) ─────────────────────────────────────────────────

// Escape a string so it can be embedded in a RegExp literal without
// interpreting special characters. The filter input is a plain
// substring - operators don't expect regex semantics from typing
// e.g. "(error)" into the box.
function escapeForRegex(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

// Split ``line`` around every case-insensitive occurrence of
// ``needle`` and wrap the matched chunks in ``<mark>`` so the
// matched substring is visually highlighted. Returns a React node
// (array of strings + spans) suitable for direct rendering. Empty
// ``needle`` short-circuits to the plain string.
function renderHighlighted(line: string, needle: string): React.ReactNode {
  if (!needle) return line;
  const re = new RegExp(escapeForRegex(needle), 'gi');
  const parts: React.ReactNode[] = [];
  let lastIndex = 0;
  let m: RegExpExecArray | null;
  let keyCount = 0;
  while ((m = re.exec(line)) !== null) {
    if (m.index > lastIndex) {
      parts.push(line.slice(lastIndex, m.index));
    }
    parts.push(
      <mark key={keyCount++} className="log-match">
        {m[0]}
      </mark>
    );
    lastIndex = m.index + m[0].length;
    // Avoid an infinite loop on zero-length matches (shouldn't happen
    // with our escaping, but defensive).
    if (m.index === re.lastIndex) re.lastIndex++;
  }
  if (lastIndex < line.length) {
    parts.push(line.slice(lastIndex));
  }
  return parts;
}
