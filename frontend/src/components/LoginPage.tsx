// Login screen - rendered when no token is present in App state.
//
// LoginBugFix1 - token is React-state-only. No "Remember me" toggle;
// no localStorage / sessionStorage / cookie write of the access
// token anywhere in the codebase. A fresh load (reload, new tab, new
// browser, new device) starts at the login screen unconditionally.
// The only way past it is a successful POST /api/auth/login in this
// browser session.

import { useState } from 'react';
import { api, AuthSession } from '../api';

interface Props {
  onLogin: (session: AuthSession) => void;
}

export function LoginPage({ onLogin }: Props) {
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    setBusy(true);
    try {
      const session = await api.authLogin(username, password);
      onLogin(session);
    } catch (err) {
      // 401s come back as Error("401 Unauthorized: …"). Show the
      // server's message when present, otherwise a generic line.
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
        <form className="panel" style={{ width: 380, maxWidth: '90vw' }} onSubmit={submit}>
          <h2>Sign in</h2>
          {error && <div className="banner error">{error}</div>}
          <label className="field">
            <span className="label">Username</span>
            <input
              type="text"
              autoFocus
              autoComplete="username"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
            />
          </label>
          <label className="field">
            <span className="label">Password</span>
            <input
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
          </label>
          <span className="help" style={{ marginTop: 4, fontSize: 12, color: 'var(--text-dim)' }}>
            Sessions are not stored. Closing the tab, reloading, or opening
            the app in a new tab will require signing in again.
          </span>
          <div className="row-buttons" style={{ marginTop: 10 }}>
            <button
              type="submit"
              className="primary"
              disabled={busy || !username.trim() || !password}
            >
              {busy ? 'Signing in…' : 'Sign in'}
            </button>
          </div>
        </form>
      </main>
    </div>
  );
}
