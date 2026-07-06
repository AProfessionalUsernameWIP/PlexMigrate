// Auth-aware blob download. The auth middleware rejects a plain
// <a href> navigation because it can't carry the bearer token, so we
// fetch as a blob, then trigger a save dialog via a synthetic
// <a download> element. Shared utility consolidating the inline copies
// that lived in LogsPanel, ServerLogsPanel, and DashboardDownloadLogsPanel.
//
// Why this lives here: the helper has zero React-coupling (no hooks,
// no JSX) and the auth surface is also non-React, so utils/ is the
// right home. The setError callback is intentionally a plain function
// so callers can wire it to their own state shape without inheriting
// a hook contract.

import { getAccessToken } from '../api';


export async function downloadBlob(
  url: string,
  filename: string,
  setError: (e: string | null) => void,
): Promise<void> {
  try {
    const token = getAccessToken();
    const headers: Record<string, string> = {};
    if (token) headers['Authorization'] = `Bearer ${token}`;
    const res = await fetch(url, { headers });
    if (!res.ok) {
      const text = await res.text().catch(() => res.statusText);
      throw new Error(`${res.status}: ${text}`);
    }
    const blob = await res.blob();
    const objectUrl = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = objectUrl;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(objectUrl);
  } catch (e) {
    setError(`Download failed: ${e}`);
  }
}
