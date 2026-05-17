// ── Server picker (v0.9.1) ─────────────────────────────────────────────
//
// Each option appears as a clickable card with a status dot, friendly
// name, URL, and (when reachable) the current ping in milliseconds.
// Unreachable servers still appear in the list but are visually
// marked: red dot, "offline" instead of milliseconds, and a dimmed
// background. Clicking selects the server.
//
// The previous build used a native ``<select>`` which doesn't allow
// rich content inside <option>. The clickable-card list is required
// by the v0.9.1 spec ("live connection status indicator next to each
// option").
//
// v0.10.0: ServerPicker now supports both single-select (the source
// selector, the snapshot-mode destination) and multi-select (the
// import/direct destination, for fan-out). Mode is chosen by the
// caller: pass ``value`` + ``onChange`` for single, ``values`` +
// ``onMultiChange`` + ``multi`` for multi. The card layout / status
// indicators are identical between the two modes; only the toggle
// behaviour and selection state differ.
//
// Selection IDENTITY (2026-05-16 bug fix): cards are keyed by the
// registry's stable server id, not the friendly name. The same
// friendly name can exist across different backends ("Jade.TV" Plex
// AND "Jade.TV" Emby); name-based selection would highlight both
// cards on click and submit the wrong server to the backend. The
// caller stores the id (or set of ids); display labels show the
// name + backend icon for the end user.

import type { PingResult, ServerView } from '../api';

export type ServerPickerSingle = {
  multi?: false;
  /** The currently-selected server's registry id, or '' for none. */
  value: string;
  /** Called with the clicked server's id. */
  onChange: (id: string) => void;
  values?: undefined;
  onMultiChange?: undefined;
};
export type ServerPickerMulti = {
  multi: true;
  /** Set of selected server ids. */
  values: Set<string>;
  /** Called with the next set of ids after a toggle. */
  onMultiChange: (next: Set<string>) => void;
  value?: undefined;
  onChange?: undefined;
};
export type ServerPickerProps = (ServerPickerSingle | ServerPickerMulti) & {
  servers: ServerView[];
  pings: Record<string, PingResult>;
  /** Disable any card whose server id is in this set (e.g. the
   * source server when picking destinations for a direct transfer). */
  excludeIds?: Set<string>;
};

export function ServerPicker(props: ServerPickerProps) {
  const { servers, pings, excludeIds } = props;
  if (servers.length === 0) {
    return (
      <div className="empty" style={{ marginTop: 4 }}>
        No registered servers. Open the <strong>Servers</strong> tab to add one.
      </div>
    );
  }
  const isSelected = (id: string): boolean => {
    if (props.multi) return props.values.has(id);
    return props.value === id;
  };
  const handleClick = (id: string): void => {
    if (props.multi) {
      const next = new Set(props.values);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      props.onMultiChange(next);
      return;
    }
    props.onChange(id);
  };
  return (
    <div className="server-picker">
      {servers.map((s) => {
        const ping = pings[s.id];
        const status = ping?.status ?? s.last_status;
        const ms = ping?.response_ms ?? s.last_response_ms ?? null;
        const isReachable = status === 'ok';
        const isExcluded = !!excludeIds?.has(s.id);
        const selected = isSelected(s.id);
        const dotClass = status === 'ok' ? 'green'
          : status === 'auth_error' || status === 'unreachable' ? 'red'
          : 'amber';
        const metaText = isExcluded
          ? '(picked as opposite)'
          : status === 'ok' && ms !== null
            ? `${ms.toFixed(0)} ms`
            : status === 'auth_error'
              ? 'auth error'
              : status === 'unreachable'
                ? 'offline'
                : '…';
        // Surface the backend type alongside the name so the end user
        // can distinguish same-named servers across backends at a
        // glance. Plex rows hide the badge (most installs are Plex-
        // only); Jellyfin / Emby rows render it.
        const backend = ((s as unknown as { service_type?: string }).service_type) || 'plex';
        const showBackendBadge = backend === 'jellyfin' || backend === 'emby';
        return (
          <button
            type="button"
            key={s.id}
            className={`server-card${selected ? ' selected' : ''}${isReachable ? '' : ' offline'}`}
            disabled={isExcluded}
            onClick={() => handleClick(s.id)}
            title={ping?.detail ?? s.last_status_detail ?? ''}
            aria-pressed={selected}
          >
            <span className={`dot ${dotClass}`} />
            <span className="server-card-body">
              <span className="server-card-name">
                {s.name}
                {showBackendBadge && (
                  <span
                    className="tag"
                    style={{
                      marginLeft: 6,
                      fontSize: 10,
                      textTransform: 'uppercase',
                    }}
                  >
                    {backend}
                  </span>
                )}
              </span>
              <span className="server-card-url">{s.url}</span>
            </span>
            <span className="server-card-meta">{metaText}</span>
          </button>
        );
      })}
    </div>
  );
}
