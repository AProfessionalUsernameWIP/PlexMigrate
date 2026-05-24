// Logs + application-log REST helpers extracted from the monolithic
// ``api`` const during Phase 3c. Covers per-run log browsing,
// app-level rotating logs, and the on-disk path lookup.

import { http } from './core';
import type { LogFile, LogFileContent, LogRun } from './types';

export const logsApi = {
  // Logs
  listLogRuns: () => http<LogRun[]>('/api/logs'),

  // Application-level logs (Settings > Application Logs). Admin-gated.
  //
  // Categories: 'db-access' (db_access.log) is the only category
  // with a real backing log file today; 'auth', 'network', 'debug'
  // are scaffolded placeholders.
  //
  // ``tailBytes`` defaults to 0, which the backend resolves to the
  // end user-tuned log_read_max_bytes (default 16 MB). Pass an
  // explicit non-zero value to read less. ``since`` selects polling
  // mode (since=0 returns the tail; since=<offset> returns bytes
  // from offset to current end). ``backup`` switches the read
  // target from the active file to a specific rotated backup
  // (e.g. "db_access.log.1"); when set, the endpoint ignores
  // ``since`` and always returns the tail of that backup.
  //
  // Response distinguishes two truncation cases:
  //
  //   * head_omitted: initial-tail of a file larger than the read
  //     cap. Older bytes are still in the same file.
  //   * rotated_during_poll: the polling cursor landed past the
  //     file's current end. Logrotate fired; older bytes are now
  //     in the .1 (or higher) backup, listed in ``backups``.
  readAppLog: (
    category: string,
    tailBytes = 0,
    since = 0,
    backup = '',
  ) =>
    http<{
      category: string;
      path: string;
      size_bytes: number;
      next_offset: number;
      head_omitted: boolean;
      rotated_during_poll: boolean;
      content: string;
      backups: Array<{ filename: string; size_bytes: number }>;
      note: string;
    }>(
      `/api/logs/app/${encodeURIComponent(category)}`
        + `?tail_bytes=${encodeURIComponent(tailBytes)}`
        + `&since=${encodeURIComponent(since)}`
        + (backup ? `&backup=${encodeURIComponent(backup)}` : ''),
    ),
  // Resolved on-disk paths for every application-level log
  // file. Surfaced in Settings > Logging > Log file locations so operators
  // can `tail -f` from a shell. Admin-gated; thin wrapper around
  // GET /api/logs/paths.
  listLogPaths: () =>
    http<{ data_dir: string; paths: Record<string, string> }>(`/api/logs/paths`),
  listLogFiles: (run: string) => http<LogFile[]>(`/api/logs/${encodeURIComponent(run)}`),
  readLogFile: (run: string, name: string, since: number = 0) =>
    http<LogFileContent>(
      `/api/logs/${encodeURIComponent(run)}/${encodeURIComponent(name)}` +
      (since > 0 ? `?since=${since}` : ''),
    ),
  // Escape hatch for files bigger than the
  // 16 MB in-browser tail cap. Returns the URL only; callers fetch
  // with the auth bearer and trigger a save dialog (same pattern as
  // ExportsPanel's downloadBlob).
  logFileDownloadUrl: (run: string, name: string) =>
    `/api/logs/${encodeURIComponent(run)}/${encodeURIComponent(name)}/download`,
  // Whole-run download as a zip - bundles every file in the run dir.
  logRunZipUrl: (run: string) =>
    `/api/logs/${encodeURIComponent(run)}/zip`,
  deleteLogRun: (run: string) =>
    http<{ deleted: string; file_count: number; errors: string[] }>(
      `/api/logs/${encodeURIComponent(run)}`,
      { method: 'DELETE' },
    ),
  deleteAllLogRuns: () =>
    http<{ deleted: number; errors: string[] }>(
      '/api/logs',
      { method: 'DELETE' },
    ),
};
