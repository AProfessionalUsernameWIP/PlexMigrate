// Root-admin-only credential reveal.
//
// Two-stage modal:
//
//   * Stage A - auth: three fields (db_admin username + db_admin password
//     + root_admin password). On submit, POSTs to
//     /api/managed-users/{server_id}/{username}/reveal-credentials.
//     The backend enforces three gates (root_admin role, root password
//     re-check, db_admin verify); any failure returns 401 with an
//     identical detail string so we can't tell which gate rejected us.
//   * Stage B - disclose: shows auth_token + plex_home_pin in copy-
//     to-clipboard fields. **30-second auto-hide**: a countdown ticks
//     down and at zero the cleartext is wiped from React state and
//     the modal closes. The intent is that accidental screen-sharing
//     does not leak a long-lived view.
//
// Plaintext NEVER leaves React state: no logging, no analytics, no
// persistence. The clipboard write goes through navigator.clipboard
// only when the operator clicks Copy.

import { useEffect, useRef, useState } from 'react';
import { api } from '../api';
import { Modal } from './Modal';

interface Props {
  serverId: string;
  username: string;
  displayName?: string | null;
  onClose: () => void;
}

const AUTO_HIDE_SECONDS = 30;

export function RevealCredentialsModal({
  serverId, username, displayName, onClose,
}: Props) {
  const [dbAdminUsername, setDbAdminUsername] = useState('');
  const [dbAdminPassword, setDbAdminPassword] = useState('');
  const [rootAdminPassword, setRootAdminPassword] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [revealed, setRevealed] = useState<{
    auth_token: string | null;
    plex_home_pin: string | null;
    emby_easy_pin: string | null;
    jellyfin_easy_pin: string | null;
  } | null>(null);
  const [secondsLeft, setSecondsLeft] = useState(AUTO_HIDE_SECONDS);
  const [copiedField, setCopiedField] = useState<
    'token' | 'pin' | 'emby_pin' | 'jellyfin_pin' | null
  >(null);
  const copiedTimerRef = useRef<number | null>(null);

  const canSubmit =
    !!dbAdminUsername.trim()
    && !!dbAdminPassword
    && !!rootAdminPassword
    && !submitting;

  const submit = async () => {
    if (!canSubmit) return;
    setError(null);
    setSubmitting(true);
    try {
      const res = await api.revealManagedUserCredentials(serverId, username, {
        db_admin_username: dbAdminUsername.trim(),
        db_admin_password: dbAdminPassword,
        root_admin_password: rootAdminPassword,
      });
      setRevealed(res);
      // Wipe the password fields from state immediately so they don't
      // sit around in memory while the reveal banner is open.
      setDbAdminPassword('');
      setRootAdminPassword('');
    } catch (e) {
      const msg = String(e);
      if (msg.includes('404')) {
        setError(
          `No managed-user row for ${username} on this server. ` +
          `Run Sync from the user list first.`
        );
      } else {
        // 401 / generic - never tell the caller which gate rejected.
        setError('Re-authentication failed — check both passwords and try again.');
      }
    } finally {
      setSubmitting(false);
    }
  };

  // Countdown + auto-hide. Restarts only on the first reveal; once
  // ``revealed`` is set, we tick to zero exactly once and then close.
  // Deliberately a raw setInterval, not pausableInterval: the wipe must
  // keep counting down even while the tab is hidden so cleartext
  // credentials are not left in memory indefinitely on a backgrounded
  // tab.
  useEffect(() => {
    if (revealed === null) return;
    setSecondsLeft(AUTO_HIDE_SECONDS);
    const id = window.setInterval(() => {
      setSecondsLeft((prev) => {
        if (prev <= 1) {
          window.clearInterval(id);
          // Wipe + close on the next tick to avoid setState-in-render.
          window.setTimeout(() => {
            setRevealed(null);
            onClose();
          }, 0);
          return 0;
        }
        return prev - 1;
      });
    }, 1000);
    return () => window.clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [revealed]);

  // Defensive: when the modal unmounts for any reason, scrub state
  // values that held cleartext.
  useEffect(() => {
    return () => {
      setRevealed(null);
      setDbAdminPassword('');
      setRootAdminPassword('');
      if (copiedTimerRef.current !== null) {
        window.clearTimeout(copiedTimerRef.current);
      }
    };
  }, []);

  const copy = async (
    value: string,
    field: 'token' | 'pin' | 'emby_pin' | 'jellyfin_pin',
  ) => {
    try {
      await navigator.clipboard.writeText(value);
      setCopiedField(field);
      if (copiedTimerRef.current !== null) {
        window.clearTimeout(copiedTimerRef.current);
      }
      copiedTimerRef.current = window.setTimeout(() => setCopiedField(null), 2000);
    } catch {
      // Clipboard write blocked (insecure context, denied permission).
      // No fallback - surfacing a banner would clutter the modal; the
      // operator can still select + Ctrl+C from the read-only field.
    }
  };

  return (
    <Modal onClose={onClose} align="center" width={560}>
        <h3 style={{ marginTop: 0, marginBottom: 4 }}>
          Reveal stored credentials
        </h3>
        <div style={{ fontSize: 12, color: 'var(--text-dim)', marginBottom: 16 }}>
          {displayName ? <><strong>{displayName}</strong> ({username})</> : <strong>{username}</strong>}
          <span style={{ marginLeft: 6 }}>on this server</span>
        </div>

        {revealed === null && (
          <>
            <div
              className="banner"
              style={{
                background: 'rgba(234, 179, 8, 0.10)',
                border: '1px solid rgba(234, 179, 8, 0.45)',
                color: 'var(--text)',
                padding: '8px 10px',
                fontSize: 12,
                marginBottom: 14,
                borderRadius: 4,
              }}
            >
              Three security gates. Both passwords are required. Every successful
              reveal is recorded to the database access log (operator, server,
              username, timestamp).
            </div>
            <label className="field" style={{ marginBottom: 10 }}>
              <span className="label">Database Admin username</span>
              <input
                type="text"
                value={dbAdminUsername}
                onChange={(e) => setDbAdminUsername(e.target.value)}
                autoComplete="off"
                disabled={submitting}
              />
            </label>
            <label className="field" style={{ marginBottom: 10 }}>
              <span className="label">Database Admin password</span>
              <input
                type="password"
                value={dbAdminPassword}
                onChange={(e) => setDbAdminPassword(e.target.value)}
                autoComplete="off"
                disabled={submitting}
              />
            </label>
            <label className="field" style={{ marginBottom: 10 }}>
              <span className="label">Your root admin password</span>
              <input
                type="password"
                value={rootAdminPassword}
                onChange={(e) => setRootAdminPassword(e.target.value)}
                onKeyDown={(e) => { if (e.key === 'Enter' && canSubmit) void submit(); }}
                autoComplete="off"
                disabled={submitting}
              />
            </label>
            {error && (
              <div className="banner error" style={{ marginTop: 8, fontSize: 12 }}>
                {error}
              </div>
            )}
            <div className="row-buttons" style={{ marginTop: 14 }}>
              <button
                type="button"
                className="primary"
                onClick={() => void submit()}
                disabled={!canSubmit}
              >
                {submitting ? 'Verifying…' : 'Reveal'}
              </button>
              <button type="button" onClick={onClose} disabled={submitting}>
                Cancel
              </button>
            </div>
          </>
        )}

        {revealed !== null && (
          <>
            <div
              className="banner"
              style={{
                background: 'rgba(239, 68, 68, 0.12)',
                border: '1px solid var(--bad, #ef4444)',
                color: 'var(--text)',
                padding: '8px 10px',
                fontSize: 12,
                marginBottom: 14,
                borderRadius: 4,
              }}
            >
              Cleartext below. Auto-hides in <strong>{secondsLeft}s</strong>.
              Copy what you need now — re-opening the modal requires both
              passwords again.
            </div>

            <CredentialField
              label="Auth token"
              value={revealed.auth_token}
              copied={copiedField === 'token'}
              onCopy={(v) => void copy(v, 'token')}
            />
            <CredentialField
              label="Plex Home PIN"
              value={revealed.plex_home_pin}
              copied={copiedField === 'pin'}
              onCopy={(v) => void copy(v, 'pin')}
            />
            <CredentialField
              label="Emby EasyPassword (PIN)"
              value={revealed.emby_easy_pin}
              copied={copiedField === 'emby_pin'}
              onCopy={(v) => void copy(v, 'emby_pin')}
            />
            <CredentialField
              label="Jellyfin EasyPassword (PIN)"
              value={revealed.jellyfin_easy_pin}
              copied={copiedField === 'jellyfin_pin'}
              onCopy={(v) => void copy(v, 'jellyfin_pin')}
            />

            <div className="row-buttons" style={{ marginTop: 14 }}>
              <button
                type="button"
                onClick={() => { setRevealed(null); onClose(); }}
              >
                Close now
              </button>
            </div>
          </>
        )}
    </Modal>
  );
}

function CredentialField({
  label, value, copied, onCopy,
}: {
  label: string;
  value: string | null;
  copied: boolean;
  onCopy: (v: string) => void;
}) {
  return (
    <div style={{ marginBottom: 10 }}>
      <div
        style={{
          display: 'flex', justifyContent: 'space-between',
          alignItems: 'baseline', marginBottom: 4,
        }}
      >
        <span className="label">{label}</span>
        {value !== null && (
          <button
            type="button"
            onClick={() => onCopy(value)}
            style={{ fontSize: 11, padding: '2px 8px' }}
          >
            {copied ? 'Copied ✓' : 'Copy'}
          </button>
        )}
      </div>
      {value === null ? (
        <em style={{ color: 'var(--text-dim)', fontSize: 12 }}>
          Not stored.
        </em>
      ) : (
        <input
          type="text"
          readOnly
          value={value}
          onFocus={(e) => e.currentTarget.select()}
          style={{
            width: '100%',
            fontFamily: 'monospace',
            fontSize: 12,
            background: 'rgba(255, 255, 255, 0.06)',
          }}
        />
      )}
    </div>
  );
}
