// Plan[RUN-JOB-UI] follow-up: ad-hoc single-user copy panel.
//
// Surfaces under Servers > User Management when the end user clicks
// a managed user row. The panel unfolds inline below the row with:
//   * Target server picker (filters to Jellyfin / Emby; Plex
//     destinations excluded - Plex cannot create users via API).
//   * Target username field (defaults to the source's display_name).
//   * Auto-generated password with Regen + Copy buttons.
//   * "Run automatic transfer" checkbox (default OFF).
//   * When checked: library picker + metric checkboxes for the
//     follow-up data transfer.
//   * Typed CREATE confirmation gate.
//
// Submit sequence:
//   1. POST /api/users/copy_to_destination -> creates user synchronously.
//      Backend also auto-writes a user_identity_map row.
//   2. If "run automatic transfer" was checked, the panel then
//      submits a separate POST /api/job/direct with the new user
//      pre-filled as user_filter so only their data flows.
//
// Failure of step 1 aborts the flow with an inline error. Failure
// of step 2 surfaces a soft warning (user creation succeeded; the
// data transfer can be retried manually). Two-phase rollback on
// step 1 is handled by services/user_creation.py on the backend.

import { useEffect, useMemo, useState } from 'react';
import { api, ServerView, ServerManagedUser } from '../api';


export interface UserCopyPanelProps {
  /** Source server (the one the user lives on today). */
  sourceServer: ServerView;
  /** The source user being copied. */
  sourceUser: ServerManagedUser;
  /** All registered servers; the panel filters to non-Plex for
   * the target picker. */
  allServers: ServerView[];
  /** Library list for the target server, fetched by the parent
   * when the target is selected. Empty when no target yet or while
   * the fetch is in flight. */
  targetLibraries?: Array<{ name: string }>;
  /** Called when the panel collapses (Cancel button or post-success
   * auto-collapse). */
  onClose: () => void;
  /** Optional callback for the parent to refresh its user list
   * after a successful copy. */
  onCopySucceeded?: () => void;
}


function _generatePassword(): string {
  // 16 ascii-safe characters from the browser CSPRNG.
  const ALPHA = 'ABCDEFGHIJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789';
  const out: string[] = [];
  try {
    const buf = new Uint32Array(16);
    crypto.getRandomValues(buf);
    for (let i = 0; i < 16; i += 1) out.push(ALPHA[buf[i] % ALPHA.length]);
  } catch {
    for (let i = 0; i < 16; i += 1) {
      out.push(ALPHA[Math.floor(Math.random() * ALPHA.length)]);
    }
  }
  return out.join('');
}


