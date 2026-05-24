// Sudo-style elevation modal.
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

import { ElevationForm } from './ElevationForm';
import { Modal } from './Modal';

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
  // The shared ElevationForm owns the password / focus / reset /
  // authElevate / error logic. This wrapper only supplies the accent-
  // toned, glyphed, "You are about to: ..." sudo presentation; it
  // passes the form's callbacks straight through since onElevated
  // already takes the elevated-until timestamp.
  if (!open) return null;

  return (
    <Modal onClose={onCancel} align="center" width={480} tone="accent">
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

      <ElevationForm
        open={open}
        submitLabel="Elevate"
        busyLabel="Verifying…"
        onElevated={onElevated}
        onCancel={onCancel}
      />
    </Modal>
  );
}
