// Multi-deploy bar for Playlist Management. Cartesian fan-out:
// every checked source playlist is copied to every checked destination
// user. N source playlists × K dest users = N×K copies fired serially.
//
// The checked dest users in the panel above ARE the deploy targets;
// there is no separate "destination user" dropdown next to Deploy.
//
// A ``mode`` prop adapts the
// labels for Smart Playlist mode, where the queued playlists are
// migrated (filter re-applied) rather than copied. The parent's
// deploy handler owns the actual fan-out shape.
//
// The bar disables Deploy with an actionable tooltip when any gate
// fails (no playlists / no dest users / submission in flight).

import type { PlaylistMgmtUser, PlaylistSpec } from '../api';

// Per-playlist Smart Playlist Migration mode. 'filter' re-applies the
// saved filter; 'hard_copy' transfers
// the current matched items as a normal static playlist.
export type SmartMigrateMode = 'filter' | 'hard_copy';

export interface QueuedPlaylist {
  // Stable frontend identifier — keyed off userKey() in the parent.
  // Used for queue Map keying + display only. NOT sent to the backend.
  sourceUserId: string;
  // Backend-resolvable user id (backend_user_id || username). Sent as
  // the `source_user_id` field on the copy POST.
  sourceApiUserId: string;
  sourceUsername: string;
  playlist: PlaylistSpec;
  // Smart Playlist mode only: this playlist's migration mode. Unset
  // in copy mode (the field is ignored there).
  smartMode?: SmartMigrateMode;
}

// Each Deploy submits N jobs to the queue; the ActiveDeploysPanel
// below renders live progress + result rows for each job (and
// persists across page reload). The bar's "Deploying…" state is
// just the brief window while we POST every submission - typically
// sub-second.

interface Props {
  queued: QueuedPlaylist[];
  checkedDestUsers: PlaylistMgmtUser[];
  submitting: boolean;
  onDeploy: () => void;
  // 'smart' = Smart Playlist mode: the queued (smart) playlists are
  // migrated, not copied. Changes the summary + button labels only;
  // the gate is identical (a destination user is still picked, even
  // though a Plex destination ignores it).
  mode?: 'copy' | 'smart';
}

export function PlaylistMgmtDeployBar({
  queued,
  checkedDestUsers,
  submitting,
  onDeploy,
  mode = 'copy',
}: Props) {
  const smart = mode === 'smart';
  const totalCopies = queued.length * checkedDestUsers.length;

  const blocker =
    queued.length === 0
      ? (smart
        ? 'Check at least one smart playlist above.'
        : 'Check at least one source playlist above.')
    : checkedDestUsers.length === 0
      ? 'Check at least one destination user above.'
    : submitting
      ? (smart
        ? 'A migration is already in flight; wait for it to finish.'
        : 'Copies are already in flight; wait for the batch to complete.')
    : undefined;

  const canDeploy = !blocker;

  return (
    <div className="panel" style={{ marginTop: 12 }}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12, flexWrap: 'wrap' }}>
        <div style={{ flex: 1, fontSize: 13 }}>
          {smart ? (
            <>
              <strong>{queued.length}</strong> smart playlist{queued.length === 1 ? '' : 's'}{' '}
              × <strong>{checkedDestUsers.length}</strong> destination user{checkedDestUsers.length === 1 ? '' : 's'}{' '}
              = <strong>{totalCopies}</strong> migration{totalCopies === 1 ? '' : 's'}
            </>
          ) : (
            <>
              <strong>{queued.length}</strong> source playlist{queued.length === 1 ? '' : 's'}{' '}
              × <strong>{checkedDestUsers.length}</strong> destination user{checkedDestUsers.length === 1 ? '' : 's'}{' '}
              = <strong>{totalCopies}</strong> cop{totalCopies === 1 ? 'y' : 'ies'}
            </>
          )}
          {queued.length > 0 && (
            <div style={{ color: 'var(--text-dim)', fontSize: 11, marginTop: 4 }}>
              From: {queued.map((q) => `${q.sourceUsername} ▸ ${q.playlist.name}`).slice(0, 3).join(', ')}
              {queued.length > 3 ? `, +${queued.length - 3} more` : ''}
            </div>
          )}
          {checkedDestUsers.length > 0 && (
            <div style={{ color: 'var(--text-dim)', fontSize: 11, marginTop: 2 }}>
              To: {checkedDestUsers.map((u) => u.username).slice(0, 5).join(', ')}
              {checkedDestUsers.length > 5 ? `, +${checkedDestUsers.length - 5} more` : ''}
            </div>
          )}
        </div>

        <button
          data-testid="plmgmt-deploy-button"
          type="button"
          className="primary"
          disabled={!canDeploy}
          title={blocker}
          onClick={onDeploy}
        >
          {submitting
            ? (smart ? 'Migrating…' : 'Deploying…')
            : smart
              ? (totalCopies === 0
                ? 'Migrate smart playlists'
                : `Migrate ${totalCopies} smart playlist${totalCopies === 1 ? '' : 's'}`)
              : (totalCopies === 0
                ? 'Deploy copies'
                : `Deploy ${totalCopies} cop${totalCopies === 1 ? 'y' : 'ies'}`)}
        </button>
      </div>

      {submitting && (
        <div style={{ marginTop: 10, fontSize: 11, color: 'var(--text-dim)' }}>
          {smart ? 'Submitting migration…' : 'Queuing copies…'}
        </div>
      )}
    </div>
  );
}
