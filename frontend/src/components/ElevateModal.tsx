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
import { ElevationForm } from './ElevationForm';
import { Modal } from './Modal';


export interface ElevateModalProps {
  open: boolean;
  /** Called with ``true`` after a successful elevate, ``false`` on cancel. */
  onClose: (elevated: boolean) => void;
}


export function ElevateModal({ open, onClose }: ElevateModalProps) {
  // The shared ElevationForm owns the password / focus / reset /
  // authElevate / error logic. This wrapper only supplies the 403-
  // retry presentation and adapts the form's (expiresAt, cancel)
  // callbacks to this modal's onClose(elevated: boolean) contract.
  // ``busy`` is mirrored here so a backdrop / Escape close can't
  // dismiss the modal mid-request.
  const [busy, setBusy] = useState<boolean>(false);
  const busyRef = useRef<boolean>(false);
  busyRef.current = busy;

  // Drop the local busy mirror when the modal closes so a fresh open
  // doesn't inherit a stale "in-flight" guard.
  useEffect(() => {
    if (!open) setBusy(false);
  }, [open]);

  if (!open) return null;

  return (
    <Modal
      onClose={() => { if (!busyRef.current) onClose(false); }}
      align="center"
      width={480}
      ariaLabel="Confirm your password to continue"
    >
      <h2 style={{ marginTop: 0, marginBottom: 8 }}>
        Confirm your password
      </h2>
      <p style={{ marginTop: 0, fontSize: 13, color: 'var(--text-dim)' }}>
        This action is gated on a recent password re-confirmation. Confirm
        your password to continue; the original request will retry
        automatically. Your session role and permissions are unchanged.
      </p>
      <ElevationForm
        open={open}
        submitLabel="Confirm"
        busyLabel="Confirming…"
        onBusyChange={setBusy}
        onElevated={() => onClose(true)}
        onCancel={() => onClose(false)}
      />
    </Modal>
  );
}
