// First-boot setup screen - rendered exactly once per fresh install,
// when /api/auth/status reports ``setup_needed: true``. Creates the
// initial admin account via /api/auth/setup, which self-locks after
// the first call so this page never re-renders on subsequent boots.
//
// The setup endpoint returns an access token alongside the user row,
// so a successful setup transitions straight into the main UI without
// a second round-trip through the login form.

import { useState } from 'react';
import { api, AuthSession } from '../api';

interface Props {
  onSetupComplete: (session: AuthSession) => void;
}

export function SetupPage({ onSetupComplete }: Props) {
  const [username, setUsername] = useState('');
  const [displayName, setDisplayName] = useState('');
  const [password, setPassword] = useState('');
  const [confirm, setConfirm] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Client-side gates so the user sees the obvious problems before a
  // round-trip. The backend re-validates anyway - these are UX, not
  // security.
  const passwordsMatch = password === confirm;
  const passwordLongEnough = password.length >= 8;
  const usernameOk = username.trim().length > 0;
  const canSubmit = !busy && usernameOk && passwordLongEnough && passwordsMatch;

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    setBusy(true);
    try {
      const dn = displayName.trim();
      const session = await api.authSetup(username, password, dn || undefined);
      onSetupComplete(session);
    } catch (err) {
      setError(String(err).replace(/^Error: /, ''));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">PlexMigrate</div>
      </header>
      <main className="main" style={{ display: 'flex', justifyContent: 'center', paddingTop: 40 }}>
        <form className="panel" style={{ width: 480, maxWidth: '90vw' }} onSubmit={submit}>
          <h2>Create the root admin account</h2>
          <p style={{ color: 'var(--text-dim)', fontSize: 13, marginTop: 0 }}>
            Auth is always on as of PR-A2. Create the first administrator
            account to continue - this account has full privileges and is the
            only one that can manage other user accounts. The setup screen
            self-locks after the first user is created; additional accounts are
            managed from inside the app under <strong>Settings → User Accounts</strong>.
          </p>
          {error && <div className="banner error">{error}</div>}
          <label className="field">
            <span className="label">Username</span>
            <span className="help">
              Used to sign in. Lowercase, no spaces is conventional but anything non-empty works.
            </span>
            <input
              type="text"
              autoFocus
              autoComplete="username"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
            />
          </label>
          <label className="field">
            <span className="label">Display name (optional)</span>
            <span className="help">
              Shown in the UI instead of the username. Can be changed later from
              Settings → Account.
            </span>
            <input
              type="text"
              autoComplete="off"
              value={displayName}
              onChange={(e) => setDisplayName(e.target.value)}
            />
          </label>
          <label className="field">
            <span className="label">Password</span>
            <span className="help">
              Minimum 8 characters. Stored as a bcrypt hash - never sent anywhere outside
              this server.
            </span>
            <input
              type="password"
              autoComplete="new-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
          </label>
          <label className="field">
            <span className="label">Confirm password</span>
            <input
              type="password"
              autoComplete="new-password"
              value={confirm}
              onChange={(e) => setConfirm(e.target.value)}
            />
            {confirm && !passwordsMatch && (
              <span className="help" style={{ color: 'var(--bad)' }}>
                Passwords don't match.
              </span>
            )}
          </label>
          <div className="row-buttons">
            <button type="submit" className="primary" disabled={!canSubmit}>
              {busy ? 'Creating…' : 'Create admin and sign in'}
            </button>
          </div>
        </form>
      </main>
    </div>
  );
}
