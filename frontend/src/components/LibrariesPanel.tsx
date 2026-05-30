// ── Libraries panel ──────────────────────────────────────────────────
//
// Library picker for snapshot / direct-transfer jobs. Empty selection
// is the engine signal for "every library on the source" (snapshot)
// or "every library present on both servers" (direct). The matrix
// panel below this one builds on the selection here.

import type { LibraryDescriptor } from '../api';
import type { Mode } from './ModeAndServersPanel';

interface Props {
  mode: Mode;
  libraries: LibraryDescriptor[];
  librariesError: string | null;
  selectedLibs: Set<string>;
  onToggleLib: (name: string) => void;
  onSelectAll: () => void;
  onClearAll: () => void;
}

export function LibrariesPanel(props: Props) {
  const { mode, libraries, librariesError, selectedLibs, onToggleLib, onSelectAll, onClearAll } = props;
  return (
    <div className="panel">
      <h2>Libraries</h2>
      {librariesError ? (
        <div className="banner error">Could not list libraries: {librariesError}.</div>
      ) : (
        <>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 6 }}>
            <span className="label">Libraries to {mode === 'snapshot' ? 'snapshot' : 'transfer'}</span>
            <div className="row-buttons">
              <button onClick={onSelectAll}>All</button>
              <button onClick={onClearAll}>None</button>
            </div>
          </div>
          <span className="help" style={{ display: 'block', color: 'var(--text-dim)', fontSize: 12, marginBottom: 6 }}>
            Empty selection = {mode === 'snapshot' ? 'every library on the source' : 'every library present on both servers'}.
            Mirrors <code>--libraries "Movies,TV Shows,Music"</code>.
          </span>
          <div className="checkbox-grid">
            {libraries.length === 0 ? (
              <div className="empty">Pick a server above to load its libraries.</div>
            ) : libraries.map((lib) => (
              <label key={lib.name} className="switch">
                <input
                  type="checkbox"
                  data-testid={`job-library-${lib.name.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '')}`}
                  checked={selectedLibs.has(lib.name)}
                  onChange={() => onToggleLib(lib.name)}
                />
                <span>{lib.name}</span>
                <span className="help">({lib.type}, {lib.count.toLocaleString()})</span>
              </label>
            ))}
          </div>
        </>
      )}
    </div>
  );
}
