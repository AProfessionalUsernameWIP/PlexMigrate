// Plan[RUN-JOB-UI] PR-4: D-OWNER user-creation modal.
//
// End user-facing surface that lists source-side managed users not
// present on the destination, lets the end user pick a target
// username + password per row, and emits the spec list back to
// JobFormPanel. JobFormPanel forwards the list on submit as
// ``user_create_specs``; ``server/jobs.py`` walks it through
// ``services/user_creation.py`` BEFORE any item-state write.
//
// Two safety layers per the end user's choice on RJ-OWNER-DEFER-CREATE
// (ship UI + backend together):
//   1. Per-row "Skip" action so the end user can opt out of any
//      individual create without aborting the whole job.
//   2. A typed CREATE confirmation that gates the "Save and proceed"
//      button. End user must literally type the word CREATE before
//      the button enables. Matches the RESET button on the ETA
//      tunables panel for consistency.
//
// Each row carries a generated password via crypto.getRandomValues
// + a "Regenerate" button. The password is plaintext in this React
// state for the duration of the modal session, transits via HTTPS
// in the submit payload, then becomes Fernet-encrypted at rest in
// managed_users.service_password_enc on the backend.

import { useEffect, useMemo, useState } from 'react';


export interface ProposedUser {
  /** Username on the SOURCE server (modal's row identifier). */
  source_user_handle: string;
  /** End user-visible display name from source's managed_users. */
  source_display_name: string;
  /** Suggested target username (defaults to display_name then handle). */
  suggested_target_username: string;
}


export interface UserCreateSpec {
  source_user_handle: string;
  target_username: string;
  temp_password: string;
  target_user_policy?: Record<string, unknown> | null;
}


export interface UserCreationModalProps {
  /** Modal open / close state. */
  open: boolean;
  /** Source users that don't yet exist on the destination. */
  proposed: ProposedUser[];
  /** Existing specs to seed the modal with (re-open / edit flow). */
  initialSpecs?: UserCreateSpec[];
  /** Confirm callback: end user typed CREATE + clicked the action. */
  onConfirm: (specs: UserCreateSpec[]) => void;
  /** Cancel / close without saving. */
  onClose: () => void;
}


type RowAction = 'create' | 'skip';

interface ModalRow {
  source_user_handle: string;
  source_display_name: string;
  target_username: string;
  temp_password: string;
  action: RowAction;
}


function _generatePassword(): string {
  // 16 characters from a URL-safe alphabet via the browser CSPRNG.
  // The generated value is plaintext in JS state for the modal's
  // lifetime; the backend encrypts at rest.
  const ALPHA = 'ABCDEFGHIJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789';
  const out: string[] = [];
  const buf = new Uint32Array(16);
  try {
    crypto.getRandomValues(buf);
    for (let i = 0; i < 16; i += 1) {
      out.push(ALPHA[buf[i] % ALPHA.length]);
    }
  } catch {
    // Fallback: Math.random is fine for a temp password the end user
    // is going to surface to a person anyway. Browsers without crypto
    // are rare on supported versions.
    for (let i = 0; i < 16; i += 1) {
      out.push(ALPHA[Math.floor(Math.random() * ALPHA.length)]);
    }
  }
  return out.join('');
}


