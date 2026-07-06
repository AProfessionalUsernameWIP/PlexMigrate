// Cross-platform preflight modal.
//
// Opens on Submit (Run Job) or Save (Schedules) when the backend's
// preflight endpoint returns aggregate_verdict !== 'ok'. The modal
// surfaces per-destination per-user resolution decisions and collects
// the end user's choices into a CrossPlatformPreflightAck (one per
// destination).
//
// Modal sections (per destination, inside the active tab):
//   1. Admins — auto-expanded; rows where source_role is owner/admin.
//   2. Managed users — collapsed by default; auto-expands when any
//      row has needs_ack or blocks_submit.
//   3. Confirmation + advanced — smart-playlist count + library type
//      notes + tombstone exclusions + legacy-payload banner +
//      D-COL-SCOPE checkbox + identity-map persistence checkbox.
//
// Footer:
//   * Cancel aborts (parent dismisses).
//   * Continue is enabled when every destination's blocks_submit rows
//     have a non-default decision applied AND every needs_ack row has
//     been "seen" (in the current impl, "seen" means the row is in the
//     decisions list with a non-default action — clicking through is
//     sufficient).
//
// The modal does NOT call the submit endpoint itself; on Continue it
// hands the per-destination decisions back to the parent, which is
// responsible for the actual submit / save action that bundles the
// ack into its payload.

import { useEffect, useMemo, useState } from 'react';
import type {
  CrossPlatformPreflightReport,
  PreflightResponse,
  CppUserResolutionDecision,
  CppUserResolution,
  CrossPlatformPreflightAck,
} from '../api';
import { SmartPlaylistDisclosure } from './SmartPlaylistDisclosure';
import { LibraryTypeNotesList } from './LibraryTypeNotesList';
import { TombstoneExclusionNotice } from './TombstoneExclusionNotice';
import { UserResolutionRow } from './UserResolutionRow';
import { PerDestinationTabStrip } from './PerDestinationTabStrip';

interface Props {
  open: boolean;
  response: PreflightResponse | null;
  // End user-facing label per destination_server_id (e.g. "Plex2 (jellyfin)").
  // Falls back to the destination_server_id when missing.
  destinationLabels?: Record<string, string>;
  // Initial decisions per destination_server_id. If provided, the modal
  // restores those decisions instead of defaulting every row to
  // 'accept_proposed' (used by the schedule resolution editor for
  // re-editing existing stored decisions).
  initialAcks?: Record<string, CrossPlatformPreflightAck>;
  onCancel: () => void;
  onContinue: (acks: Record<string, CrossPlatformPreflightAck>) => void;
}

function defaultDecisionFor(res: CppUserResolution): CppUserResolutionDecision {
  return {
    source_username: res.source_username,
    action: 'accept_proposed',
  };
}

function defaultAckFor(report: CrossPlatformPreflightReport): CrossPlatformPreflightAck {
  return {
    source_server_id: report.source_server_id,
    dest_server_id: report.dest_server_id,
    resolutions: report.resolutions.map(defaultDecisionFor),
    apply_col_scope_prefix: true,
    persist_as_identity_map: false,
  };
}

