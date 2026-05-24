// Shared elevation form body.
//
// Both ElevateModal (the 403-driven retry path) and ElevationModal
// (the preemptive sudo path) collect a password, POST it to
// /api/auth/elevate, wipe the cleartext from state on success, and
// surface errors inline. That password input + auto-focus + reset-on-
// open + authElevate + error display logic lived twice; it lives here
// once. Each modal keeps its own thin <Modal> wrapper that differs
// only in heading, intro copy, button label, tone, and callback shape.
//
// This component renders the <form> body only (no Modal shell), so a
// wrapper is free to choose tone="accent", a glyph, etc.

import { useEffect, useRef, useState } from 'react';
import { api } from '../api';
import { errorText } from '../utils/format';

export interface ElevationFormProps {
  // Drives the reset-on-open effect. Wrappers already early-return
  // when closed, but the effect still needs the transition signal.
  open: boolean;
  // Rendered above the password label (heading + intro copy). The
  // wrapper owns this so the accent/glyph/actionDescription framing
  // stays per-modal.
  children?: React.ReactNode;
  // Confirm-button label; the busy state appends nothing, the wrapper
  // supplies both forms.
  submitLabel: string;
  busyLabel: string;
  // Called with the elevated-until unix timestamp after a successful
  // POST /api/auth/elevate. The password is already wiped from state.
  onElevated: (expiresAt: number) => void;
  // Called when the user clicks Cancel.
  onCancel: () => void;
  // Reports the in-flight state so the wrapper can guard its Modal
  // onClose (backdrop / Escape) against closing mid-request.
  onBusyChange?: (busy: boolean) => void;
}

export function ElevationForm({
  open,
  children,
  submitLabel,
  busyLabel,
  onElevated,
  onCancel,
  onBusyChange,
}: ElevationFormProps) {
  const [password, setPassword] = useState<string>('');
  const [busy, setBusy] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);

  const setBusyState = (next: boolean) => {
    setBusy(next);
    onBusyChange?.(next);
  };

  // Auto-focus the password input when the modal opens so the end
  // user can start typing immediately. Reset state on each open so a
  // previous cancel doesn't leak its password or error.
  useEffect(() => {
    if (open) {
      setPassword('');
      setError(null);
      setBusyState(false);
      // Defer focus to the next frame so the input is in the DOM.
      window.setTimeout(() => inputRef.current?.focus(), 0);
    }
    // setBusyState is stable enough for this open-transition effect;
    // matching the original modals' [open]-only dependency.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  const submit = async () => {
    if (!password) {
      setError('Password is required.');
      inputRef.current?.focus();
      return;
    }
    setBusyState(true);
    setError(null);
    try {
      const res = await api.authElevate(password);
      // Success: clear the in-memory password before invoking the
      // callback so the cleartext doesn't linger in component state
      // beyond the moment of use.
      setPassword('');
      onElevated(res.elevated_until);
    } catch (e) {
      // Common case: wrong password returns 401 with a generic
      // detail. Surface inline; keep the modal open so the end user
      // can retry without losing their place.
      setError(errorText(e));
      setBusyState(false);
      inputRef.current?.focus();
      inputRef.current?.select();
    }
  };

  return (
    <form
      data-testid="elevation-modal"
      onSubmit={(ev) => {
        ev.preventDefault();
        if (!busy) void submit();
      }}
    >
      {children}
      {error && (
        <div
          className="banner error"
          style={{ marginTop: 10, fontSize: 12 }}
          data-testid="elevation-error"
        >
          {error}
        </div>
      )}
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
          disabled={busy}
          style={{ width: '100%' }}
          data-testid="elevation-password"
        />
      </label>
      <div
        style={{
          display: 'flex',
          justifyContent: 'flex-end',
          gap: 8,
          marginTop: 14,
        }}
      >
        <button type="button" onClick={onCancel} disabled={busy}>
          Cancel
        </button>
        <button
          type="submit"
          className="primary"
          disabled={busy || !password}
          data-testid="elevation-submit"
        >
          {busy ? busyLabel : submitLabel}
        </button>
      </div>
    </form>
  );
}
