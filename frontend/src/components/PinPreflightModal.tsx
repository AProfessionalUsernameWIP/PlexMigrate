// PR-12 preflight modal.
//
// The Run Job form shows this modal between the operator's "Run"
// click and the actual job submission when the backend's preflight
// check (``POST /api/job/preflight-pin-check``) returns a non-empty
// list of at-risk managed users. At-risk = no stored auth token AND
// no stored Plex Home PIN on file for that user. The engine can still
// proceed via admin-token impersonation, but PIN-scoped content may
// come back incomplete, so we surface the list before commit.
//
// "Continue anyway" sets ``pin_preflight_acknowledged: true`` (and
// stamps ``pin_preflight_at_risk`` for the audit log) on the next
// submit, which the backend remaps to underscore-prefixed synthetic
// params on the JobRecord. The engine writes a per-run warning line
// to ``runtime.log`` when the flag is set.
//
// "Cancel" aborts the submit; the operator can fix things (save the
// missing PINs under Servers - User Management) and try again.

interface Props {
  open: boolean;
  // The usernames returned by the preflight endpoint. Rendered as a
  // bullet list in the modal body.
  atRiskUsers: string[];
  onCancel: () => void;
  onContinue: () => void;
}

export function PinPreflightModal({
  open,
  atRiskUsers,
  onCancel,
  onContinue,
}: Props) {
  if (!open) return null;

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
      <div
        onClick={(e) => e.stopPropagation()}
        style={{
          background: 'var(--panel, #131826)',
          border: '1px solid var(--warn, #d97706)',
          borderRadius: 8,
          maxWidth: 560,
          width: '100%',
          padding: 20,
          boxShadow: '0 10px 30px rgba(0,0,0,0.5)',
        }}
      >
        <div style={{ display: 'flex', alignItems: 'baseline', gap: 10, marginBottom: 10 }}>
          <span style={{ fontSize: 22, color: 'var(--warn, #d97706)' }}>!</span>
          <h2 style={{ margin: 0, fontSize: 16 }}>PIN-protected users detected</h2>
        </div>

        <div style={{ fontSize: 13, color: 'var(--text)', lineHeight: 1.5 }}>
          <p style={{ marginTop: 0 }}>
            The following users have Plex Home PINs set and no PIN is
            stored in the database. The engine will fall back to
            admin-token impersonation for them, which may return
            incomplete data for PIN-scoped content.
          </p>

          <ul
            style={{
              listStyle: 'disc',
              paddingLeft: 22,
              maxHeight: 180,
              overflowY: 'auto',
              margin: '8px 0 12px 0',
              background: 'var(--panel-alt, #1b2233)',
              border: '1px solid var(--border, #2a3146)',
              borderRadius: 6,
              padding: '8px 8px 8px 28px',
            }}
          >
            {atRiskUsers.map((u) => (
              <li key={u} style={{ marginBottom: 2 }}>{u}</li>
            ))}
          </ul>

          <p style={{ marginBottom: 0, fontSize: 12, color: 'var(--text-dim)' }}>
            To resolve before running: open <strong>Servers - User Management</strong>,
            select this server, and save each user's PIN. Then come back
            and run the job. The next sync will capture their tokens
            automatically.
          </p>
        </div>

        <div
          style={{
            display: 'flex',
            justifyContent: 'flex-end',
            gap: 8,
            marginTop: 16,
          }}
        >
          <button onClick={onCancel}>Cancel</button>
          <button className="danger" onClick={onContinue}>
            Continue anyway
          </button>
        </div>
      </div>
    </div>
  );
}
