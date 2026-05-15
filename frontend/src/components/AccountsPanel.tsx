// Settings → Account Management → Database Admin Account.
//
// Styled after UserAccountsExplorer's detail view: an identity panel
// at the top, an "Account actions" panel with modal-driven mutations
// (Change username / Change password), and a separate setup panel
// shown only when no db_admin row exists yet.
//
// Visibility is gated upstream by ``db_admin.access`` (admin and
// root_admin). The backend additionally re-checks the per-request
// JWT role AND the current db_admin password supplied in every
// update payload, so a 401/403 surfaces here cleanly through the
// existing error banners if the gate ever leaks.

import { useEffect, useState } from 'react';
import { api } from '../api';
import { InfoTip } from './InfoTip';


export function AccountsPanel() {
  return (
    <>
      <DbAdminSection />
    </>
  );
}


// ── Top-level switcher: loading / setup / detail view ───────────────────────

function DbAdminSection() {
  const [hasAdmin, setHasAdmin] = useState<boolean | null>(null);
  const [username, setUsername] = useState<string | null>(null);
  const [topError, setTopError] = useState<string | null>(null);

  const refresh = async () => {
    try {
      const s = await api.getDbAdminStatus();
      setHasAdmin(s.has_admin);
      setUsername(s.username);
      setTopError(null);
    } catch (e) {
      setTopError(String(e));
    }
  };
  useEffect(() => { void refresh(); }, []);

  if (topError) {
    return <div className="banner error">{topError}</div>;
  }
  if (hasAdmin === null) {
    return <div className="panel"><div className="empty">Loading database admin…</div></div>;
  }
  if (!hasAdmin) {
    return <SetupPanel onSetupComplete={refresh} />;
  }
  return <DetailView username={username ?? ''} onChanged={refresh} />;
}


// ── Detail view (db_admin exists) ───────────────────────────────────────────

function DetailView({
  username,
  onChanged,
}: {
  username: string;
  onChanged: () => void | Promise<void>;
}) {
  const [usernameModal, setUsernameModal] = useState(false);
  const [passwordModal, setPasswordModal] = useState(false);
  const [ok, setOk] = useState<string | null>(null);

  return (
    <>
      <div className="panel">
        <h2 style={{ marginTop: 0 }}>
          {username || 'Database admin'}{' '}
          <DbAdminBadge />
          <InfoTip>
            Independent privileged credential. Authorises destructive
            writes in Servers → User Management. Stored in
            <code> auth.db</code> as <code>role='db_admin'</code>;
            bcrypt-hashed and verified per-request. Separate from any
            login account.
          </InfoTip>
        </h2>
        <span className="help" style={{ display: 'block', color: 'var(--text-dim)', fontSize: 12, marginBottom: 12 }}>
          Gate credential for destructive writes in Servers → User Management.
        </span>
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 24, fontSize: 13 }}>
          <div>
            <span className="label">Username:</span>{' '}
            <strong className="mono">{username || <em style={{ color: 'var(--text-dim)' }}>-</em>}</strong>
          </div>
          <div>
            <span className="label">Role:</span>{' '}
            <strong>db_admin</strong> <span style={{ color: 'var(--text-dim)', fontSize: 12 }}>(non-login)</span>
          </div>
          <div>
            <span className="label">Used by:</span>{' '}
            <strong>Servers → User Management</strong>
          </div>
        </div>
        {ok && <div className="banner good" style={{ marginTop: 12 }}>{ok}</div>}
      </div>

      <div className="panel">
        <h2>What this account can do</h2>
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16, fontSize: 13 }}>
          <div>
            <strong>db_admin can:</strong>
            <ul style={{ marginTop: 4, paddingLeft: 18 }}>
              <li>Authorise destructive writes in Servers → User Management</li>
              <li>Rename / re-credential itself with the current password</li>
            </ul>
          </div>
          <div>
            <strong>db_admin cannot:</strong>
            <ul style={{ marginTop: 4, paddingLeft: 18, color: 'var(--text-dim)' }}>
              <li>Log in to the application (not a login role)</li>
              <li>Manage other user accounts</li>
              <li>Be deleted from this UI (re-deploy to clear)</li>
            </ul>
          </div>
        </div>
      </div>

      <div className="panel">
        <h2>
          Account actions
          <InfoTip>
            Each change requires the <em>current</em> database admin
            password as the per-call gate. Your application login
            session is not used to authorise these writes.
          </InfoTip>
        </h2>
        <div className="row-buttons">
          <button onClick={() => setUsernameModal(true)}>Change username…</button>
          <button onClick={() => setPasswordModal(true)}>Change password…</button>
        </div>
      </div>

      {usernameModal && (
        <ChangeUsernameModal
          currentUsername={username}
          onClose={() => setUsernameModal(false)}
          onSuccess={async (next) => {
            setUsernameModal(false);
            await onChanged();
            setOk(`Username changed to ${next}.`);
          }}
        />
      )}
      {passwordModal && (
        <ChangePasswordModal
          currentUsername={username}
          onClose={() => setPasswordModal(false)}
          onSuccess={() => {
            setPasswordModal(false);
            setOk('Password changed.');
          }}
        />
      )}
    </>
  );
}


