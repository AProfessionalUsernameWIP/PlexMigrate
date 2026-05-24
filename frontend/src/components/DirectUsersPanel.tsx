// ── Direct-transfer user intersection panel ─────────
//
// Three groups computed by raw identifier match:
//   - Transferable : on both source AND destination → checkboxes,
//                    default checked. End user can uncheck to exclude.
//   - Source only  : on source, missing on destination → grayed out
//                    with a "Not on destination server" note.
//   - Destination  : the spec's informational footer about inviting
//                    users via the Servers tab. No button / action.
//
// Owner rows are never rendered here  owner data always transfers,
// independent of this filter. If neither source nor destination has
// any managed users at all, the parent component still mounts this
// panel because it serves as a confirmation that there's nothing
// per-user to filter; we render a single "No managed users on
// either side" line to make that explicit.

import type { ServerUser } from '../api';


// Collapse duplicate rows that share the same ``plex_id``.
// The owner row + a managed-user row representing the same human
// (identity-linked) end up with identical plex_ids after the
// JobFormPanel normalisation; without this dedupe both render as
// separate checkboxes. We prefer the row with ``kind='owner'`` since
// the operator is "still technically the owner" regardless of also
// having a managed-user record on a peer server, and fill in
// display_name from whichever side has one.
function dedupeByPlexId(users: ServerUser[]): ServerUser[] {
  const byId = new Map<string, ServerUser>();
  for (const u of users) {
    const key = u.plex_id;
    if (!key) {
      // Defensive: a row with empty plex_id can't be deduped against
      // anything else; keep it as-is so the parent's intersection
      // logic continues to find it on the right side.
      byId.set(`__empty_${byId.size}`, u);
      continue;
    }
    const existing = byId.get(key);
    if (!existing) {
      byId.set(key, u);
      continue;
    }
    const owner = u.kind === 'owner' ? u : (existing.kind === 'owner' ? existing : u);
    const other = owner === u ? existing : u;
    byId.set(key, {
      ...owner,
      display_name: owner.display_name || other.display_name || '',
    });
  }
  return Array.from(byId.values());
}


