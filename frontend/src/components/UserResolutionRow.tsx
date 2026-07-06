// Cross-platform preflight per-user picker row.
//
// Reads a CppUserResolution from the backend's preflight report.
// Renders: source avatar (text-only badge for now), source username +
// role badge, row counts summary, proposed resolution chip, action
// dropdown, and conditional fields based on the chosen action.
//
// Action options enforce the Plex constraint matrix at the picker:
//   * dest_kind === 'plex': 'create' option is hidden (Plex requires
//     the Home invite flow); 'map' with final_role='admin' is hidden
//     (Plex doesn't expose admin elevation).
//
// Decisions flow to the parent via onChange. The parent's
// CrossPlatformPreflightAck for this row is computed from the row's
// action + conditional fields.

import { useState } from 'react';
import type { CppUserResolution, CppDestUserOption, CppUserResolutionDecision } from '../api';
import { InlineCreateUserForm } from './InlineCreateUserForm';

interface Props {
  resolution: CppUserResolution;
  destKind: 'plex' | 'jellyfin' | 'emby' | string;
  destServerId: string;
  destServerLabel: string;
  decision: CppUserResolutionDecision;
  onChange: (next: CppUserResolutionDecision) => void;
}

const ROLE_COLOURS: Record<string, string> = {
  owner: 'var(--accent, #4a7afc)',
  admin: 'var(--warn, #d97706)',
  managed: 'var(--text-dim)',
};

const VERDICT_TONE: Record<string, { bg: string; fg: string; label: string }> = {
  identity_map:           { bg: 'rgba(34,197,94,0.10)',  fg: 'var(--success, #16a34a)', label: 'identity map' },
  direct_match:           { bg: 'rgba(34,197,94,0.10)',  fg: 'var(--success, #16a34a)', label: 'direct match' },
  single_admin_fallback:  { bg: 'rgba(217,119,6,0.10)',  fg: 'var(--warn, #d97706)',    label: 'single-admin fallback' },
  role_flip_ack:          { bg: 'rgba(217,119,6,0.10)',  fg: 'var(--warn, #d97706)',    label: 'role flip - needs ack' },
  tombstone_blocked:      { bg: 'rgba(239,68,68,0.10)',  fg: 'var(--bad, #ef4444)',     label: 'tombstone - blocked' },
  zero_row_skip:          { bg: 'rgba(217,119,6,0.10)',  fg: 'var(--warn, #d97706)',    label: 'zero-row skip' },
  no_match:               { bg: 'rgba(239,68,68,0.10)',  fg: 'var(--bad, #ef4444)',     label: 'no match - blocked' },
  multi_admin_collapse:   { bg: 'rgba(217,119,6,0.10)',  fg: 'var(--warn, #d97706)',    label: 'multi-admin collapse' },
};

