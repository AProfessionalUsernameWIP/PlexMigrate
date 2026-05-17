// Item 1 (admin-management plan, 2026-05-15): forced upgrade-split modal.
//
// Fires once, on the first authenticated load of a LEGACY install
// (auth_setup_version = 1 in settings). The legacy end user (who is
// currently the only root_admin on the system) must create a SEPARATE
// dedicated root account here before the main UI is allowed to mount.
// On success, the end user's existing account is demoted to ``admin``
// and the install is marked setup_version=2. The frontend forces a
// fresh login afterward because the end user's current JWT still
// claims root_admin and the new state needs to be reflected.
//
// The split is non-dismissable per end user decision: legacy installs
// MUST split before further use of the application.

import { useState } from 'react';
import { api } from '../api';

interface Props {
  callerUsername: string;
  onComplete: () => void;
}

export function UpgradeSplitModal({ callerUsername, onComplete }: Props) {
  const [callerPassword, setCallerPassword] = useState('');
  const [rootUsername, setRootUsername] = useState('');
  const [rootDisplayName, setRootDisplayName] = useState('');
  const [rootPassword, setRootPassword] = useState('');
  const [rootConfirm, setRootConfirm] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState(false);

  const passwordsMatch = rootPassword === rootConfirm;
  const rootCredsValid =
    rootUsername.trim().length > 0 &&
    rootUsername.trim() !== callerUsername &&
    rootPassword.length >= 8 &&
    passwordsMatch;
  const canSubmit = !busy && callerPassword.length > 0 && rootCredsValid;

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!canSubmit) return;
    setError(null);
    setBusy(true);
    try {
      await api.authUpgradeSplit({
        caller_password: callerPassword,
        root_username: rootUsername.trim(),
        root_password: rootPassword,
        root_display_name: rootDisplayName.trim() || undefined,
      });
      // Clear local password state immediately on success.
      setCallerPassword('');
      setRootPassword('');
      setRootConfirm('');
      setSuccess(true);
    } catch (err) {
      setError(String(err).replace(/^Error: /, ''));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div
      style={{
        position: 'fixed',
        inset: 0,
        background: 'rgba(0,0,0,0.7)',
        zIndex: 1100,
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        padding: 20,
        overflowY: 'auto',
      }}
    >
      <form
        onSubmit={submit}
        className="panel"
        style={{ width: 560, maxWidth: '95vw' }}
      >
        <h2 style={{ marginTop: 0 }}>One-time account split required</h2>
        <p style={{ fontSize: 13, color: 'var(--text-dim)', lineHeight: 1.5, marginTop: 0 }}>
          This install was set up with a single account that combined
          everyday admin work and privileged root actions in one
          credential. The current security model separates these:
          your account becomes a day-to-day <strong>admin</strong>,
          and a NEW dedicated <strong>root</strong> account is the
          only credential that can manage other users or grant root
          permission elsewhere.
        </p>
        <p style={{ fontSize: 13, color: 'var(--text-dim)', lineHeight: 1.5 }}>
          You cannot skip this step. After you submit, your account
          becomes an admin and you will be signed out so the change
          can take effect. Sign back in with your existing username and
          password (now an admin account) for everyday work; use the
          new root credentials only when elevating for privileged
          actions.
        </p>

        {success ? (
          <>
            <div className="banner good">
              Split complete. Your account is now an admin. Sign back in to continue.
            </div>
            <div className="row-buttons">
              <button type="button" className="primary" onClick={onComplete}>
                Sign out and continue
              </button>
            </div>
          </>
        ) : (
          <>
            {error && <div className="banner error">{error}</div>}

            <h3 style={{ marginBottom: 0 }}>Confirm your current password</h3>
            <label className="field">
              <span className="label">Your current password</span>
              <span className="help">
                The password you just used to sign in as <code>{callerUsername}</code>.
                Required so this action cannot be hijacked by an opened session.
              </span>
              <input
                type="password"
                autoComplete="current-password"
                value={callerPassword}
                onChange={(e) => setCallerPassword(e.target.value)}
              />
            </label>

            <h3 style={{ marginBottom: 0 }}>New root account credentials</h3>
            <label className="field">
              <span className="label">Root username</span>
              <span className="help">
                Must differ from your own username (<code>{callerUsername}</code>).
                Conventional choices: <code>root</code>, <code>admin-root</code>,
                or anything memorable that signals "this is the privileged credential".
              </span>
              <input
                type="text"
                autoComplete="off"
                value={rootUsername}
                onChange={(e) => setRootUsername(e.target.value)}
              />
              {rootUsername.trim() === callerUsername && rootUsername.trim() !== '' && (
                <span className="help" style={{ color: 'var(--bad)' }}>
                  Root username must differ from your own.
                </span>
              )}
            </label>
            <label className="field">
              <span className="label">Root display name (optional)</span>
              <input
                type="text"
                autoComplete="off"
                value={rootDisplayName}
                onChange={(e) => setRootDisplayName(e.target.value)}
              />
            </label>
            <label className="field">
              <span className="label">Root password</span>
              <span className="help">
                Minimum 8 characters. Pick a strong, distinct password and save it
                somewhere safe; losing every root password without a backup root
                account requires deleting <code>server_data/auth.db</code> to
                redo setup (Plex data is preserved).
              </span>
              <input
                type="password"
                autoComplete="new-password"
                value={rootPassword}
                onChange={(e) => setRootPassword(e.target.value)}
              />
            </label>
            <label className="field">
              <span className="label">Confirm root password</span>
              <input
                type="password"
                autoComplete="new-password"
                value={rootConfirm}
                onChange={(e) => setRootConfirm(e.target.value)}
              />
              {rootConfirm && !passwordsMatch && (
                <span className="help" style={{ color: 'var(--bad)' }}>
                  Passwords don't match.
                </span>
              )}
            </label>

            <div className="row-buttons">
              <button type="submit" className="primary" disabled={!canSubmit}>
                {busy ? 'Creating root account…' : 'Complete split'}
              </button>
            </div>
          </>
        )}
      </form>
    </div>
  );
}
