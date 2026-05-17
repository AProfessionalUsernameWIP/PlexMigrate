// Per-user playlists card. One instance per checked user (source or
// destination side) on the Playlist Management surface.
//
// Source-side cards:
//   * Each playlist row has a checkbox; checking it adds to the
//     parent's multi-select deploy queue.
//   * Smart playlists are tagged + disabled (can't be copied
//     cross-backend; criteria differ).
//   * "View items" button opens the PlaylistDetailView modal.
//
// Destination-side cards:
//   * Read-only. Surfaces the existing playlists on the dest user so
//     the end user can spot name collisions before deploying.
//   * "View items" still works.
//
// Each card fetches its own playlists; on first failure we surface
// the error inline rather than crashing the whole multi-card region.

import { useEffect, useState } from 'react';
import { api, PlaylistMgmtStructuredError } from '../api';
import type { PlaylistDetail, PlaylistSpec } from '../api';
import { PlaylistDetailView } from './PlaylistDetailView';

interface Props {
  side: 'source' | 'dest';
  serverId: string;
  serverLabel: string;
  userId: string;
  username: string;
  role?: 'owner' | 'admin' | 'managed' | string;
  // Source-only: parent-owned multi-select set keyed by playlist_id.
  selectedPlaylistIds?: Set<string>;
  onTogglePlaylist?: (p: PlaylistSpec) => void;
  // Dest-only: highlight any playlist whose name collides with one of
  // the source-side selected playlists.
  collisionNames?: Set<string>;
  // Bumped after a copy completes so this card re-fetches and the
  // end user sees the new playlist row appear on the dest user.
  refreshNonce?: number;
  // Called when the backend reports that this user no longer exists
  // on the live server (DEST_USER_NOT_FOUND). Parent uses this to
  // remove the user from the relevant checked-Set so the card doesn't
  // keep re-rendering after a refresh.
  onUserNotFound?: () => void;
}

