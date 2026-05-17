// ── Set Backend panel + cross-backend sub-card ─────────────────────────
//
// Backend-class layer above Mode & Servers. Renders the backend tab
// strip (Plex / Jellyfin / Emby / Cross-platform), per-workflow
// banner copy, and the cross-backend sub-card with D-RATE radio +
// D-OWNER trigger + D-COL-SCOPE warning.
//
// Routing model (2026-05-16 simplification):
//   * Plex / Jellyfin / Emby tabs filter BOTH source and destination
//     pickers below to that backend. Cross-backend transfer is
//     impossible from these tabs by construction.
//   * Cross-platform tab unlocks "open selection" mode: source and
//     destination pickers show every registered server regardless of
//     backend. This is the only tab from which a cross-backend route
//     (source.backend != dest.backend) can be constructed.
//   * The cross-backend sub-card (D-RATE / D-OWNER / D-COL-SCOPE)
//     renders only when the end user's actual picker selection
//     produces a cross-backend route - that's the ``isCrossBackend``
//     prop, computed in the parent from the picked source + dest
//     server.service_type values.
//
// The previous destination-backend chip row was removed - the tab
// strip is the single backend selector now, and the sub-card's
// visibility keys off actual picker selection rather than chip state.

import type { ServerView } from '../api';
import { backendCounts, type BackendType } from './BackendTabStrip';

export type WorkflowMode = 'plex' | 'jellyfin' | 'emby' | 'cross';
export type BackendOrCross = 'plex' | 'jellyfin' | 'emby';
export type RateMode = 'default' | 'tunable' | 'numeric_only';

interface Props {
  servers: ServerView[];
  workflowMode: WorkflowMode;
  onWorkflowChange: (mode: WorkflowMode) => void;
  populatedBackendsCount: number;
  // Source backend label for the sub-card. Derived in the parent
  // from the picked source server's service_type.
  sourceBackend: BackendOrCross;
  // Destination backend label for the sub-card. Derived in the
  // parent from the picked destination server(s)' service_type.
  // When fan-out crosses backends, the parent picks any one of
  // them - the sub-card surfaces the warning either way via
  // isCrossBackend.
  destBackend: BackendOrCross;
  // True when source.service_type !== dest.service_type for the
  // picker selection. Drives sub-card visibility.
  isCrossBackend: boolean;
  rateMode: RateMode;
  onRateModeChange: (m: RateMode) => void;
  rateThreshold: string;
  onRateThresholdChange: (v: string) => void;
  userCreateSpecCount: number;
  onOpenUserCreateModal: () => void;
}