export function DirectUsersPanel(props: {
  // Same picker, three contexts. The mode drives copy + the
  // "source only" panel's wording (the user picker re-uses the same
  // intersection logic regardless of which side is source vs dest).
  mode?: 'direct' | 'snapshot' | 'restore';
  sourceUsers: ServerUser[];
  destUsers: ServerUser[];
  included: Set<string>;
  onToggle: (plex_id: string) => void;
  onAll: () => void;
  onNone: () => void;
  loadError: string | null;
}) {
  const { mode = 'direct', sourceUsers, destUsers, included, onToggle, onAll, onNone, loadError } = props;
  // The owner is selectable alongside managed users.
  // Intersection is by raw identifier across both kinds; unchecking
  // the owner narrows the transfer so library-level data
  // (collections + the four owner-scoped blocks) is skipped.
  //
  // Dedupe by plex_id. The snapshot's owner row + a managed-user row
  // representing the SAME human (e.g., the operator is the owner of
  // Server A and a Plex Home managed user on Server B sharing the
  // same Plex.tv account) collide on plex_id after the JobFormPanel
  // owner-normalisation. Without the dedupe both rows render as
  // separate checkboxes that toggle together (identity_map links
  // them), which is confusing because they are the same person. We
  // collapse duplicates here, preferring the owner row + filling in
  // display_name from whichever side has one. ``raw_name`` keeps the
  // owner's value (the canonical email) so the picker remains stable.
  const dedupedSource = dedupeByPlexId(sourceUsers);
  const dstIds = new Set(destUsers.map((u) => u.plex_id));
  const transferable = dedupedSource.filter((u) => dstIds.has(u.plex_id));
  const sourceOnly = dedupedSource.filter((u) => !dstIds.has(u.plex_id));

  const allEmpty = sourceUsers.length === 0 && destUsers.length === 0;

  // Mode-driven copy. The picker logic is identical; only the
  // end user-facing language changes.
  const copy: { help: string; missingLabel: string; missingTitle: string } = (() => {
    if (mode === 'snapshot') {
      return {
        help: "Pick which users' data the snapshot captures. The server owner's library-level data (collections + watch / playlists / ratings) is included only when the owner row is checked.",
        missingLabel: '',
        missingTitle: '',
      };
    }
    if (mode === 'restore') {
      return {
        help: "Pick which users' data to restore. Users present in the snapshot but not on the destination are greyed out - invite them to Plex Home on the destination to enable restore.",
        missingLabel: 'Not on destination',
        missingTitle: 'No matching account on the destination server.',
      };
    }
    return {
      help: "Pick which managed users' watch history, playlists, collections, and ratings travel with this direct transfer. The server owner's data always transfers regardless of what's checked here.",
      missingLabel: 'Not on destination server',
      missingTitle: 'Not on destination server',
    };
  })();

  return (
    <div className="panel">
      <h2>Users</h2>
      <span className="help" style={{ display: 'block', color: 'var(--text-dim)', fontSize: 12, marginBottom: 8 }}>
        {copy.help}
      </span>
      {loadError && (
        <div className="banner error" style={{ fontSize: 12, marginBottom: 8 }}>
          Could not fully load user lists: {loadError}
        </div>
      )}
      {allEmpty ? (
        <div className="empty" style={{ fontSize: 12 }}>
          No managed users on either server  the owner's data will transfer alone.
        </div>
      ) : (
        <>
          <div style={{ marginBottom: 10 }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 4 }}>
              <strong style={{ fontSize: 13 }}>Transferable ({transferable.length})</strong>
              <div className="row-buttons">
                <button onClick={onAll} disabled={transferable.length === 0}>All</button>
                <button onClick={onNone} disabled={transferable.length === 0}>None</button>
              </div>
            </div>
            {transferable.length === 0 ? (
              <div className="empty" style={{ fontSize: 12 }}>
                No users available on both servers.
              </div>
            ) : (
              <div className="checkbox-grid">
                {transferable.map((u) => (
                  <label key={u.plex_id} className="switch">
                    <input
                      type="checkbox"
                      checked={included.has(u.plex_id)}
                      onChange={() => onToggle(u.plex_id)}
                    />
                    <span>
                      {/* Prefer the operator's chosen display name
                          (set on the Servers tab) over the raw
                          identifier. Owner's raw_name is the Plex.tv
                          email and gets noisy in this list; falling
                          back to it only when no display name exists
                          keeps the UI readable. */}
                      <strong>{u.display_name || u.raw_name}</strong>{' '}
                      {/* Owner / Managed badge so it's clear the owner
                          is a selectable target with a different scope
                          than managed users. */}
                      <span
                        className={`tag ${u.kind === 'owner' ? 'started' : 'phase'}`}
                        style={{ fontSize: 10, marginLeft: 4 }}
                      >
                        {u.kind === 'owner' ? 'Owner' : 'Managed'}
                      </span>
                    </span>
                  </label>
                ))}
              </div>
            )}
          </div>
          {/* "Source only" panel renders in direct / restore modes
              when the source carries users the destination doesn't.
              In snapshot mode we treat ``destUsers === sourceUsers``
              so this block stays empty by construction. */}
          {mode !== 'snapshot' && sourceOnly.length > 0 && (
            <div style={{ marginBottom: 10 }}>
              <strong style={{ fontSize: 13 }}>
                {mode === 'restore' ? 'Snapshot only' : 'Source only'} ({sourceOnly.length})
              </strong>
              <div className="checkbox-grid" style={{ opacity: 0.55 }}>
                {sourceOnly.map((u) => (
                  <label key={u.plex_id} className="switch" title={copy.missingTitle}>
                    <input type="checkbox" checked={false} disabled />
                    <span>
                      {/* Prefer display_name same as the transferable list. */}
                      <strong>{u.display_name || u.raw_name}</strong>{' '}
                      <span style={{ color: 'var(--text-dim)', fontSize: 12 }}>
                        - {copy.missingLabel}
                      </span>
                    </span>
                  </label>
                ))}
              </div>
            </div>
          )}
          {mode !== 'snapshot' && (
            <div style={{ marginTop: 6, fontSize: 12, color: 'var(--text-dim)' }}>
              Users not on the destination can be invited via the Servers tab.
            </div>
          )}
        </>
      )}
    </div>
  );
}
