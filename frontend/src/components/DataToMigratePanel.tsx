// ── Data to migrate panel ────────────────────────────────────────────
//
// Wraps LibraryMetricsMatrix with the form-level "what list of
// libraries drives the matrix" decision:
//   * snapshot / direct: every library on the source server. The
//     matrix is the single source of truth for both library inclusion
//     AND per-library metric flags - end users exclude a library by
//     clicking its row "none" button (zeroes its metric row). The
//     legacy ``Libraries`` checkbox panel that used to live above
//     this matrix has been removed; ``selectedLibs`` is ignored here
//     and only kept on the prop for now to avoid breaking callers.
//   * restore from snapshot: the selected snapshot's library list,
//     plus an ``unavailableMetrics`` set derived from the snapshot's
//     captured_types so the end user can't toggle a metric the
//     snapshot never captured.
//   * restore from file: no client-side library list yet; the panel
//     renders nothing and backend defaults (everything on) take over.
//
// Renders ``null`` when no library list is available. The
// atLeastOneType banner is the form-level Submit gate's visible
// counterpart.

import type { LibraryDescriptor, Snapshot } from '../api';
import { LibraryMetricsMatrix } from './LibraryMetricsMatrix';
import type { LibraryMetricsMap, LibraryMetricsRow } from './LibraryMetricsMatrix';
import type { Mode } from './ModeAndServersPanel';

interface Props {
  mode: Mode;
  libraries: LibraryDescriptor[];
  selectedLibs: Set<string>;
  // Restore-from-snapshot inputs. Run Job passes
  // ``restoreSource='snapshot'`` when the end user picked a registered
  // snapshot; SchedulesPanel can omit / hard-wire 'snapshot' since
  // schedules don't restore from arbitrary files.
  restoreSource?: 'snapshot' | 'file';
  selectedSnapshot?: Snapshot | null;
  snapshotHasWatchHistory?: boolean;
  snapshotHasRatings?: boolean;
  snapshotHasPlaylists?: boolean;
  snapshotHasCollections?: boolean;
  libraryMetrics: LibraryMetricsMap | null;
  onLibraryMetricsChange: (next: LibraryMetricsMap) => void;
  atLeastOneType: boolean;
}

export function DataToMigratePanel(props: Props) {
  const {
    mode,
    libraries,
    selectedLibs,
    restoreSource = 'snapshot',
    selectedSnapshot,
    snapshotHasWatchHistory = true,
    snapshotHasRatings = true,
    snapshotHasPlaylists = true,
    snapshotHasCollections = true,
    libraryMetrics,
    onLibraryMetricsChange,
    atLeastOneType,
  } = props;

  let matrixLibraries: string[] | null = null;
  let unavailable: Set<keyof LibraryMetricsRow> | undefined;
  let unavailableTooltip: string | undefined;

  if ((mode === 'snapshot' || mode === 'direct') && libraries.length > 0) {
    // Show every library on the source. End users exclude a library
    // by clicking its row "none" button in the matrix (zeroes the
    // row); the parent form derives the libraries[] payload field
    // from libraryMetrics (libs with at least one true flag).
    matrixLibraries = libraries.map((l) => l.name);
    // selectedLibs is intentionally ignored - see component header.
    void selectedLibs;
  } else if (
    mode === 'restore'
    && restoreSource === 'snapshot'
    && selectedSnapshot
    && (selectedSnapshot.libraries || []).length > 0
  ) {
    matrixLibraries = selectedSnapshot.libraries;
    unavailable = new Set();
    if (!snapshotHasWatchHistory) unavailable.add('watch_history');
    if (!snapshotHasRatings) unavailable.add('ratings');
    if (!snapshotHasPlaylists) unavailable.add('playlists');
    if (!snapshotHasCollections) unavailable.add('collections');
    unavailableTooltip =
      'Not captured in the selected snapshot. Cells in this column are forced off.';
  }

  if (!matrixLibraries) return null;

  const isRestoreFromSnapshot =
    mode === 'restore' && restoreSource === 'snapshot';
  return (
    <div className="panel">
      <h2>Data to migrate</h2>
      <span className="help" style={{ display: 'block', color: 'var(--text-dim)', fontSize: 12, marginBottom: 8 }}>
        {isRestoreFromSnapshot ? (
          <>
            Pick which data types each library should restore from the snapshot.
            Defaults are everything the snapshot captured. Columns greyed out
            with <em>n/a</em> aren't present in this snapshot's <code>.db</code>
            and can't be enabled. The header row toggles a whole metric column
            on or off across every library at once.
          </>
        ) : (
          <>
            Pick which data types each selected library should capture. Defaults
            are everything-on; uncheck the cells you want this run to skip. The
            header row toggles a whole metric column on or off across every
            library at once.
          </>
        )}
      </span>
      <LibraryMetricsMatrix
        selectedLibraries={matrixLibraries}
        value={libraryMetrics}
        onChange={onLibraryMetricsChange}
        unavailableMetrics={unavailable}
        unavailableTooltip={unavailableTooltip}
      />
      {!atLeastOneType && (
        <div className="banner error" style={{ marginTop: 8 }}>
          At least one metric must be checked for at least one library -
          otherwise the job has nothing to do.
        </div>
      )}
    </div>
  );
}
