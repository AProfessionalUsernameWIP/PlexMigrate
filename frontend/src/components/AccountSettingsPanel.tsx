// PR-A5 - Account Settings (self-service).
//
// Every logged-in role sees this panel under Settings → Account. It
// manages only the caller's own row:
//
//   * Display name - purely cosmetic, replaces the username in the UI
//     wherever the caller appears. Persisted to auth.db.display_name
//     via POST /api/auth/users/me/display-name. Cleared by submitting
//     an empty string.
//   * Password - change the caller's own password. Requires the
//     current password as the per-call gate (verified via the new
//     /api/auth/verify-password endpoint), then posted via the same
//     update mechanism root_admin uses on PATCH /api/auth/users/{me}.
//
// No role / username changes here - those are root_admin operations
// in the User Accounts explorer.

import { useEffect, useState } from 'react';
import { api } from '../api';
import { useAuthContext } from '../contexts/AuthContext';
import { useClockDisplay } from '../contexts/ClockContext';
import { InfoTip } from './InfoTip';


export function AccountSettingsPanel() {
  return (
    <>
      <AccountIdentitySection />
      <DisplayNameSection />
      <PasswordSection />
      <ClockDisplaySection />
    </>
  );
}


// ── Identity block: who you are + when you signed in + how long ──────────────

function AccountIdentitySection() {
  const auth = useAuthContext();
  // Live tick so "Session duration" updates without a snapshot refresh.
  // Re-render once per second. ~24 bytes of state per re-render; the
  // operator usually leaves this panel after a few seconds.
  const [, setTick] = useState(0);
  useEffect(() => {
    const id = window.setInterval(() => setTick((x) => x + 1), 1000);
    return () => window.clearInterval(id);
  }, []);

  const lastLoginText = auth.lastLogin
    ? new Date(auth.lastLogin * 1000).toLocaleString()
    : '(never - this is your first login)';
  const sessionDurationText = auth.lastLogin
    ? formatDuration(Date.now() / 1000 - auth.lastLogin)
    : '-';

  return (
    <div className="panel">
      <h2>Account</h2>
      <span className="help" style={{ display: 'block', color: 'var(--text-dim)', fontSize: 12, marginBottom: 12 }}>
        Signed in as <strong>{auth.username}</strong> ({auth.role.replace('_', ' ')}).
        {auth.displayName && (
          <> Display name in the UI is <strong>{auth.displayName}</strong>.</>
        )}
      </span>
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 24, fontSize: 13 }}>
        <div>
          <span className="label">Last sign-in:</span>{' '}
          <strong>{lastLoginText}</strong>
        </div>
        <div>
          <span className="label">Session duration:</span>{' '}
          <strong className="mono">{sessionDurationText}</strong>
        </div>
        {auth.createdAt && (
          <div>
            <span className="label">Account created:</span>{' '}
            <strong>{new Date(auth.createdAt * 1000).toLocaleDateString()}</strong>
          </div>
        )}
        <InfoTip>
          Session duration is time since your last login. A page reload
          doesn't reset it; the timer is tied to the JWT, which lives 24 h.
        </InfoTip>
      </div>
    </div>
  );
}

function formatDuration(secs: number): string {
  if (secs < 0) return '-';
  const s = Math.floor(secs);
  const hh = Math.floor(s / 3600);
  const mm = Math.floor((s % 3600) / 60);
  const ss = s % 60;
  const pad = (n: number) => (n < 10 ? `0${n}` : String(n));
  return `${pad(hh)}:${pad(mm)}:${pad(ss)}`;
}