// ── Modals ──────────────────────────────────────────────────────────────────

function ChangeUsernameModal({
  currentUsername,
  onClose,
  onSuccess,
}: {
  currentUsername: string;
  onClose: () => void;
  onSuccess: (next: string) => void | Promise<void>;
}) {
  const [draft, setDraft] = useState('');
  const [currentPassword, setCurrentPassword] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const trimmed = draft.trim();
  const dirty = !!trimmed && trimmed !== currentUsername;
  const canSubmit = !submitting && dirty && !!currentPassword;

  const submit = async () => {
    setError(null);
    if (!canSubmit) return;
    setSubmitting(true);
    try {
      await api.updateDbAdmin({ current_password: currentPassword, new_username: trimmed });
      await onSuccess(trimmed);
    } catch (e) {
      setError(String(e));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <ModalShell title={`Rename ${currentUsername || 'database admin'}`} onClose={onClose}>
      <span className="help" style={{ display: 'block', color: 'var(--text-dim)', fontSize: 12, marginBottom: 8 }}>
        Picks a new username for the database admin row. Doesn't have to match
        any application login account.
      </span>
      {error && <div className="banner error">{error}</div>}
      <label className="field">
        <span className="label">New username</span>
        <input
          type="text"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          placeholder={currentUsername}
          autoComplete="off"
          autoFocus
        />
      </label>
      <label className="field">
        <span className="label">Current database admin password</span>
        <input
          type="password"
          value={currentPassword}
          onChange={(e) => setCurrentPassword(e.target.value)}
          autoComplete="current-password"
        />
      </label>
      <div className="row-buttons" style={{ marginTop: 12 }}>
        <button className="primary" disabled={!canSubmit} onClick={submit}>
          {submitting ? 'Saving…' : 'Change username'}
        </button>
        <button onClick={onClose} disabled={submitting}>Cancel</button>
      </div>
    </ModalShell>
  );
}


function ChangePasswordModal({
  currentUsername,
  onClose,
  onSuccess,
}: {
  currentUsername: string;
  onClose: () => void;
  onSuccess: () => void;
}) {
  const [currentPassword, setCurrentPassword] = useState('');
  const [newPassword, setNewPassword] = useState('');
  const [confirm, setConfirm] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const canSubmit =
    !submitting &&
    currentPassword.length > 0 &&
    newPassword.length >= 8 &&
    newPassword === confirm;

  const submit = async () => {
    setError(null);
    if (!canSubmit) return;
    setSubmitting(true);
    try {
      await api.updateDbAdmin({ current_password: currentPassword, new_password: newPassword });
      onSuccess();
    } catch (e) {
      setError(String(e));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <ModalShell title={`Change password for ${currentUsername || 'database admin'}`} onClose={onClose}>
      <span className="help" style={{ display: 'block', color: 'var(--text-dim)', fontSize: 12, marginBottom: 8 }}>
        Sets a new password for the database admin. Must be ≥ 8 characters.
        Verified per-request against <code>auth.db</code>.
      </span>
      {error && <div className="banner error">{error}</div>}
      <label className="field">
        <span className="label">Current database admin password</span>
        <input
          type="password"
          value={currentPassword}
          onChange={(e) => setCurrentPassword(e.target.value)}
          autoComplete="current-password"
          autoFocus
        />
      </label>
      <div className="grid-2">
        <label className="field">
          <span className="label">New password</span>
          <span className="help">≥ 8 characters.</span>
          <input
            type="password"
            value={newPassword}
            onChange={(e) => setNewPassword(e.target.value)}
            autoComplete="new-password"
          />
        </label>
        <label className="field">
          <span className="label">Confirm new password</span>
          <input
            type="password"
            value={confirm}
            onChange={(e) => setConfirm(e.target.value)}
            autoComplete="new-password"
          />
        </label>
      </div>
      <div className="row-buttons" style={{ marginTop: 12 }}>
        <button className="primary" disabled={!canSubmit} onClick={submit}>
          {submitting ? 'Saving…' : 'Change password'}
        </button>
        <button onClick={onClose} disabled={submitting}>Cancel</button>
      </div>
    </ModalShell>
  );
}


// ── Setup panel (no db_admin yet) ───────────────────────────────────────────

function SetupPanel({ onSetupComplete }: { onSetupComplete: () => void | Promise<void> }) {
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [confirm, setConfirm] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const canSubmit =
    !submitting &&
    username.trim().length > 0 &&
    password.length >= 8 &&
    password === confirm;

  const submit = async () => {
    setError(null);
    if (!canSubmit) return;
    setSubmitting(true);
    try {
      await api.setupDbAdmin(username.trim(), password);
      await onSetupComplete();
    } catch (e) {
      setError(String(e));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="panel">
      <h2 style={{ marginTop: 0 }}>
        Database Admin <DbAdminBadge />
      </h2>
      <div className="banner info" style={{ fontSize: 12, marginBottom: 12 }}>
        No database admin exists yet. Create one below - required before the
        User Management section under Servers can authorise any write.
      </div>
      {error && <div className="banner error">{error}</div>}
      <label className="field">
        <span className="label">Username</span>
        <span className="help">Does not have to match any application login account.</span>
        <input
          type="text"
          value={username}
          onChange={(e) => setUsername(e.target.value)}
          autoComplete="off"
        />
      </label>
      <div className="grid-2">
        <label className="field">
          <span className="label">Password</span>
          <span className="help">≥ 8 characters.</span>
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete="new-password"
          />
        </label>
        <label className="field">
          <span className="label">Confirm password</span>
          <input
            type="password"
            value={confirm}
            onChange={(e) => setConfirm(e.target.value)}
            autoComplete="new-password"
          />
        </label>
      </div>
      <div className="row-buttons" style={{ marginTop: 12 }}>
        <button className="primary" onClick={submit} disabled={!canSubmit}>
          {submitting ? 'Creating…' : 'Set up database admin'}
        </button>
      </div>
    </div>
  );
}


// ── Shared modal shell + role badge (mirrors UserAccountsExplorer) ──────────

function ModalShell({
  title,
  onClose,
  children,
}: {
  title: string;
  onClose: () => void;
  children: React.ReactNode;
}) {
  return (
    <div
      onClick={onClose}
      style={{
        position: 'fixed', inset: 0,
        background: 'rgba(0,0,0,0.55)',
        zIndex: 1000,
        display: 'flex', alignItems: 'flex-start', justifyContent: 'center',
        paddingTop: '8vh',
      }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        className="panel"
        style={{ width: 520, maxWidth: '92vw', maxHeight: '80vh', overflowY: 'auto' }}
      >
        <h2 style={{ marginTop: 0 }}>{title}</h2>
        {children}
      </div>
    </div>
  );
}


function DbAdminBadge() {
  return (
    <span style={{
      background: '#5a3a85',
      color: '#fff',
      padding: '2px 8px',
      borderRadius: 999,
      fontSize: 11,
      fontWeight: 600,
      verticalAlign: 'middle',
    }}>
      db_admin
    </span>
  );
}
