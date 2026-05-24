// Modal for saving a Plex Home user's X-Plex-Token so the Playlist
// Management copy path can act AS that user (rather than the
// destination admin) when the `playlist_mgmt_plex_home_auth_mode`
// tunable is set to `per_user_token`.
//
// Backend endpoint:
//   POST /api/managed-users/{server_id}/{username}/plex-home-token
//
// The backend re-authenticates db_admin on every call; this modal
// caches the credentials in PARENT React state for the panel session
// so the end user types them once when onboarding several Plex Home
// users in sequence. Unmount of PlaylistManagementPanel clears the
// cache. The in-memory cache is the deliberate UX choice for batch
// onboarding; ElevateModal-style every-time prompting would require
// lifting cachedAdmin / setCachedAdmin out of state.

import { useEffect, useState } from 'react';
import { api } from '../api';
import type { ServerManagedUser } from '../api';
import { errorText } from '../utils/format';
import { Modal } from './Modal';

export interface AdminCreds {
  username: string;
  password: string;
}

interface Props {
  open: boolean;
  serverId: string;
  serverLabel: string;
  username: string;
  // The role chip from PlaylistMgmtUser; rendered in the modal header
  // so the end user knows which Plex Home account they're authorizing.
  userRole?: 'owner' | 'admin' | 'managed' | string;
  // End user already has a saved token for this user? Modal renders
  // an extra "clear token" button when true.
  hasExistingToken: boolean;
  // db_admin credentials reused across saves in the same panel
  // session. Pass null on first open; pass back the saved value on
  // subsequent opens so the field starts collapsed.
  cachedAdmin: AdminCreds | null;
  onAdminCached: (c: AdminCreds) => void;
  onClose: () => void;
  onSaved: (updated: ServerManagedUser) => void;
}

export function PlexHomeTokenModal({
  open,
  serverId,
  serverLabel,
  username,
  userRole,
  hasExistingToken,
  cachedAdmin,
  onAdminCached,
  onClose,
  onSaved,
}: Props) {
  const [adminUsername, setAdminUsername] = useState(cachedAdmin?.username ?? '');
  const [adminPassword, setAdminPassword] = useState(cachedAdmin?.password ?? '');
  const [reauthOpen, setReauthOpen] = useState(!cachedAdmin);
  const [token, setToken] = useState('');
  const [displayName, setDisplayName] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Reset transient state every time the modal is opened for a
  // different user / freshly. The cached admin creds intentionally
  // persist across opens (that's the whole point of the cache).
  useEffect(() => {
    if (!open) return;
    setToken('');
    setDisplayName('');
    setError(null);
    setSubmitting(false);
    setReauthOpen(!cachedAdmin);
    setAdminUsername(cachedAdmin?.username ?? '');
    setAdminPassword(cachedAdmin?.password ?? '');
  }, [open, username, cachedAdmin]);

  if (!open) return null;

  const submit = async (mode: 'save' | 'clear') => {
    setError(null);
    if (!adminUsername || !adminPassword) {
      setError('Enter your db_admin credentials.');
      setReauthOpen(true);
      return;
    }
    if (mode === 'save' && !token.trim()) {
      setError('Paste the user\'s X-Plex-Token first.');
      return;
    }
    setSubmitting(true);
    try {
      const updated = await api.setPlexHomeToken(serverId, username, {
        db_admin_username: adminUsername,
        db_admin_password: adminPassword,
        auth_token: mode === 'save' ? token.trim() : null,
        clear_auth_token: mode === 'clear',
        display_name: displayName.trim() || null,
      });
      onAdminCached({ username: adminUsername, password: adminPassword });
      onSaved(updated);
    } catch (e) {
      setError(errorText(e));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Modal onClose={onClose} align="center" width={520} maxHeight="calc(100vh - 32px)">
        <h3 style={{ marginTop: 0 }}>
          {hasExistingToken ? 'Replace' : 'Save'} Plex Home token
        </h3>
        <div style={{ fontSize: 12, color: 'var(--text-dim)', marginBottom: 12 }}>
          For <strong>{username}</strong>{userRole ? ` (${userRole})` : ''} on <strong>{serverLabel}</strong>.
        </div>

        <div className="banner info" style={{ fontSize: 12, marginBottom: 12 }}>
          The orchestrator uses this token to create playlists AS this user when
          <code> playlist_mgmt_plex_home_auth_mode = per_user_token</code>. Get the
          token from <a href="https://plex.tv/users/account" target="_blank" rel="noreferrer">plex.tv ▸ account</a>:
          sign in as the Plex Home user, open Settings ▸ Authorized Devices, pick a
          device, and copy its <code>X-Plex-Token</code>. (Alternatively, append
          <code>?X-Plex-Token=&lt;known token&gt;</code> to
          <code> https://plex.tv/users/account.json</code> to read it programmatically.)
        </div>

        {reauthOpen ? (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 8, marginBottom: 12 }}>
            <label className="field">
              <span className="label">db_admin username</span>
              <input
                type="text"
                autoComplete="username"
                value={adminUsername}
                onChange={(e) => setAdminUsername(e.target.value)}
              />
            </label>
            <label className="field">
              <span className="label">db_admin password</span>
              <input
                type="password"
                autoComplete="current-password"
                value={adminPassword}
                onChange={(e) => setAdminPassword(e.target.value)}
              />
            </label>
          </div>
        ) : (
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 12, fontSize: 12 }}>
            <span style={{ color: 'var(--text-dim)' }}>
              Authenticated as <strong>{adminUsername}</strong>.
            </span>
            <button
              type="button"
              onClick={() => setReauthOpen(true)}
              style={{ fontSize: 11 }}
            >
              Re-authenticate
            </button>
          </div>
        )}

        <label className="field" style={{ marginBottom: 12 }}>
          <span className="label">X-Plex-Token</span>
          <input
            type="text"
            value={token}
            onChange={(e) => setToken(e.target.value)}
            placeholder="xxxxxxxxxxxxxxxxxxxx"
            spellCheck={false}
            autoCapitalize="off"
          />
        </label>

        <label className="field" style={{ marginBottom: 12 }}>
          <span className="label">Display name (optional)</span>
          <span className="help" style={{ fontSize: 11 }}>
            Friendly label saved alongside this row. Blank leaves the existing value untouched.
          </span>
          <input
            type="text"
            value={displayName}
            onChange={(e) => setDisplayName(e.target.value)}
            placeholder={username}
          />
        </label>

        {error && (
          <div className="banner error" style={{ fontSize: 12, marginBottom: 12 }}>
            {error}
          </div>
        )}

        <div className="row-buttons" style={{ justifyContent: 'flex-end', gap: 8 }}>
          <button type="button" onClick={onClose} disabled={submitting}>
            Cancel
          </button>
          {hasExistingToken && (
            <button
              type="button"
              className="danger"
              onClick={() => void submit('clear')}
              disabled={submitting}
            >
              {submitting ? 'Working…' : 'Clear token'}
            </button>
          )}
          <button
            type="button"
            className="primary"
            onClick={() => void submit('save')}
            disabled={submitting}
          >
            {submitting ? 'Saving…' : hasExistingToken ? 'Replace token' : 'Save token'}
          </button>
        </div>
    </Modal>
  );
}
