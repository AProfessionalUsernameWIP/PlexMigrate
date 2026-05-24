// First-run mirror prompt. Renders as a non-modal panel at the top
// of the Servers panel: the mirror is server-scoped, not a job, so
// it belongs near server registration rather than overlaying the
// whole UI.
//
// The component renders nothing when ``open`` is false. The parent
// (ServersPanel) gates ``open`` on the engine_mirror_first_run_dialog_seen
// tunable and flips that tunable to true once the operator picks
// either action; the panel never re-appears after acknowledgment.

import { useState } from 'react';
import { api } from '../api';


export interface FirstRunMirrorDialogProps {
  open: boolean;
  onClose: (decision: 'yes' | 'not_now' | 'cancel') => void;
}


export function FirstRunMirrorDialog({
  open,
  onClose,
}: FirstRunMirrorDialogProps) {
  const [submitting, setSubmitting] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const [serverCount, setServerCount] = useState<number | null>(null);

  if (!open) return null;

  const handleYes = async () => {
    setSubmitting(true);
    setError(null);
    try {
      const r = await api.syncAllServerMirrors();
      setServerCount(r.results.length);
      onClose('yes');
    } catch (e) {
      setError((e as Error).message || 'Failed to start mirror sync.');
    } finally {
      setSubmitting(false);
    }
  };

  const handleNotNow = () => {
    onClose('not_now');
  };

  return (
    <section
      className="first-run-mirror-panel banner info"
      role="region"
      aria-labelledby="first-run-mirror-title"
      style={{
        marginBottom: 16,
        padding: 16,
        border: '1px solid var(--accent, #2b6cb0)',
        borderRadius: 6,
      }}
    >
      <h3 id="first-run-mirror-title" style={{ marginTop: 0 }}>
        Speed up cross-server jobs?
      </h3>
      <p style={{ marginBottom: 8 }}>
        We can mirror your server library metadata to a local database
        so playlist transfers, restores, and direct transfers resolve
        items in milliseconds instead of seconds.
      </p>
      <p style={{ marginBottom: 8 }}>
        The mirror updates in the background after each snapshot. Your
        snapshot accuracy is unchanged: snapshots always read live
        from the server, never from the mirror.
      </p>
      {error && (
        <div className="banner error" role="alert" style={{ marginBottom: 8 }}>
          {error}
        </div>
      )}
      {serverCount !== null && (
        <div className="banner info" style={{ marginBottom: 8 }}>
          Mirror sync started for {serverCount} server(s). Track
          per-server progress with the Drift History tab.
        </div>
      )}
      <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
        <button
          type="button"
          disabled={submitting}
          onClick={handleNotNow}
        >
          Not now
        </button>
        <button
          type="button"
          className="primary"
          disabled={submitting}
          onClick={handleYes}
        >
          {submitting ? 'Starting sync...' : 'Yes, sync now'}
        </button>
      </div>
    </section>
  );
}
