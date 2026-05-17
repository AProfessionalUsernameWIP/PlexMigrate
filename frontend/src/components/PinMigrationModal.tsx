// Item 3 (admin-management plan, 2026-05-15): cross-server PIN migration modal.
//
// Fires when an Add-Server or Refresh-server flow surfaces one or more
// managed users whose Plex Home PIN is stored on a DIFFERENT registered
// server. The end user can pick which to migrate (default all checked)
// and confirm. The actual write requires sudo-style elevation - the
// modal awaits the ElevationContext, which opens its own modal if a
// fresh password re-confirm is needed.

import { useState } from 'react';
import { api, PinMigrationSuggestion } from '../api';
import { useElevation } from '../contexts/ElevationContext';

interface Props {
  open: boolean;
  serverId: string;
  serverName: string;
  suggestions: PinMigrationSuggestion[];
  onCancel: () => void;
  // Called after the apply completes (success or partial). Parent can
  // use this to refresh the user list and dismiss the modal.
  onApplied: (result: { applied: string[]; skipped: string[]; errors: string[] }) => void;
}

export function PinMigrationModal({
  open,
  serverId,
  serverName,
  suggestions,
  onCancel,
  onApplied,
}: Props) {
  // Build the "selected" map keyed by suggestion identity. Default
  // all checked because the end user is the one who triggered the
  // prompt by adding / refreshing the server.
  const [selected, setSelected] = useState<Set<string>>(() => {
    const out = new Set<string>();
    for (const s of suggestions) out.add(rowKey(s));
    return out;
  });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const elevation = useElevation();

  if (!open) return null;
  if (suggestions.length === 0) {
    // Defensive: parent should not open the modal with no suggestions,
    // but if it does, render a friendly "nothing to do" state.
    return (
      <div onClick={onCancel} style={overlayStyle}>
        <div onClick={(e) => e.stopPropagation()} style={panelStyle}>
          <h2 style={{ margin: 0 }}>No PIN migrations available</h2>
          <p style={{ fontSize: 13, color: 'var(--text-dim)' }}>
            No managed users on <strong>{serverName}</strong> have a matching
            PIN stored on another server.
          </p>
          <div style={{ display: 'flex', justifyContent: 'flex-end' }}>
            <button onClick={onCancel}>Close</button>
          </div>
        </div>
      </div>
    );
  }

  const toggleRow = (s: PinMigrationSuggestion) => {
    const k = rowKey(s);
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(k)) next.delete(k); else next.add(k);
      return next;
    });
  };

  const setAll = (checked: boolean) => {
    setSelected(() => {
      if (!checked) return new Set();
      const next = new Set<string>();
      for (const s of suggestions) next.add(rowKey(s));
      return next;
    });
  };

  const confirmedRows = (): PinMigrationSuggestion[] =>
    suggestions.filter((s) => selected.has(rowKey(s)));

  const submit = async () => {
    setError(null);
    const rows = confirmedRows();
    if (rows.length === 0) return;
    setBusy(true);
    try {
      // Require elevation first (sudo-style). If the end user's
      // session is already elevated, this resolves immediately.
      const elevated = await elevation.requireElevation(
        `migrate ${rows.length} PIN(s) to ${serverName}`,
      );
      if (!elevated) {
        setBusy(false);
        return;
      }
      const result = await api.pinMigrationApply(serverId, rows);
      onApplied(result);
    } catch (err) {
      setError(String(err).replace(/^Error: /, ''));
    } finally {
      setBusy(false);
    }
  };

  const allChecked = selected.size === suggestions.length;
  const someChecked = selected.size > 0 && !allChecked;

  return (
    <div onClick={onCancel} style={overlayStyle}>
      <div onClick={(e) => e.stopPropagation()} style={{ ...panelStyle, maxWidth: 620 }}>
        <h2 style={{ margin: 0 }}>PIN-protected users detected</h2>
        <p style={{ fontSize: 13, color: 'var(--text-dim)', marginTop: 8, lineHeight: 1.5 }}>
          The following users on <strong>{serverName}</strong> have a Plex
          Home PIN stored on another registered server. Migrating
          copies the PIN here so PIN-scoped content is available
          without re-entering it. Confirming requires root_admin
          elevation; you will be prompted to re-enter your password if
          your session isn't already elevated.
        </p>

        {error && <div className="banner error">{error}</div>}

        <div style={{ display: 'flex', alignItems: 'center', gap: 12, margin: '12px 0 6px 0', fontSize: 12 }}>
          <label style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
            <input
              type="checkbox"
              checked={allChecked}
              ref={(el) => { if (el) el.indeterminate = someChecked; }}
              onChange={(e) => setAll(e.target.checked)}
            />
            <span>Select all</span>
          </label>
          <span style={{ color: 'var(--text-dim)' }}>
            {selected.size} of {suggestions.length} selected
          </span>
        </div>

        <ul style={{
          listStyle: 'none',
          paddingLeft: 0,
          margin: 0,
          maxHeight: 280,
          overflowY: 'auto',
          background: 'var(--panel-alt, #1b2233)',
          border: '1px solid var(--border, #2a3146)',
          borderRadius: 6,
        }}>
          {suggestions.map((s) => {
            const k = rowKey(s);
            return (
              <li key={k} style={{ padding: '8px 12px', borderTop: '1px solid var(--border, #2a3146)' }}>
                <label style={{ display: 'flex', alignItems: 'center', gap: 10, fontSize: 13 }}>
                  <input
                    type="checkbox"
                    checked={selected.has(k)}
                    onChange={() => toggleRow(s)}
                  />
                  <span style={{ flex: 1 }}>
                    <strong>{s.target_username}</strong>
                    <span style={{ color: 'var(--text-dim)', marginLeft: 8 }}>
                      from <code>{s.source_server_name || s.source_server_id}</code>
                      {s.source_username !== s.target_username && (
                        <span> (stored as <code>{s.source_username}</code>)</span>
                      )}
                    </span>
                  </span>
                  <span style={{
                    fontSize: 10,
                    padding: '2px 8px',
                    borderRadius: 999,
                    background: s.match_kind === 'machine_id' ? '#2e5a3f' : '#5a4a2e',
                    color: '#fff',
                    fontWeight: 700,
                    letterSpacing: 0.3,
                    textTransform: 'uppercase',
                  }}>
                    {s.match_kind === 'machine_id' ? 'ID match' : 'name match'}
                  </span>
                </label>
              </li>
            );
          })}
        </ul>

        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8, marginTop: 16 }}>
          <button onClick={onCancel} disabled={busy}>Cancel</button>
          <button
            className="primary"
            onClick={submit}
            disabled={busy || selected.size === 0}
          >
            {busy ? 'Migrating…' : `Migrate ${selected.size} PIN(s)`}
          </button>
        </div>
      </div>
    </div>
  );
}

function rowKey(s: PinMigrationSuggestion): string {
  return `${s.target_username}|${s.source_server_id}|${s.source_username}`;
}

const overlayStyle: React.CSSProperties = {
  position: 'fixed',
  inset: 0,
  background: 'rgba(0,0,0,0.55)',
  zIndex: 1000,
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'center',
  padding: 20,
};

const panelStyle: React.CSSProperties = {
  background: 'var(--panel, #131826)',
  border: '1px solid var(--accent, #2e7df6)',
  borderRadius: 8,
  maxWidth: 560,
  width: '100%',
  padding: 20,
  boxShadow: '0 10px 30px rgba(0,0,0,0.5)',
};
