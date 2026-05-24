// Shared display formatters.
//
// Consolidates helpers that were independently re-implemented across
// many panels. Each function here is the single canonical version;
// panels import from this module rather than defining their own.

/**
 * Human-readable byte size. "0 B" for an exact zero, "-" for an
 * invalid / negative / non-finite input. Scales B -> KB -> MB -> GB
 * -> TB. One decimal place, dropped when the value is a whole-unit
 * size or >= 100 (so "512 KB", not "512.0 KB").
 */
export function formatBytes(n: number): string {
  if (!Number.isFinite(n) || n < 0) return '-';
  if (n === 0) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let v = n;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i += 1;
  }
  return `${v.toFixed(i === 0 || v >= 100 ? 0 : 1)} ${units[i]}`;
}

/**
 * Coerce an unknown thrown value to a display string: the Error's
 * message when it is an Error, otherwise String() of the value.
 */
export function errorText(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

/**
 * Locale date-time string from a UNIX epoch in SECONDS. Returns "-"
 * for a non-finite, zero, or negative input (no "Invalid Date" or
 * 1969 leakage).
 */
export function formatTimestamp(epochSeconds: number): string {
  if (!Number.isFinite(epochSeconds) || epochSeconds <= 0) return '-';
  return new Date(epochSeconds * 1000).toLocaleString();
}