export function WorkflowStrip(props: Props) {
  const {
    servers,
    workflowMode,
    onWorkflowChange,
    populatedBackendsCount,
    sourceBackend,
    destBackend,
    isCrossBackend,
    rateMode,
    onRateModeChange,
    rateThreshold,
    onRateThresholdChange,
    userCreateSpecCount,
    onOpenUserCreateModal,
  } = props;
  const showFirstRunNotice = workflowMode === 'jellyfin' || workflowMode === 'emby';

  return (
    <div className="panel" style={{ padding: '10px 14px' }}>
      <div style={{ marginBottom: 6, fontSize: 12, color: 'var(--text-dim)' }}>
        Set Backend
      </div>
      <nav className="tabs sub-tabs" role="tablist" aria-label="Workflow backend selector">
        {(['plex', 'jellyfin', 'emby'] as BackendType[]).map((b) => {
          const counts = backendCounts(servers);
          const c = counts[b];
          const disabled = c === 0;
          const label = b === 'plex' ? 'Plex' : b === 'jellyfin' ? 'Jellyfin' : 'Emby';
          return (
            <button
              key={b}
              type="button"
              role="tab"
              aria-selected={workflowMode === b}
              disabled={disabled}
              title={disabled
                ? `No ${label} servers registered. Add one under Servers > Overview.`
                : `${c} ${label} server${c === 1 ? '' : 's'}. Same-backend ${label}->${label} workflow.`}
              className={workflowMode === b ? 'active' : ''}
              onClick={() => onWorkflowChange(b)}
            >
              {label} ({c})
            </button>
          );
        })}
        <button
          type="button"
          role="tab"
          aria-selected={workflowMode === 'cross'}
          disabled={populatedBackendsCount < 2}
          title={populatedBackendsCount < 2
            ? 'Cross-platform requires at least two different backend types registered.'
            : 'Pickers below show every registered server regardless of backend.'}
          className={workflowMode === 'cross' ? 'active' : ''}
          onClick={() => onWorkflowChange('cross')}
          style={{
            marginLeft: 12,
            borderLeft: '1px solid var(--border, #444)',
            paddingLeft: 16,
          }}
        >
          Cross-platform
        </button>
      </nav>
      {/* Per-tab banner. Plex: production-ready, no banner.
          Jellyfin / Emby: first-run informational notice (PR-Backends
          shipped 2026-05-16). Cross-platform tab: open selection
          mode - source / dest pickers below show every registered
          server. Cross-backend routes (source.backend !=
          dest.backend) remain gated frontend-side until PR-CrossPolish
          (Phase 2) ships; a same-backend route from the cross-platform
          tab is permitted today. */}
      {workflowMode === 'cross' && (
        <div
          className="banner"
          style={{
            background: 'rgba(217, 119, 6, 0.10)',
            border: '1px solid var(--warn, #d97706)',
            color: 'var(--warn, #d97706)',
            padding: '8px 12px',
            borderRadius: 6,
            marginTop: 8,
            fontSize: 12,
          }}
        >
          <strong>Cross-platform: open selection.</strong>{' '}
          The pickers below show every registered server regardless of
          backend. A same-backend route from here behaves identically to
          using the dedicated backend tab above. A cross-backend route
          (source backend differs from destination backend) is permitted
          to be configured but Submit is gated until the Phase 2 cross-
          backend support ships.
        </div>
      )}
      {showFirstRunNotice && (
        <div
          className="banner"
          style={{
            background: 'rgba(59, 130, 246, 0.08)',
            border: '1px solid var(--info, #3b82f6)',
            color: 'var(--info, #3b82f6)',
            padding: '8px 12px',
            borderRadius: 6,
            marginTop: 8,
            fontSize: 12,
          }}
        >
          <strong>{workflowMode === 'jellyfin' ? 'Jellyfin' : 'Emby'} workflow (new).</strong>{' '}
          Same-backend {workflowMode === 'jellyfin' ? 'Jellyfin' : 'Emby'} jobs are
          enabled as of 2026-05-16 (PR-Backends). Adapter contract tests are green
          but this is the first release with end-to-end operator use, so please
          file any quirks you see.
        </div>
      )}

      {/* Cross-backend sub-card: D-RATE radio + D-OWNER user-create
          trigger + D-COL-SCOPE warning. Visible only when the
          end user's actual picker selection produces a cross-backend
          route (sourceBackend !== destBackend), which today is only
          reachable from the Cross-platform tab. */}
      {isCrossBackend && (
        <div
          className="panel"
          style={{
            marginTop: 10,
            background: 'rgba(217, 119, 6, 0.05)',
            border: '1px solid var(--warn, #d97706)',
            padding: '10px 14px',
          }}
        >
          <div style={{ fontWeight: 600, marginBottom: 6, color: 'var(--warn, #d97706)' }}>
            Cross-backend transfer settings
          </div>
          <div style={{ fontSize: 12, color: 'var(--text-dim)', marginBottom: 12 }}>
            Source ({sourceBackend === 'plex' ? 'Plex' : sourceBackend === 'jellyfin' ? 'Jellyfin' : 'Emby'}){' '}
            → Destination ({destBackend === 'plex' ? 'Plex' : destBackend === 'jellyfin' ? 'Jellyfin' : 'Emby'}).
            Per Plan[MULTI-BACKEND] decisions D-RATE + D-OWNER + D-COL-SCOPE, this route surfaces extra controls.
          </div>

          {/* D-RATE radio group */}
          <fieldset className="field" style={{ borderRadius: 6, padding: '8px 12px', marginBottom: 10 }}>
            <legend style={{ padding: '0 6px', fontWeight: 600, fontSize: 12 }}>Rating cross-mapping (D-RATE)</legend>
            <label style={{ display: 'flex', alignItems: 'center', gap: 6, padding: '3px 0', fontSize: 13 }}>
              <input
                type="radio"
                name="d-rate-mode"
                checked={rateMode === 'default'}
                onChange={() => onRateModeChange('default')}
              />
              Favorite at or above 5 (default).
              <span style={{ fontSize: 11, color: 'var(--text-dim)', marginLeft: 4 }}>
                Write both numeric Rating AND IsFavorite when source rating is 5 or higher.
              </span>
            </label>
            <label style={{ display: 'flex', alignItems: 'center', gap: 6, padding: '3px 0', fontSize: 13 }}>
              <input
                type="radio"
                name="d-rate-mode"
                checked={rateMode === 'tunable'}
                onChange={() => onRateModeChange('tunable')}
              />
              Threshold-tunable. At or above:
              <input
                type="number"
                min={0}
                max={10}
                step={0.5}
                value={rateThreshold}
                disabled={rateMode !== 'tunable'}
                onChange={(e) => onRateThresholdChange(e.target.value)}
                style={{ width: 64, marginLeft: 4 }}
              />
              <span style={{ fontSize: 11, color: 'var(--text-dim)', marginLeft: 4 }}>
                stars. Defaults to 5.0 when blank.
              </span>
            </label>
            <label style={{ display: 'flex', alignItems: 'center', gap: 6, padding: '3px 0', fontSize: 13 }}>
              <input
                type="radio"
                name="d-rate-mode"
                checked={rateMode === 'numeric_only'}
                onChange={() => onRateModeChange('numeric_only')}
              />
              Numeric only.
              <span style={{ fontSize: 11, color: 'var(--text-dim)', marginLeft: 4 }}>
                Write only the numeric Rating; never set IsFavorite.
              </span>
            </label>
          </fieldset>

          {/* Phase D (2026-05-16): D-OWNER trigger removed. User
              creation on the destination is now handled per-row inside
              CrossPlatformPreflightModal via the InlineCreateUserForm.
              The status line that used to live here has been deleted;
              the modal surfaces missing-user decisions at submit / save
              time. Props ``userCreateSpecCount`` and
              ``onOpenUserCreateModal`` are retained on the interface
              for back-compat with the call sites; they are no-ops in
              this body and may be dropped in a future cleanup. */}
        </div>
      )}
    </div>
  );
}
