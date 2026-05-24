// Cross-platform preflight per-destination tab strip. Inside
// CrossPlatformPreflightModal the end user may see one report per
// destination (fan-out / schedule with N dests). Each tab shows the
// destination name + per-tab verdict badge so the end user can scan
// which destinations need attention.

import type { CrossPlatformPreflightReport } from '../api';

interface Props {
  destinationIds: string[];
  reports: Record<string, CrossPlatformPreflightReport>;
  activeId: string;
  onChange: (destinationId: string) => void;
}

const VERDICT_PILL: Record<string, { bg: string; fg: string; label: string }> = {
  ok:           { bg: 'rgba(34,197,94,0.15)',  fg: 'var(--success, #16a34a)', label: 'ok' },
  ack_required: { bg: 'rgba(217,119,6,0.15)',  fg: 'var(--warn, #d97706)',    label: 'needs attention' },
  blocked:      { bg: 'rgba(239,68,68,0.15)',  fg: 'var(--bad, #ef4444)',     label: 'blocked' },
};

export function PerDestinationTabStrip({
  destinationIds,
  reports,
  activeId,
  onChange,
}: Props) {
  if (destinationIds.length <= 1) return null;
  return (
    <nav
      className="tabs sub-tabs"
      role="tablist"
      aria-label="Preflight destinations"
      style={{ marginBottom: 12, gap: 4, flexWrap: 'wrap' }}
    >
      {destinationIds.map((destId) => {
        const r = reports[destId];
        const verdict = r?.overall_verdict ?? 'ok';
        const pill = VERDICT_PILL[verdict] || VERDICT_PILL.ok;
        const blocking = r?.resolutions.filter((row) => row.blocks_submit).length ?? 0;
        const acking = r?.resolutions.filter((row) => row.needs_ack && !row.blocks_submit).length ?? 0;
        const countLabel =
          verdict === 'blocked' ? `blocked: ${blocking}`
          : verdict === 'ack_required' ? `needs attention: ${acking || blocking}`
          : 'ok';
        const destLabel = r?.dest_server_id || destId;
        return (
          <button
            key={destId}
            type="button"
            role="tab"
            aria-selected={destId === activeId}
            className={destId === activeId ? 'active' : ''}
            onClick={() => onChange(destId)}
            style={{ fontSize: 12, display: 'flex', alignItems: 'center', gap: 6 }}
          >
            <span>{destLabel}</span>
            <span
              style={{
                fontSize: 10,
                padding: '2px 6px',
                borderRadius: 3,
                background: pill.bg,
                color: pill.fg,
              }}
            >
              {countLabel}
            </span>
          </button>
        );
      })}
    </nav>
  );
}
