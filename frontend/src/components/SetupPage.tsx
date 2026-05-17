// First-boot setup screen - rendered exactly once per fresh install,
// when /api/auth/status reports ``setup_needed: true``.
//
// Item 1 (admin-management plan, 2026-05-15): the setup is a TWO-STEP
// wizard now. Step 1 collects the end user's day-to-day admin credentials.
// Step 2 collects a SEPARATE root account credential. Both are submitted
// atomically via /api/auth/setup-v2 so a partial setup cannot leave the
// install with only one account.
//
// The setup endpoint returns an access token alongside the user row
// (the admin account), so a successful setup transitions straight
// into the main UI signed in as the admin. The root account is for
// privileged actions only; the end user re-enters its password via
// sudo-style elevation when those actions fire.

import { useState } from 'react';
import { api, AuthSession } from '../api';

interface Props {
  onSetupComplete: (session: AuthSession) => void;
}

interface CredentialFields {
  username: string;
  displayName: string;
  password: string;
  confirm: string;
}

function emptyCreds(): CredentialFields {
  return { username: '', displayName: '', password: '', confirm: '' };
}

export function SetupPage({ onSetupComplete }: Props) {
  const [step, setStep] = useState<1 | 2>(1);
  const [admin, setAdmin] = useState<CredentialFields>(emptyCreds);
  const [root, setRoot] = useState<CredentialFields>(emptyCreds);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const credsValid = (c: CredentialFields) =>
    c.username.trim().length > 0 &&
    c.password.length >= 8 &&
    c.password === c.confirm;
  const adminValid = credsValid(admin);
  const rootValid = credsValid(root);
  const usernamesDiffer =
    admin.username.trim() !== root.username.trim() ||
    admin.username.trim() === '';

  const goToStep2 = (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    if (!adminValid) return;
    setStep(2);
  };

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    if (!rootValid || !usernamesDiffer) return;
    setBusy(true);
    try {
      const session = await api.authSetupV2({
        admin_username: admin.username.trim(),
        admin_password: admin.password,
        admin_display_name: admin.displayName.trim() || undefined,
        root_username: root.username.trim(),
        root_password: root.password,
        root_display_name: root.displayName.trim() || undefined,
      });
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
        <form
          className="panel"
          style={{ width: 520, maxWidth: '90vw' }}
          onSubmit={step === 1 ? goToStep2 : submit}
        >
          <div style={{ display: 'flex', gap: 8, marginBottom: 6, fontSize: 12, color: 'var(--text-dim)' }}>
            <span style={{ fontWeight: step === 1 ? 'bold' : 'normal', color: step === 1 ? 'var(--text)' : undefined }}>
              Step 1 of 2: Admin account
            </span>
            <span>•</span>
            <span style={{ fontWeight: step === 2 ? 'bold' : 'normal', color: step === 2 ? 'var(--text)' : undefined }}>
              Step 2 of 2: Root account
            </span>
          </div>

          {step === 1 ? (
            <>
              <h2>Create your admin account</h2>
              <p style={{ color: 'var(--text-dim)', fontSize: 13, marginTop: 0 }}>
                This is the account you'll use day-to-day to run jobs, configure
                servers, and manage schedules. In the next step you'll create
                a SEPARATE root account for privileged actions like creating
                other users or granting root permission. Treat them like a
                regular user and the root password on a Linux system: use the
                admin account most of the time, switch to root only when
                you need to.
              </p>
              {error && <div className="banner error">{error}</div>}
              <CredentialFieldset value={admin} onChange={setAdmin} />
              <div className="row-buttons">
                <button type="submit" className="primary" disabled={!adminValid}>
                  Next: create root account
                </button>
              </div>
            </>
          ) : (
            <>
              <h2>Create the separate root account</h2>
              <p style={{ color: 'var(--text-dim)', fontSize: 13, marginTop: 0 }}>
                The root account is the only credential that can manage other
                users, grant root permission, or perform destructive admin
                actions. Pick a DIFFERENT username and a strong, distinct
                password. <strong>Save this password somewhere safe</strong>:
                if you lose both root passwords (this one, and any future
                ones you grant), the only recovery is to delete
                <code> server_data/auth.db </code> and redo this setup. Your
                Plex data is preserved either way.
              </p>
              {error && <div className="banner error">{error}</div>}
              {!usernamesDiffer && root.username.trim() && (
                <div className="banner error">
                  Root and admin accounts must have different usernames.
                </div>
              )}
              <CredentialFieldset
                value={root}
                onChange={setRoot}
                usernameHelp="Must differ from your admin username."
              />
              <div className="row-buttons" style={{ gap: 8 }}>
                <button type="button" onClick={() => { setError(null); setStep(1); }}>
                  Back
                </button>
                <button
                  type="submit"
                  className="primary"
                  disabled={busy || !rootValid || !usernamesDiffer}
                >
                  {busy ? 'Creating both accounts…' : 'Finish setup and sign in'}
                </button>
              </div>
            </>
          )}
        </form>
      </main>
    </div>
  );
}

// Internal helper component: the shared username/displayName/password
// fields used by both steps. Keeping them in one place ensures the two
// steps stay visually consistent and that the validation lives in one
// definition.
function CredentialFieldset({
  value,
  onChange,
  usernameHelp,
}: {
  value: CredentialFields;
  onChange: (next: CredentialFields) => void;
  usernameHelp?: string;
}) {
  const patch = (k: keyof CredentialFields, v: string) =>
    onChange({ ...value, [k]: v });
  const passwordsMatch = value.password === value.confirm;

  return (
    <>
      <label className="field">
        <span className="label">Username</span>
        <span className="help">
          {usernameHelp ?? 'Used to sign in. Lowercase, no spaces is conventional but anything non-empty works.'}
        </span>
        <input
          type="text"
          autoFocus
          autoComplete="username"
          value={value.username}
          onChange={(e) => patch('username', e.target.value)}
        />
      </label>
      <label className="field">
        <span className="label">Display name (optional)</span>
        <span className="help">
          Shown in the UI instead of the username. Can be changed later from Settings.
        </span>
        <input
          type="text"
          autoComplete="off"
          value={value.displayName}
          onChange={(e) => patch('displayName', e.target.value)}
        />
      </label>
      <label className="field">
        <span className="label">Password</span>
        <span className="help">
          Minimum 8 characters. Stored as a bcrypt hash; never sent anywhere outside this server.
        </span>
        <input
          type="password"
          autoComplete="new-password"
          value={value.password}
          onChange={(e) => patch('password', e.target.value)}
        />
      </label>
      <label className="field">
        <span className="label">Confirm password</span>
        <input
          type="password"
          autoComplete="new-password"
          value={value.confirm}
          onChange={(e) => patch('confirm', e.target.value)}
        />
        {value.confirm && !passwordsMatch && (
          <span className="help" style={{ color: 'var(--bad)' }}>
            Passwords don't match.
          </span>
        )}
      </label>
    </>
  );
}
