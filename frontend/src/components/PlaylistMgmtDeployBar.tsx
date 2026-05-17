// Multi-deploy bar for Playlist Management. Cartesian fan-out:
// every checked source playlist is copied to every checked destination
// user. N source playlists × K dest users = N×K copies fired serially.
//
// 2026-05-16 (end user request): the prior "destination user" dropdown
// next to Deploy was removed — the end user had already checked dest
// users in the panel above, picking again from those same checks was
// redundant. The checked dest users ARE the targets now.
//
// The bar disables Deploy with an actionable tooltip when any gate
// fails (no playlists / no dest users / submission in flight).

import type { PlaylistMgmtUser, PlaylistSpec } from '../api';

export interface QueuedPlaylist {
  // Stable frontend identifier — keyed off userKey() in the parent.
  // Used for queue Map keying + display only. NOT sent to the backend.
  sourceUserId: string;
  // Backend-resolvable user id (backend_user_id || username). Sent as
  // the `source_user_id` field on the copy POST.
  sourceApiUserId: string;
  sourceUsername: string;
  playlist: PlaylistSpec;
}

// 2026-05-17 (end user request, Option A): the prior inline progress
// state was removed because each Deploy now submits N jobs to the
// queue; the ActiveDeploysPanel below renders live progress + result
// rows for each job (and persists across page reload). The bar's
// "Deploying…" state is just the brief window while we POST every
// submission — typically sub-second.

interface Props {
  queued: QueuedPlaylist[];
  checkedDestUsers: PlaylistMgmtUser[];
  submitting: boolean;
  onDeploy: () => void;
}

export function PlaylistMgmtDeployBar({
  queued,
  checkedDestUsers,
  submitting,
  onDeploy,
}: Props) {
  const totalCopies = queued.length * checkedDestUsers.length;

  const blocker =
    queued.length === 0
      ? 'Check at least one source playlist above.'
    : checkedDestUsers.length === 0
      ? 'Check at least one destination user above.'
    : submitting
      ? 'Copies are already in flight; wait for the batch to complete.'
    : undefined;

  const canDeploy = !blocker;

  return (
    <div className="panel" style={{ marginTop: 12 }}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12, flexWrap: 'wrap' }}>
        <div style={{ flex: 1, fontSize: 13 }}>
          <strong>{queued.length}</strong> source playlist{queued.length === 1 ? '' : 's'}{' '}
          × <strong>{checkedDestUsers.length}</strong> destination user{checkedDestUsers.length === 1 ? '' : 's'}{' '}
          = <strong>{totalCopies}</strong> cop{totalCopies === 1 ? 'y' : 'ies'}
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
          type="button"
          className="primary"
          disabled={!canDeploy}
          title={blocker}
          onClick={onDeploy}
        >
          {submitting
            ? 'Deploying…'
            : totalCopies === 0
              ? 'Deploy copies'
              : `Deploy ${totalCopies} cop${totalCopies === 1 ? 'y' : 'ies'}`}
        </button>
      </div>

      {submitting && (
        <div style={{ marginTop: 10, fontSize: 11, color: 'var(--text-dim)' }}>
          Queuing copies…
        </div>
      )}
    </div>
  );
}
