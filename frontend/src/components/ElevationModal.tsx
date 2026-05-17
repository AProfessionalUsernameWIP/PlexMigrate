// Item 1 (admin-management plan, 2026-05-15): sudo-style elevation modal.
//
// When a root_admin attempts a root-level destructive action (creating
// or modifying another user account, granting / revoking root, applying
// a cross-server PIN migration), the action's request handler returns
// HTTP 403 with a message instructing the client to elevate first.
// This modal collects the end user's current password, hits
// /api/auth/elevate, and on success re-runs the original action.
//
// The elevation is server-side state keyed by JWT session id and lasts
// for the configured TTL (default 10 minutes). It survives /refresh
// (new access JWT, same sid) and clears on logout or container restart.

import { useEffect, useRef, useState } from 'react';
import { api } from '../api';

interface Props {
  open: boolean;
  // Human-readable description of the action being elevated for.
  // Shown to the end user so they understand why a password prompt
  // just appeared. Examples: "create user alice", "grant root to bob",
  // "migrate PINs to Plex2".
  actionDescription: string;
  onCancel: () => void;
  // Called after a successful elevation. The parent uses this to
  // retry the original action that triggered the 403.
  onElevated: (expiresAt: number) => void;
}

export function ElevationModal({ open, actionDescription, onCancel, onElevated }: Props) {
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);

  // Clear state when the modal opens so a previous attempt's password
  // doesn't linger in memory after a cancel.
  useEffect(() => {
    if (open) {
      setPassword('');
      setError(null);
      setBusy(false);
      // Focus on next paint; the input isn't mounted before the
      // dialog node renders.
      setTimeout(() => inputRef.current?.focus(), 0);
    }
  }, [open]);

  if (!open) return null;

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!password) return;
    setError(null);
    setBusy(true);
    try {
      const res = await api.authElevate(password);
      // Wipe the password from local state before invoking the
      // success callback so it isn't sitting in memory longer than
      // necessary.
      setPassword('');
      onElevated(res.elevated_until);
    } catch (err) {
      const msg = String(err).replace(/^Error: /, '');
      setError(msg || 'Elevation failed.');
    } finally {
      setBusy(false);
    }
  };

  return (
    <div
      onClick={onCancel}
      style={{
        position: 'fixed',
        inset: 0,
        background: 'rgba(0,0,0,0.55)',
        zIndex: 1000,
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        padding: 20,
      }}
    >
      <form
        onSubmit={submit}
        onClick={(e) => e.stopPropagation()}
        style={{
          background: 'var(--panel, #131826)',
          border: '1px solid var(--accent, #2e7df6)',
          borderRadius: 8,
          maxWidth: 480,
          width: '100%',
          padding: 20,
          boxShadow: '0 10px 30px rgba(0,0,0,0.5)',
        }}
      >
        <div style={{ display: 'flex', alignItems: 'baseline', gap: 10, marginBottom: 10 }}>
          <span style={{ fontSize: 22, color: 'var(--accent, #2e7df6)' }}>↑</span>
          <h2 style={{ margin: 0, fontSize: 16 }}>Confirm your password to continue</h2>
        </div>

        <p style={{ fontSize: 13, color: 'var(--text-dim)', marginTop: 0, lineHeight: 1.5 }}>
          You are about to: <strong style={{ color: 'var(--text)' }}>{actionDescription}</strong>.
          Re-enter your current password to elevate. The elevation
          lasts about 10 minutes, then expires and any further
          privileged action will prompt again.
        </p>

        {error && <div className="banner error" style={{ marginBottom: 12 }}>{error}</div>}

        <label className="field">
          <span className="label">Password</span>
          <input
            ref={inputRef}
            type="password"
            autoComplete="current-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            disabled={busy}
          />
        </label>

        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8, marginTop: 16 }}>
          <button type="button" onClick={onCancel} disabled={busy}>Cancel</button>
          <button type="submit" className="primary" disabled={busy || !password}>
            {busy ? 'Verifying…' : 'Elevate'}
          </button>
        </div>
      </form>
    </div>
  );
}
