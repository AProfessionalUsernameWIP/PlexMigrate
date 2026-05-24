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
import type { PlaylistDetail, PlaylistSpec, SmartPlaylistPreview } from '../api';
import { PlaylistDetailView } from './PlaylistDetailView';
import { Modal } from './Modal';
import { SmartFilterTree } from './SmartFilterTree';
import { errorText } from '../utils/format';

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
  // Parent-owned set of playlist_type values to INCLUDE
  // (e.g. {'audio','video','photo'}). When undefined, no filter is
  // applied. Playlists with empty playlist_type (older cache rows)
  // always render so an older cache doesn't disappear from
  // the UI after the operator unticks a type.
  playlistTypeFilter?: Set<string>;
  // Smart Playlist mode. When
  // true the source card lists SMART playlists as selectable and
  // hides regular ones (the inverse of copy mode), and each smart row
  // gets an "Inspect filter" expander. No effect on the dest side.
  smartMode?: boolean;
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
  playlistTypeFilter,
  smartMode = false,
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

  // Smart Playlist mode: per-row decoded-filter inspector. Previews
  // are fetched lazily (one live round-trip each) only when the
  // operator expands a smart playlist row.
  const [previews, setPreviews] = useState<Map<string, SmartPlaylistPreview>>(new Map());
  const [previewLoading, setPreviewLoading] = useState<Set<string>>(new Set());
  const [previewErrors, setPreviewErrors] = useState<Map<string, string>>(new Map());
  const [expandedFilterId, setExpandedFilterId] = useState<string | null>(null);

  const loadPreview = async (playlistId: string) => {
    if (previews.has(playlistId) || previewLoading.has(playlistId)) return;
    setPreviewLoading((s) => new Set(s).add(playlistId));
    setPreviewErrors((m) => {
      const n = new Map(m);
      n.delete(playlistId);
      return n;
    });
    try {
      const p = await api.previewSmartPlaylist(serverId, playlistId);
      setPreviews((m) => new Map(m).set(playlistId, p));
    } catch (e) {
      setPreviewErrors((m) => new Map(m).set(playlistId, errorText(e)));
    } finally {
      setPreviewLoading((s) => {
        const n = new Set(s);
        n.delete(playlistId);
        return n;
      });
    }
  };

  const toggleFilter = (playlistId: string) => {
    setExpandedFilterId((cur) => (cur === playlistId ? null : playlistId));
    void loadPreview(playlistId);
  };

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
        setError(errorText(e));
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
        setError(errorText(e));
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
      // A smart playlist's item list is a live filter evaluation.
      // The per-user playlist cache stores smart playlists with an
      // EMPTY item list (list_playlists skips the slow per-smart-
      // playlist items() walk for performance), so a cached read
      // would show "Empty playlist". Force a live fetch for smart
      // playlists so the server evaluates the filter and returns the
      // real, current matching items.
      const d = await api.playlistMgmtGetDetail(
        serverId, userId, p.playlist_id, p.is_smart,
      );
      setDetail(d);
    } catch (e) {
      setDetailError(errorText(e));
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
        // Source side shows one playlist kind at a time: copy mode
        // shows regular playlists (a smart playlist's items are not
        // what gets transferred), Smart Playlist mode shows smart
        // playlists (their filter is what gets migrated). The dest
        // side always shows everything so the operator can spot name
        // collisions.
        let visible: PlaylistSpec[];
        if (side !== 'source') {
          visible = playlists;
        } else if (smartMode) {
          visible = playlists.filter((p) => p.is_smart);
        } else {
          visible = playlists.filter((p) => !p.is_smart);
        }
        // Count of the hidden OTHER kind on the source side: smart
        // playlists in copy mode, regular playlists in smart mode.
        const hiddenOtherCount = side === 'source'
          ? playlists.length - visible.length
          : 0;
        // Type filter (parent-owned). Apply BEFORE
        // grouping so the per-library summary line + group headers
        // reflect what's actually shown. A playlist with no
        // playlist_type (older cache rows) always passes - better
        // than disappearing silently when the operator unticks a type.
        let hiddenByTypeCount = 0;
        if (playlistTypeFilter) {
          const before = visible.length;
          visible = visible.filter((p) => {
            const t = (p.playlist_type || '').toLowerCase();
            if (!t) return true;
            return playlistTypeFilter.has(t);
          });
          hiddenByTypeCount = before - visible.length;
        }
        if (visible.length === 0) {
          let emptyMsg: string;
          if (hiddenByTypeCount > 0) {
            emptyMsg = `No playlists match the current type filter. (${hiddenByTypeCount} hidden by the Audio/Video/Photo checkboxes.)`;
          } else if (smartMode && side === 'source') {
            emptyMsg = hiddenOtherCount > 0
              ? `No smart playlists for this user. (${hiddenOtherCount} regular playlist${hiddenOtherCount === 1 ? '' : 's'} hidden: Smart Playlist mode shows only smart playlists.)`
              : 'No smart playlists found for this user.';
          } else if (hiddenOtherCount > 0) {
            emptyMsg = `No transferable playlists. (${hiddenOtherCount} smart playlist${hiddenOtherCount === 1 ? '' : 's'} hidden: migrate these from the Smart Playlist tab.)`;
          } else {
            emptyMsg = 'No playlists found for this user.';
          }
          return (
            <div className="empty" style={{ fontSize: 12 }}>{emptyMsg}</div>
          );
        }
        // Group rendered playlists by
        // their source library so the user can see "3 from Music,
        // 2 from Movies" instead of one undifferentiated list. Group
        // label preference order:
        //   1. primary_library_name (best — friendly library title)
        //   2. primary_library_id   (stable when name absent)
        //   3. playlist_type        (audio/video/photo, when neither
        //      library_id nor name is available — typical for J/E
        //      whose lightweight list response doesn't carry library)
        //   4. "(no library)"       (terminal fallback)
        const groupKeyFor = (p: PlaylistSpec): string => {
          if (p.primary_library_name) return p.primary_library_name;
          if (p.primary_library_id) return `lib:${p.primary_library_id}`;
          if (p.playlist_type) return `type:${p.playlist_type}`;
          return '(no library)';
        };
        const groupLabelFor = (p: PlaylistSpec, key: string): string => {
          if (p.primary_library_name) return p.primary_library_name;
          if (p.primary_library_id) return `Library ${p.primary_library_id}`;
          if (p.playlist_type) {
            const t = p.playlist_type;
            return t.charAt(0).toUpperCase() + t.slice(1) + ' playlists';
          }
          return key;
        };
        const grouped = new Map<string, { label: string; items: PlaylistSpec[] }>();
        for (const p of visible) {
          const k = groupKeyFor(p);
          const cur = grouped.get(k);
          if (cur) {
            cur.items.push(p);
          } else {
            grouped.set(k, { label: groupLabelFor(p, k), items: [p] });
          }
        }
        // Stable display order: alphabetical by group label, with the
        // "(no library)" bucket pinned last so it doesn't push real
        // libraries down the list.
        const groupEntries = Array.from(grouped.entries()).sort(([, a], [, b]) => {
          const aTerminal = a.label === '(no library)';
          const bTerminal = b.label === '(no library)';
          if (aTerminal && !bTerminal) return 1;
          if (!aTerminal && bTerminal) return -1;
          return a.label.localeCompare(b.label);
        });
        return (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 4, maxHeight: 320, overflowY: 'auto' }}>
          {hiddenOtherCount > 0 && (
            <div className="empty" style={{ fontSize: 11, marginBottom: 4 }}>
              {smartMode
                ? `${hiddenOtherCount} regular playlist${hiddenOtherCount === 1 ? '' : 's'} hidden (Smart Playlist mode).`
                : `${hiddenOtherCount} smart playlist${hiddenOtherCount === 1 ? '' : 's'} hidden (migrate from the Smart Playlist tab).`}
            </div>
          )}
          {hiddenByTypeCount > 0 && (
            <div className="empty" style={{ fontSize: 11, marginBottom: 4 }}>
              {hiddenByTypeCount} playlist{hiddenByTypeCount === 1 ? '' : 's'} hidden by the type filter.
            </div>
          )}
          {/* Operator-facing summary so they see "3 from Music, 2 from
              Movies" at a glance before scrolling. */}
          {groupEntries.length > 1 && (
            <div
              style={{
                fontSize: 11,
                color: 'var(--text-dim)',
                paddingBottom: 4,
                borderBottom: '1px solid var(--border, rgba(255,255,255,0.08))',
                marginBottom: 4,
              }}
            >
              {groupEntries
                .map(([, g]) => `${g.items.length} from ${g.label}`)
                .join(' · ')}
            </div>
          )}
          {groupEntries.map(([key, group]) => (
            <div key={key} style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
              <div
                style={{
                  fontSize: 11,
                  fontWeight: 600,
                  color: 'var(--text-dim)',
                  textTransform: 'uppercase',
                  letterSpacing: 0.5,
                  padding: '6px 10px 2px',
                }}
              >
                {group.label} ({group.items.length})
              </div>
              {group.items.map((p) => {
                const isSelected = side === 'source' && (selectedPlaylistIds?.has(p.playlist_id) ?? false);
                const isCollision = side === 'dest' && (collisionNames?.has(p.name) ?? false);
                const checkable = side === 'source';
                const showInspect = smartMode && side === 'source' && p.is_smart;
                const filterExpanded = expandedFilterId === p.playlist_id;
                const preview = previews.get(p.playlist_id);
                const previewErr = previewErrors.get(p.playlist_id);
                return (
                  <div key={p.playlist_id} data-testid={`plmgmt-playlist-${p.playlist_id}`}>
                    <div
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
                      }}
                    >
                      {checkable && (
                        <input
                          type="checkbox"
                          checked={isSelected}
                          onChange={() => onTogglePlaylist?.(p)}
                        />
                      )}
                      <span style={{ flex: 1, fontSize: 12 }}>
                        <strong>{p.name}</strong>
                        {p.is_smart && (
                          <span className="tag failed" style={{ fontSize: 10, marginLeft: 6 }}>smart</span>
                        )}
                        {/* Per-row type chip when present; helps the
                            operator distinguish video / photo playlists
                            even when several libraries share a type. */}
                        {p.playlist_type && (
                          <span
                            className="tag"
                            style={{ fontSize: 10, marginLeft: 6, opacity: 0.7 }}
                            title={`Playlist type: ${p.playlist_type}`}
                          >
                            {p.playlist_type}
                          </span>
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
                      {showInspect && (
                        <button
                          type="button"
                          onClick={() => toggleFilter(p.playlist_id)}
                          style={{ fontSize: 11, padding: '2px 6px' }}
                          title="Decode and show this smart playlist's filter."
                        >
                          {filterExpanded ? 'Hide filter' : 'Inspect filter'}
                        </button>
                      )}
                      <button
                        type="button"
                        onClick={() => void openDetail(p)}
                        style={{ fontSize: 11, padding: '2px 6px' }}
                        title="Show the items inside this playlist."
                      >
                        View items
                      </button>
                    </div>
                    {showInspect && filterExpanded && (
                      <div
                        style={{
                          margin: '4px 0 6px 28px',
                          padding: 8,
                          background: 'var(--bg-alt, rgba(127,127,127,0.08))',
                          borderRadius: 4,
                        }}
                      >
                        {previewLoading.has(p.playlist_id) && (
                          <span className="help">Decoding filter…</span>
                        )}
                        {previewErr && (
                          <div className="banner error" style={{ fontSize: 12 }}>{previewErr}</div>
                        )}
                        {preview && (
                          <>
                            <div style={{ fontSize: 12, marginBottom: 4 }}>
                              <strong>{preview.filter.description}</strong>
                            </div>
                            <div style={{ fontSize: 11, color: 'var(--text-dim)', marginBottom: 6 }}>
                              library: {preview.filter.library_name || '(unknown)'}
                              {' '}({preview.filter.library_type || '?'})
                            </div>
                            {preview.filter.root ? (
                              <SmartFilterTree node={preview.filter.root} />
                            ) : (
                              <span className="help">
                                This smart playlist has no filter clauses.
                              </span>
                            )}
                            {preview.filter.unresolved_source_ids.length > 0 && (
                              <div style={{ color: 'var(--warn)', fontSize: 11, marginTop: 6 }}>
                                {preview.filter.unresolved_source_ids.length}
                                {' '}tag id(s) on the source had no name and may
                                not migrate cleanly.
                              </div>
                            )}
                          </>
                        )}
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          ))}
        </div>
        );
      })()}

      {/* Items detail modal for any playlist in this card. */}
      {detailFor && (
        <Modal onClose={closeDetail} align="center" width={640} maxHeight="calc(100vh - 32px)">
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
        </Modal>
      )}
    </div>
  );
}
