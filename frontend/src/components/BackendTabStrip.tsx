// Shared component that renders a top-level "Plex / Jellyfin / Emby"
// tab strip above an existing per-server selector. Surfaces that
// adopt it become two-tier:
//
//   [ Plex ] [ Jellyfin ] [ Emby ]          <- backend tier (this)
//   [ Server A ] [ Server B ] ...           <- per-server tier (caller)
//
// Behaviour rules:
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
import { useBackendTint } from '../contexts/BackendTintContext';


export type BackendType = 'plex' | 'jellyfin' | 'emby';


/** A non-backend mode tab rendered after the backend group, mirroring
 * Run Job's Cross-platform tab. Used by Playlist Transfer for its
 * "Smart Playlist" mode. */
export interface ExtraModeTab {
  id: string;
  label: string;
  title?: string;
  disabled?: boolean;
}

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
  /** Optional non-backend mode tabs rendered after the backend tabs,
   * visually separated. When any extra tab is supplied the strip
   * always renders (an extra mode such as Smart Playlist must stay
   * reachable on a single-backend install). */
  extraTabs?: ExtraModeTab[];
  /** Id of the lit extra tab. When set, no backend tab shows active. */
  activeExtraTab?: string | null;
  /** Called when an extra mode tab is clicked. */
  onExtraTabSelect?: (id: string) => void;
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
  extraTabs,
  activeExtraTab,
  onExtraTabSelect,
}: BackendTabStripProps) {
  const counts = backendCounts(servers);
  const populatedBackends = BACKEND_ORDER.filter((b) => counts[b] > 0);
  const hasExtraTabs = !!extraTabs && extraTabs.length > 0;

  // Surface the current backend selection to the Hearth backend-tint
  // system. When this strip is visible the user is explicitly picking
  // a backend, so we publish that choice. When the strip is hidden
  // (single-backend install) we fall through to the only populated
  // backend so the tint still tracks what they're working with.
  // Mixed-strip surfaces (e.g. Recent Runtimes with hideWhenSingleBackend
  // disabled while user has multiple backends) keep emitting the
  // active backend; "mixed" tint is reserved for surfaces that
  // explicitly visualize multiple backends at once.
  const effectiveTint = populatedBackends.length === 0
    ? null
    : (populatedBackends.length === 1 ? populatedBackends[0] : activeBackend);
  useBackendTint(effectiveTint);

  // Hide-when-single rule. Doesn't fire when zero backends are
  // populated either (empty install) - rendering an all-disabled
  // strip would be visual noise. Suppressed entirely when extra
  // mode tabs are present: those must stay reachable even on a
  // single-backend install (Smart Playlist mode is the case).
  if (hideWhenSingleBackend && populatedBackends.length <= 1 && !hasExtraTabs) {
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
        // An active extra tab (e.g. Smart Playlist) owns the strip;
        // no backend tab is lit while it is selected.
        const isActive = activeBackend === b && !activeExtraTab;
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
      {/* Extra mode tabs (e.g. Smart Playlist), separated from the
          backend group the same way Run Job offsets Cross-platform. */}
      {(extraTabs || []).map((tab, i) => {
        const isActive = activeExtraTab === tab.id;
        return (
          <button
            key={tab.id}
            type="button"
            role="tab"
            aria-selected={isActive}
            disabled={tab.disabled}
            title={tab.title}
            className={isActive ? 'active' : ''}
            onClick={() => onExtraTabSelect?.(tab.id)}
            style={i === 0
              ? { marginLeft: 12, borderLeft: '1px solid var(--border, #444)', paddingLeft: 16 }
              : undefined}
          >
            {tab.label}
          </button>
        );
      })}
    </nav>
  );
}
