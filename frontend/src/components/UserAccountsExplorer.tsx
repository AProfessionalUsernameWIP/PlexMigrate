// PR-A5 : User Accounts explorer (root_admin only).
//
// Two render states inside one panel, no page routing, no browse
// modals (destructive confirms only):
//
//   * List view  : table of every login user (viewer / operator /
//                  manager / root_admin). Whole rows are clickable;
//                  drilling into a user re-renders the panel into
//                  the detail view.
//   * Detail view : identity block + editable controls (role,
//                   display name, reset password, delete) + human-
//                   readable permission summary derived from the
//                   role. "← Back to user list" returns to the list.
//
// db_admin rows are intentionally excluded : they're a non-login
// credential managed only via Settings → Accounts → Database Admin
// Account.

import { useEffect, useState } from 'react';
import { api, ManagedUser, Role } from '../api';
import { permissionSummaryForRole, ROLE_RANK, useAuthContext } from '../contexts/AuthContext';


export function UserAccountsExplorer() {
  const [users, setUsers] = useState<ManagedUser[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [view, setView] = useState<{ kind: 'list' } | { kind: 'detail'; username: string }>({ kind: 'list' });
  const [showAddModal, setShowAddModal] = useState(false);

  const refresh = async () => {
    setError(null);
    try {
      const r = await api.listManagedUsers();
      setUsers(r.users);
    } catch (e) {
      setError(String(e));
    }
  };
  useEffect(() => { refresh(); }, []);

  // If the operator is deep in the detail view and that user
  // disappears (deleted, renamed) on a refresh, return to the list.
  useEffect(() => {
    if (view.kind === 'detail' && users !== null && !users.some((u) => u.username === view.username)) {
      setView({ kind: 'list' });
    }
  }, [users, view]);

  if (error) {
    return <div className="banner error">{error}</div>;
  }
  if (users === null) {
    return <div className="panel"><div className="empty">Loading users…</div></div>;
  }

  if (view.kind === 'detail') {
    const target = users.find((u) => u.username === view.username);
    if (!target) {
      return (
        <div className="panel">
          <div className="empty">That user is no longer in the database.</div>
        </div>
      );
    }
    return (
      <UserDetailView
        user={target}
        onBack={() => setView({ kind: 'list' })}
        onChanged={refresh}
        onDeleted={() => { void refresh(); setView({ kind: 'list' }); }}
      />
    );
  }

  return (
    <>
      <div className="panel">
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8 }}>
          <h2 style={{ margin: 0 }}>User Accounts</h2>
          <button className="primary" onClick={() => setShowAddModal(true)}>+ Add account</button>
        </div>
        <span className="help" style={{ display: 'block', color: 'var(--text-dim)', fontSize: 12, marginBottom: 8 }}>
          Every login account on this install. Click a row to manage that user's role,
          display name, password, or to delete them. The database admin credential
          is managed separately under <strong>Accounts → Database Admin Account</strong>.
        </span>

        <table className="list" style={{ width: '100%' }}>
          <thead>
            <tr>
              <th>Display name</th>
              <th>Username</th>
              <th>Role</th>
              <th>Created</th>
              <th>Last login</th>
            </tr>
          </thead>
          <tbody>
            {users.length === 0 ? (
              <tr><td colSpan={5}><div className="empty">No accounts yet.</div></td></tr>
            ) : users.map((u) => (
              <tr
                key={u.username}
                onClick={() => setView({ kind: 'detail', username: u.username })}
                style={{ cursor: 'pointer' }}
              >
                <td>{u.display_name || <em style={{ color: 'var(--text-dim)' }}>-</em>}</td>
                <td className="mono">{u.username}</td>
                <td><RoleBadge role={u.role} /></td>
                <td>{new Date(u.created_at * 1000).toLocaleDateString()}</td>
                <td>{u.last_login ? new Date(u.last_login * 1000).toLocaleString() : <em style={{ color: 'var(--text-dim)' }}>never</em>}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {showAddModal && (
        <AddUserModal
          onClose={() => setShowAddModal(false)}
          onCreated={() => { setShowAddModal(false); void refresh(); }}
        />
      )}
    </>
  );
}


// ── Detail view ─────────────────────────────────────────────────────────────

function UserDetailView({
  user,
  onBack,
  onChanged,
  onDeleted,
}: {
  user: ManagedUser;
  onBack: () => void;
  onChanged: () => void | Promise<void>;
  onDeleted: () => void;
}) {
  // Per-row protection. ``admin`` callers cannot modify root_admin
  // rows (the sudo-root constraint). We key off ``effectiveRole`` so
  // a root_admin who used Switch View Mode to drop to admin sees the
  // root_admin row as locked in the preview - matches what a real
  // admin would experience. The backend uses the JWT's real role to
  // enforce; this is purely UI gating.
  const caller = useAuthContext();
  const callerCanModify =
    caller.effectiveRole === 'root_admin' ||
    (caller.effectiveRole === 'admin' && user.role !== 'root_admin');
  const isRoot = user.role === 'root_admin';
  const [error, setError] = useState<string | null>(null);
  const [ok, setOk] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  // Inline edit state for display name + role
  const [displayDraft, setDisplayDraft] = useState(user.display_name ?? '');
  const [roleDraft, setRoleDraft] = useState<Role>(user.role);
  useEffect(() => {
    // If the operator navigates between detail views without unmounting
    // (rare but possible) keep the drafts in sync with the underlying row.
    setDisplayDraft(user.display_name ?? '');
    setRoleDraft(user.role);
  }, [user.username, user.display_name, user.role]);

  const displayDirty = (displayDraft.trim() || null) !== (user.display_name ?? null);
  const roleDirty = roleDraft !== user.role;

  const saveDisplay = async () => {
    setError(null); setOk(null); setSubmitting(true);
    try {
      const trimmed = displayDraft.trim();
      await api.updateManagedUser(user.username, {
        display_name: trimmed || undefined,
        clear_display_name: trimmed === '' ? true : undefined,
      });
      setOk('Display name saved.');
      await onChanged();
    } catch (e) {
      setError(String(e));
    } finally {
      setSubmitting(false);
    }
  };

  const saveRole = async () => {
    if (isRoot || roleDraft === 'root_admin') return; // guard
    setError(null); setOk(null); setSubmitting(true);
    try {
      await api.updateManagedUser(user.username, {
        role: roleDraft as 'viewer' | 'operator' | 'manager' | 'admin',
      });
      setOk('Role changed. Takes effect on the affected user\'s next request.');
      await onChanged();
    } catch (e) {
      setError(String(e));
    } finally {
      setSubmitting(false);
    }
  };

  // Destructive actions: reset password + delete. Each uses a modal.
  const [resetModal, setResetModal] = useState(false);
  const [deleteModal, setDeleteModal] = useState(false);

  const perms = permissionSummaryForRole(user.role);

  return (
    <>
      <div className="panel">
        <button onClick={onBack} style={{ marginBottom: 12 }}>← Back to user list</button>
        <h2 style={{ marginTop: 0 }}>
          {user.display_name || user.username}{' '}
          <RoleBadge role={user.role} />
        </h2>
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 24, fontSize: 13 }}>
          <div>
            <span className="label">Username:</span>{' '}
            <strong className="mono">{user.username}</strong>
          </div>
          <div>
            <span className="label">Created:</span>{' '}
            {new Date(user.created_at * 1000).toLocaleString()}
          </div>
          <div>
            <span className="label">Last login:</span>{' '}
            {user.last_login
              ? new Date(user.last_login * 1000).toLocaleString()
              : <em style={{ color: 'var(--text-dim)' }}>never</em>}
          </div>
        </div>
        {error && <div className="banner error" style={{ marginTop: 12 }}>{error}</div>}
        {ok && <div className="banner good" style={{ marginTop: 12 }}>{ok}</div>}
      </div>

      {/* ── Display name edit (only when caller can modify the row) ── */}
      {callerCanModify && (
        <div className="panel">
          <h2>Display name</h2>
          <span className="help" style={{ display: 'block', color: 'var(--text-dim)', fontSize: 12, marginBottom: 8 }}>
            Shown in the UI instead of the username. Cosmetic only - login still
            uses <code className="mono">{user.username}</code>. Leave blank to clear.
          </span>
          <label className="field">
            <span className="label">Display name</span>
            <input
              type="text"
              value={displayDraft}
              onChange={(e) => setDisplayDraft(e.target.value)}
              placeholder={user.username}
              autoComplete="off"
            />
          </label>
          <div className="row-buttons">
            <button
              className="primary"
              disabled={submitting || !displayDirty}
              onClick={saveDisplay}
            >
              {submitting ? 'Saving…' : 'Save display name'}
            </button>
          </div>
        </div>
      )}

      {/* ── Role edit (non-root only, and caller must be allowed to modify this row) ── */}
      {!isRoot && callerCanModify && (
        <div className="panel">
          <h2>Role</h2>
          <span className="help" style={{ display: 'block', color: 'var(--text-dim)', fontSize: 12, marginBottom: 8 }}>
            Changes take effect on the affected user's next API call - they don't
            need to log out and back in.
            {caller.effectiveRole === 'admin' && (
              <> Only root_admin can promote a user to admin or higher.</>
            )}
          </span>
          <label className="field">
            <span className="label">Role</span>
            <select value={roleDraft} onChange={(e) => setRoleDraft(e.target.value as Role)}>
              <option value="viewer">Viewer</option>
              <option value="operator">Operator</option>
              <option value="manager">Manager</option>
              {/* Only root_admin can promote up to admin (sudo-root).
                  Admin can demote but not promote to its own level
                  here - keeps the UI honest about what the backend
                  will accept. */}
              {caller.effectiveRole === 'root_admin' && (
                <option value="admin">Admin (sudo-root)</option>
              )}
            </select>
          </label>
          <div className="row-buttons">
            <button
              className="primary"
              disabled={submitting || !roleDirty}
              onClick={saveRole}
            >
              {submitting ? 'Saving…' : 'Save role'}
            </button>
          </div>
        </div>
      )}

      {/* ── Permission summary (human-readable) ───────────────────── */}
      <div className="panel">
        <h2>What this role can do</h2>
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16, fontSize: 13 }}>
          <div>
            <strong>{prettyRole(user.role)} can:</strong>
            <ul style={{ marginTop: 4, paddingLeft: 18 }}>
              {perms.can.length === 0
                ? <li><em>(none)</em></li>
                : perms.can.map((p) => <li key={p}>{p}</li>)}
            </ul>
          </div>
          <div>
            <strong>{prettyRole(user.role)} cannot:</strong>
            <ul style={{ marginTop: 4, paddingLeft: 18, color: 'var(--text-dim)' }}>
              {perms.cannot.length === 0
                ? <li><em>(no restrictions)</em></li>
                : perms.cannot.map((p) => <li key={p}>{p}</li>)}
            </ul>
          </div>
        </div>
      </div>

      {/* ── Destructive actions ───────────────────────────────────── */}
      {callerCanModify && (
        <div className="panel">
          <h2>Account actions</h2>
          <div className="row-buttons">
            <button onClick={() => setResetModal(true)}>Reset password…</button>
            {!isRoot && (
              <button className="danger" onClick={() => setDeleteModal(true)}>Delete account…</button>
            )}
          </div>
          {isRoot && (
            <span className="help" style={{ display: 'block', color: 'var(--text-dim)', fontSize: 12, marginTop: 6 }}>
              The root admin account cannot be deleted or demoted - those controls
              are intentionally absent for this row to prevent accidental lock-out.
            </span>
          )}
        </div>
      )}
      {!callerCanModify && isRoot && caller.effectiveRole === 'admin' && (
        <div className="panel">
          <span className="help" style={{ color: 'var(--text-dim)', fontSize: 12 }}>
            The root admin row is read-only from your account. Only the root admin
            can change its own credentials.
          </span>
        </div>
      )}

      {resetModal && (
        <ResetPasswordModal
          username={user.username}
          onClose={() => setResetModal(false)}
          onSuccess={() => { setResetModal(false); void onChanged(); setOk('Password reset.'); }}
        />
      )}
      {deleteModal && (
        <DeleteUserModal
          username={user.username}
          onClose={() => setDeleteModal(false)}
          onSuccess={onDeleted}
        />
      )}
    </>
  );
}


