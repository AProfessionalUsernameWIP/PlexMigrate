// Compact chip showing a (server, user) cache's age + freshness.
// Surfaces on each user row inside PlaylistColumn so the end user
// sees at a glance whether the playlist list they're looking at is
// fresh (green), aged but usable (yellow), or stale (red).
//
// Tooltips give the absolute timestamp + the end user-tunable
// thresholds for context (defaults from Plan[PLAYLIST-MANAGEMENT]
// section 8a: 30m general / 15m snapshot threshold / 12h invalidate).

import type { PlaylistCacheStatus } from '../api';

interface Props {
  status: PlaylistCacheStatus | null | undefined;
}

function formatRelative(seconds: number): string {
  if (seconds < 60) return `${Math.round(seconds)}s ago`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h ago`;
  return `${Math.round(seconds / 86400)}d ago`;
}

export function PlaylistCacheStatusChip({ status }: Props) {
  if (!status) {
    return (
      <span
        className="tag"
        style={{
          fontSize: 10,
          background: 'transparent',
          color: 'var(--text-dim)',
          border: '1px dashed var(--border, #444)',
          padding: '2px 6px',
          borderRadius: 3,
        }}
        title="No cache entry yet. Click 'Refresh' to populate."
      >
        no cache
      </span>
    );
  }
  const tone = status.is_stale
    ? { bg: 'rgba(239, 68, 68, 0.10)', fg: 'var(--bad, #ef4444)', label: 'stale' }
    : status.age_seconds > 1800
      ? { bg: 'rgba(217, 119, 6, 0.10)', fg: 'var(--warn, #d97706)', label: 'aging' }
      : { bg: 'rgba(34, 197, 94, 0.10)', fg: 'var(--success, #16a34a)', label: 'fresh' };
  const absolute = new Date(status.last_refreshed_at * 1000).toLocaleString();
  return (
    <span
      className="tag"
      style={{
        fontSize: 10,
        padding: '2px 6px',
        borderRadius: 3,
        background: tone.bg,
        color: tone.fg,
        cursor: 'help',
      }}
      title={`Last refreshed ${absolute}. ${status.playlists_count} playlists cached. State: ${tone.label}.`}
    >
      {formatRelative(status.age_seconds)}
    </span>
  );
}
