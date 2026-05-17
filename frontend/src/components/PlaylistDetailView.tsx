// Items list for the picked source playlist. Surfaces the playlist's
// titles + types + durations so the end user can verify they're
// about to copy what they think.
//
// Smart-playlist refusal: per Plan[PLAYLIST-MANAGEMENT] section 8a
// (end user decision #7) we refuse smart-playlist copies. This view
// surfaces the refusal with a clear banner; the parent's Deploy
// button is also gated on `!detail.is_smart`.

import type { PlaylistDetail } from '../api';

interface Props {
  detail: PlaylistDetail | null;
  loading: boolean;
  error: string | null;
}

function formatDuration(ms: number | null): string {
  if (ms == null) return '-';
  const totalSec = Math.floor(ms / 1000);
  const min = Math.floor(totalSec / 60);
  const sec = totalSec % 60;
  if (min < 60) return `${min}m ${sec.toString().padStart(2, '0')}s`;
  const hr = Math.floor(min / 60);
  const remMin = min % 60;
  return `${hr}h ${remMin.toString().padStart(2, '0')}m`;
}

export function PlaylistDetailView({ detail, loading, error }: Props) {
  if (loading) {
    return (
      <div className="empty" style={{ fontSize: 12 }}>
        Loading playlist items…
      </div>
    );
  }
  if (error) {
    return (
      <div className="banner error" style={{ fontSize: 12 }}>
        Could not load playlist: {error}
      </div>
    );
  }
  if (!detail) {
    return (
      <div className="empty" style={{ fontSize: 12 }}>
        Pick a playlist on the left to see its items.
      </div>
    );
  }

  return (
    <div>
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 10, marginBottom: 8 }}>
        <strong style={{ fontSize: 13 }}>{detail.name}</strong>
        <span style={{ fontSize: 11, color: 'var(--text-dim)' }}>
          {detail.items.length} item{detail.items.length === 1 ? '' : 's'}
          {detail.from_cache && ' (cached)'}
        </span>
      </div>

      {detail.is_smart && (
        <div
          className="banner"
          style={{
            background: 'rgba(217, 119, 6, 0.10)',
            border: '1px solid var(--warn, #d97706)',
            color: 'var(--warn, #d97706)',
            padding: '8px 12px',
            borderRadius: 6,
            marginBottom: 8,
            fontSize: 12,
          }}
        >
          <strong>Smart playlist.</strong> Smart-playlist criteria don't
          translate across backends (different filter languages, different
          field names). The Deploy button is disabled for this playlist.
          To copy the items as a static list, recreate the playlist as a
          regular (non-smart) playlist on the source first.
        </div>
      )}

      <div
        style={{
          maxHeight: 320,
          overflowY: 'auto',
          background: 'var(--panel-alt, #1b2233)',
          border: '1px solid var(--border, #2a3146)',
          borderRadius: 6,
          padding: 6,
        }}
      >
        <table className="list" style={{ width: '100%', fontSize: 12 }}>
          <thead>
            <tr>
              <th style={{ textAlign: 'left' }}>Title</th>
              <th style={{ textAlign: 'left', width: 80 }}>Type</th>
              <th style={{ textAlign: 'right', width: 80 }}>Duration</th>
            </tr>
          </thead>
          <tbody>
            {detail.items.length === 0 ? (
              <tr>
                <td colSpan={3} className="empty" style={{ fontSize: 12 }}>
                  Empty playlist.
                </td>
              </tr>
            ) : detail.items.map((item, i) => (
              <tr key={`${item.title}-${i}`}>
                <td>{item.title}</td>
                <td style={{ color: 'var(--text-dim)' }}>{item.type}</td>
                <td className="num" style={{ textAlign: 'right' }}>
                  {formatDuration(item.duration_ms)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