// ── Modals (destructive confirms only) ──────────────────────────────────────

function AddUserModal({ onClose, onCreated }: { onClose: () => void; onCreated: () => void }) {
  const caller = useAuthContext();
  const [username, setUsername] = useState('');
  const [displayName, setDisplayName] = useState('');
  const [password, setPassword] = useState('');
  const [confirm, setConfirm] = useState('');
  const [role, setRole] = useState<'viewer' | 'operator' | 'manager' | 'admin'>('viewer');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const canSubmit =
    !submitting &&
    username.trim().length > 0 &&
    password.length >= 8 &&
    password === confirm;

  const create = async () => {
    setError(null);
    if (!canSubmit) return;
    setSubmitting(true);
    try {
      await api.createManagedUser({
        username: username.trim(),
        password,
        role,
        display_name: displayName.trim() || undefined,
      });
      onCreated();
    } catch (e) {
      setError(String(e));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <ModalShell title="Add account" onClose={onClose}>
      {error && <div className="banner error">{error}</div>}
      <label className="field">
        <span className="label">Username</span>
        <input
          type="text"
          value={username}
          onChange={(e) => setUsername(e.target.value)}
          autoComplete="off"
          autoFocus
        />
      </label>
      <label className="field">
        <span className="label">Display name (optional)</span>
        <input
          type="text"
          value={displayName}
          onChange={(e) => setDisplayName(e.target.value)}
          autoComplete="off"
        />
      </label>
      <label className="field">
        <span className="label">Role</span>
        <select value={role} onChange={(e) => setRole(e.target.value as typeof role)}>
          <option value="viewer">Viewer : read-only</option>
          <option value="operator">Operator : start jobs</option>
          <option value="manager">Manager : start + stop jobs, edit schedules</option>
          {/* Admin (sudo-root) can only be granted by root_admin.
              An admin caller could technically be allowed to create
              other admins, but we keep the rule strict: root_admin is
              the only one who can hand out sudo-root. */}
          {caller.role === 'root_admin' && (
            <option value="admin">Admin (sudo-root) : full access, cannot modify root admin</option>
          )}
        </select>
      </label>
      <div className="grid-2">
        <label className="field">
          <span className="label">Password</span>
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
        <button className="primary" disabled={!canSubmit} onClick={create}>
          {submitting ? 'Creating…' : 'Create account'}
        </button>
        <button onClick={onClose} disabled={submitting}>Cancel</button>
      </div>
    </ModalShell>
  );
}


function ResetPasswordModal({
  username,
  onClose,
  onSuccess,
}: {
  username: string;
  onClose: () => void;
  onSuccess: () => void;
}) {
  const [newPassword, setNewPassword] = useState('');
  const [confirm, setConfirm] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const canSubmit =
    !submitting && newPassword.length >= 8 && newPassword === confirm;

  const submit = async () => {
    setError(null);
    if (!canSubmit) return;
    setSubmitting(true);
    try {
      await api.resetManagedUserPassword(username, newPassword);
      onSuccess();
    } catch (e) {
      setError(String(e));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <ModalShell title={`Reset password for ${username}`} onClose={onClose}>
      <span className="help" style={{ display: 'block', color: 'var(--text-dim)', fontSize: 12, marginBottom: 8 }}>
        Sets a new password for <code className="mono">{username}</code>. Their existing
        JWT remains valid until expiry; future logins will require the new password.
      </span>
      {error && <div className="banner error">{error}</div>}
      <div className="grid-2">
        <label className="field">
          <span className="label">New password</span>
          <span className="help">≥ 8 characters.</span>
          <input
            type="password"
            value={newPassword}
            onChange={(e) => setNewPassword(e.target.value)}
            autoComplete="new-password"
            autoFocus
          />
        </label>
        <label className="field">
          <span className="label">Confirm</span>
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
          {submitting ? 'Resetting…' : 'Reset password'}
        </button>
        <button onClick={onClose} disabled={submitting}>Cancel</button>
      </div>
    </ModalShell>
  );
}


function DeleteUserModal({
  username,
  onClose,
  onSuccess,
}: {
  username: string;
  onClose: () => void;
  onSuccess: () => void;
}) {
  const [typed, setTyped] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const canSubmit = !submitting && typed === username;

  const submit = async () => {
    setError(null);
    if (!canSubmit) return;
    setSubmitting(true);
    try {
      await api.deleteManagedUser(username);
      onSuccess();
    } catch (e) {
      setError(String(e));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <ModalShell title={`Delete ${username}?`} onClose={onClose}>
      <div className="banner error" style={{ fontSize: 12 }}>
        This permanently removes the account. The user will be unable to log in
        afterwards; any existing JWT they hold becomes useless on its next
        request because the role lookup will fail.
      </div>
      <label className="field" style={{ marginTop: 12 }}>
        <span className="label">Type the username to confirm</span>
        <span className="help">
          Type <code className="mono">{username}</code> exactly.
        </span>
        <input
          type="text"
          value={typed}
          onChange={(e) => setTyped(e.target.value)}
          autoComplete="off"
          autoFocus
        />
      </label>
      {error && <div className="banner error" style={{ marginTop: 8 }}>{error}</div>}
      <div className="row-buttons" style={{ marginTop: 12 }}>
        <button className="danger" disabled={!canSubmit} onClick={submit}>
          {submitting ? 'Deleting…' : 'Delete account'}
        </button>
        <button onClick={onClose} disabled={submitting}>Cancel</button>
      </div>
    </ModalShell>
  );
}


// ── Shared modal shell ──────────────────────────────────────────────────────

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


// ── Tiny helpers ────────────────────────────────────────────────────────────

function RoleBadge({ role }: { role: Role }) {
  const colors: Record<Role, { bg: string; fg: string }> = {
    viewer:     { bg: '#3a4150', fg: '#cfd6e4' },
    operator:   { bg: '#3a5fb0', fg: '#fff' },
    manager:    { bg: '#3a8a55', fg: '#fff' },
    admin:      { bg: '#b06a2a', fg: '#fff' },
    root_admin: { bg: '#a04545', fg: '#fff' },
  };
  const c = colors[role];
  return (
    <span style={{
      background: c.bg,
      color: c.fg,
      padding: '2px 8px',
      borderRadius: 999,
      fontSize: 11,
      fontWeight: 600,
      verticalAlign: 'middle',
    }}>
      {prettyRole(role)}
    </span>
  );
}

function prettyRole(role: Role): string {
  switch (role) {
    case 'viewer':     return 'Viewer';
    case 'operator':   return 'Operator';
    case 'manager':    return 'Manager';
    case 'admin':      return 'Admin';
    case 'root_admin': return 'Root admin';
  }
}

// Re-exported for callers that need the same ordering elsewhere.
export { ROLE_RANK };