export function UserResolutionRow({
  resolution,
  destKind,
  destServerId,
  destServerLabel,
  decision,
  onChange,
}: Props) {
  const [createOpen, setCreateOpen] = useState(false);
  const isPlexDest = destKind === 'plex';
  const verdict = VERDICT_TONE[resolution.proposed_resolution] || {
    bg: 'transparent',
    fg: 'var(--text-dim)',
    label: resolution.proposed_resolution,
  };
  const eligibleDestUsers = resolution.available_dest_users.filter((u) => !u.is_tombstoned);

  const updateDecision = (patch: Partial<CppUserResolutionDecision>) => {
    onChange({ ...decision, ...patch });
  };

  return (
    <div
      className="panel"
      style={{
        marginBottom: 8,
        padding: '10px 12px',
        borderLeft: `3px solid ${resolution.blocks_submit ? 'var(--bad, #ef4444)' : (resolution.needs_ack ? 'var(--warn, #d97706)' : 'var(--border, #2a3146)')}`,
      }}
    >
      {/* Header row: source identity + proposed verdict chip */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 6 }}>
        <strong style={{ fontSize: 13 }}>{resolution.source_username}</strong>
        <span
          className="tag"
          style={{
            fontSize: 10,
            color: ROLE_COLOURS[resolution.source_role] ?? 'var(--text-dim)',
          }}
        >
          {resolution.source_role}
        </span>
        <span
          style={{
            fontSize: 10,
            padding: '2px 8px',
            borderRadius: 3,
            background: verdict.bg,
            color: verdict.fg,
            marginLeft: 'auto',
          }}
        >
          {verdict.label}
        </span>
      </div>

      {/* Row counts summary */}
      <div style={{ fontSize: 11, color: 'var(--text-dim)', marginBottom: 8 }}>
        {resolution.source_row_counts.watch_history} watch ·{' '}
        {resolution.source_row_counts.ratings} ratings ·{' '}
        {resolution.source_row_counts.playlists} playlists ·{' '}
        {resolution.source_row_counts.collections} collections
        {resolution.proposed_dest_username && (
          <span style={{ marginLeft: 8 }}>
            → proposed: <strong>{resolution.proposed_dest_username}</strong>
            {resolution.proposed_dest_role && (
              <span> ({resolution.proposed_dest_role})</span>
            )}
          </span>
        )}
      </div>

      {/* Inline warnings */}
      {resolution.warnings.length > 0 && (
        <div style={{ fontSize: 11, color: 'var(--warn, #d97706)', marginBottom: 8 }}>
          {resolution.warnings.map((w, i) => (
            <div key={`${i}-${String(w).slice(0, 32)}`}>⚠ {w}</div>
          ))}
        </div>
      )}

      {/* Action picker */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
        <span style={{ fontSize: 12, color: 'var(--text-dim)' }}>Action:</span>
        <select
          value={decision.action}
          onChange={(e) => {
            const action = e.target.value as CppUserResolutionDecision['action'];
            updateDecision({
              action,
              // Clear stale fields when switching action
              dest_user_id: undefined,
              final_role: undefined,
              create_username: undefined,
              create_role: undefined,
              create_password: undefined,
              admin_acknowledgement: undefined,
            });
            setCreateOpen(false);
          }}
          style={{ fontSize: 12 }}
        >
          <option value="accept_proposed">Accept proposed</option>
          <option value="map">Map to existing user</option>
          {!isPlexDest && <option value="create">Create new on destination</option>}
          <option value="drop">Drop (skip this user)</option>
        </select>

        {/* action=map: dest user picker + final role (when non-Plex) */}
        {decision.action === 'map' && (
          <>
            <select
              value={decision.dest_user_id ?? ''}
              onChange={(e) => updateDecision({ dest_user_id: e.target.value || undefined })}
              style={{ fontSize: 12 }}
            >
              <option value="">- pick a destination user -</option>
              {eligibleDestUsers.map((u) => (
                <option key={u.backend_user_id} value={u.backend_user_id}>
                  {u.username} ({u.role})
                </option>
              ))}
            </select>
            {!isPlexDest && (
              <select
                value={decision.final_role ?? 'managed'}
                onChange={(e) => updateDecision({ final_role: e.target.value as 'admin' | 'managed' })}
                style={{ fontSize: 12 }}
                title="Role the destination user should END at after this run. Plex destinations don't expose role elevation."
              >
                <option value="managed">Final role: managed</option>
                <option value="admin">Final role: admin</option>
              </select>
            )}
          </>
        )}

        {/* action=create: inline form trigger */}
        {decision.action === 'create' && !isPlexDest && (
          <button
            type="button"
            onClick={() => setCreateOpen(true)}
            disabled={createOpen}
            style={{ fontSize: 12 }}
          >
            {decision.create_username
              ? `Created: ${decision.create_username}`
              : 'Configure create…'}
          </button>
        )}

        {/* action=drop: confirmation copy */}
        {decision.action === 'drop' && (
          <span style={{ fontSize: 11, color: 'var(--text-dim)' }}>
            This user's data will not transfer.
          </span>
        )}
      </div>

      {/* Inline create form drawer */}
      {decision.action === 'create' && createOpen && !isPlexDest && (
        <InlineCreateUserForm
          destinationServerId={destServerId}
          destinationLabel={destServerLabel}
          destKind={destKind}
          onCancel={() => setCreateOpen(false)}
          onCreated={(user: CppDestUserOption, wasNewlyCreated) => {
            // Auto-switch the row to "map" against the just-created
            // user so the rest of the flow consumes the new dest_user_id.
            updateDecision({
              action: 'map',
              dest_user_id: user.backend_user_id,
              final_role: user.role === 'admin' ? 'admin' : 'managed',
              create_username: user.username,
            });
            setCreateOpen(false);
            // Surface the idempotency flag for operator awareness via
            // the row's warnings (one-shot; the parent can persist if
            // it cares).
            if (!wasNewlyCreated) {
              // No state slot for this today; rely on the parent's
              // warning channel to surface it.
            }
          }}
        />
      )}
    </div>
  );
}