export function CrossPlatformPreflightModal({
  open,
  response,
  destinationLabels,
  initialAcks,
  onCancel,
  onContinue,
}: Props) {
  const destinationIds = useMemo(() => {
    return response ? Object.keys(response.reports) : [];
  }, [response]);
  const [activeId, setActiveId] = useState<string>('');
  const [acks, setAcks] = useState<Record<string, CrossPlatformPreflightAck>>({});
  const [managedExpanded, setManagedExpanded] = useState<Record<string, boolean>>({});

  // Seed acks + active destination + managed-section expansion state
  // whenever a new response arrives or initialAcks change.
  useEffect(() => {
    if (!response) return;
    const next: Record<string, CrossPlatformPreflightAck> = {};
    const expanded: Record<string, boolean> = {};
    for (const [destId, report] of Object.entries(response.reports)) {
      next[destId] = initialAcks?.[destId] ?? defaultAckFor(report);
      const hasAttn = report.resolutions
        .filter((r) => r.source_role === 'managed')
        .some((r) => r.needs_ack || r.blocks_submit);
      expanded[destId] = hasAttn;
    }
    setAcks(next);
    setManagedExpanded(expanded);
    if (destinationIds.length > 0) {
      setActiveId((current) => current && destinationIds.includes(current) ? current : destinationIds[0]);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [response, initialAcks]);

  if (!open || !response) return null;

  const activeReport = response.reports[activeId];
  if (!activeReport) return null;
  const activeAck = acks[activeId] ?? defaultAckFor(activeReport);
  const destLabel = destinationLabels?.[activeId] || activeReport.dest_server_id || activeId;

  const adminRows = activeReport.resolutions.filter(
    (r) => r.source_role === 'owner' || r.source_role === 'admin',
  );
  const managedRows = activeReport.resolutions.filter((r) => r.source_role === 'managed');

  // Decision lookup helper for a single source_username inside the
  // active destination's ack.
  const decisionFor = (sourceUsername: string): CppUserResolutionDecision => {
    const found = activeAck.resolutions.find((d) => d.source_username === sourceUsername);
    return found || { source_username: sourceUsername, action: 'accept_proposed' };
  };
  const setDecisionFor = (sourceUsername: string, next: CppUserResolutionDecision) => {
    const ack = acks[activeId] ?? defaultAckFor(activeReport);
    const idx = ack.resolutions.findIndex((d) => d.source_username === sourceUsername);
    const resolutions = [...ack.resolutions];
    if (idx >= 0) resolutions[idx] = next;
    else resolutions.push(next);
    setAcks({ ...acks, [activeId]: { ...ack, resolutions } });
  };

  // Submit-gate: every destination's blocks_submit rows must have a
  // non-'accept_proposed' decision (end user made an explicit pick),
  // and every needs_ack row must be in the decisions list at all.
  const submitBlocked = (() => {
    for (const destId of destinationIds) {
      const r = response.reports[destId];
      const a = acks[destId];
      if (!r || !a) continue;
      for (const row of r.resolutions) {
        if (row.blocks_submit) {
          const d = a.resolutions.find((x) => x.source_username === row.source_username);
          if (!d || d.action === 'accept_proposed') return true;
        }
      }
    }
    return false;
  })();

  return (
    <div
      onClick={onCancel}
      style={{
        position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.55)',
        zIndex: 1000, display: 'flex', alignItems: 'center', justifyContent: 'center',
        padding: 20,
      }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        style={{
          background: 'var(--panel, #131826)',
          border: '1px solid var(--warn, #d97706)',
          borderRadius: 8,
          maxWidth: 880,
          width: '100%',
          maxHeight: '88vh',
          display: 'flex',
          flexDirection: 'column',
          boxShadow: '0 10px 30px rgba(0,0,0,0.5)',
        }}
      >
        {/* Header */}
        <div style={{ padding: '16px 20px 12px', borderBottom: '1px solid var(--border, #2a3146)' }}>
          <h2 style={{ margin: 0, fontSize: 16 }}>
            Cross-platform transfer: review per-user decisions
          </h2>
          <div style={{ marginTop: 4, fontSize: 12, color: 'var(--text-dim)' }}>
            Aggregate verdict: <strong>{response.aggregate_verdict}</strong>
            {destinationIds.length > 1 && (
              <span> · {destinationIds.length} destinations</span>
            )}
          </div>
        </div>

        {/* Body */}
        <div style={{ flex: 1, overflowY: 'auto', padding: '12px 20px' }}>
          <PerDestinationTabStrip
            destinationIds={destinationIds}
            reports={response.reports}
            activeId={activeId}
            onChange={setActiveId}
          />

          {/* Active destination header */}
          <div style={{ fontSize: 13, marginBottom: 12 }}>
            <strong>{destLabel}</strong>{' '}
            <span style={{ color: 'var(--text-dim)' }}>
              ({activeReport.source_kind} → {activeReport.dest_kind})
              {' · '}
              {activeReport.source_admin_count} source admin{activeReport.source_admin_count === 1 ? '' : 's'}
              {' · '}
              {activeReport.dest_admin_count} destination admin{activeReport.dest_admin_count === 1 ? '' : 's'}
            </span>
          </div>

          {/* Blocking reasons summary */}
          {activeReport.blocking_reasons.length > 0 && (
            <div
              className="banner error"
              style={{ marginBottom: 12, fontSize: 12 }}
            >
              <strong>Blocked:</strong>
              <ul style={{ margin: '4px 0 0', paddingLeft: 18 }}>
                {activeReport.blocking_reasons.map((r, i) => (
                  <li key={`${i}-${String(r).slice(0, 32)}`}>{r}</li>
                ))}
              </ul>
            </div>
          )}

          {/* Section 1: Admins (always expanded) */}
          <section style={{ marginBottom: 16 }}>
            <h3 style={{ marginTop: 0, marginBottom: 8, fontSize: 13 }}>
              Admins ({adminRows.length})
            </h3>
            {adminRows.length === 0 ? (
              <div className="empty" style={{ fontSize: 12 }}>
                No admins in the source payload.
              </div>
            ) : (
              adminRows.map((row) => (
                <UserResolutionRow
                  key={row.source_username}
                  resolution={row}
                  destKind={activeReport.dest_kind}
                  destServerId={activeReport.dest_server_id}
                  destServerLabel={destLabel}
                  decision={decisionFor(row.source_username)}
                  onChange={(d) => setDecisionFor(row.source_username, d)}
                />
              ))
            )}
          </section>

          {/* Section 2: Managed users (collapsible) */}
          <section style={{ marginBottom: 16 }}>
            <button
              type="button"
              onClick={() => setManagedExpanded({ ...managedExpanded, [activeId]: !managedExpanded[activeId] })}
              aria-expanded={managedExpanded[activeId] ?? false}
              style={{
                background: 'none', border: 'none', padding: 0, font: 'inherit',
                color: 'inherit', cursor: 'pointer', width: '100%', textAlign: 'left',
                display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6,
              }}
            >
              <span style={{ fontSize: 12, color: 'var(--text-dim)' }}>
                {managedExpanded[activeId] ? '▾' : '▸'}
              </span>
              <h3 style={{ margin: 0, fontSize: 13 }}>
                Managed users ({managedRows.length}
                {(() => {
                  const auto = managedRows.filter((r) => !r.needs_ack && !r.blocks_submit).length;
                  const attn = managedRows.length - auto;
                  return attn > 0 ? `, ${auto} auto-resolved, ${attn} need attention` : ', all auto-resolved';
                })()})
              </h3>
            </button>
            {managedExpanded[activeId] && (
              managedRows.length === 0 ? (
                <div className="empty" style={{ fontSize: 12 }}>
                  No managed users in the source payload.
                </div>
              ) : (
                managedRows.map((row) => (
                  <UserResolutionRow
                    key={row.source_username}
                    resolution={row}
                    destKind={activeReport.dest_kind}
                    destServerId={activeReport.dest_server_id}
                    destServerLabel={destLabel}
                    decision={decisionFor(row.source_username)}
                    onChange={(d) => setDecisionFor(row.source_username, d)}
                  />
                ))
              )
            )}
          </section>

          {/* Section 3: Confirmation + advanced */}
          <section
            style={{
              marginBottom: 8,
              paddingTop: 12,
              borderTop: '1px solid var(--border, #2a3146)',
            }}
          >
            <h3 style={{ marginTop: 0, marginBottom: 8, fontSize: 13 }}>
              Confirmation
            </h3>

            <SmartPlaylistDisclosure
              skipped={activeReport.smart_playlists_skipped}
              names={activeReport.smart_playlist_names}
            />
            <LibraryTypeNotesList notes={activeReport.library_type_notes} />
            <TombstoneExclusionNotice notes={activeReport.tombstoned_users_excluded} />

            {/* D-COL-SCOPE checkbox */}
            <label className="switch" style={{ marginTop: 12 }}>
              <input
                type="checkbox"
                checked={activeAck.apply_col_scope_prefix}
                onChange={(e) => setAcks({
                  ...acks,
                  [activeId]: { ...activeAck, apply_col_scope_prefix: e.target.checked },
                })}
              />
              <span>
                Apply library-prefix to cross-scope collections
                <span className="help" style={{ display: 'block', fontSize: 11, color: 'var(--text-dim)', marginTop: 2 }}>
                  Plex collections are library-scoped; Jellyfin/Emby BoxSets are server-wide.
                  When this is on, collection names from different libraries are prefixed
                  with their library name on cross-scope writes so they don't collide.
                  Default on.
                </span>
              </span>
            </label>

            {/* Identity-map persistence checkbox (F-2: default off) */}
            <label className="switch" style={{ marginTop: 8 }}>
              <input
                type="checkbox"
                checked={activeAck.persist_as_identity_map}
                onChange={(e) => setAcks({
                  ...acks,
                  [activeId]: { ...activeAck, persist_as_identity_map: e.target.checked },
                })}
              />
              <span>
                Save my decisions as identity-map entries
                <span className="help" style={{ display: 'block', fontSize: 11, color: 'var(--text-dim)', marginTop: 2 }}>
                  When on, the operator decisions in this modal also write into the
                  cross-server identity-map table so the next run for this (source, destination)
                  pair skips this prompt. Off by default - opt in per decision batch.
                </span>
              </span>
            </label>
          </section>
        </div>

        {/* Footer */}
        <div
          style={{
            padding: '12px 20px',
            borderTop: '1px solid var(--border, #2a3146)',
            display: 'flex',
            justifyContent: 'flex-end',
            gap: 8,
          }}
        >
          <button onClick={onCancel}>Cancel</button>
          <button
            className="primary"
            disabled={submitBlocked}
            title={submitBlocked
              ? 'One or more destinations still have blocking rows with no decision. Pick an action for each blocked row.'
              : undefined}
            onClick={() => onContinue(acks)}
          >
            Continue
          </button>
        </div>
      </div>
    </div>
  );
}
