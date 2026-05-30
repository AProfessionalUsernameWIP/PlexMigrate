// Server Commands - root-admin developer console.
//
// One subtab per registered server. The panel reads each server's
// mirror database (kept fresh by the background sync worker), so
// browsing never calls a media-server API. Item editing is staged by
// default; "Stage commands" off sends edits live as they are made.
//
// The item explorer borrows the Jobs panel's selection style: pick a
// user, a library, and a data category as cards, then Load - results
// appear in their own panel, with the selected item's command panel
// pinned to the top. Columns adapt to the chosen category and show
// playlist membership, rating, and which users hold data.
//
// Gated upstream: App.tsx mounts this only for root_admin when
// dev_console_enabled is on; every endpoint re-checks both.

import { useCallback, useEffect, useMemo, useState } from 'react';
import { useConfirm } from './ConfirmModal';
import {
  api,
  devConsoleWsClient,
  DevConsoleCollection,
  DevConsoleEvent,
  DevConsoleGroup,
  DevConsoleItem,
  DevConsoleItemFilters,
  DevConsoleItemsResponse,
  DevConsolePerUserState,
  DevConsolePlaylist,
  DevConsoleServer,
  DevConsoleServerDetail,
  DevConsoleSmartFilterField,
  DevConsoleStagedChange,
  DevConsoleUser,
} from '../api';
import { errorText } from '../utils/format';

const RAW_OPS: Record<string, string[]> = {
  plex: ['scrobble', 'unscrobble', 'rate', 'progress'],
  jellyfin: ['set_userdata', 'mark_played', 'delete_played'],
  emby: ['set_userdata', 'mark_played', 'delete_played'],
};
const PAGE_SIZES = [10, 20, 50];

// Data categories the explorer can view. Each maps to a value column.
const CATEGORIES: Array<{ id: string; label: string }> = [
  { id: 'watch', label: 'Watch counts' },
  { id: 'ratings', label: 'Ratings' },
  { id: 'favorites', label: 'Favorites' },
  { id: 'resume', label: 'Resume points' },
  { id: 'playlists', label: 'Playlist membership' },
];

const ALL_USERS = '*';

type GroupKind = 'artist' | 'album' | 'show' | 'season' | 'playlist';

// One row in the new-playlist basket: a single item, or a whole group
// (album / artist / show / season) kept grouped and expandable so the
// operator can see what a "+" pulled in.
interface BasketEntry {
  id: string;
  kind: 'item' | 'group';
  groupKind?: GroupKind;
  label: string;
  items: DevConsoleItem[];
}

// One entry in the "Remove from playlist" staging panel: a playlist
// and the tracks queued to be removed FROM it. The minus button in
// the By-playlist view stages a removal here rather than removing it
// live; the panel's Confirm applies them. ``libraryId`` / ``user``
// are captured at stage time so each removal runs against the right
// scope even after the operator switches library or user.
interface RemovalGroup {
  playlistId: string;
  playlistName: string;
  libraryId: string;
  user: string;
  items: DevConsoleItem[];
}

let _basketSeq = 0;
function mkEntryId(): string {
  _basketSeq += 1;
  return `e${_basketSeq}`;
}

