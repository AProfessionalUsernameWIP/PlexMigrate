// Pre-submit confirmation for Replace Restore.
//
// The Run Job and Schedules forms show this modal when the operator
// has picked Replace mode and hits the run / save button. The modal
// lists the destructive consequences and requires the operator to
// type the literal word REPLACE before the confirm button activates.
// Cancelling aborts the submit; confirming sets ``confirm_replace``
// to true on the outgoing payload.
//
// The backend Pydantic validator also enforces confirm_replace=true
// when mode=replace (a malicious API caller can't bypass the gate),
// so this modal is the UX layer of a two-layer protection.

import { useEffect, useState } from 'react';

interface Props {
  open: boolean;
  // Friendly label shown in the modal body. For restore jobs this is
  // the destination server name; for direct transfers it's the
  // destination(s); for schedules it's "this scheduled job".
  targetLabel: string;
  // Whether the form has auto-capture enabled. Surfaced in the modal
  // body so the operator sees what safety belt (if any) is in place.
  autoCaptureBeforeReplace: boolean;
  onCancel: () => void;
  onConfirm: () => void;
}

const REQUIRED_TYPED = 'REPLACE';

export function ReplaceConfirmModal({
  open,
  targetLabel,
  autoCaptureBeforeReplace,
  onCancel,
  onConfirm,
}: Props) {
  const [typed, setTyped] = useState('');

  // Reset the typed field every time the modal opens. Without this, a
  // cancelled+reopened modal would still have "REPLACE" in the field
  // and the button would be primed - the operator effectively skipped
  // the typed-confirmation gate on the second pass.
  useEffect(() => {
    if (open) setTyped('');
  }, [open]);

  if (!open) return null;

  const ready = typed.trim() === REQUIRED_TYPED;

  return (
    <div
      onClick={onCancel}
      style={{
        position: 'fixed',
        inset: 0,
        background: 'rgba(0,0,0,0.55)',
        zIndex: 1000,
        display: 'flex',
        alignItems: 'flex-start',
        justifyContent: 'center',
        paddingTop: '8vh',
      }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        className="panel"
        style={{ width: 520, maxWidth: '94vw' }}
      >
        <h2 style={{ marginTop: 0 }}>
          <span style={{ color: 'var(--warn, #f5a623)' }}>Replace</span> will overwrite destination data
        </h2>
        <p style={{ marginTop: 0 }}>
          You're about to run <strong>Replace Restore</strong> against{' '}
          <strong>{targetLabel}</strong>. Unlike the default Merge restore,
          Replace makes the destination <em>exactly</em> match the snapshot,
          which means:
        </p>
        <ul style={{ marginTop: 0, paddingLeft: 20, lineHeight: 1.7 }}>
          <li>
            View counts and resume positions <strong>reset</strong> to the
            snapshot's values - destination plays added after the snapshot
            was taken go away.
          </li>
          <li>
            Ratings <strong>overwrite</strong> any current value (Merge mode
            would have skipped them).
          </li>
          <li>
            Playlist and collection members not in the snapshot are{' '}
            <strong>removed</strong>. Members in both stay; members only in
            the snapshot are added.
          </li>
          <li>
            Smart playlists, snapshot files, and the items in your library
            are <strong>untouched</strong>.
          </li>
        </ul>

        <div
          style={{
            marginTop: 14,
            padding: '8px 12px',
            borderRadius: 4,
            background: autoCaptureBeforeReplace
              ? 'rgba(34, 197, 94, 0.08)'
              : 'rgba(245, 166, 35, 0.10)',
            border: `1px solid ${
              autoCaptureBeforeReplace ? 'var(--ok, #22c55e)' : 'var(--warn, #f5a623)'
            }`,
            fontSize: 13,
          }}
        >
          {autoCaptureBeforeReplace ? (
            <>
              <strong>Safety belt is on:</strong> a fresh snapshot of the
              destination will be captured before the Replace runs so you
              have a rollback point. If the pre-snapshot fails, the Replace
              aborts.
            </>
          ) : (
            <>
              <strong>Safety belt is off.</strong> No pre-Replace snapshot
              will be captured. If you pick the wrong source snapshot,
              there's no automatic rollback. Cancel and re-enable the
              auto-capture checkbox if you want a recovery point.
            </>
          )}
        </div>

        <label
          style={{
            display: 'block',
            marginTop: 16,
            fontSize: 13,
          }}
        >
          Type <code style={{ background: 'var(--panel-alt, #1b2233)', padding: '0 4px' }}>REPLACE</code>{' '}
          (uppercase, exact match) to confirm:
          <input
            type="text"
            value={typed}
            onChange={(e) => setTyped(e.target.value)}
            autoFocus
            autoComplete="off"
            spellCheck={false}
            placeholder="REPLACE"
            style={{
              display: 'block',
              width: '100%',
              marginTop: 6,
              fontFamily: 'var(--mono, monospace)',
              fontSize: 16,
              letterSpacing: 2,
              padding: '8px 10px',
              boxSizing: 'border-box',
            }}
          />
        </label>

        <div
          className="row-buttons"
          style={{ marginTop: 16, display: 'flex', gap: 8, justifyContent: 'flex-end' }}
        >
          <button onClick={onCancel}>Cancel</button>
          <button
            className="primary"
            disabled={!ready}
            onClick={() => {
              if (ready) onConfirm();
            }}
            style={{
              background: ready ? 'var(--warn, #f5a623)' : undefined,
              borderColor: ready ? 'var(--warn, #f5a623)' : undefined,
              color: ready ? '#1a1a1a' : undefined,
            }}
          >
            Replace Restore
          </button>
        </div>
      </div>
    </div>
  );
}
