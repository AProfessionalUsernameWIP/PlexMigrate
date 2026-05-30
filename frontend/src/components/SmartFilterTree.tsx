// Recursive renderer for a decoded smart-playlist filter. Shared by
// the Smart Playlist mode inspector in UserPlaylistsCard and the
// migration results in SmartMigrationProgressPanel. The filter shape
// comes from services/smart_playlist.py to_dict().

import type { SmartFilterClause, SmartFilterNode } from '../api';

// Operator-facing phrasing for the raw Plex filter operator suffix.
// Mirrors services/smart_playlist.py _OPERATOR_PHRASES; kept compact
// for the inline tree (the backend also ships a full plain-language
// ``description`` string shown above the tree).
const OP_PHRASES: Record<string, string> = {
  '': 'is',
  '=': 'is',
  '!': 'is not',
  '!=': 'is not',
  '>>': 'greater than',
  '>>=': 'at least',
  '<<': 'less than',
  '<<=': 'at most',
  '<': 'before',
  '>': 'after',
};

export function clauseLine(node: SmartFilterClause): string {
  const op = OP_PHRASES[node.operator] ?? node.operator ?? 'is';
  return `${node.field} ${op} ${node.values.join(' / ')}`;
}

export function SmartFilterTree({
  node,
  depth = 0,
}: {
  node: SmartFilterNode;
  depth?: number;
}) {
  if (node.kind === 'group') {
    return (
      <div style={{ marginLeft: depth > 0 ? 14 : 0 }}>
        <div style={{ fontWeight: 600, fontSize: 11, color: 'var(--text-dim)' }}>
          {node.match === 'and' ? 'ALL of:' : 'ANY of:'}
        </div>
        {node.children.map((child, i) => (
          <SmartFilterTree key={`${depth}-${i}`} node={child} depth={depth + 1} />
        ))}
      </div>
    );
  }
  return (
    <div style={{ marginLeft: depth > 0 ? 14 : 0, fontSize: 12, padding: '1px 0' }}>
      <span>{clauseLine(node)}</span>
      <span
        className="tag"
        style={{ marginLeft: 6, fontSize: 9, textTransform: 'uppercase' }}
      >
        {node.value_kind}
      </span>
      {node.unresolved_ids.length > 0 && (
        <span style={{ color: 'var(--bad)', marginLeft: 6, fontSize: 11 }}>
          unresolved id(s): {node.unresolved_ids.join(', ')}
        </span>
      )}
    </div>
  );
}