function ago(ts: number | null | undefined): string {
  if (!ts) return 'never';
  const s = Math.max(0, Math.floor(Date.now() / 1000 - ts));
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

// ── Card selector ───────────────────────────────────────────────────────────

function SelectCard({
  label, sub, active, onClick,
}: {
  label: string; sub?: string; active: boolean; onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      style={{
        padding: '6px 12px',
        border: '1px solid ' + (active ? 'var(--accent)' : 'var(--border)'),
        background: active ? 'rgba(59,130,246,0.12)' : 'transparent',
        borderRadius: 4,
        cursor: 'pointer',
        fontWeight: active ? 600 : 400,
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'flex-start',
        minWidth: 80,
      }}
    >
      <span>{label}</span>
      {sub && <span className="muted" style={{ fontSize: 11 }}>{sub}</span>}
    </button>
  );
}

function CardRow({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div style={{ display: 'flex', gap: 8, alignItems: 'flex-start', marginTop: 8 }}>
      <span style={{ width: 70, paddingTop: 6, fontWeight: 600 }}>{label}</span>
      <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>{children}</div>
    </div>
  );
}

// ── Raw per-call API passthrough ────────────────────────────────────────────

function RawOpPanel({
  serverId, serviceType, itemId, user, onResult,
}: {
  serverId: string; serviceType: string; itemId: string;
  user: string | undefined; onResult: (msg: string, ok: boolean) => void;
}) {
  const ops = RAW_OPS[serviceType] || [];
  const [op, setOp] = useState(ops[0] || '');
  const [rating, setRating] = useState('5');
  const [timeMs, setTimeMs] = useState('0');
  const [body, setBody] = useState('{"PlayCount": 1, "Played": true}');
  const [busy, setBusy] = useState(false);

  useEffect(() => { setOp((RAW_OPS[serviceType] || [])[0] || ''); }, [serviceType]);
  // FECORE-17: reset the raw-JSON body to its default when the selected
  // item changes, so item B doesn't show item A's edited JSON.
  useEffect(() => { setBody('{"PlayCount": 1, "Played": true}'); }, [itemId]);

  const fire = useCallback(async () => {
    setBusy(true);
    try {
      let params: Record<string, unknown> = {};
      if (op === 'rate') params = { rating: parseFloat(rating) || 0 };
      else if (op === 'progress') params = { time: parseInt(timeMs, 10) || 0, state: 'stopped' };
      else if (op === 'set_userdata') {
        try { params = { body: JSON.parse(body) }; }
        catch { onResult('raw set_userdata: body is not valid JSON', false); setBusy(false); return; }
      }
      const r = await api.devConsoleRawCall(serverId, itemId, { op, user, params });
      onResult(`raw ${op}: ${JSON.stringify(r)}`, true);
    } catch (e) {
      onResult(`raw ${op}: ${errorText(e)}`, false);
    } finally { setBusy(false); }
  }, [op, rating, timeMs, body, serverId, itemId, user, onResult]);

  if (ops.length === 0) return <p className="muted">No raw ops for this backend.</p>;

  return (
    <div style={{ marginTop: 8, borderTop: '1px solid var(--border)', paddingTop: 8 }}>
      <h5 style={{ margin: '4px 0' }}>Raw API call <span className="muted">(always live)</span></h5>
      <div className="dc-cmd-row">
        <label>Op</label>
        <select value={op} onChange={(e) => setOp(e.target.value)}>
          {ops.map((o) => <option key={o} value={o}>{o}</option>)}
        </select>
        {op === 'rate' && (
          <input type="number" min={0} max={10} step={0.5} value={rating}
            onChange={(e) => setRating(e.target.value)} style={{ width: 70 }} />
        )}
        {op === 'progress' && (
          <input type="number" min={0} value={timeMs}
            onChange={(e) => setTimeMs(e.target.value)} style={{ width: 90 }} />
        )}
        <button disabled={busy} onClick={fire}>Send raw call</button>
      </div>
      {op === 'set_userdata' && (
        <div className="dc-cmd-row">
          <label>Body (JSON)</label>
          <textarea value={body} onChange={(e) => setBody(e.target.value)}
            rows={2} style={{ width: 340, fontFamily: 'monospace' }} />
        </div>
      )}
    </div>
  );
}

// ── Per-user state table (drill-in detail) ──────────────────────────────────

function PerUserTable({ rows }: { rows: DevConsolePerUserState[] }) {
  if (rows.length === 0) {
    return <p className="muted">No user has state on this item yet.</p>;
  }
  return (
    <table className="dc-table" style={{ marginTop: 4 }}>
      <thead>
        <tr><th>User</th><th>Plays</th><th>Rating</th><th>Fav</th><th>Resume</th></tr>
      </thead>
      <tbody>
        {rows.map((u) => (
          <tr key={u.username}>
            <td>{u.display_name || u.username}</td>
            <td>{u.view_count}</td>
            <td>{u.user_rating ?? '-'}</td>
            <td>{u.is_favorite ? 'yes' : '-'}</td>
            <td>{u.view_offset_ms > 0 ? `${u.view_offset_ms} ms` : '-'}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

// ── Selected-item command + detail panel (pinned to top) ────────────────────

function ItemPanel({
  serverId, serviceType, item, users, defaultUser, staging,
  playlists, collections, onResult, onClose,
}: {
  serverId: string; serviceType: string; item: DevConsoleItem;
  users: DevConsoleUser[]; defaultUser: string; staging: boolean;
  playlists: DevConsolePlaylist[]; collections: DevConsoleCollection[];
  onResult: (msg: string, ok: boolean) => void; onClose: () => void;
}) {
  // "Command as" - which user the per-item commands target. Defaults
  // to the explorer's selected user; an explicit picker means the
  // all-users view can still act on a specific user.
  const [actUser, setActUser] = useState(defaultUser === ALL_USERS ? '' : defaultUser);
  const [viewCount, setViewCount] = useState(String(item.view_count));
  const [rating, setRating] = useState(String(item.user_rating ?? 0));
  const [resumeMs, setResumeMs] = useState(String(item.view_offset_ms));
  const [busy, setBusy] = useState(false);
  const [addPl, setAddPl] = useState('');
  const [addCol, setAddCol] = useState('');

  useEffect(() => {
    setViewCount(String(item.view_count));
    setRating(String(item.user_rating ?? 0));
    setResumeMs(String(item.view_offset_ms));
  }, [item]);
  useEffect(() => {
    setActUser(defaultUser === ALL_USERS ? '' : defaultUser);
  }, [defaultUser]);

  const command = useCallback(
    async (op: string, payload: Record<string, unknown>) => {
      setBusy(true);
      try {
        const r = await api.devConsoleCommand(serverId, item.library_id, item.backend_item_id, {
          op, user: actUser || undefined, stage: staging, payload,
        });
        if (r.unsupported) onResult(`${op}: not supported by this backend`, false);
        else if (r.staged) onResult(`${op} staged for ${actUser || 'owner'}`, true);
        else onResult(`${op}: ${r.success ? 'sent live' : 'failed'} - ${r.detail}`, r.success);
      } catch (e) {
        onResult(`${op}: ${errorText(e)}`, false);
      } finally { setBusy(false); }
    },
    [serverId, item.library_id, item.backend_item_id, actUser, staging, onResult],
  );

  const member = useCallback(
    async (kind: 'playlist' | 'collection', id: string, add: boolean) => {
      setBusy(true);
      try {
        const body = add
          ? { add_item_ids: [item.backend_item_id], library_id: item.library_id, user: actUser || undefined }
          : { remove_item_ids: [item.backend_item_id], library_id: item.library_id, user: actUser || undefined };
        if (kind === 'playlist') await api.devConsolePlaylistMembers(serverId, id, body);
        else await api.devConsoleCollectionMembers(serverId, id, body);
        onResult(`${kind} membership updated (live)`, true);
      } catch (e) {
        onResult(`${kind} membership: ${errorText(e)}`, false);
      } finally { setBusy(false); }
    },
    [serverId, item.backend_item_id, item.library_id, actUser, onResult],
  );

  return (
    <div className="panel" style={{ marginBottom: 12, borderColor: 'var(--good)' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between' }}>
        <h4 style={{ marginTop: 0 }}>
          {item.show_title ? `${item.show_title} - ` : ''}
          {item.artist ? `${item.artist} - ` : ''}
          {item.title}
          <span className="muted" style={{ fontWeight: 'normal' }}> ({item.type})</span>
          {item.staged && <span style={{ color: 'var(--warn)', marginLeft: 8 }}>staged</span>}
        </h4>
        <button onClick={onClose}>Close</button>
      </div>

      <div className="dc-cmd-row">
        <label>Command as</label>
        <select value={actUser} onChange={(e) => setActUser(e.target.value)}>
          <option value="">Owner</option>
          {users.filter((u) => u.role !== 'owner').map((u) => (
            <option key={u.backend_user_id || u.username} value={u.username}>
              {u.display_name || u.username}
            </option>
          ))}
        </select>
        <span className="muted">
          {staging ? 'edits are staged until Send' : 'edits sent live'}
        </span>
      </div>

      <div className="dc-cmd-row">
        <label>Watch count</label>
        <input type="number" min={0} value={viewCount}
          onChange={(e) => setViewCount(e.target.value)} style={{ width: 80 }} />
        <button disabled={busy} onClick={() => command('set_watch',
          { view_count: Math.max(0, parseInt(viewCount, 10) || 0) })}>Set</button>
        <button disabled={busy} onClick={() => command('set_watch',
          { view_count: (parseInt(viewCount, 10) || 0) + 1 })}>+1</button>
        <button disabled={busy} onClick={() => command('set_watch', { view_count: 0 })}>Zero</button>
      </div>
      <div className="dc-cmd-row">
        <label>Rating (0-10)</label>
        <input type="number" min={0} max={10} step={0.5} value={rating}
          onChange={(e) => setRating(e.target.value)} style={{ width: 80 }} />
        <button disabled={busy} onClick={() => command('set_rating',
          { rating: Math.min(10, Math.max(0, parseFloat(rating) || 0)) })}>Set rating</button>
      </div>
      <div className="dc-cmd-row">
        <label>Resume (ms)</label>
        <input type="number" min={0} value={resumeMs}
          onChange={(e) => setResumeMs(e.target.value)} style={{ width: 110 }} />
        <button disabled={busy} onClick={() => command('set_resume',
          { offset_ms: Math.max(0, parseInt(resumeMs, 10) || 0) })}>Set resume</button>
      </div>
      <div className="dc-cmd-row">
        <label>Favorite</label>
        <button disabled={busy} onClick={() => command('set_favorite', { favorite: true })}>Mark</button>
        <button disabled={busy} onClick={() => command('set_favorite', { favorite: false })}>Clear</button>
        {serviceType === 'plex' && <span className="muted">Plex has no favorite toggle.</span>}
      </div>

      {item.per_user && (
        <div style={{ marginTop: 8, borderTop: '1px solid var(--border)', paddingTop: 8 }}>
          <h5 style={{ margin: '4px 0' }}>Per-user state</h5>
          <PerUserTable rows={item.per_user} />
        </div>
      )}

      <div style={{ marginTop: 8, borderTop: '1px solid var(--border)', paddingTop: 8 }}>
        <h5 style={{ margin: '4px 0' }}>Membership <span className="muted">(live)</span></h5>
        <div className="dc-cmd-row">
          <label>Playlist</label>
          <select value={addPl} onChange={(e) => setAddPl(e.target.value)}>
            <option value="">(pick a playlist)</option>
            {playlists.map((p) => <option key={p.playlist_id} value={p.playlist_id}>{p.name}</option>)}
          </select>
          <button disabled={busy || !addPl} onClick={() => member('playlist', addPl, true)}>Add</button>
          <button disabled={busy || !addPl} onClick={() => member('playlist', addPl, false)}>Remove</button>
        </div>
        <div className="dc-cmd-row">
          <label>Collection</label>
          <select value={addCol} onChange={(e) => setAddCol(e.target.value)}>
            <option value="">(pick a collection)</option>
            {collections.map((c) => <option key={c.collection_id} value={c.collection_id}>{c.name}</option>)}
          </select>
          <button disabled={busy || !addCol} onClick={() => member('collection', addCol, true)}>Add</button>
          <button disabled={busy || !addCol} onClick={() => member('collection', addCol, false)}>Remove</button>
        </div>
      </div>

      <RawOpPanel serverId={serverId} serviceType={serviceType}
        itemId={item.backend_item_id} user={actUser || undefined} onResult={onResult} />
    </div>
  );
}

// ── Create-playlist box ─────────────────────────────────────────────────────

// Bare clickable glyph - no button chrome, just the symbol.
const BARE_GLYPH: React.CSSProperties = {
  border: 'none', background: 'transparent', cursor: 'pointer',
  color: 'inherit', padding: '0 4px', fontWeight: 700, fontSize: 14,
};

// Group a basket entry's flat items into its mid-level sub-groups -
// albums for an artist entry, seasons for a show entry.
function basketSubGroups(
  items: DevConsoleItem[], subKind: 'album' | 'season',
): Array<{ key: string; label: string; items: DevConsoleItem[] }> {
  const map = new Map<string, { key: string; label: string; items: DevConsoleItem[] }>();
  for (const it of items) {
    const key = subKind === 'album'
      ? (it.album || '(no album)')
      : (it.season_index != null ? String(it.season_index) : '?');
    const label = subKind === 'album'
      ? (it.album || '(no album)')
      : (it.season_index != null ? `Season ${it.season_index}` : 'Season ?');
    let g = map.get(key);
    if (!g) { g = { key, label, items: [] }; map.set(key, g); }
    g.items.push(it);
  }
  return [...map.values()];
}

// A paginated leaf-track list inside a basket group entry. A big
// artist / album / playlist holds hundreds of tracks; show 10 at a
// time with prev/next rather than dumping the whole list.
function BasketLeafList({
  items, onRemove,
}: {
  items: DevConsoleItem[];
  onRemove: (itemId: string) => void;
}) {
  const [page, setPage] = useState(0);
  const PAGE = 10;
  const pageCount = Math.max(1, Math.ceil(items.length / PAGE));
  const p = Math.min(page, pageCount - 1);
  const shown = items.slice(p * PAGE, p * PAGE + PAGE);
  return (
    <>
      {shown.map((it) => (
        <div key={it.backend_item_id}
          style={{ display: 'flex', alignItems: 'center', gap: 4, marginLeft: 18 }}>
          <span>{it.title}</span>
          <button type="button" style={BARE_GLYPH} title="Remove track"
            onClick={() => onRemove(it.backend_item_id)}>-</button>
        </div>
      ))}
      {items.length > PAGE && (
        <div className="dc-pager" style={{ marginLeft: 18 }}>
          <button disabled={p <= 0} onClick={() => setPage(p - 1)}>Prev</button>
          <span className="muted">
            Page {p + 1} of {pageCount} - {items.length} item(s)
          </span>
          <button disabled={p >= pageCount - 1}
            onClick={() => setPage(p + 1)}>Next</button>
        </div>
      )}
    </>
  );
}

// One basket entry, rendered as a removable descent tree: an artist
// opens into albums, an album into tracks (show -> season -> episode
// likewise). A bare minus sits at every level so any artist, album,
// or single track can be removed on its own.
function BasketEntryRow({
  entry, openKeys, onToggle, onRemoveEntry, onRemoveItems,
}: {
  entry: BasketEntry;
  openKeys: Set<string>;
  onToggle: (key: string) => void;
  onRemoveEntry: (id: string) => void;
  onRemoveItems: (entryId: string, itemIds: string[]) => void;
}) {
  const rowStyle: React.CSSProperties = {
    display: 'flex', alignItems: 'center', gap: 4,
  };
  const minus = (onClick: () => void, title: string) => (
    <button type="button" style={BARE_GLYPH} title={title} onClick={onClick}>-</button>
  );
  const caret = (key: string) => (
    <button type="button" style={BARE_GLYPH}
      title={openKeys.has(key) ? 'Collapse' : 'Expand'}
      onClick={() => onToggle(key)}>
      {openKeys.has(key) ? '▾' : '▸'}
    </button>
  );

  if (entry.kind === 'item') {
    return (
      <div style={{ ...rowStyle, borderLeft: '2px solid var(--border)', paddingLeft: 8 }}>
        <span>{entry.items[0]?.title || entry.label}</span>
        {minus(() => onRemoveEntry(entry.id), 'Remove item')}
      </div>
    );
  }

  const open = openKeys.has(entry.id);
  const subKind: 'album' | 'season' | null =
    entry.groupKind === 'artist' ? 'album'
    : entry.groupKind === 'show' ? 'season' : null;

  return (
    <div style={{ borderLeft: '2px solid var(--border)', paddingLeft: 8 }}>
      <div style={rowStyle}>
        {caret(entry.id)}
        <span>
          <strong>{entry.groupKind}:</strong> {entry.label}{' '}
          <span className="muted">({entry.items.length} items)</span>
        </span>
        {minus(() => onRemoveEntry(entry.id), `Remove ${entry.groupKind}`)}
      </div>
      {open && subKind && basketSubGroups(entry.items, subKind).map((sg) => {
        const subKey = `${entry.id}|${sg.key}`;
        return (
          <div key={subKey} style={{ marginLeft: 18 }}>
            <div style={rowStyle}>
              {caret(subKey)}
              <span>
                <strong>{subKind}:</strong> {sg.label}{' '}
                <span className="muted">({sg.items.length})</span>
              </span>
              {minus(
                () => onRemoveItems(entry.id, sg.items.map((i) => i.backend_item_id)),
                `Remove ${subKind}`,
              )}
            </div>
            {openKeys.has(subKey) && (
              <BasketLeafList items={sg.items}
                onRemove={(id) => onRemoveItems(entry.id, [id])} />
            )}
          </div>
        );
      })}
      {open && !subKind && (
        <BasketLeafList items={entry.items}
          onRemove={(id) => onRemoveItems(entry.id, [id])} />
      )}
    </div>
  );
}

// A playlist must be created with at least one item - Plex cannot
// make an empty one. The operator builds a basket: add the selected
// item, or use "+" on a whole artist / album / show / season in the
// grouped view, then name it and Create. Whole-group adds stay
// grouped + expandable. The basket is owned by ItemExplorer.
function CreatePlaylistBox({
  serverId, libraryId, basket, selectedItem,
  onAddItem, onRemoveEntry, onRemoveItems, onClear, onResult,
}: {
  serverId: string; libraryId: string;
  basket: BasketEntry[]; selectedItem: DevConsoleItem | null;
  onAddItem: (item: DevConsoleItem) => void;
  onRemoveEntry: (id: string) => void;
  onRemoveItems: (entryId: string, itemIds: string[]) => void;
  onClear: () => void;
  onResult: (msg: string, ok: boolean) => void;
}) {
  const [name, setName] = useState('');
  const [busy, setBusy] = useState(false);
  const [openKeys, setOpenKeys] = useState<Set<string>>(new Set());

  // Distinct item ids across every basket entry - what actually gets
  // sent, so a track queued both standalone and via its album counts
  // once.
  const itemIds = useMemo(
    () => [...new Set(basket.flatMap((e) => e.items.map((i) => i.backend_item_id)))],
    [basket],
  );

  const create = useCallback(async () => {
    if (!name.trim() || itemIds.length === 0) return;
    setBusy(true);
    try {
      const r = await api.devConsoleCreatePlaylist(serverId, {
        name: name.trim(), library_id: libraryId, item_ids: itemIds,
      });
      onResult(`Created playlist "${r.name}" (${r.item_count} item(s))`, !!r.success);
      setName('');
      onClear();
    } catch (e) {
      onResult(`Create playlist: ${errorText(e)}`, false);
    } finally { setBusy(false); }
  }, [serverId, libraryId, name, itemIds, onResult, onClear]);

  const toggle = (key: string) => setOpenKeys((prev) => {
    const next = new Set(prev);
    if (next.has(key)) next.delete(key); else next.add(key);
    return next;
  });

  const alreadyIn = !!selectedItem && basket.some((e) =>
    e.items.some((i) => i.backend_item_id === selectedItem.backend_item_id));

  return (
    <div>
      <h5 style={{ margin: '2px 0 6px' }}>Create playlist</h5>
      <div className="dc-cmd-row">
        <input value={name} onChange={(e) => setName(e.target.value)}
          placeholder="playlist name" style={{ width: 180 }} />
        <button disabled={!selectedItem || alreadyIn}
          onClick={() => selectedItem && onAddItem(selectedItem)}
          title={selectedItem
            ? `Add "${selectedItem.title}" to the new playlist`
            : 'Select an item in the list first'}>
          {alreadyIn ? 'Item already added'
            : selectedItem ? `Add "${selectedItem.title}"`
            : 'Add selected item'}
        </button>
        <button disabled={busy || !name.trim() || itemIds.length === 0}
          onClick={create}>
          Create with {itemIds.length} item{itemIds.length === 1 ? '' : 's'}
        </button>
        {basket.length > 0 && (
          <button onClick={onClear} title="Empty the basket">Clear all</button>
        )}
      </div>
      {basket.length === 0 ? (
        <p className="muted" style={{ marginTop: 4 }}>
          Add items: select one in the list and "Add" it, or use the
          "+" on an artist / album / show / season row in the grouped
          view to add the whole thing.
        </p>
      ) : (
        <div style={{ marginTop: 8, display: 'flex', flexDirection: 'column', gap: 10 }}>
          {basket.map((entry) => (
            <BasketEntryRow key={entry.id} entry={entry}
              openKeys={openKeys} onToggle={toggle}
              onRemoveEntry={onRemoveEntry} onRemoveItems={onRemoveItems} />
          ))}
        </div>
      )}
    </div>
  );
}

// The "Remove from playlist" panel. The minus button on a track in
// the By-playlist view stages a removal here instead of removing it
// live; this panel groups the staged tracks under their playlist and
// applies them all on Confirm. Rendered only when something is
// staged, so it "appears" beneath Create playlist on first use.
function RemovePlaylistBox({
  serverId, removals, onRemoveItem, onRemoveGroup, onClear, onResult,
}: {
  serverId: string;
  removals: RemovalGroup[];
  onRemoveItem: (playlistId: string, itemId: string) => void;
  onRemoveGroup: (playlistId: string) => void;
  onClear: () => void;
  onResult: (msg: string, ok: boolean) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [openKeys, setOpenKeys] = useState<Set<string>>(new Set());

  const total = useMemo(
    () => removals.reduce((n, g) => n + g.items.length, 0),
    [removals],
  );

  const toggle = (key: string) => setOpenKeys((prev) => {
    const next = new Set(prev);
    if (next.has(key)) next.delete(key); else next.add(key);
    return next;
  });

  // One batched call per playlist - remove_item_ids takes the whole
  // group at once. The backend rebuilds each playlist and re-syncs;
  // the tree refreshes from the WebSocket sync event.
  const confirm = useCallback(async () => {
    setBusy(true);
    let removed = 0;
    let okGroups = 0;
    const failed: string[] = [];
    for (const g of removals) {
      try {
        await api.devConsolePlaylistMembers(serverId, g.playlistId, {
          remove_item_ids: g.items.map((i) => i.backend_item_id),
          library_id: g.libraryId,
          user: g.user === ALL_USERS ? undefined : (g.user || undefined),
        });
        removed += g.items.length;
        okGroups += 1;
      } catch (e) {
        failed.push(`"${g.playlistName}": ${errorText(e)}`);
      }
    }
    setBusy(false);
    if (removed) {
      onResult(
        `Removed ${removed} track${removed === 1 ? '' : 's'} from `
        + `${okGroups} playlist${okGroups === 1 ? '' : 's'}`,
        true,
      );
    }
    failed.forEach((m) => onResult(`Remove from ${m}`, false));
    onClear();
  }, [serverId, removals, onResult, onClear]);

  return (
    <div>
      <h5 style={{ margin: '2px 0 6px' }}>Remove from playlist</h5>
      <div className="dc-cmd-row">
        <button disabled={busy || total === 0} onClick={confirm}>
          Confirm removal of {total} track{total === 1 ? '' : 's'}
        </button>
        <button onClick={onClear} title="Discard every staged removal">
          Clear all
        </button>
      </div>
      <div style={{ marginTop: 8, display: 'flex', flexDirection: 'column', gap: 10 }}>
        {removals.map((g) => {
          const open = openKeys.has(g.playlistId);
          return (
            <div key={g.playlistId}
              style={{ borderLeft: '2px solid var(--border)', paddingLeft: 8 }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
                <button type="button" style={BARE_GLYPH}
                  title={open ? 'Collapse' : 'Expand'}
                  onClick={() => toggle(g.playlistId)}>
                  {open ? '▾' : '▸'}
                </button>
                <span>
                  <strong>playlist:</strong> {g.playlistName}{' '}
                  <span className="muted">
                    ({g.items.length} track{g.items.length === 1 ? '' : 's'})
                  </span>
                </span>
                <button type="button" style={BARE_GLYPH}
                  title={`Drop staged removals for "${g.playlistName}"`}
                  onClick={() => onRemoveGroup(g.playlistId)}>-</button>
              </div>
              {open && (
                <BasketLeafList items={g.items}
                  onRemove={(id) => onRemoveItem(g.playlistId, id)} />
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

// A smart playlist is a saved FILTER, not a fixed item list. This box
// builds one: the operator picks filter rows (field / operator /
// value), all joined with match-all or match-any. The field and
// operator lists are enumerated live from Plex so only filters the
// server actually accepts are offered. Music libraries only for now.
const SMART_LIBTYPES = ['track', 'album', 'artist'];

function CreateSmartPlaylistBox({
  serverId, libraryId, user, onResult,
}: {
  serverId: string; libraryId: string; user: string;
  onResult: (msg: string, ok: boolean) => void;
}) {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState('');
  const [libtype, setLibtype] = useState(SMART_LIBTYPES[0]);
  const [match, setMatch] = useState<'and' | 'or'>('and');
  const [rows, setRows] = useState<Array<{ id: string; field: string; op: string; value: string }>>([]);
  const [limit, setLimit] = useState('');
  const [fields, setFields] = useState<DevConsoleSmartFilterField[] | null>(null);
  const [busy, setBusy] = useState(false);

  const userArg = user === ALL_USERS ? undefined : (user || undefined);

  // The filter vocabulary is per-(library, libtype); (re)load it when
  // the box is opened or the libtype changes.
  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setFields(null);
    api.devConsoleSmartPlaylistFields(serverId, libraryId, libtype, userArg)
      .then((r) => { if (!cancelled) setFields(r.fields); })
      .catch(() => { if (!cancelled) setFields([]); });
    return () => { cancelled = true; };
  }, [open, serverId, libraryId, libtype, userArg]);

  const fieldByKey = useMemo(() => {
    const m = new Map<string, DevConsoleSmartFilterField>();
    (fields || []).forEach((f) => m.set(f.field, f));
    return m;
  }, [fields]);

  const addRow = () => {
    const f0 = (fields || [])[0];
    setRows((prev) => [...prev, {
      id: crypto.randomUUID(),
      field: f0?.field || '', op: f0?.operators[0]?.key || '', value: '',
    }]);
  };
  // FECORE-05: keyed by stable row id, not array index, so deleting a
  // row does not make later rows inherit the removed row's DOM state.
  const setRow = (id: string, patch: Partial<{ field: string; op: string; value: string }>) =>
    setRows((prev) => prev.map((r) => (r.id === id ? { ...r, ...patch } : r)));
  const removeRow = (id: string) =>
    setRows((prev) => prev.filter((r) => r.id !== id));

  const ready = rows.filter((r) => r.field.trim());

  const create = useCallback(async () => {
    if (!name.trim() || ready.length === 0) return;
    setBusy(true);
    try {
      const r = await api.devConsoleCreateSmartPlaylist(serverId, {
        name: name.trim(), library_id: libraryId, libtype, match,
        rows: ready,
        limit: limit.trim() ? parseInt(limit, 10) : undefined,
        user: userArg,
      });
      onResult(`Created smart playlist "${r.name}"`, !!r.success);
      setName(''); setRows([]); setLimit('');
    } catch (e) {
      onResult(`Create smart playlist: ${errorText(e)}`, false);
    } finally { setBusy(false); }
  }, [serverId, libraryId, libtype, match, ready, name, limit, userArg, onResult]);

  return (
    <div>
      <h5 style={{ margin: '2px 0 6px' }}>
        <button type="button" style={BARE_GLYPH}
          title={open ? 'Collapse' : 'Expand'}
          onClick={() => setOpen((v) => !v)}>
          {open ? '▾' : '▸'}
        </button>
        Create smart playlist{' '}
        <span className="muted" style={{ fontWeight: 400, fontSize: 11 }}>
          a saved filter, not a fixed list
        </span>
      </h5>
      {open && (
        <>
          <div className="dc-cmd-row">
            <input value={name} onChange={(e) => setName(e.target.value)}
              placeholder="smart playlist name" style={{ width: 180 }} />
            <label>Of
              <select value={libtype} onChange={(e) => setLibtype(e.target.value)}>
                {SMART_LIBTYPES.map((lt) => <option key={lt} value={lt}>{lt}s</option>)}
              </select>
            </label>
            <label>Match
              <select value={match}
                onChange={(e) => setMatch(e.target.value as 'and' | 'or')}>
                <option value="and">all filters</option>
                <option value="or">any filter</option>
              </select>
            </label>
            <label>Limit
              <input value={limit}
                onChange={(e) => setLimit(e.target.value.replace(/[^0-9]/g, ''))}
                placeholder="none" style={{ width: 56 }} />
            </label>
          </div>
          {fields === null ? (
            <p className="muted" style={{ marginTop: 4 }}>Loading filters...</p>
          ) : fields.length === 0 ? (
            <p className="muted" style={{ marginTop: 4 }}>
              No smart-playlist filters available for this library.
            </p>
          ) : (
            <div style={{ marginTop: 6, display: 'flex',
              flexDirection: 'column', gap: 4 }}>
              {rows.map((row) => {
                const f = fieldByKey.get(row.field);
                const ops = f && f.operators.length
                  ? f.operators : [{ key: '', title: 'is' }];
                return (
                  <div key={row.id} style={{ display: 'flex',
                    alignItems: 'center', gap: 4 }}>
                    <select value={row.field}
                      onChange={(e) => {
                        const nf = fieldByKey.get(e.target.value);
                        setRow(row.id, {
                          field: e.target.value,
                          op: nf?.operators[0]?.key || '',
                        });
                      }}>
                      {fields.map((fl) => (
                        <option key={fl.field} value={fl.field}>{fl.title}</option>
                      ))}
                    </select>
                    <select value={row.op}
                      onChange={(e) => setRow(row.id, { op: e.target.value })}>
                      {ops.map((o) => (
                        <option key={o.key} value={o.key}>{o.title}</option>
                      ))}
                    </select>
                    <input value={row.value}
                      onChange={(e) => setRow(row.id, { value: e.target.value })}
                      placeholder="value" style={{ width: 150 }} />
                    <button type="button" style={BARE_GLYPH}
                      title="Remove filter"
                      onClick={() => removeRow(row.id)}>-</button>
                  </div>
                );
              })}
              <div>
                <button type="button" onClick={addRow}>+ Add filter</button>
              </div>
            </div>
          )}
          <div className="dc-cmd-row" style={{ marginTop: 6 }}>
            <button disabled={busy || !name.trim() || ready.length === 0}
              onClick={create}>
              Create smart playlist
              {ready.length > 0 && ` (${ready.length} filter${ready.length === 1 ? '' : 's'})`}
            </button>
          </div>
        </>
      )}
    </div>
  );
}

// ── Item explorer ───────────────────────────────────────────────────────────

function perUserSummary(item: DevConsoleItem, categories: string[]): string {
  const rows = item.per_user || [];
  if (rows.length === 0) return '-';
  const cats = categories.length ? categories : ['watch'];
  const parts = rows.map((u) => {
    const who = u.display_name || u.username;
    const bits: string[] = [];
    if (cats.includes('watch') && u.view_count > 0) bits.push(`${u.view_count}p`);
    if (cats.includes('ratings') && u.user_rating != null) bits.push(`r${u.user_rating}`);
    if (cats.includes('favorites') && u.is_favorite) bits.push('fav');
    if (cats.includes('resume') && u.view_offset_ms > 0) bits.push('resume');
    return bits.length ? `${who} (${bits.join(' ')})` : who;
  });
  return parts.join(', ');
}

function itemTitle(it: DevConsoleItem): string {
  return (it.show_title ? `${it.show_title} - ` : '')
    + (it.artist ? `${it.artist} - ` : '')
    + it.title
    + (it.season_index != null && it.episode_index != null
      ? ` (S${it.season_index}E${it.episode_index})` : '');
}

// Shared item table - used by the flat list and by each grouped node.
// ``rowActions`` (optional) renders an extra trailing cell per row -
// the By-playlist view uses it for inline add / remove buttons.
function ItemTable({
  items, categories, allUsers, user, selectedId, onSelect, emptyText,
  rowActions,
}: {
  items: DevConsoleItem[]; categories: string[]; allUsers: boolean;
  user: string; selectedId: string | null;
  onSelect: (it: DevConsoleItem) => void; emptyText: string;
  rowActions?: (it: DevConsoleItem) => React.ReactNode;
}) {
  const showPlays = categories.includes('watch');
  const showFav = categories.includes('favorites');
  const showResume = categories.includes('resume');
  const colCount = 4 + (showPlays ? 1 : 0) + (showFav ? 1 : 0)
    + (showResume ? 1 : 0) + 1 + (rowActions ? 1 : 0);
  return (
    <table className="dc-table">
      <thead>
        <tr>
          <th>Title</th><th>Type</th><th>In PL</th><th>Rating</th>
          {showPlays && <th>Plays</th>}
          {showFav && <th>Fav</th>}
          {showResume && <th>Resume</th>}
          <th>{allUsers ? 'Users with data' : 'User'}</th>
          {rowActions && <th></th>}
        </tr>
      </thead>
      <tbody>
        {items.map((it) => (
          <tr key={it.backend_item_id}
            className={selectedId === it.backend_item_id ? 'active' : ''}
            onClick={() => onSelect(it)} style={{ cursor: 'pointer' }}>
            <td>{itemTitle(it)}{it.staged && <span style={{ color: 'var(--warn)' }}> *</span>}</td>
            <td>{it.type}</td>
            <td>{it.in_any_playlist ? 'yes' : '-'}</td>
            <td>{allUsers ? '-' : (it.user_rating ?? '-')}</td>
            {showPlays && <td>{allUsers ? '-' : it.view_count}</td>}
            {showFav && <td>{allUsers ? '-' : (it.is_favorite ? 'yes' : '-')}</td>}
            {showResume && <td>{allUsers ? '-' : (it.view_offset_ms > 0 ? `${it.view_offset_ms} ms` : '-')}</td>}
            <td>{allUsers ? perUserSummary(it, categories) : (user || 'owner')}</td>
            {rowActions && (
              <td onClick={(e) => e.stopPropagation()}>{rowActions(it)}</td>
            )}
          </tr>
        ))}
        {items.length === 0 && (
          <tr><td colSpan={colCount} className="muted">{emptyText}</td></tr>
        )}
      </tbody>
    </table>
  );
}

// ── Hierarchy viewer (mini media browser) ───────────────────────────────────

// Build the category / user options passed to devConsoleGroups so the
// backend prunes the tree to nodes that hold a matching leaf item.
function groupOpts(user: string, categories: string[]) {
  return {
    categories: categories.length ? categories : undefined,
    user: user === ALL_USERS ? undefined : (user || undefined),
    allUsers: user === ALL_USERS,
  };
}

// Pull every item under a hierarchy node (a whole album, artist,
// season or show) by walking the paginated item endpoint. Backs the
// "+ Playlist" action that adds an entire group to the new-playlist
// basket without drilling into each track.
async function fetchAllGroupItems(
  serverId: string, libraryId: string, base: DevConsoleItemFilters,
): Promise<DevConsoleItem[]> {
  const all: DevConsoleItem[] = [];
  let offset = 0;
  for (let guard = 0; guard < 60; guard++) {   // 60 x 50 = 3000-item ceiling
    const r = await api.devConsoleItems(
      serverId, libraryId, { ...base, offset, page_size: 50 },
    );
    all.push(...r.items);
    if (r.items.length === 0 || all.length >= r.total) break;
    offset += 50;
  }
  return all;
}

function GroupNode({
  serverId, libraryId, kind, depth, group, ancestorKey,
  user, categories, selectedId, onSelect, onAddGroup, onResult,
}: {
  serverId: string; libraryId: string; kind: 'music' | 'tv';
  depth: number; group: DevConsoleGroup;
  ancestorKey: string; user: string; categories: string[];
  selectedId: string | null; onSelect: (it: DevConsoleItem) => void;
  onAddGroup: (groupKind: GroupKind, label: string, items: DevConsoleItem[]) => void;
  onResult: (msg: string, ok: boolean) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const [children, setChildren] = useState<DevConsoleGroup[] | null>(null);
  const [items, setItems] = useState<DevConsoleItem[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [adding, setAdding] = useState(false);

  const groupKind: GroupKind = kind === 'music'
    ? (depth === 0 ? 'artist' : 'album')
    : (depth === 0 ? 'show' : 'season');

  // The item-query filter that scopes to this node's leaf items.
  const leafFilter = useCallback((): DevConsoleItemFilters => {
    const f: DevConsoleItemFilters = {};
    if (categories.length) f.categories = categories;
    if (user === ALL_USERS) f.all_users = true;
    else if (user) f.user = user;
    if (kind === 'music') {
      if (depth === 0) f.artist = group.key;
      else { f.artist = ancestorKey; f.album = group.key; }
    } else if (depth === 0) {
      f.show_title = group.key;
    } else {
      f.show_title = ancestorKey;
      if (group.key !== '') f.season_index = Number(group.key);
    }
    return f;
  }, [kind, depth, group.key, ancestorKey, user, categories]);

  const fetchChildren = useCallback(async () => {
    setLoading(true);
    try {
      if (depth === 0) {
        const sub = kind === 'music' ? 'album' : 'season';
        const r = await api.devConsoleGroups(serverId, libraryId, sub,
          group.key, groupOpts(user, categories));
        setChildren(r.groups);
      } else {
        const r = await api.devConsoleItems(serverId, libraryId,
          { ...leafFilter(), page_size: 50 });
        setItems(r.items);
      }
    } catch {
      // Leave the node empty on error.
    } finally { setLoading(false); }
  }, [serverId, libraryId, kind, depth, group.key, user, categories, leafFilter]);

  const toggle = () => {
    const next = !expanded;
    setExpanded(next);
    if (next && children === null && items === null) fetchChildren();
  };

  // Add this whole node to the new-playlist basket as one grouped,
  // expandable entry.
  const addWholeGroup = useCallback(async () => {
    setAdding(true);
    try {
      const all = await fetchAllGroupItems(serverId, libraryId, leafFilter());
      if (all.length === 0) {
        onResult(`"${group.label}" has no matching items to add`, false);
      } else {
        onAddGroup(groupKind, group.label, all);
        onResult(
          `Queued ${groupKind} "${group.label}" (${all.length} items) for the new playlist`,
          true,
        );
      }
    } catch (e) {
      onResult(`Add "${group.label}": ${errorText(e)}`, false);
    } finally { setAdding(false); }
  }, [serverId, libraryId, groupKind, group.label, leafFilter, onAddGroup, onResult]);

  return (
    <div style={{ marginLeft: depth * 18, marginTop: 2 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
        <button type="button" onClick={toggle}
          style={{
            background: 'transparent', border: 'none', cursor: 'pointer',
            color: 'inherit', padding: '2px 0', textAlign: 'left',
          }}>
          {expanded ? '▾' : '▸'} {group.label}{' '}
          <span className="muted">
            ({group.count}{depth === 0 && group.sub_count
              ? `, ${group.sub_count} ${kind === 'music' ? 'albums' : 'seasons'}`
              : ''})
          </span>
          {loading && <span className="muted"> loading...</span>}
        </button>
        <button type="button" disabled={adding} onClick={addWholeGroup}
          title="Add to playlist"
          style={{
            border: 'none', background: 'transparent', cursor: 'pointer',
            color: 'inherit', padding: '0 6px', fontWeight: 700, fontSize: 15,
          }}>
          {adding ? '···' : '+'}
        </button>
      </div>
      {expanded && depth === 0 && children && children.map((c) => (
        <GroupNode key={c.key || c.label} serverId={serverId} libraryId={libraryId}
          kind={kind} depth={1} group={c} ancestorKey={group.key}
          user={user} categories={categories}
          selectedId={selectedId} onSelect={onSelect}
          onAddGroup={onAddGroup} onResult={onResult} />
      ))}
      {expanded && depth === 1 && items && (
        <div style={{ marginLeft: 18 }}>
          <ItemTable items={items} categories={categories}
            allUsers={user === ALL_USERS} user={user}
            selectedId={selectedId} onSelect={onSelect}
            emptyText="No matching items here." />
        </div>
      )}
    </div>
  );
}

function sortGroups(groups: DevConsoleGroup[], sort: string): DevConsoleGroup[] {
  const out = groups.slice();
  if (sort === 'name_desc') out.sort((a, b) => b.label.localeCompare(a.label));
  else if (sort === 'count_desc') out.sort((a, b) => b.count - a.count);
  else if (sort === 'subcount_desc') out.sort((a, b) => b.sub_count - a.sub_count);
  else out.sort((a, b) => a.label.localeCompare(b.label));
  return out;
}

function GroupTree({
  serverId, libraryId, kind, user, categories, search, sort,
  selectedId, onSelect, onAddGroup, onResult,
}: {
  serverId: string; libraryId: string; kind: 'music' | 'tv';
  user: string; categories: string[]; search: string; sort: string;
  selectedId: string | null; onSelect: (it: DevConsoleItem) => void;
  onAddGroup: (groupKind: GroupKind, label: string, items: DevConsoleItem[]) => void;
  onResult: (msg: string, ok: boolean) => void;
}) {
  const [top, setTop] = useState<DevConsoleGroup[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setTop(null);
    setError(null);
    api.devConsoleGroups(serverId, libraryId, kind === 'music' ? 'artist' : 'show',
      undefined, groupOpts(user, categories))
      .then((r) => setTop(r.groups))
      .catch((e) => setError(errorText(e)));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [serverId, libraryId, kind, user, categories]);

  if (error) return <p className="error">{error}</p>;
  if (!top) return <p className="muted">Loading {kind === 'music' ? 'artists' : 'shows'}...</p>;
  if (top.length === 0) return <p className="muted">Nothing to browse in this library.</p>;

  const needle = search.trim().toLowerCase();
  const displayed = sortGroups(
    needle ? top.filter((g) => g.label.toLowerCase().includes(needle)) : top,
    sort,
  );
  return (
    <div className="dc-table-wrap" style={{ maxHeight: 480, overflow: 'auto' }}>
      {displayed.length === 0 && (
        <p className="muted">No {kind === 'music' ? 'artist' : 'show'} matches "{search}".</p>
      )}
      {displayed.map((g) => (
        <GroupNode key={g.key || g.label} serverId={serverId} libraryId={libraryId}
          kind={kind} depth={0} group={g} ancestorKey=""
          user={user} categories={categories}
          selectedId={selectedId} onSelect={onSelect}
          onAddGroup={onAddGroup} onResult={onResult} />
      ))}
    </div>
  );
}

// ── By-playlist viewer ──────────────────────────────────────────────────────

const PLAYLIST_PAGE = 10;

// One playlist node: shows the playlist's items that live IN the
// current library (a playlist can span libraries; the explorer is
// per-library). Expanding loads them a page at a time so a 400-item
// playlist does not dump everything at once; "+" queues the whole
// playlist regardless of the open page.
function PlaylistNode({
  serverId, libraryId, group, user, categories,
  selectedId, onSelect, onAddItem, onAddGroup, onStageRemoval, onResult,
}: {
  serverId: string; libraryId: string; group: DevConsoleGroup;
  user: string; categories: string[];
  selectedId: string | null; onSelect: (it: DevConsoleItem) => void;
  onAddItem: (item: DevConsoleItem) => void;
  onAddGroup: (groupKind: GroupKind, label: string, items: DevConsoleItem[]) => void;
  onStageRemoval: (playlistId: string, playlistName: string, item: DevConsoleItem) => void;
  onResult: (msg: string, ok: boolean) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const [resp, setResp] = useState<DevConsoleItemsResponse | null>(null);
  const [offset, setOffset] = useState(0);
  const [loading, setLoading] = useState(false);
  const [adding, setAdding] = useState(false);

  const baseFilter = useCallback((): DevConsoleItemFilters => {
    const f: DevConsoleItemFilters = { in_playlist: group.key };
    if (user === ALL_USERS) f.all_users = true;
    else if (user) f.user = user;
    return f;
  }, [group.key, user]);

  const load = useCallback(async (nextOffset: number) => {
    setLoading(true);
    try {
      const r = await api.devConsoleItems(serverId, libraryId,
        { ...baseFilter(), offset: nextOffset, page_size: PLAYLIST_PAGE });
      setResp(r);
      setOffset(r.offset);
    } catch {
      // Leave the node as-is on error.
    } finally { setLoading(false); }
  }, [serverId, libraryId, baseFilter]);

  const toggle = () => {
    const next = !expanded;
    setExpanded(next);
    if (next && resp === null) load(0);
  };

  // Inline per-track add / subtract - only the By-playlist view shows
  // these. "+" queues the track for the new playlist; "-" stages a
  // removal FROM this playlist into the Remove-from-playlist panel
  // (nothing hits the live server until that panel's Confirm).
  const rowActions = (it: DevConsoleItem) => (
    <span style={{ display: 'flex', gap: 2 }}>
      <button type="button" style={BARE_GLYPH}
        title="Add this track to the new playlist"
        onClick={() => onAddItem(it)}>+</button>
      <button type="button" style={BARE_GLYPH}
        title={`Stage "${it.title}" for removal from "${group.label}"`}
        onClick={() => onStageRemoval(group.key, group.label, it)}>-</button>
    </span>
  );

  const addWhole = useCallback(async () => {
    setAdding(true);
    try {
      const all = await fetchAllGroupItems(serverId, libraryId, baseFilter());
      if (all.length === 0) {
        onResult(`"${group.label}" has no items in this library`, false);
      } else {
        onAddGroup('playlist', group.label, all);
        onResult(
          `Queued playlist "${group.label}" (${all.length} items) for the new playlist`,
          true,
        );
      }
    } catch (e) {
      onResult(`Add "${group.label}": ${errorText(e)}`, false);
    } finally { setAdding(false); }
  }, [serverId, libraryId, group.label, baseFilter, onAddGroup, onResult]);

  const items = resp?.items ?? [];
  const total = resp?.total ?? 0;
  const pageCount = Math.max(1, Math.ceil(total / PLAYLIST_PAGE));
  const pageNum = Math.floor(offset / PLAYLIST_PAGE) + 1;

  return (
    <div style={{ marginTop: 2 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
        <button type="button" onClick={toggle}
          style={{
            background: 'transparent', border: 'none', cursor: 'pointer',
            color: 'inherit', padding: '2px 0', textAlign: 'left',
          }}>
          {expanded ? '▾' : '▸'} {group.label}{' '}
          <span className="muted">({group.count} in this library)</span>
          {loading && <span className="muted"> loading...</span>}
        </button>
        <button type="button" disabled={adding} onClick={addWhole}
          title="Add to playlist"
          style={{
            border: 'none', background: 'transparent', cursor: 'pointer',
            color: 'inherit', padding: '0 6px', fontWeight: 700, fontSize: 15,
          }}>
          {adding ? '···' : '+'}
        </button>
      </div>
      {expanded && resp && (
        <div style={{ marginLeft: 18 }}>
          <ItemTable items={items} categories={categories}
            allUsers={user === ALL_USERS} user={user}
            selectedId={selectedId} onSelect={onSelect}
            rowActions={rowActions}
            emptyText="No items from this playlist in this library." />
          {total > PLAYLIST_PAGE && (
            <div className="dc-pager">
              <button disabled={loading || offset <= 0}
                onClick={() => load(Math.max(0, offset - PLAYLIST_PAGE))}>Prev</button>
              <span className="muted">
                Page {pageNum} of {pageCount} - {total} item(s)
              </span>
              <button disabled={loading || offset + PLAYLIST_PAGE >= total}
                onClick={() => load(offset + PLAYLIST_PAGE)}>Next</button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function PlaylistTree({
  serverId, libraryId, user, categories, search, sort,
  selectedId, onSelect, onAddItem, onAddGroup, onStageRemoval, onResult,
}: {
  serverId: string; libraryId: string; user: string; categories: string[];
  search: string; sort: string;
  selectedId: string | null; onSelect: (it: DevConsoleItem) => void;
  onAddItem: (item: DevConsoleItem) => void;
  onAddGroup: (groupKind: GroupKind, label: string, items: DevConsoleItem[]) => void;
  onStageRemoval: (playlistId: string, playlistName: string, item: DevConsoleItem) => void;
  onResult: (msg: string, ok: boolean) => void;
}) {
  const [top, setTop] = useState<DevConsoleGroup[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setTop(null);
    setError(null);
    const fetchGroups = () => api.devConsoleGroups(
      serverId, libraryId, 'playlist', undefined, groupOpts(user, []));
    fetchGroups()
      .then((r) => {
        if (cancelled) return;
        setTop(r.groups);
        // A managed user's playlists are mirrored on demand. Use the
        // fast playlist-only sync (the full sync re-walks every
        // library item and is far too slow for a user switch), then
        // re-fetch so the tree fills in immediately.
        if (!r.user_synced && user && user !== ALL_USERS) {
          api.devConsoleSyncUserPlaylists(serverId, user)
            .then(() => fetchGroups())
            .then((r2) => { if (!cancelled) setTop(r2.groups); })
            .catch(() => {});
        }
      })
      .catch((e) => { if (!cancelled) setError(errorText(e)); });
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [serverId, libraryId, user]);

  if (error) return <p className="error">{error}</p>;
  if (!top) return <p className="muted">Loading playlists...</p>;
  if (top.length === 0) {
    return <p className="muted">No playlist has items in this library.</p>;
  }

  const needle = search.trim().toLowerCase();
  const displayed = sortGroups(
    needle ? top.filter((g) => g.label.toLowerCase().includes(needle)) : top,
    sort,
  );
  return (
    <div className="dc-table-wrap" style={{ maxHeight: 480, overflow: 'auto' }}>
      {displayed.length === 0 && (
        <p className="muted">No playlist matches "{search}".</p>
      )}
      {displayed.map((g) => (
        <PlaylistNode key={g.key} serverId={serverId} libraryId={libraryId}
          group={g} user={user} categories={categories}
          selectedId={selectedId} onSelect={onSelect}
          onAddItem={onAddItem} onAddGroup={onAddGroup}
          onStageRemoval={onStageRemoval} onResult={onResult} />
      ))}
    </div>
  );
}

function ItemExplorer({
  serverId, serviceType, detail, playlists, collections,
  staging, refreshKey, onResult,
}: {
  serverId: string; serviceType: string; detail: DevConsoleServerDetail;
  playlists: DevConsolePlaylist[]; collections: DevConsoleCollection[];
  staging: boolean; refreshKey: number;
  onResult: (msg: string, ok: boolean) => void;
}) {
  const [user, setUser] = useState('');               // '' = owner, username, or ALL_USERS
  const [libraryId, setLibraryId] = useState('');
  // The View categories are multi-select: each one narrows the result
  // set to items matching ANY selected category. Empty = whole library.
  const [categories, setCategories] = useState<string[]>([]);
  const [browseMode, setBrowseMode] = useState<'flat' | 'grouped' | 'playlist'>('flat');
  const [search, setSearch] = useState('');
  const [sort, setSort] = useState('name_asc');
  const [pageSize, setPageSize] = useState(20);
  const [offset, setOffset] = useState(0);
  const [resp, setResp] = useState<DevConsoleItemsResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<DevConsoleItem | null>(null);
  // New-playlist basket. Owned here (not in CreatePlaylistBox) so the
  // grouped tree's "+" buttons can push whole groups into it. Each
  // entry is either a single item or a kept-together group.
  const [basket, setBasket] = useState<BasketEntry[]>([]);
  // Staged playlist removals, grouped by playlist. Owned here so the
  // By-playlist tree's "-" buttons feed RemovePlaylistBox.
  const [removals, setRemovals] = useState<RemovalGroup[]>([]);

  const managedUsers = detail.users.filter((u) => u.role !== 'owner');
  const lib = detail.libraries.find((l) => l.library_id === libraryId) || null;
  const kind: 'music' | 'tv' | 'movie' = !lib ? 'movie'
    : lib.type === 'artist' ? 'music'
    : lib.type === 'show' ? 'tv' : 'movie';
  const allUsers = user === ALL_USERS;
  const catKey = categories.slice().sort().join(',');

  const toggleCategory = (id: string) => setCategories((prev) =>
    prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]);

  // New-playlist basket mutators. A single item is skipped if it is
  // already a standalone entry; a group is skipped if the same
  // groupKind + label is already queued.
  const addBasketItem = useCallback((item: DevConsoleItem) => {
    setBasket((prev) => {
      if (prev.some((e) => e.kind === 'item'
          && e.items[0]?.backend_item_id === item.backend_item_id)) {
        return prev;
      }
      return [...prev, {
        id: mkEntryId(), kind: 'item', label: item.title, items: [item],
      }];
    });
  }, []);
  const addBasketGroup = useCallback((
    groupKind: GroupKind, label: string, items: DevConsoleItem[],
  ) => {
    setBasket((prev) => {
      if (prev.some((e) => e.kind === 'group'
          && e.groupKind === groupKind && e.label === label)) {
        return prev;
      }
      return [...prev, {
        id: mkEntryId(), kind: 'group', groupKind, label, items,
      }];
    });
  }, []);
  const removeBasketEntry = useCallback((id: string) => {
    setBasket((prev) => prev.filter((e) => e.id !== id));
  }, []);
  // Remove specific items from inside one entry (a sub-album, a single
  // track). An entry left with no items is dropped.
  const removeBasketItems = useCallback((entryId: string, itemIds: string[]) => {
    const drop = new Set(itemIds);
    setBasket((prev) => prev
      .map((e) => e.id === entryId
        ? { ...e, items: e.items.filter((i) => !drop.has(i.backend_item_id)) }
        : e)
      .filter((e) => e.items.length > 0));
  }, []);
  const clearBasket = useCallback(() => setBasket([]), []);

  // "Remove from playlist" staging. The By-playlist "-" button stages
  // a (playlist, track) pair here; RemovePlaylistBox applies them on
  // Confirm. Library + user are captured per group so the removal
  // runs against the right scope. A track already staged for the same
  // playlist is skipped.
  const stageRemoval = useCallback((
    playlistId: string, playlistName: string, item: DevConsoleItem,
  ) => {
    setRemovals((prev) => {
      const g = prev.find((x) => x.playlistId === playlistId);
      if (g) {
        if (g.items.some((i) => i.backend_item_id === item.backend_item_id)) {
          return prev;
        }
        return prev.map((x) => x.playlistId === playlistId
          ? { ...x, items: [...x.items, item] } : x);
      }
      return [...prev, {
        playlistId, playlistName, libraryId, user, items: [item],
      }];
    });
  }, [libraryId, user]);
  // Unstage one track; a group left empty is dropped.
  const unstageRemovalItem = useCallback((playlistId: string, itemId: string) => {
    setRemovals((prev) => prev
      .map((g) => g.playlistId === playlistId
        ? { ...g, items: g.items.filter((i) => i.backend_item_id !== itemId) }
        : g)
      .filter((g) => g.items.length > 0));
  }, []);
  const unstageRemovalGroup = useCallback((playlistId: string) => {
    setRemovals((prev) => prev.filter((g) => g.playlistId !== playlistId));
  }, []);
  const clearRemovals = useCallback(() => setRemovals([]), []);

  const loadFlat = useCallback(async (nextOffset: number) => {
    if (!libraryId) return;
    setLoading(true);
    setError(null);
    try {
      const filters: DevConsoleItemFilters = { offset: nextOffset, page_size: pageSize };
      if (user === ALL_USERS) filters.all_users = true;
      else if (user) filters.user = user;
      if (categories.length) filters.categories = categories;
      if (search.trim()) filters.search = search.trim();
      filters.sort = sort;
      const r = await api.devConsoleItems(serverId, libraryId, filters);
      setResp(r);
      setOffset(r.offset);
      if (!r.user_synced) {
        const want = r.all_users
          ? managedUsers.map((u) => u.username)
          : (r.user ? [r.user] : []);
        if (want.length) api.devConsoleRequestSync(serverId, want).catch(() => {});
      }
    } catch (e) {
      setError(errorText(e));
      setResp(null);
    } finally { setLoading(false); }
  }, [serverId, libraryId, user, categories, pageSize, search, sort, managedUsers]);

  // Auto-load: the explorer is "live" once a library is picked - any
  // selector change (user, View category, page size, sort, browse
  // mode) or an external refresh re-queries.
  useEffect(() => {
    setSelected(null);
    if (browseMode === 'flat' && libraryId) loadFlat(0);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [serverId, libraryId, user, catKey, pageSize, sort, browseMode, refreshKey]);

  // Movie / photo libraries have no hierarchy; force the flat view.
  useEffect(() => {
    if (kind === 'movie' && browseMode === 'grouped') setBrowseMode('flat');
  }, [kind, browseMode]);

  // "By playlist" is only meaningful with the playlist-membership
  // category selected; fall back to flat when it is cleared.
  useEffect(() => {
    if (browseMode === 'playlist' && !categories.includes('playlists')) {
      setBrowseMode('flat');
    }
  }, [browseMode, categories]);

  // The grouped sorts (by count) are meaningless in the flat list;
  // reset to a name sort whenever the browse mode flips.
  useEffect(() => { setSort('name_asc'); }, [browseMode]);

  // A playlist is library-scoped; drop the basket and any staged
  // removals when the library (or server) changes so nothing crosses
  // libraries by accident.
  useEffect(() => { setBasket([]); setRemovals([]); }, [serverId, libraryId]);

  const items = resp?.items || [];
  const total = resp?.total || 0;
  const pageCount = Math.max(1, Math.ceil(total / pageSize));
  const pageNum = Math.floor(offset / pageSize) + 1;

  return (
    <div className="panel" style={{ marginTop: 12 }}>
      <h4 style={{ marginTop: 0 }}>Item explorer</h4>

      {/* Jobs-panel style selectors. */}
      <CardRow label="User">
        <SelectCard label="Owner" active={user === ''} onClick={() => setUser('')} />
        {managedUsers.map((u) => (
          <SelectCard key={u.backend_user_id || u.username}
            label={u.display_name || u.username}
            active={user === u.username} onClick={() => setUser(u.username)} />
        ))}
        <SelectCard label="All users" active={allUsers} onClick={() => setUser(ALL_USERS)} />
      </CardRow>
      <CardRow label="Library">
        {detail.libraries.length === 0 && <span className="muted">No libraries mirrored yet.</span>}
        <span data-testid="devc-library-picker" style={{ display: 'contents' }}>
          {detail.libraries.map((l) => (
            <SelectCard key={l.library_id} label={l.name}
              sub={`${l.type}${l.item_count != null ? ` . ${l.item_count}` : ''}`}
              active={libraryId === l.library_id}
              onClick={() => setLibraryId(l.library_id)} />
          ))}
        </span>
      </CardRow>
      <CardRow label="View">
        {CATEGORIES.map((cat) => (
          <SelectCard key={cat.id} label={cat.label}
            active={categories.includes(cat.id)}
            onClick={() => toggleCategory(cat.id)} />
        ))}
        {categories.length > 0 && (
          <SelectCard label="Clear" active={false} onClick={() => setCategories([])} />
        )}
      </CardRow>
      {libraryId && (kind !== 'movie' || categories.includes('playlists')) && (
        <CardRow label="Browse">
          <SelectCard label="Flat list" active={browseMode === 'flat'}
            onClick={() => setBrowseMode('flat')} />
          {kind !== 'movie' && (
            <SelectCard label={kind === 'music' ? 'By artist / album' : 'By show / season'}
              active={browseMode === 'grouped'}
              onClick={() => setBrowseMode('grouped')} />
          )}
          {categories.includes('playlists') && (
            <SelectCard label="By playlist" active={browseMode === 'playlist'}
              onClick={() => setBrowseMode('playlist')} />
          )}
        </CardRow>
      )}
      <p className="muted" style={{ marginTop: 8 }}>
        {!libraryId
          ? 'Pick a library to load its items.'
          : categories.length
            ? `Showing items matching: ${categories.join(', ')}.`
            : 'Showing the whole library - pick a View to narrow it.'}
      </p>

      {error && <p className="error">{error}</p>}

      {/* Create-playlist - its own small panel above the results. */}
      {libraryId && (
        <div className="panel" style={{ marginTop: 12 }}>
          <CreatePlaylistBox serverId={serverId} libraryId={libraryId}
            basket={basket} selectedItem={selected}
            onAddItem={addBasketItem} onRemoveEntry={removeBasketEntry}
            onRemoveItems={removeBasketItems} onClear={clearBasket}
            onResult={(m, ok) => { onResult(m, ok); }} />
        </div>
      )}

      {/* Remove-from-playlist - appears beneath Create playlist once
          the By-playlist "-" button has staged something. */}
      {libraryId && removals.length > 0 && (
        <div className="panel" style={{ marginTop: 12 }}>
          <RemovePlaylistBox serverId={serverId} removals={removals}
            onRemoveItem={unstageRemovalItem} onRemoveGroup={unstageRemovalGroup}
            onClear={clearRemovals}
            onResult={(m, ok) => { onResult(m, ok); }} />
        </div>
      )}

      {/* Create smart playlist - its own panel; music libraries only. */}
      {libraryId && kind === 'music' && (
        <div className="panel" style={{ marginTop: 12 }}>
          <CreateSmartPlaylistBox serverId={serverId} libraryId={libraryId}
            user={user} onResult={(m, ok) => { onResult(m, ok); }} />
        </div>
      )}

      {/* Results panel - its own box; command panel pinned at the top. */}
      {libraryId && (
        <div className="panel" style={{ marginTop: 12 }}>
          {selected && (
            <ItemPanel
              serverId={serverId} serviceType={serviceType} item={selected}
              users={detail.users} defaultUser={user} staging={staging}
              playlists={playlists} collections={collections}
              onResult={(m, ok) => { onResult(m, ok); if (browseMode === 'flat') loadFlat(offset); }}
              onClose={() => setSelected(null)}
            />
          )}

          {/* Search + sort - shared by the flat list and the tree. */}
          <div className="dc-pager">
            <label>Search
              <input value={search} onChange={(e) => setSearch(e.target.value)}
                onKeyDown={(e) => { if (e.key === 'Enter' && browseMode === 'flat') loadFlat(0); }}
                placeholder={browseMode === 'grouped'
                  ? (kind === 'music' ? 'artist name' : 'show name')
                  : browseMode === 'playlist' ? 'playlist name'
                  : 'item title'}
                style={{ width: 160 }} />
            </label>
            <label>Sort
              <select value={sort} onChange={(e) => setSort(e.target.value)}>
                <option value="name_asc">Name A-Z</option>
                <option value="name_desc">Name Z-A</option>
                {browseMode === 'grouped' && (
                  <option value="count_desc">
                    Most {kind === 'music' ? 'tracks' : 'episodes'}
                  </option>
                )}
                {browseMode === 'grouped' && (
                  <option value="subcount_desc">
                    Most {kind === 'music' ? 'albums' : 'seasons'}
                  </option>
                )}
                {browseMode === 'playlist' && (
                  <option value="count_desc">Most items in this library</option>
                )}
              </select>
            </label>
            {browseMode === 'grouped' && (
              <span className="muted">search filters the {kind === 'music' ? 'artists' : 'shows'}</span>
            )}
            {browseMode === 'playlist' && (
              <span className="muted">search filters the playlists</span>
            )}
          </div>

          {browseMode === 'grouped' && kind !== 'movie' ? (
            <GroupTree key={`${libraryId}|${user}|${catKey}|${refreshKey}`}
              serverId={serverId} libraryId={libraryId}
              kind={kind === 'music' ? 'music' : 'tv'}
              user={user} categories={categories}
              search={search} sort={sort}
              selectedId={selected?.backend_item_id ?? null}
              onSelect={setSelected}
              onAddGroup={addBasketGroup}
              onResult={onResult} />
          ) : browseMode === 'playlist' ? (
            <PlaylistTree key={`pl|${libraryId}|${user}|${refreshKey}`}
              serverId={serverId} libraryId={libraryId}
              user={user} categories={categories}
              search={search} sort={sort}
              selectedId={selected?.backend_item_id ?? null}
              onSelect={setSelected}
              onAddItem={addBasketItem}
              onAddGroup={addBasketGroup}
              onStageRemoval={stageRemoval}
              onResult={onResult} />
          ) : (
            <>
              <div className="dc-pager">
                <label>Per page
                  <select value={pageSize}
                    onChange={(e) => setPageSize(parseInt(e.target.value, 10))}>
                    {PAGE_SIZES.map((n) => <option key={n} value={n}>{n}</option>)}
                  </select>
                </label>
                <button disabled={loading || offset <= 0}
                  onClick={() => loadFlat(Math.max(0, offset - pageSize))}>Prev</button>
                <span className="muted">Page {pageNum} of {pageCount} - {total} item(s)</span>
                <button disabled={loading || offset + pageSize >= total}
                  onClick={() => loadFlat(offset + pageSize)}>Next</button>
              </div>
              {resp && resp.mirror_cold && <p className="muted">Mirror not synced yet.</p>}
              {resp && resp.all_users && !resp.user_synced && (
                <p className="muted">Syncing every user's view - reload shortly for the full picture.</p>
              )}
              <div
                className="dc-table-wrap"
                style={{ maxHeight: 440, overflow: 'auto' }}
                data-testid="devc-items-grid"
              >
                <ItemTable items={items} categories={categories}
                  allUsers={allUsers} user={user}
                  selectedId={selected?.backend_item_id ?? null}
                  onSelect={setSelected}
                  emptyText={loading ? 'Loading...' : 'No items match.'} />
              </div>
            </>
          )}
        </div>
      )}
    </div>
  );
}

// ── Staged-changes tray ─────────────────────────────────────────────────────

function StagedTray({
  serverId, pending, onResult, onChanged,
}: {
  serverId: string; pending: DevConsoleStagedChange[];
  onResult: (msg: string, ok: boolean) => void; onChanged: () => void;
}) {
  const [busy, setBusy] = useState(false);
  if (pending.length === 0) return null;

  const send = async () => {
    setBusy(true);
    try {
      const r = await api.devConsoleSendStaged(serverId);
      onResult(`Sent staged changes: ${r.sent} ok, ${r.failed} failed`, r.failed === 0);
    } catch (e) {
      onResult(`Send staged: ${errorText(e)}`, false);
    } finally { setBusy(false); onChanged(); }
  };
  const discard = async () => {
    setBusy(true);
    try {
      const r = await api.devConsoleDiscardStaged(serverId);
      onResult(`Discarded ${r.discarded} staged change(s)`, true);
    } catch (e) {
      onResult(`Discard staged: ${errorText(e)}`, false);
    } finally { setBusy(false); onChanged(); }
  };

  return (
    <div className="panel" style={{ marginTop: 12, borderColor: 'var(--warn)' }}>
      <h4 style={{ marginTop: 0 }}>Staged changes ({pending.length}) - not yet sent</h4>
      <div style={{ maxHeight: 160, overflow: 'auto' }}>
        <ul className="dc-events">
          {pending.map((c) => (
            <li key={c.id}>
              {c.op} on <strong>{c.item_title || c.backend_item_id}</strong>
              {c.username ? ` (user ${c.username})` : ' (owner)'}
              {' '}<span className="muted">{JSON.stringify(c.payload)}</span>
            </li>
          ))}
        </ul>
      </div>
      <div className="dc-cmd-row">
        <button disabled={busy} onClick={send} style={{ background: 'var(--good)' }}>Send all</button>
        <button disabled={busy} onClick={discard}>Discard all</button>
        <span className="muted">
          Background sync is frozen for this server until you Send or Discard.
        </span>
      </div>
    </div>
  );
}

// ── Per-server view ─────────────────────────────────────────────────────────

function ServerView({ serverId, events }: { serverId: string; events: DevConsoleEvent[] }) {
  const [detail, setDetail] = useState<DevConsoleServerDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [playlists, setPlaylists] = useState<DevConsolePlaylist[]>([]);
  const [collections, setCollections] = useState<DevConsoleCollection[]>([]);
  const [staged, setStaged] = useState<DevConsoleStagedChange[]>([]);
  const [staging, setStaging] = useState(true);
  const [log, setLog] = useState<{ msg: string; ok: boolean; ts: number }[]>([]);
  const [refreshKey, setRefreshKey] = useState(0);
  const [syncing, setSyncing] = useState(false);

  const note = useCallback((msg: string, ok: boolean) => {
    setLog((prev) => [{ msg, ok, ts: Date.now() }, ...prev].slice(0, 10));
  }, []);

  const reload = useCallback(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    api.devConsoleServerDetail(serverId)
      .then((d) => {
        if (cancelled) return;
        setDetail(d);
        return Promise.all([
          api.devConsolePlaylists(serverId).catch(() => ({ playlists: [] })),
          api.devConsoleCollections(serverId).catch(() => ({ collections: [] })),
          api.devConsoleStaged(serverId).catch(() => ({ pending: [], history: [], server_id: serverId })),
        ]);
      })
      .then((res) => {
        if (cancelled || !res) return;
        setPlaylists(res[0].playlists);
        setCollections(res[1].collections);
        setStaged(res[2].pending);
      })
      .catch((e) => { if (!cancelled) setError(errorText(e)); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [serverId]);

  useEffect(() => reload(), [serverId, reload]);

  // Cold mirror -> request a first sync.
  useEffect(() => {
    if (detail && !detail.synced && !syncing) {
      setSyncing(true);
      api.devConsoleRequestSync(serverId).catch(() => {});
    }
  }, [detail, serverId, syncing]);

  const lastEvent = events[0];
  useEffect(() => {
    if (!lastEvent || lastEvent.server_id !== serverId) return;
    if (lastEvent.type === 'sync' && lastEvent.phase === 'complete') {
      setSyncing(false);
      reload();
      setRefreshKey((k) => k + 1);
    } else if (lastEvent.type === 'sync' && lastEvent.phase === 'error') {
      setSyncing(false);
      reload();
    } else if (lastEvent.type === 'staged_sent' || lastEvent.type === 'staged_discarded') {
      reload();
      setRefreshKey((k) => k + 1);
    }
  }, [lastEvent, serverId, reload]);

  const requestSync = useCallback(() => {
    setSyncing(true);
    api.devConsoleRequestSync(serverId)
      .then(() => note('Mirror sync requested', true))
      .catch((e) => { setSyncing(false); note(`Sync request: ${errorText(e)}`, false); });
  }, [serverId, note]);

  // Delete the mirror cache and re-sync from scratch. The clean reset
  // when a schema change or a partial sync left the mirror stale.
  const confirm = useConfirm();
  const rebuildMirror = useCallback(async () => {
    if (!(await confirm({
      body: "Delete this server's mirror cache and re-sync it from scratch? "
        + 'Any pending staged changes are discarded.',
      danger: true,
    }))) return;
    setSyncing(true);
    api.devConsoleResetMirror(serverId)
      .then(() => note('Mirror cleared; fresh sync requested', true))
      .catch((e) => {
        setSyncing(false);
        note(`Rebuild mirror: ${errorText(e)}`, false);
      });
  }, [serverId, note, confirm]);

  const afterChange = useCallback(() => {
    api.devConsoleStaged(serverId).then((s) => setStaged(s.pending)).catch(() => {});
    setRefreshKey((k) => k + 1);
    reload();
  }, [serverId, reload]);

  if (loading && !detail) return <p className="muted">Loading mirror...</p>;
  if (error) {
    return (
      <div className="panel">
        <p className="error">Could not load server: {error}</p>
        <button onClick={reload}>Retry</button>
      </div>
    );
  }
  if (!detail) return null;

  return (
    <div>
      <div className="panel" style={{ marginTop: 12 }}>
        <div className="dc-cmd-row">
          <strong>Mirror:</strong>
          {detail.synced
            ? <span>synced {ago(detail.last_full_sync_at)}</span>
            : <span className="muted">{syncing ? 'syncing...' : 'not synced'}</span>}
          <button disabled={syncing} onClick={requestSync}>
            {syncing ? 'Syncing...' : 'Sync now'}
          </button>
          <button disabled={syncing} onClick={rebuildMirror}
            title="Delete the mirror cache and re-sync from scratch">
            Rebuild mirror
          </button>
          {detail.last_sync_error && (
            <span className="error">last sync error: {detail.last_sync_error}</span>
          )}
        </div>
        <div className="dc-cmd-row">
          <label className="dc-check">
            <input type="checkbox" checked={staging}
              onChange={(e) => setStaging(e.target.checked)} />
            Stage commands
          </label>
          <span className="muted">
            {staging
              ? 'Edits queue in the mirror; nothing is sent until you hit Send.'
              : 'Edits are sent to the live server immediately.'}
          </span>
        </div>
      </div>

      <StagedTray serverId={serverId} pending={staged}
        onResult={note} onChanged={afterChange} />

      {detail.synced ? (
        <ItemExplorer
          serverId={serverId} serviceType={detail.service_type}
          detail={detail} playlists={playlists} collections={collections}
          staging={staging} refreshKey={refreshKey}
          onResult={(m, ok) => { note(m, ok); afterChange(); }}
        />
      ) : (
        <p className="muted">
          The mirror for this server is being built. The explorer
          appears once the first sync finishes.
        </p>
      )}

      {log.length > 0 && (
        <div className="panel" style={{ marginTop: 12 }}>
          <h4 style={{ marginTop: 0 }}>Recent results</h4>
          <ul className="dc-events" style={{ maxHeight: 200, overflow: 'auto' }}>
            {log.map((l, i) => (
              <li key={i} className={l.ok ? '' : 'error'}>
                <span className="muted">{new Date(l.ts).toLocaleTimeString()}</span>{' '}
                {l.msg}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

// ── Live event log ──────────────────────────────────────────────────────────

function EventLog({ events }: { events: DevConsoleEvent[] }) {
  return (
    <div className="panel" style={{ marginTop: 12 }}>
      <h4 style={{ marginTop: 0 }}>Live events <span className="muted">(last 10)</span></h4>
      {events.length === 0 && <p className="muted">No events yet.</p>}
      <ul className="dc-events" style={{ maxHeight: 180, overflow: 'auto' }}>
        {events.map((ev, i) => (
          <li key={i}>
            <span className="muted">
              {new Date((ev.server_ts || 0) * 1000).toLocaleTimeString()}
            </span>{' '}
            [{ev.type}] {String(ev.op ?? ev.phase ?? '')}{' '}
            <span className="muted">{String(ev.detail ?? '')}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

// ── Top-level panel ─────────────────────────────────────────────────────────

export function ServerCommandsPanel() {
  const [servers, setServers] = useState<DevConsoleServer[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [activeServer, setActiveServer] = useState<string | null>(null);
  const [events, setEvents] = useState<DevConsoleEvent[]>([]);
  const [job, setJob] = useState<Record<string, unknown> | null>(null);

  useEffect(() => {
    api.devConsoleServers()
      .then((r) => {
        setServers(r.servers);
        if (r.servers.length > 0) setActiveServer((cur) => cur ?? r.servers[0].server_id);
      })
      .catch((e) => setError(errorText(e)));
  }, []);

  useEffect(() => {
    const unsub = devConsoleWsClient.subscribe((ev) => {
      if (ev.type === 'heartbeat' || ev.type === 'hello') {
        setJob((ev.job as Record<string, unknown> | null) ?? null);
        return;
      }
      setEvents((prev) => [ev, ...prev].slice(0, 10));
    });
    return unsub;
  }, []);

  useEffect(() => {
    if (activeServer) devConsoleWsClient.setWatch(activeServer);
  }, [activeServer]);

  const activeObj = useMemo(
    () => (servers || []).find((s) => s.server_id === activeServer) || null,
    [servers, activeServer],
  );

  if (error) {
    return (
      <div className="panel">
        <h3>Server Commands</h3>
        <p className="error">{error}</p>
      </div>
    );
  }
  if (!servers) {
    return (
      <div className="panel">
        <h3>Server Commands</h3>
        <p className="muted">Loading servers...</p>
      </div>
    );
  }

  return (
    <div className="server-commands-panel">
      <div className="panel">
        <h3 style={{ marginTop: 0 }}>Server Commands</h3>
        <p className="muted">
          Root-admin developer console. Browses each server's mirror
          database; edits are staged by default and sent on demand.
        </p>
        {job && (job as { state?: string }).state && (
          <p style={{ color: 'var(--warn)' }}>
            Background job: {String((job as { mode?: string }).mode || '')}{' '}
            ({String((job as { state?: string }).state || '')})
          </p>
        )}
        {servers.length === 0 && <p className="muted">No servers registered.</p>}
        {servers.length > 0 && (
          <nav className="tabs sub-tabs">
            {servers.map((s) => (
              <button key={s.server_id}
                className={activeServer === s.server_id ? 'active' : ''}
                onClick={() => setActiveServer(s.server_id)}>
                {s.name || s.server_id}
                <span className="muted"> ({s.service_type})</span>
              </button>
            ))}
          </nav>
        )}
      </div>

      {activeObj && (
        <ServerView key={activeObj.server_id} serverId={activeObj.server_id} events={events} />
      )}

      <EventLog events={events} />
    </div>
  );
}