export function UserCreationModal({
  open,
  proposed,
  initialSpecs,
  onConfirm,
  onClose,
}: UserCreationModalProps) {
  // Build the row state once when the modal opens. Re-seed when the
  // proposed list changes or initialSpecs change between opens.
  const [rows, setRows] = useState<ModalRow[]>([]);
  useEffect(() => {
    if (!open) return;
    setRows(proposed.map((p) => {
      const existing = (initialSpecs || []).find(
        (s) => s.source_user_handle === p.source_user_handle,
      );
      return {
        source_user_handle: p.source_user_handle,
        source_display_name: p.source_display_name,
        target_username: existing?.target_username || p.suggested_target_username,
        temp_password: existing?.temp_password || _generatePassword(),
        action: 'create',
      };
    }));
    setConfirmText('');
  }, [open, proposed, initialSpecs]);

  const [confirmText, setConfirmText] = useState<string>('');
  const isConfirmed = confirmText.trim() === 'CREATE';

  const activeRows = useMemo(
    () => rows.filter((r) => r.action === 'create'),
    [rows],
  );
  const validRows = useMemo(
    () => activeRows.filter((r) =>
      r.target_username.trim().length > 0 && r.temp_password.length > 0,
    ),
    [activeRows],
  );
  const hasInvalidActiveRows = useMemo(
    () => activeRows.length !== validRows.length,
    [activeRows.length, validRows.length],
  );

  if (!open) return null;

  const updateRow = (idx: number, patch: Partial<ModalRow>) => {
    setRows((prev) => prev.map((r, i) => (i === idx ? { ...r, ...patch } : r)));
  };

  const handleSave = () => {
    if (!isConfirmed) return;
    if (hasInvalidActiveRows) return;
    const specs: UserCreateSpec[] = validRows.map((r) => ({
      source_user_handle: r.source_user_handle,
      target_username: r.target_username.trim(),
      temp_password: r.temp_password,
      target_user_policy: null,
    }));
    onConfirm(specs);
  };

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby="user-creation-modal-title"
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
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div
        className="panel"
        style={{
          background: 'var(--bg-1, #1c1c1c)',
          maxWidth: 920,
          width: '100%',
          maxHeight: '90vh',
          overflow: 'auto',
          padding: 20,
          border: '1px solid var(--warn, #d97706)',
        }}
      >
        <h2 id="user-creation-modal-title" style={{ marginTop: 0, color: 'var(--warn, #d97706)' }}>
          Create users on destination
        </h2>
        <p style={{ fontSize: 13, color: 'var(--text-dim)', marginTop: 0 }}>
          These users exist on the source but not on the destination.
          Each will be created on the destination server before the
          transfer fires. <strong>This is irreversible</strong>: if
          you proceed and later abort, the created users remain on
          destination unless the backend's rollback path triggers (it
          fires only when a downstream create fails).
        </p>

        {rows.length === 0 ? (
          <div style={{ padding: '16px 0', color: 'var(--text-dim)' }}>
            No users to create. Close the modal and proceed.
          </div>
        ) : (
          <table className="list" style={{ width: '100%', fontSize: 13 }}>
            <thead>
              <tr>
                <th style={{ textAlign: 'left' }}>Source user</th>
                <th style={{ textAlign: 'left' }}>Target username</th>
                <th style={{ textAlign: 'left' }}>Temp password</th>
                <th style={{ textAlign: 'left' }}>Action</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r, idx) => {
                const targetInvalid = r.action === 'create' && r.target_username.trim().length === 0;
                return (
                  <tr key={r.source_user_handle}>
                    <td style={{ fontWeight: 600 }}>
                      {r.source_display_name || r.source_user_handle}
                      {r.source_display_name && (
                        <div style={{ fontSize: 10, color: 'var(--text-dim)' }}>
                          {r.source_user_handle}
                        </div>
                      )}
                    </td>
                    <td>
                      <input
                        type="text"
                        value={r.target_username}
                        disabled={r.action === 'skip'}
                        onChange={(e) => updateRow(idx, { target_username: e.target.value })}
                        style={{
                          width: '100%',
                          fontSize: 12,
                          border: targetInvalid ? '1px solid var(--danger, #ef4444)' : undefined,
                        }}
                      />
                      {targetInvalid && (
                        <div style={{ fontSize: 10, color: 'var(--danger, #ef4444)' }}>
                          Required
                        </div>
                      )}
                    </td>
                    <td style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
                      <input
                        type="text"
                        readOnly
                        value={r.temp_password}
                        disabled={r.action === 'skip'}
                        style={{ flex: 1, fontFamily: 'monospace', fontSize: 11 }}
                      />
                      <button
                        type="button"
                        title="Regenerate password"
                        disabled={r.action === 'skip'}
                        onClick={() => updateRow(idx, { temp_password: _generatePassword() })}
                        style={{ fontSize: 10, padding: '2px 8px' }}
                      >
                        Regen
                      </button>
                      <button
                        type="button"
                        title="Copy password to clipboard"
                        disabled={r.action === 'skip'}
                        onClick={() => {
                          try {
                            navigator.clipboard.writeText(r.temp_password);
                          } catch {
                            // Best-effort; older browsers without clipboard API
                          }
                        }}
                        style={{ fontSize: 10, padding: '2px 8px' }}
                      >
                        Copy
                      </button>
                    </td>
                    <td>
                      <select
                        value={r.action}
                        onChange={(e) => updateRow(idx, { action: e.target.value as RowAction })}
                        style={{ fontSize: 12 }}
                      >
                        <option value="create">Create</option>
                        <option value="skip">Skip</option>
                      </select>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}

        {rows.length > 0 && (
          <>
            <div style={{ marginTop: 14, fontSize: 12 }}>
              <strong>Summary:</strong>{' '}
              {validRows.length} to create, {rows.length - activeRows.length} skipped
              {hasInvalidActiveRows && (
                <span style={{ color: 'var(--danger, #ef4444)', marginLeft: 8 }}>
                  ({activeRows.length - validRows.length} row(s) need a target username)
                </span>
              )}
            </div>

            <div style={{
              marginTop: 14,
              padding: 12,
              borderRadius: 6,
              background: 'rgba(239, 68, 68, 0.08)',
              border: '1px solid var(--danger, #ef4444)',
            }}>
              <div style={{ fontSize: 12, marginBottom: 8 }}>
                <strong>Type the word CREATE to confirm.</strong> This
                operation is destructive: it writes new user accounts
                directly on the destination server. Created users will
                exist until manually deleted (or until the backend's
                rollback path triggers on a downstream failure).
              </div>
              <input
                type="text"
                value={confirmText}
                onChange={(e) => setConfirmText(e.target.value)}
                placeholder="Type CREATE here"
                style={{ width: '100%', fontFamily: 'monospace', fontSize: 13 }}
                autoFocus
              />
            </div>
          </>
        )}

        <div style={{ marginTop: 16, display: 'flex', gap: 10, justifyContent: 'flex-end' }}>
          <button type="button" onClick={onClose} style={{ fontSize: 12, padding: '6px 14px' }}>
            Cancel
          </button>
          <button
            type="button"
            className="primary"
            disabled={
              rows.length === 0
              || !isConfirmed
              || hasInvalidActiveRows
              || validRows.length === 0
            }
            onClick={handleSave}
            style={{ fontSize: 12, padding: '6px 14px' }}
          >
            Save and proceed
          </button>
        </div>
      </div>
    </div>
  );
}
