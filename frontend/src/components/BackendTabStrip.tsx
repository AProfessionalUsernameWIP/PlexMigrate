// Phase A of the backend-filter UI restructure
// (see Finding[BACKEND-FILTER-AUDIT]-2026-05-16.md).
//
// Shared component that renders a top-level "Plex / Jellyfin / Emby"
// tab strip above an existing per-server selector. Surfaces that
// adopt it become two-tier:
//
//   [ Plex ] [ Jellyfin ] [ Emby ]          <- backend tier (this)
//   [ Server A ] [ Server B ] ...           <- per-server tier (caller)
//
// Behaviour rules pinned in the audit Finding:
//
// * ``hideWhenSingleBackend`` defaults true. A Plex-only install
//   never sees this strip - the existing UX stays unchanged for
//   end users who haven't added a non-Plex server.
// * Backends with zero registered servers render disabled with a
//   tooltip ("No Jellyfin servers registered. Add one under
//   Servers > Overview.").
// * The active backend is controlled state - the caller owns the
//   useState. This component is purely presentational so it can be
//   reused across surfaces that want different reset behaviour when
//   the backend changes (per-server selector reset on RecentRuntimes,
//   for example).
//
// Backend label / order is fixed: Plex first (the original / most
// common), then Jellyfin, then Emby. Matches the order the
// ``service_type`` discriminator enum uses everywhere else.

import { ServerView } from '../api';


export type BackendType = 'plex' | 'jellyfin' | 'emby';


export interface BackendTabStripProps {
  /** All registered servers across every backend. The strip
   * derives counts + enables/disables per-backend buttons from
   * this list. */
  servers: ServerView[];
  /** Currently active backend. The caller filters its own data
   * by this value. */
  activeBackend: BackendType;
  /** Called when the end user clicks a different backend tab. */
  onChange: (next: BackendType) => void;
  /** When true (default), the strip renders nothing if only one
   * backend type has registered servers - keeps the legacy
   * single-Plex UX unchanged. Set to false when you want the
   * strip visible regardless (e.g. on a settings page that's
   * explicitly about multi-backend behaviour). */
  hideWhenSingleBackend?: boolean;
  /** Margin override; defaults to a small bottom margin. */
  style?: React.CSSProperties;
}


// Stable order regardless of how the end user-installed list happens
// to enumerate. Matches the service_type CHECK constraint values.
const BACKEND_ORDER: ReadonlyArray<BackendType> = ['plex', 'jellyfin', 'emby'] as const;


const BACKEND_LABEL: Record<BackendType, string> = {
  plex: 'Plex',
  jellyfin: 'Jellyfin',
  emby: 'Emby',
};


function _backendOf(s: ServerView): BackendType {
  // Defensive: rows that predate the picker default to 'plex'.
  // The backend's CHECK constraint enforces the same fallback.
  const raw = (s as unknown as { service_type?: string }).service_type;
  if (raw === 'jellyfin' || raw === 'emby') return raw;
  return 'plex';
}


export function backendCounts(servers: ServerView[]): Record<BackendType, number> {
  const out: Record<BackendType, number> = { plex: 0, jellyfin: 0, emby: 0 };
  for (const s of servers) out[_backendOf(s)] += 1;
  return out;
}


export function serversForBackend(
  servers: ServerView[],
  backend: BackendType,
): ServerView[] {
  return servers.filter((s) => _backendOf(s) === backend);
}


export function BackendTabStrip({
  servers,
  activeBackend,
  onChange,
  hideWhenSingleBackend = true,
  style,
}: BackendTabStripProps) {
  const counts = backendCounts(servers);
  const populatedBackends = BACKEND_ORDER.filter((b) => counts[b] > 0);

  // Hide-when-single rule. Doesn't fire when zero backends are
  // populated either (empty install) - rendering an all-disabled
  // strip would be visual noise.
  if (hideWhenSingleBackend && populatedBackends.length <= 1) {
    return null;
  }

  return (
    <nav
      role="tablist"
      aria-label="Filter by backend type"
      className="tabs sub-tabs"
      style={{ marginBottom: 8, ...(style || {}) }}
    >
      {BACKEND_ORDER.map((b) => {
        const count = counts[b];
        const isActive = activeBackend === b;
        const isDisabled = count === 0;
        const baseLabel = `${BACKEND_LABEL[b]} (${count})`;
        const title = isDisabled
          ? `No ${BACKEND_LABEL[b]} servers registered. Add one under Servers > Overview.`
          : `${count} ${BACKEND_LABEL[b]} server${count === 1 ? '' : 's'}`;
        return (
          <button
            key={b}
            type="button"
            role="tab"
            aria-selected={isActive}
            disabled={isDisabled}
            title={title}
            className={isActive ? 'active' : ''}
            onClick={() => onChange(b)}
          >
            {baseLabel}
          </button>
        );
      })}
    </nav>
  );
}
