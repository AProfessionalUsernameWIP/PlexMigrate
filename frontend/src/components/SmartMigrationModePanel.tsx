// Per-playlist migration-mode picker for Playlist Transfer's Smart
// Playlist mode.
//
// Each queued smart playlist gets its own mode: "Migrate filter"
// re-applies the saved filter on the destination; "Hard copy" takes
// the current matched items and transfers them as a normal static
// playlist. The "Set all" control flips every queued playlist at once
// (and becomes the default any newly-checked playlist inherits).

import type { QueuedPlaylist, SmartMigrateMode } from './PlaylistMgmtDeployBar';

interface Props {
  queued: QueuedPlaylist[];
  // Set one queued playlist's mode.
  onSetMode: (entry: QueuedPlaylist, mode: SmartMigrateMode) => void;
  // Set every queued playlist's mode (also the default for new adds).
  onSetAll: (mode: SmartMigrateMode) => void;
}

export function SmartMigrationModePanel({ queued, onSetMode, onSetAll }: Props) {
  if (queued.length === 0) return null;
  const hardCount = queued.filter((q) => q.smartMode === 'hard_copy').length;
  const filterCount = queued.length - hardCount;

  return (
    <div className="panel" style={{ marginTop: 12 }}>
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 10,
          flexWrap: 'wrap',
          marginBottom: 6,
        }}
      >
        <h3 style={{ margin: 0 }}>Migration mode</h3>
        <span style={{ color: 'var(--text-dim)', fontSize: 12 }}>
          {filterCount} filter · {hardCount} hard copy
        </span>
        <span style={{ flex: 1 }} />
        <span style={{ fontSize: 12 }}>Set all:</span>
        <button type="button" onClick={() => onSetAll('filter')}>
          Migrate filter
        </button>
        <button type="button" onClick={() => onSetAll('hard_copy')}>
          Hard copy
        </button>
      </div>
      <ul style={{ listStyle: 'none', margin: 0, padding: 0 }}>
        {queued.map((entry) => (
          <li
            key={`${entry.sourceUserId}::${entry.playlist.playlist_id}`}
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: 8,
              padding: '5px 0',
              borderTop: '1px solid var(--border, #2a2f37)',
              fontSize: 12,
            }}
          >
            <span style={{ flex: 1 }}>
              <span style={{ color: 'var(--text-dim)' }}>
                {entry.sourceUsername} ▸{' '}
              </span>
              <strong>{entry.playlist.name}</strong>
            </span>
            <select
              value={entry.smartMode ?? 'filter'}
              onChange={(e) => onSetMode(entry, e.target.value as SmartMigrateMode)}
            >
              <option value="filter">Migrate filter</option>
              <option value="hard_copy">Hard copy (current items)</option>
            </select>
          </li>
        ))}
      </ul>
      <p className="help" style={{ margin: '8px 0 0' }}>
        <strong>Migrate filter</strong> re-applies the saved filter on the
        destination: a true smart playlist on a Plex destination, a static
        snapshot on Jellyfin / Emby. <strong>Hard copy</strong> evaluates
        the filter now and transfers the current matched items as a normal
        playlist, on any destination.
      </p>
    </div>
  );
}
