// Shared substring-highlight helper.
//
// Used by the log viewers (LogTailer, ApplicationLogsPanel) to wrap
// every case-insensitive occurrence of a filter substring in a
// <mark> for visual emphasis. Consolidated here so the two panels
// share one canonical implementation rather than each defining its
// own.

import type { ReactNode } from 'react';

/**
 * Escape a string so it can be embedded in a RegExp literal without
 * interpreting special characters. The filter input is a plain
 * substring - end users don't expect regex semantics from typing
 * e.g. "(error)" into the box.
 */
export function escapeForRegex(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

/**
 * Split ``line`` around every case-insensitive occurrence of
 * ``needle`` and wrap the matched chunks in a ``<mark>`` so the
 * matched substring is visually highlighted. Returns a React node
 * (array of strings + spans) suitable for direct rendering. Empty
 * ``needle`` short-circuits to the plain string.
 */
export function renderHighlighted(line: string, needle: string): ReactNode {
  if (!needle) return line;
  const re = new RegExp(escapeForRegex(needle), 'gi');
  const parts: ReactNode[] = [];
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
