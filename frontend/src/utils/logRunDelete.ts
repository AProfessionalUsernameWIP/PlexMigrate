// Shared per-run log deletion handler. The two log panels
// (LogsPanel + ServerLogsPanel) need identical API+message+refresh
// behaviour but each owns its own state setters and "is this the
// active run?" check, so the helper takes the wiring as a callback
// pack rather than swallowing the state shape into a hook.

import { api } from '../api';


export interface LogRunDeleteOpts {
  setError: (e: string | null) => void;
  setInfo: (i: string | null) => void;
  onActiveRunDeleted?: (name: string) => void;
  refresh: () => Promise<void>;
}


export async function performLogRunDelete(
  name: string,
  opts: LogRunDeleteOpts,
): Promise<void> {
  opts.setError(null);
  opts.setInfo(null);
  try {
    const r = await api.deleteLogRun(name);
    opts.setInfo(
      `Deleted ${r.deleted} (${r.file_count} file${r.file_count === 1 ? '' : 's'})`
      + (r.errors.length ? ` with ${r.errors.length} error(s): ${r.errors.join(' · ')}` : '.'),
    );
    opts.onActiveRunDeleted?.(name);
    await opts.refresh();
  } catch (e) {
    opts.setError(String(e));
  }
}
