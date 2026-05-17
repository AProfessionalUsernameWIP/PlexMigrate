// Phase C (admin-management follow-up, 2026-05-15): per-library
// metric matrix.
//
// Replaces the four global include_* checkboxes (Watch history,
// Ratings, Playlists, Collections) with a per-library grid: one row
// per selected library, four checkboxes per row. The end user can
// have one library capture only watch history while another captures
// everything else, etc. The engine consults this map per library at
// gather time.
//
// Shape: { [library_name]: { watch_history, ratings, playlists,
// collections } }. ``undefined`` value for a library is treated as
// "default everything on" by the engine helper; the matrix renders
// missing entries with checked boxes so the end user can see the
// default at a glance.

import { useMemo } from 'react';

export interface LibraryMetricsRow {
  watch_history: boolean;
  ratings: boolean;
  playlists: boolean;
  collections: boolean;
}
export type LibraryMetricsMap = Record<string, LibraryMetricsRow>;

const METRICS: { key: keyof LibraryMetricsRow; label: string }[] = [
  { key: 'watch_history', label: 'Watch history' },
  { key: 'ratings', label: 'Ratings' },
  { key: 'playlists', label: 'Playlists' },
  { key: 'collections', label: 'Collections' },
];

interface Props {
  // The libraries the end user currently has selected. Used to drive
  // which rows render; libraries not in this list are not editable.
  selectedLibraries: string[];
  // Current per-library map. ``null`` or ``undefined`` is treated as
  // "all metrics on for every library" - the form passes ``null``
  // on first render, the end user's first checkbox click materialises
  // an entry.
  value: LibraryMetricsMap | null | undefined;
  onChange: (next: LibraryMetricsMap) => void;
  // 2026-05-16: optional snapshot-gating set. Metrics in this set are
  // not available in the upstream data source (e.g. restoring from a
  // snapshot whose ``captured_types`` did not include ratings) and
  // render as visually-disabled checkboxes forced to false. The header
  // "all" checkbox + row "all/none" button skip these columns when
  // computing aggregate state. Defaults to an empty set; surfaces
  // that don't have snapshot gating (snapshot / direct modes) pass
  // nothing and see today's behaviour.
  unavailableMetrics?: ReadonlySet<keyof LibraryMetricsRow>;
  // Optional copy override for the column-header tooltip on
  // unavailable metrics. End user-facing language varies by surface
  // (restore-from-snapshot vs future restore-from-file).
  unavailableTooltip?: string;
}

const ALL_ON: LibraryMetricsRow = {
  watch_history: true,
  ratings: true,
  playlists: true,
  collections: true,
};

// Stable empty-set sentinel used when the caller omits unavailableMetrics.
// Using a singleton keeps useMemo's dep array stable across renders.
const EMPTY_UNAVAILABLE: ReadonlySet<keyof LibraryMetricsRow> = new Set();