function DisplayNameSection() {
  const auth = useAuthContext();
  const [draft, setDraft] = useState(auth.displayName ?? '');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [ok, setOk] = useState<string | null>(null);

  const dirty = (draft.trim() || null) !== (auth.displayName ?? null);

  const save = async () => {
    setError(null);
    setOk(null);
    setSubmitting(true);
    try {
      await api.updateOwnDisplayName(draft.trim());
      setOk(draft.trim() ? 'Display name updated.' : 'Display name cleared.');
      // Refresh the AuthContext so the topbar chip + every other
      // place that reads displayName updates without a page reload.
      if (auth.refreshMe) await auth.refreshMe();
    } catch (e) {
      setError(String(e));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="panel">
      <h2>
        Display name
        <InfoTip>
          Shown in the UI instead of your username. Cosmetic only; login
          still uses your real username. Leave blank to clear.
        </InfoTip>
      </h2>
      {error && <div className="banner error">{error}</div>}
      {ok && <div className="banner good">{ok}</div>}
      <label className="field">
        <span className="label">Display name</span>
        <input
          type="text"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          placeholder={auth.username}
          autoComplete="off"
        />
      </label>
      <div className="row-buttons">
        <button
          className="primary"
          disabled={submitting || !dirty}
          onClick={save}
        >
          {submitting ? 'Saving…' : 'Save display name'}
        </button>
      </div>
    </div>
  );
}


function ClockDisplaySection() {
  const clock = useClockDisplay();

  // For the ``custom`` mode the operator types the HH:MM they want
  // the topbar to read right now. We translate that into an offset
  // from the server's current time and stash it. Stored offset is
  // relative to server time; we don't track the typed HH:MM after
  // submit because the operator's local moves on.
  const [customDraft, setCustomDraft] = useState(() =>
    formatHHMM(new Date(Date.now() + clock.customOffsetMs)),
  );
  const [error, setError] = useState<string | null>(null);
  const [ok, setOk] = useState<string | null>(null);

  const applyCustom = () => {
    setError(null); setOk(null);
    const m = customDraft.match(/^(\d{1,2}):(\d{2})$/);
    if (!m) {
      setError('Use HH:MM (24-hour). Example: 14:30');
      return;
    }
    const hh = parseInt(m[1], 10);
    const mm = parseInt(m[2], 10);
    if (hh < 0 || hh > 23 || mm < 0 || mm > 59) {
      setError('Hours 0–23, minutes 0–59.');
      return;
    }
    // Compute the offset that maps server-now → typed HH:MM today.
    // We anchor the target to today's date in the browser's local
    // timezone - that's what the operator's eyes are on.
    const target = new Date();
    target.setHours(hh, mm, 0, 0);
    const offsetMs = target.getTime() - Date.now();
    clock.setCustomOffsetMs(offsetMs);
    clock.setMode('custom');
    setOk('Custom clock applied. The topbar now shows this offset from server time.');
  };

  return (
    <div className="panel">
      <h2>
        Clock display
        <InfoTip>
          Controls only the topbar clock. Schedules and logs always use
          the server's clock. Preference is per-browser and persists
          across reloads.
        </InfoTip>
      </h2>
      {error && <div className="banner error">{error}</div>}
      {ok && <div className="banner good">{ok}</div>}

      <label className="switch">
        <input
          type="radio"
          name="clock-mode"
          checked={clock.mode === 'server'}
          onChange={() => clock.setMode('server')}
        />
        <span>
          Server time (default)
          <InfoTip>
            Matches the backend's <code>TZ</code>; same value the engine
            uses for schedules and log timestamps.
          </InfoTip>
        </span>
      </label>
      <label className="switch">
        <input
          type="radio"
          name="clock-mode"
          checked={clock.mode === 'local'}
          onChange={() => clock.setMode('local')}
        />
        <span>
          My device's local time
          <InfoTip>
            Browser clock + timezone. Useful when the server is in a
            different zone from you.
          </InfoTip>
        </span>
      </label>
      <label className="switch">
        <input
          type="radio"
          name="clock-mode"
          checked={clock.mode === 'custom'}
          onChange={() => clock.setMode('custom')}
        />
        <span>
          Custom time
          <InfoTip>
            Show the topbar at an arbitrary offset from server time.
            Type the time it should read right now and click Apply; the
            offset persists across reloads.
          </InfoTip>
        </span>
      </label>

      <div className="grid-2" style={{ marginTop: 8 }}>
        <label className="field">
          <span className="label">Custom time (HH:MM, 24-hour)</span>
          <input
            type="text"
            value={customDraft}
            onChange={(e) => setCustomDraft(e.target.value)}
            placeholder="14:30"
            disabled={clock.mode !== 'custom'}
          />
        </label>
        <div style={{ alignSelf: 'flex-end' }}>
          <button
            className="primary"
            disabled={clock.mode !== 'custom'}
            onClick={applyCustom}
          >
            Apply custom time
          </button>
        </div>
      </div>
    </div>
  );
}

function formatHHMM(d: Date): string {
  const pad = (n: number) => (n < 10 ? `0${n}` : String(n));
  return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
}


function PasswordSection() {
  const auth = useAuthContext();
  const [currentPassword, setCurrentPassword] = useState('');
  const [newPassword, setNewPassword] = useState('');
  const [confirm, setConfirm] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [ok, setOk] = useState<string | null>(null);

  const canSubmit =
    !submitting &&
    currentPassword.length > 0 &&
    newPassword.length >= 8 &&
    newPassword === confirm;

  const save = async () => {
    setError(null);
    setOk(null);
    if (!canSubmit) return;
    setSubmitting(true);
    try {
      // Server-side verify (PR-A2 endpoint) before issuing the change.
      // The endpoint reads the username from the JWT so the operator
      // can never verify anyone else's password.
      const v = await api.authVerifyPassword(currentPassword);
      if (!v.valid) {
        setError('Current password is incorrect.');
        return;
      }
      // Root admin uses the dedicated login-account update endpoint;
      // other roles use the generic user-update path. For self-service
      // we route both through the same patch surface that root_admin
      // uses to reset another user's password - except we PATCH our
      // own row, which is permitted by ``require_role('root_admin')``
      // only. Non-root self-service password changes hit a 403 with
      // that surface today.
      //
      // Pragma: every role can change their own password. Either:
      //   (a) add a /api/auth/users/me/password endpoint, or
      //   (b) repurpose /api/auth/login-account/update for root_admin
      //       and surface non-root self-service via the same patch path.
      // PR-A5 adopts (a) - see auth_router.py for the new route.
      await api.changeOwnPassword(currentPassword, newPassword);
      setOk('Password changed. Your existing session remains valid.');
      setCurrentPassword('');
      setNewPassword('');
      setConfirm('');
    } catch (e) {
      setError(String(e));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="panel">
      <h2>Change password</h2>
      <span className="help" style={{ display: 'block', color: 'var(--text-dim)', fontSize: 12, marginBottom: 8 }}>
        Changes the password for <strong>{auth.username}</strong>. Your existing
        session stays valid; future logins will require the new password.
      </span>
      {error && <div className="banner error">{error}</div>}
      {ok && <div className="banner good">{ok}</div>}
      <label className="field">
        <span className="label">Current password</span>
        <input
          type="password"
          value={currentPassword}
          onChange={(e) => setCurrentPassword(e.target.value)}
          autoComplete="current-password"
        />
      </label>
      <div className="grid-2">
        <label className="field">
          <span className="label">New password</span>
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
      <div className="row-buttons">
        <button className="primary" disabled={!canSubmit} onClick={save}>
          {submitting ? 'Saving…' : 'Change password'}
        </button>
      </div>
    </div>
  );
}
