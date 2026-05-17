// Phase 6 of the dashboard / log reorg.
//
// Inline re-authentication modal. The api.ts http<T> helper calls
// ``onElevationRequired()`` whenever a request returns 403 with the
// "recent password re-confirmation" marker; App.tsx registers a
// handler that mounts this modal and resolves the handler's promise
// after the user confirms or cancels.
//
// End user-facing flow:
//
// 1. End user clicks an action gated on elevation (e.g. grant a
//    settings.tunables permission).
// 2. Backend returns 403 with the elevation marker.
// 3. http<T> calls the handler, which opens this modal.
// 4. End user types their password and clicks Confirm.
// 5. Modal calls POST /api/auth/elevate. On success, the modal
//    resolves the handler promise true and dismisses; the original
//    request is silently retried by http<T> and surfaces success
//    (or any other error) to the original caller.
// 6. On wrong-password (401) or other error, the modal stays open
//    and surfaces the error inline so the end user can retry.
// 7. On cancel, the modal resolves false and dismisses; the
//    original request throws its 403 as it would have without the
//    modal so the caller sees a clean failure.
//
// Security posture is unchanged: the elevation gate stays in place,
// the password is verified by the backend, and a wrong password
// never elevates. This is purely a UX improvement that removes the
// need for the end user to know about /api/auth/elevate.

import { useEffect, useRef, useState } from 'react';
import { api } from '../api';


export interface ElevateModalProps {
  open: boolean;
  /** Called with ``true`` after a successful elevate, ``false`` on cancel. */
  onClose: (elevated: boolean) => void;
}


export function ElevateModal({ open, onClose }: ElevateModalProps) {
  const [password, setPassword] = useState<string>('');
  const [submitting, setSubmitting] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);

  // Auto-focus the password input when the modal opens so the
  // end user can start typing immediately. Reset state on each open
  // so a previous cancel doesn't leak its password or error.
  useEffect(() => {
    if (open) {
      setPassword('');
      setError(null);
      setSubmitting(false);
      // Defer focus to the next frame so the input is in the DOM.
      window.setTimeout(() => inputRef.current?.focus(), 0);
    }
  }, [open]);

  if (!open) return null;

  const submit = async () => {
    if (!password) {
      setError('Password is required.');
      inputRef.current?.focus();
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      await api.authElevate(password);
      // Success: clear the in-memory password before resolving so
      // the cleartext doesn't linger in component state beyond the
      // moment of use.
      setPassword('');
      onClose(true);
    } catch (e) {
      // Common case: wrong password returns 401 with a generic
      // detail. Surface inline; keep the modal open so the end user
      // can retry without losing their place.
      const msg = e instanceof Error ? e.message : String(e);
      setError(msg);
      setSubmitting(false);
      inputRef.current?.focus();
      inputRef.current?.select();
    }
  };

  const cancel = () => {
    setPassword('');
    setError(null);
    onClose(false);
  };

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label="Confirm your password to continue"
      style={{
        position: 'fixed',
        inset: 0,
        background: 'rgba(0,0,0,0.55)',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        zIndex: 1000,
      }}
      onClick={(ev) => {
        // Backdrop click cancels. Inner clicks stopPropagation below.
        if (ev.target === ev.currentTarget && !submitting) cancel();
      }}
    >
      <div
        className="panel"
        style={{
          background: 'var(--bg, #1f1f1f)',
          border: '1px solid var(--border, #444)',
          borderRadius: 6,
          padding: 18,
          minWidth: 360,
          maxWidth: 480,
          boxShadow: '0 6px 20px rgba(0,0,0,0.5)',
        }}
        onClick={(ev) => ev.stopPropagation()}
      >
        <h2 style={{ marginTop: 0, marginBottom: 8 }}>
          Confirm your password
        </h2>
        <p style={{ marginTop: 0, fontSize: 13, color: 'var(--text-dim)' }}>
          This action is gated on a recent password re-confirmation. Confirm
          your password to continue; the original request will retry
          automatically. Your session role and permissions are unchanged.
        </p>
        <form
          onSubmit={(ev) => {
            ev.preventDefault();
            if (!submitting) void submit();
          }}
        >
          <label style={{ display: 'block', marginTop: 8 }}>
            <span className="label" style={{ display: 'block', marginBottom: 4 }}>
              Password
            </span>
            <input
              ref={inputRef}
              type="password"
              value={password}
              autoComplete="current-password"
              onChange={(ev) => setPassword(ev.target.value)}
              disabled={submitting}
              onKeyDown={(ev) => {
                if (ev.key === 'Escape' && !submitting) {
                  ev.preventDefault();
                  cancel();
                }
              }}
              style={{ width: '100%' }}
            />
          </label>
          {error && (
            <div
              className="banner error"
              style={{ marginTop: 10, fontSize: 12 }}
            >
              {error}
            </div>
          )}
          <div
            style={{
              display: 'flex',
              justifyContent: 'flex-end',
              gap: 8,
              marginTop: 14,
            }}
          >
            <button
              type="button"
              onClick={cancel}
              disabled={submitting}
            >
              Cancel
            </button>
            <button
              type="submit"
              className="primary"
              disabled={submitting || !password}
            >
              {submitting ? 'Confirming…' : 'Confirm'}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