export function LibraryMetricsMatrix({
  selectedLibraries,
  value,
  onChange,
  unavailableMetrics,
  unavailableTooltip,
}: Props) {
  const unavailable = unavailableMetrics ?? EMPTY_UNAVAILABLE;
  const tooltipForUnavailable =
    unavailableTooltip
    || 'Not available in the selected data source. Forced off for this run.';
  // Build a row-per-library view, defaulting missing libraries to
  // "all on" so the matrix never renders a row of unchecked boxes
  // unless the end user explicitly unchecked them. Unavailable metrics
  // are force-cleared in every row so the rendered state never
  // contradicts the gating: even if libraryMetrics has stale
  // ratings=true entries from a previous selection, the matrix shows
  // them off (and onChange below cannot write them back on).
  const rows = useMemo(() => {
    const m: LibraryMetricsMap = {};
    for (const lib of selectedLibraries) {
      const base = value && value[lib] ? value[lib] : { ...ALL_ON };
      const row: LibraryMetricsRow = { ...base };
      for (const m_key of METRICS) {
        if (unavailable.has(m_key.key)) {
          row[m_key.key] = false;
        }
      }
      m[lib] = row;
    }
    return m;
  }, [selectedLibraries, value, unavailable]);

  const setCell = (lib: string, key: keyof LibraryMetricsRow, checked: boolean) => {
    // Defensive: ignore writes against an unavailable metric. The
    // checkbox is rendered disabled below; this guard is belt + braces.
    if (unavailable.has(key)) return;
    const next: LibraryMetricsMap = { ...(value || {}) };
    const row = next[lib] ? { ...next[lib] } : { ...ALL_ON };
    row[key] = checked;
    // Re-clear every unavailable metric so a stale value can never
    // round-trip back through setCell on an adjacent metric write.
    for (const u of unavailable) row[u] = false;
    next[lib] = row;
    onChange(next);
  };

  const setAllForLibrary = (lib: string, checked: boolean) => {
    const next: LibraryMetricsMap = { ...(value || {}) };
    next[lib] = {
      watch_history: checked && !unavailable.has('watch_history'),
      ratings: checked && !unavailable.has('ratings'),
      playlists: checked && !unavailable.has('playlists'),
      collections: checked && !unavailable.has('collections'),
    };
    onChange(next);
  };

  const setAllForMetric = (key: keyof LibraryMetricsRow, checked: boolean) => {
    if (unavailable.has(key)) return;
    const next: LibraryMetricsMap = { ...(value || {}) };
    for (const lib of selectedLibraries) {
      const row = next[lib] ? { ...next[lib] } : { ...ALL_ON };
      row[key] = checked;
      for (const u of unavailable) row[u] = false;
      next[lib] = row;
    }
    onChange(next);
  };

  if (selectedLibraries.length === 0) {
    return (
      <fieldset className="field" style={{ borderRadius: 8, padding: '10px 12px' }}>
        <legend style={{ padding: '0 6px', fontWeight: 600 }}>Per-library metrics</legend>
        <span className="help" style={{ marginTop: 0 }}>
          Pick libraries above first. The metric matrix populates one row per
          selected library so you can choose which data types to capture for
          each library individually.
        </span>
      </fieldset>
    );
  }

  return (
    <fieldset className="field" style={{ borderRadius: 8, padding: '10px 12px' }}>
      <legend style={{ padding: '0 6px', fontWeight: 600 }}>Per-library metrics</legend>
      <span className="help" style={{ marginTop: 0 }}>
        Pick which data types each selected library should capture. Checkboxes
        default to everything-on (the pre-per-library behaviour) so you only
        need to UNcheck the metrics a given library should skip. The header
        row's checkboxes toggle a whole metric column on or off across every
        library at once.
      </span>
      <div style={{ overflowX: 'auto', marginTop: 8 }}>
        <table className="list" style={{ width: '100%', fontSize: 13 }}>
          <thead>
            <tr>
              <th style={{ textAlign: 'left', minWidth: 160 }}>Library</th>
              {METRICS.map((m) => {
                const colDisabled = unavailable.has(m.key);
                return (
                  <th
                    key={m.key}
                    style={{
                      textAlign: 'center',
                      opacity: colDisabled ? 0.4 : undefined,
                    }}
                    title={colDisabled ? tooltipForUnavailable : undefined}
                  >
                    <div>{m.label}</div>
                    <div style={{ fontSize: 10, fontWeight: 400, color: 'var(--text-dim)' }}>
                      <label style={{ display: 'inline-flex', alignItems: 'center', gap: 4 }}>
                        <input
                          type="checkbox"
                          checked={!colDisabled && selectedLibraries.every((lib) => rows[lib][m.key])}
                          disabled={colDisabled}
                          ref={(el) => {
                            if (!el) return;
                            if (colDisabled) {
                              el.indeterminate = false;
                              return;
                            }
                            const states = selectedLibraries.map((lib) => rows[lib][m.key]);
                            const all = states.every(Boolean);
                            const none = states.every((s) => !s);
                            el.indeterminate = !all && !none;
                          }}
                          onChange={(e) => setAllForMetric(m.key, e.target.checked)}
                        />
                        all libraries
                      </label>
                    </div>
                    {colDisabled && (
                      <div style={{ fontSize: 9, color: 'var(--warn, #d97706)', fontStyle: 'italic' }}>
                        n/a
                      </div>
                    )}
                  </th>
                );
              })}
              <th style={{ textAlign: 'center', minWidth: 80, fontSize: 11, color: 'var(--text-dim)' }}>
                Row
              </th>
            </tr>
          </thead>
          <tbody>
            {selectedLibraries.map((lib) => {
              const row = rows[lib];
              const allOn = METRICS.every((m) => row[m.key]);
              const noneOn = METRICS.every((m) => !row[m.key]);
              return (
                <tr key={lib}>
                  <td style={{ fontWeight: 600 }}>{lib}</td>
                  {METRICS.map((m) => {
                    const cellDisabled = unavailable.has(m.key);
                    return (
                      <td
                        key={m.key}
                        style={{
                          textAlign: 'center',
                          opacity: cellDisabled ? 0.4 : undefined,
                        }}
                        title={cellDisabled ? tooltipForUnavailable : undefined}
                      >
                        <input
                          type="checkbox"
                          checked={!cellDisabled && row[m.key]}
                          disabled={cellDisabled}
                          onChange={(e) => setCell(lib, m.key, e.target.checked)}
                        />
                      </td>
                    );
                  })}
                  <td style={{ textAlign: 'center', fontSize: 11 }}>
                    <button
                      type="button"
                      onClick={() => setAllForLibrary(lib, !allOn)}
                      title={allOn ? 'Uncheck every metric for this library' : 'Check every metric for this library'}
                      style={{ fontSize: 11 }}
                    >
                      {allOn ? 'none' : noneOn ? 'all' : 'all'}
                    </button>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </fieldset>
  );
}
