// Shared database-admin auth modal.
//
// Collects Database Admin Account credentials before a destructive
// write. Used by ExportsPanel (snapshot / JSON-archive deletes) and
// UserManagementPanel (global-tombstone unhide + managed-user writes).
// The optional "Keep JSON archive" checkbox renders only when
// showKeepJson is set; non-snapshot callers leave it off and ignore
// the keepJson flag handed to onSubmit.

import { useState } from 'react';
import { Modal } from './Modal';

export function DbAdminAuthModal({
  action,
  onCancel,
  onSubmit,
  showKeepJson = false,
  keepJsonHint,
}: {
  action: string;
  onCancel: () => void;
  // ``keepJson`` is forwarded only when ``showKeepJson`` is true; the
  // caller can ignore it for non-snapshot delete flows.
  onSubmit: (
    creds: { username: string; password: string },
    options: { keepJson: boolean },
  ) => Promise<void>;
  // When true, render a "Keep JSON archive" checkbox above the
  // confirm button. Defaults off. Used by the snapshot delete flow
  // so the operator can move the .plexexport.json sidecar into the
  // JSON archives panel instead of deleting it with the .db.
  showKeepJson?: boolean;
  // Short paragraph rendered next to the checkbox to explain what
  // it does. Caller-supplied so the wording can match the specific
  // destructive action.
  keepJsonHint?: string;
}) {
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [keepJson, setKeepJson] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const canSubmit = !submitting && username.length > 0 && password.length > 0;

  const submit = async () => {
    if (!canSubmit) return;
    setError(null);
    setSubmitting(true);
    try {
      await onSubmit({ username, password }, { keepJson });
    } catch (e) {
      setError(String(e));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Modal onClose={onCancel} title="Confirm with database admin" width={480}>
        <span className="help" style={{ display: 'block', color: 'var(--text-dim)', fontSize: 12, marginBottom: 12 }}>
          <strong>{action}</strong> requires Database Admin Account
          credentials. Set up or rotate these under
          Settings → Account Management → Database Admin Account.
        </span>
        {error && <div className="banner error">{error}</div>}
        <label className="field">
          <span className="label">Database admin username</span>
          <input
            type="text"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            autoComplete="username"
            autoFocus
          />
        </label>
        <label className="field">
          <span className="label">Database admin password</span>
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete="current-password"
          />
        </label>
        {showKeepJson && (
          <label className="switch" style={{ marginTop: 12 }}>
            <input
              type="checkbox"
              checked={keepJson}
              onChange={(e) => setKeepJson(e.target.checked)}
            />
            <span>
              Keep JSON archive
              {keepJsonHint && (
                <span className="help" style={{ display: 'block', color: 'var(--text-dim)', fontSize: 11, marginTop: 2 }}>
                  {keepJsonHint}
                </span>
              )}
            </span>
          </label>
        )}
        <div className="row-buttons" style={{ marginTop: 12 }}>
          <button className="primary" disabled={!canSubmit} onClick={submit}>
            {submitting ? 'Confirming…' : 'Confirm'}
          </button>
          <button onClick={onCancel} disabled={submitting}>Cancel</button>
        </div>
    </Modal>
  );
}