export function UserCopyPanel({
  sourceServer,
  sourceUser,
  allServers,
  targetLibraries,
  onClose,
  onCopySucceeded,
}: UserCopyPanelProps) {
  // Eligible targets: any Jellyfin / Emby server that is not the
  // source itself. Plex destinations cannot be created via API and
  // are filtered out at the boundary; the backend rejects them too.
  const eligibleTargets = useMemo(
    () =>
      allServers.filter(
        (s) =>
          s.id !== sourceServer.id
          && (s.service_type === 'jellyfin' || s.service_type === 'emby'),
      ),
    [allServers, sourceServer.id],
  );

  const [targetServerId, setTargetServerId] = useState<string>(
    eligibleTargets[0]?.id || '',
  );
  const [targetUsername, setTargetUsername] = useState<string>(
    sourceUser.display_name || sourceUser.username || '',
  );
  const [tempPassword, setTempPassword] = useState<string>(_generatePassword());
  const [runTransfer, setRunTransfer] = useState<boolean>(false);
  const [selectedLibs, setSelectedLibs] = useState<Set<string>>(new Set());
  const [includeWatchHistory, setIncludeWatchHistory] = useState<boolean>(true);
  const [includeRatings, setIncludeRatings] = useState<boolean>(true);
  const [includePlaylists, setIncludePlaylists] = useState<boolean>(true);
  const [includeCollections, setIncludeCollections] = useState<boolean>(true);
  const [confirmText, setConfirmText] = useState<string>('');
  const [submitting, setSubmitting] = useState<boolean>(false);
  const [statusMessage, setStatusMessage] = useState<string | null>(null);
  const [statusKind, setStatusKind] = useState<'success' | 'error' | null>(null);

  // Reset when the source user changes (parent re-mounts).
  useEffect(() => {
    setTargetUsername(sourceUser.display_name || sourceUser.username || '');
    setTempPassword(_generatePassword());
    setRunTransfer(false);
    setSelectedLibs(new Set());
    setConfirmText('');
    setStatusMessage(null);
    setStatusKind(null);
  }, [sourceUser]);

  const isConfirmed = confirmText.trim() === 'CREATE';
  const targetServer = eligibleTargets.find((s) => s.id === targetServerId) || null;
  const canSubmit =
    !!targetServer
    && targetUsername.trim().length > 0
    && tempPassword.length > 0
    && isConfirmed
    && !submitting;

  const submit = async () => {
    if (!targetServer) return;
    setSubmitting(true);
    setStatusMessage(null);
    setStatusKind(null);
    try {
      const createResult = await api.copyUserToDestination({
        source_server_id: sourceServer.id,
        target_server_id: targetServer.id,
        source_user_handle: sourceUser.username,
        target_username: targetUsername.trim(),
        temp_password: tempPassword,
      });
      let transferMessage = '';
      if (runTransfer) {
        // Follow-up direct-transfer job. The backend auto-wrote the
        // identity map row on the create above, so user_filter
        // matched on the source handle resolves cleanly to the
        // newly-created destination user.
        try {
          const payload: Record<string, unknown> = {
            source_server_name: sourceServer.name,
            dest_server_names: [targetServer.name],
            libraries: Array.from(selectedLibs),
            include_watch_history: includeWatchHistory,
            include_ratings: includeRatings,
            include_playlists: includePlaylists,
            include_collections: includeCollections,
            user_filter: [sourceUser.username],
            include_managed_users: true,
            mode: 'merge',
            confirm_replace: false,
            auto_capture_before_replace: false,
            strict_match: true,
            merge_watch_strategy: 'higher',
          };
          const job = await api.submitDirect(payload);
          transferMessage = ` Direct-transfer job ${job.job_id} queued for ${selectedLibs.size} library/ies.`;
        } catch (e) {
          transferMessage = ` Note: user created, but the follow-up transfer failed to submit: ${String(e)}`;
        }
      }
      setStatusMessage(
        `User created on destination: ${createResult.target_username} (backend id ${createResult.backend_user_id}). Identity mapping written.${transferMessage}`,
      );
      setStatusKind('success');
      if (onCopySucceeded) onCopySucceeded();
    } catch (e) {
      setStatusMessage(`User creation failed: ${String(e)}`);
      setStatusKind('error');
    } finally {
      setSubmitting(false);
    }
  };

  if (eligibleTargets.length === 0) {
    return (
      <div
        className="panel"
        style={{
          background: 'rgba(217, 119, 6, 0.05)',
          border: '1px solid var(--warn, #d97706)',
          padding: '10px 14px',
          fontSize: 13,
        }}
      >
        <div style={{ marginBottom: 6, color: 'var(--warn, #d97706)' }}>
          <strong>No eligible target servers.</strong>
        </div>
        <div style={{ fontSize: 12, color: 'var(--text-dim)' }}>
          Register a Jellyfin or Emby server under Servers &gt; Overview to enable this flow.
          Plex destinations cannot be created via API.
        </div>
        <div style={{ marginTop: 8 }}>
          <button type="button" onClick={onClose} style={{ fontSize: 11 }}>Close</button>
        </div>
      </div>
    );
  }

  return (
    <div
      className="panel"
      style={{
        background: 'rgba(74, 122, 252, 0.04)',
        border: '1px solid var(--accent, #4a7afc)',
        padding: '12px 16px',
        marginTop: 6,
      }}
    >
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8 }}>
        <div style={{ fontSize: 13 }}>
          <strong>Copy user to destination</strong>
          <span style={{ color: 'var(--text-dim)', marginLeft: 8 }}>
            Source: {sourceServer.name} / {sourceUser.display_name || sourceUser.username}
          </span>
        </div>
        <button type="button" onClick={onClose} style={{ fontSize: 11, padding: '2px 8px' }}>
          Close
        </button>
      </div>

      {/* Target server picker */}
      <div style={{ marginTop: 6, display: 'flex', gap: 8, alignItems: 'center', fontSize: 13 }}>
        <span style={{ minWidth: 130, color: 'var(--text-dim)' }}>Target server:</span>
        <select
          value={targetServerId}
          onChange={(e) => setTargetServerId(e.target.value)}
          style={{ flex: 1, fontSize: 12 }}
        >
          {eligibleTargets.map((s) => (
            <option key={s.id} value={s.id}>
              {s.name} ({s.service_type})
            </option>
          ))}
        </select>
      </div>

      {/* Target username + password */}
      <div style={{ marginTop: 8, display: 'flex', gap: 8, alignItems: 'center', fontSize: 13 }}>
        <span style={{ minWidth: 130, color: 'var(--text-dim)' }}>Target username:</span>
        <input
          type="text"
          value={targetUsername}
          onChange={(e) => setTargetUsername(e.target.value)}
          style={{ flex: 1, fontSize: 12 }}
        />
      </div>
      <div style={{ marginTop: 8, display: 'flex', gap: 8, alignItems: 'center', fontSize: 13 }}>
        <span style={{ minWidth: 130, color: 'var(--text-dim)' }}>Temp password:</span>
        <input
          type="text"
          readOnly
          value={tempPassword}
          style={{ flex: 1, fontFamily: 'monospace', fontSize: 11 }}
        />
        <button
          type="button"
          onClick={() => setTempPassword(_generatePassword())}
          style={{ fontSize: 10, padding: '2px 8px' }}
        >
          Regen
        </button>
        <button
          type="button"
          onClick={() => {
            try { navigator.clipboard.writeText(tempPassword); } catch { /* */ }
          }}
          style={{ fontSize: 10, padding: '2px 8px' }}
        >
          Copy
        </button>
      </div>

      {/* Run automatic transfer toggle */}
      <div style={{ marginTop: 10, fontSize: 13 }}>
        <label style={{ display: 'flex', alignItems: 'center', gap: 8, cursor: 'pointer' }}>
          <input
            type="checkbox"
            checked={runTransfer}
            onChange={(e) => setRunTransfer(e.target.checked)}
          />
          <strong>Run automatic transfer</strong>
          <span style={{ fontSize: 11, color: 'var(--text-dim)', marginLeft: 4 }}>
            After creating the user, queue a direct-transfer job that copies this user's
            data from {sourceServer.name} to the target.
          </span>
        </label>
      </div>

      {/* Expanded transfer options */}
      {runTransfer && (
        <div
          style={{
            marginTop: 10,
            padding: '8px 12px',
            background: 'rgba(74, 122, 252, 0.07)',
            borderRadius: 6,
            fontSize: 12,
          }}
        >
          <div style={{ marginBottom: 6, fontWeight: 600 }}>Transfer scope</div>
          <div style={{ marginBottom: 8 }}>
            <strong>Libraries:</strong>
            <div style={{ marginTop: 4, display: 'flex', gap: 8, flexWrap: 'wrap' }}>
              {(targetLibraries || []).length === 0 ? (
                <span style={{ color: 'var(--text-dim)' }}>
                  No libraries available yet (pick a target server first or wait for fetch).
                </span>
              ) : (
                (targetLibraries || []).map((L) => (
                  <label key={L.name} style={{ display: 'flex', alignItems: 'center', gap: 4, fontSize: 11 }}>
                    <input
                      type="checkbox"
                      checked={selectedLibs.has(L.name)}
                      onChange={(e) => {
                        setSelectedLibs((prev) => {
                          const next = new Set(prev);
                          if (e.target.checked) next.add(L.name);
                          else next.delete(L.name);
                          return next;
                        });
                      }}
                    />
                    {L.name}
                  </label>
                ))
              )}
            </div>
          </div>
          <div style={{ marginBottom: 6 }}>
            <strong>Metrics:</strong>
            <div style={{ marginTop: 4, display: 'flex', gap: 12, flexWrap: 'wrap' }}>
              <label><input type="checkbox" checked={includeWatchHistory} onChange={(e) => setIncludeWatchHistory(e.target.checked)} /> Watch history</label>
              <label><input type="checkbox" checked={includeRatings} onChange={(e) => setIncludeRatings(e.target.checked)} /> Ratings</label>
              <label><input type="checkbox" checked={includePlaylists} onChange={(e) => setIncludePlaylists(e.target.checked)} /> Playlists</label>
              <label><input type="checkbox" checked={includeCollections} onChange={(e) => setIncludeCollections(e.target.checked)} /> Collections</label>
            </div>
          </div>
        </div>
      )}

      {/* Typed CREATE confirmation */}
      <div style={{ marginTop: 12, padding: 10, borderRadius: 6, background: 'rgba(239, 68, 68, 0.08)', border: '1px solid var(--danger, #ef4444)', fontSize: 12 }}>
        <div style={{ marginBottom: 6 }}>
          <strong>Type CREATE to confirm.</strong> This writes a new user account directly on the destination server.
        </div>
        <input
          type="text"
          value={confirmText}
          onChange={(e) => setConfirmText(e.target.value)}
          placeholder="Type CREATE here"
          style={{ width: '100%', fontFamily: 'monospace', fontSize: 13 }}
        />
      </div>

      {/* Actions */}
      <div style={{ marginTop: 12, display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
        <button type="button" onClick={onClose} style={{ fontSize: 12, padding: '5px 12px' }}>
          Cancel
        </button>
        <button
          type="button"
          className="primary"
          disabled={!canSubmit}
          onClick={submit}
          style={{ fontSize: 12, padding: '5px 12px' }}
        >
          {submitting ? 'Working...' : runTransfer ? 'Copy user and start transfer' : 'Copy user'}
        </button>
      </div>

      {/* Status */}
      {statusMessage && (
        <div
          style={{
            marginTop: 10,
            padding: '8px 12px',
            borderRadius: 4,
            background: statusKind === 'success'
              ? 'rgba(34, 197, 94, 0.10)'
              : 'rgba(239, 68, 68, 0.10)',
            border: '1px solid ' + (statusKind === 'success' ? 'var(--success, #22c55e)' : 'var(--danger, #ef4444)'),
            color: statusKind === 'success' ? 'var(--success, #22c55e)' : 'var(--danger, #ef4444)',
            fontSize: 12,
          }}
        >
          {statusMessage}
        </div>
      )}
    </div>
  );
}
