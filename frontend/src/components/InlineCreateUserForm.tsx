// Inline user-creation form embedded inside a UserResolutionRow when
// the end user picks "Create new on destination". Calls
// POST /api/jobs/inline-create-user on confirm. On success, hands
// the new DestUserOption back to the parent so the row can switch its
// action to "Map" against the freshly-created user.
//
// Admin role requires an explicit acknowledgement checkbox (the backend
// model rejects role='admin' without acknowledgement=true).
//
// Plex destinations should hide this form entirely - the picker blocks
// the "Create" action when dest_kind === 'plex' per the constraint
// matrix. This component is the second line of defense: if Plex slips
// through, the backend returns 400 with a pointer to the Plex Home
// invite flow.

import { useState } from 'react';
import { api } from '../api';
import type { CppDestUserOption, InlineCreateUserBody, InlineCreateUserResponse } from '../api';
import { errorText } from '../utils/format';

interface Props {
  destinationServerId: string;
  destinationLabel: string;
  destKind: 'plex' | 'jellyfin' | 'emby' | string;
  onCreated: (user: CppDestUserOption, wasNewlyCreated: boolean) => void;
  onCancel: () => void;
}

export function InlineCreateUserForm({
  destinationServerId,
  destinationLabel,
  destKind,
  onCreated,
  onCancel,
}: Props) {
  const [username, setUsername] = useState('');
  const [role, setRole] = useState<'managed' | 'admin'>('managed');
  const [password, setPassword] = useState('');
  const [acknowledgement, setAcknowledgement] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (destKind === 'plex') {
    return (
      <div className="banner" style={{ fontSize: 12, marginTop: 8, padding: '8px 12px' }}>
        Plex destinations require the Plex Home invite flow for user creation.
        Open <code>plex.tv &gt; Manage Library Access</code> and invite the user,
        then re-run the preflight.
      </div>
    );
  }

  const canSubmit =
    username.trim().length > 0
    && !submitting
    && (role === 'managed' || acknowledgement);

  return (
    <div
      style={{
        marginTop: 8,
        padding: '10px 12px',
        background: 'var(--panel-alt, #1b2233)',
        border: '1px solid var(--border, #2a3146)',
        borderRadius: 6,
        fontSize: 12,
      }}
    >
      <div style={{ fontSize: 12, color: 'var(--text-dim)', marginBottom: 8 }}>
        Create a new user on <strong>{destinationLabel}</strong>:
      </div>
      <label className="field" style={{ marginBottom: 8 }}>
        <span className="label">Username</span>
        <input
          type="text"
          value={username}
          onChange={(e) => setUsername(e.target.value)}
          placeholder="alice"
          autoFocus
        />
      </label>
      <div style={{ marginBottom: 8 }}>
        <span className="label" style={{ display: 'block', marginBottom: 4 }}>Role</span>
        <label style={{ display: 'inline-flex', alignItems: 'center', gap: 4, marginRight: 12 }}>
          <input
            type="radio"
            name={`create-role-${destinationServerId}`}
            checked={role === 'managed'}
            onChange={() => { setRole('managed'); setAcknowledgement(false); }}
          />
          Managed
        </label>
        <label style={{ display: 'inline-flex', alignItems: 'center', gap: 4 }}>
          <input
            type="radio"
            name={`create-role-${destinationServerId}`}
            checked={role === 'admin'}
            onChange={() => setRole('admin')}
          />
          Admin
        </label>
      </div>
      <label className="field" style={{ marginBottom: 8 }}>
        <span className="label">
          Password
          <span style={{ color: 'var(--text-dim)', fontWeight: 'normal', marginLeft: 4, fontSize: 11 }}>
            (optional; backend may require depending on its policy)
          </span>
        </span>
        <input
          type="text"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          placeholder="(leave blank to use destination defaults)"
        />
      </label>
      {role === 'admin' && (
        <label className="switch" style={{ marginBottom: 8, color: 'var(--warn, #d97706)' }}>
          <input
            type="checkbox"
            checked={acknowledgement}
            onChange={(e) => setAcknowledgement(e.target.checked)}
          />
          <span>
            I understand this will create an administrator with full access
            on <strong>{destinationLabel}</strong>.
          </span>
        </label>
      )}
      {error && (
        <div className="banner error" style={{ fontSize: 11, marginBottom: 8 }}>
          {error}
        </div>
      )}
      <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
        <button type="button" onClick={onCancel} disabled={submitting}>Cancel</button>
        <button
          type="button"
          className="primary"
          disabled={!canSubmit}
          onClick={async () => {
            setSubmitting(true);
            setError(null);
            try {
              const body: InlineCreateUserBody = {
                destination_server_id: destinationServerId,
                username: username.trim(),
                role,
                initial_password: password || undefined,
                acknowledgement,
              };
              const res: InlineCreateUserResponse = await api.jobsInlineCreateUser(body);
              onCreated(res.user, res.was_newly_created);
            } catch (e) {
              setError(errorText(e));
            } finally {
              setSubmitting(false);
            }
          }}
        >
          {submitting ? 'Creating…' : 'Create'}
        </button>
      </div>
    </div>
  );
}