export function UserPlaylistsCard({
  side,
  serverId,
  serverLabel,
  userId,
  username,
  role,
  selectedPlaylistIds,
  onTogglePlaylist,
  collisionNames,
  refreshNonce,
  onUserNotFound,
}: Props) {
  const [playlists, setPlaylists] = useState<PlaylistSpec[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Separate "user is a ghost in our cache" state so we can render a
  // dedicated banner with a "Remove from selection" button rather than
  // a generic error. DEST_USER_NOT_FOUND happens when managed_users
  // still has a row but the live server doesn't recognise the id —
  // typically because the end user removed the user from Plex Home
  // since the last cache sync. A column-level Refresh fixes the cache.
  const [userMissing, setUserMissing] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [detailFor, setDetailFor] = useState<PlaylistSpec | null>(null);
  const [detail, setDetail] = useState<PlaylistDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState<string | null>(null);

  // Initial fetch + refetch on refresh nonce bump
  useEffect(() => {
    if (!serverId || !userId) return;
    let cancelled = false;
    setPlaylists(null);
    setError(null);
    setUserMissing(false);
    api.playlistMgmtListPlaylists(serverId, userId)
      .then((r) => { if (!cancelled) setPlaylists(r.playlists || []); })
      .catch((e) => {
        if (cancelled) return;
        if (
          e instanceof PlaylistMgmtStructuredError
          && e.code === 'DEST_USER_NOT_FOUND'
        ) {
          setUserMissing(true);
          return;
        }
        setError(String(e instanceof Error ? e.message : e));
      });
    return () => { cancelled = true; };
  }, [serverId, userId, refreshNonce]);

  const forceRefresh = async () => {
    if (!serverId || !userId) return;
    setRefreshing(true);
    setError(null);
    setUserMissing(false);
    try {
      await api.playlistMgmtRefreshCache(serverId, userId);
      const r = await api.playlistMgmtListPlaylists(serverId, userId, true);
      setPlaylists(r.playlists || []);
    } catch (e) {
      if (
        e instanceof PlaylistMgmtStructuredError
        && e.code === 'DEST_USER_NOT_FOUND'
      ) {
        setUserMissing(true);
      } else {
        setError(String(e instanceof Error ? e.message : e));
      }
    } finally {
      setRefreshing(false);
    }
  };

  const openDetail = async (p: PlaylistSpec) => {
    setDetailFor(p);
    setDetail(null);
    setDetailError(null);
    setDetailLoading(true);
    try {
      const d = await api.playlistMgmtGetDetail(serverId, userId, p.playlist_id);
      setDetail(d);
    } catch (e) {
      setDetailError(String(e instanceof Error ? e.message : e));
    } finally {
      setDetailLoading(false);
    }
  };

  const closeDetail = () => {
    setDetailFor(null);
    setDetail(null);
    setDetailError(null);
  };

  const roleLabel = role ? role.charAt(0).toUpperCase() + role.slice(1) : '';
  const roleClass = role === 'owner' ? 'started' : role === 'admin' ? 'failed' : 'phase';
  const sideLabel = side === 'source' ? 'Source' : 'Destination';

  return (
    <div className="panel" style={{ marginTop: 12 }}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 8, marginBottom: 8 }}>
        <h4 style={{ margin: 0 }}>
          <span className="tag" style={{ fontSize: 10, marginRight: 8, background: 'var(--bg-panel-alt, #1f2435)' }}>
            {sideLabel}
          </span>
          <strong>{username}</strong>
          {role && (
            <span className={`tag ${roleClass}`} style={{ fontSize: 10, marginLeft: 6 }}>
              {roleLabel}
            </span>
          )}
          <span style={{ color: 'var(--text-dim)', fontSize: 11, marginLeft: 8 }}>@ {serverLabel}</span>
        </h4>
        <button
          type="button"
          onClick={() => void forceRefresh()}
          disabled={refreshing}
          style={{ fontSize: 11 }}
          title="Force-refresh this user's playlist list. Bypasses cache."
        >
          {refreshing ? 'Refreshing…' : 'Refresh'}
        </button>
      </div>

      {userMissing ? (
        <div
          className="banner"
          style={{
            fontSize: 12,
            background: 'rgba(245, 166, 35, 0.10)',
            border: '1px solid var(--warn, #f5a623)',
            padding: 8,
            borderRadius: 4,
          }}
        >
          Could not resolve <strong>{username}</strong> on the live server.{' '}
          Most often this is a transient API issue (Plex.tv rate-limit, token
          hiccup); occasionally it means the user was removed from Plex Home
          since the last cache sync. Click the column's <strong>Refresh</strong>{' '}
          button to re-sync, then try again. If they really are gone, you can{' '}
          <button
            type="button"
            onClick={() => onUserNotFound?.()}
            style={{ fontSize: 11, marginLeft: 4 }}
            title="Untick this user above."
          >
            remove them from selection
          </button>.
        </div>
      ) : error ? (
        <div className="banner error" style={{ fontSize: 12 }}>
          Could not load playlists: {error}
        </div>
      ) : playlists === null ? (
        <div className="empty" style={{ fontSize: 12 }}>Loading playlists…</div>
      ) : (() => {
        // 2026-05-16 (operator request): smart playlists are never
        // offered up for transfer — their definition is criteria-based
        // and not portable across backends (or even across servers).
        // Filter them out on the SOURCE side entirely so they don't
        // clutter the picker; dest side keeps showing them so the
        // end user can see what's already on the target.
        const visible = side === 'source'
          ? playlists.filter((p) => !p.is_smart)
          : playlists;
        const hiddenSmartCount = side === 'source'
          ? playlists.length - visible.length
          : 0;
        if (visible.length === 0) {
          return (
            <div className="empty" style={{ fontSize: 12 }}>
              {hiddenSmartCount > 0
                ? `No transferable playlists. (${hiddenSmartCount} smart playlist${hiddenSmartCount === 1 ? '' : 's'} hidden — smart playlists can't be copied because their criteria don't port across servers.)`
                : 'No playlists found for this user.'}
            </div>
          );
        }
        return (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 4, maxHeight: 320, overflowY: 'auto' }}>
          {hiddenSmartCount > 0 && (
            <div className="empty" style={{ fontSize: 11, marginBottom: 4 }}>
              {hiddenSmartCount} smart playlist{hiddenSmartCount === 1 ? '' : 's'} hidden (not transferable).
            </div>
          )}
          {visible.map((p) => {
            const isSelected = side === 'source' && (selectedPlaylistIds?.has(p.playlist_id) ?? false);
            const isCollision = side === 'dest' && (collisionNames?.has(p.name) ?? false);
            const checkable = side === 'source';
            return (
              <div
                key={p.playlist_id}
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  gap: 8,
                  padding: '6px 10px',
                  borderRadius: 4,
                  background: isSelected
                    ? 'var(--bg-panel)'
                    : isCollision
                      ? 'var(--bg-panel-alt, rgba(245, 166, 35, 0.08))'
                      : 'transparent',
                  border: isSelected
                    ? '1px solid var(--accent, #4a7afc)'
                    : isCollision
                      ? '1px solid var(--warn, #f5a623)'
                      : '1px solid transparent',
                  opacity: checkable && p.is_smart ? 0.55 : 1,
                }}
              >
                {checkable && (
                  <input
                    type="checkbox"
                    checked={isSelected}
                    disabled={p.is_smart}
                    onChange={() => onTogglePlaylist?.(p)}
                    title={p.is_smart
                      ? 'Smart playlists cannot be copied across backends (criteria differ).'
                      : undefined}
                  />
                )}
                <span style={{ flex: 1, fontSize: 12 }}>
                  <strong>{p.name}</strong>
                  {p.is_smart && (
                    <span className="tag failed" style={{ fontSize: 10, marginLeft: 6 }}>smart</span>
                  )}
                  {isCollision && (
                    <span
                      className="tag"
                      style={{ fontSize: 10, marginLeft: 6, background: 'var(--warn, #f5a623)', color: '#000' }}
                      title="A source playlist with this name is queued for deploy. Existing dest playlist may collide."
                    >
                      name match
                    </span>
                  )}
                </span>
                <span style={{ color: 'var(--text-dim)', fontSize: 11 }}>
                  {p.item_count} item{p.item_count === 1 ? '' : 's'}
                </span>
                <button
                  type="button"
                  onClick={() => void openDetail(p)}
                  style={{ fontSize: 11, padding: '2px 6px' }}
                  title="Show the items inside this playlist."
                >
                  View items
                </button>
              </div>
            );
          })}
        </div>
        );
      })()}

      {/* Items detail modal for any playlist in this card. */}
      {detailFor && (
        <div
          role="dialog"
          aria-modal="true"
          style={{
            position: 'fixed',
            inset: 0,
            background: 'rgba(0,0,0,0.55)',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            zIndex: 1000,
          }}
          onClick={closeDetail}
        >
          <div
            className="panel"
            onClick={(e) => e.stopPropagation()}
            style={{
              maxWidth: 640,
              width: 'calc(100% - 32px)',
              maxHeight: 'calc(100vh - 32px)',
              overflowY: 'auto',
            }}
          >
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8 }}>
              <h3 style={{ margin: 0 }}>{detailFor.name}</h3>
              <button type="button" onClick={closeDetail}>Close</button>
            </div>
            <div style={{ fontSize: 12, color: 'var(--text-dim)', marginBottom: 12 }}>
              {username} @ {serverLabel}
            </div>
            <PlaylistDetailView
              detail={detail}
              loading={detailLoading}
              error={detailError}
            />
          </div>
        </div>
      )}
    </div>
  );
}
