// Library Mapping sub-tab under Servers.
//
// The original row-based table relied on the auto-matcher computing
// suggestions first, which silently dropped servers whose mirror DB
// was empty. The new layout shows BOTH servers' library lists as two
// side-by-side columns, lit up from /api/library-mapping/sides which
// falls back to live adapter calls when the mirror is cold. Auto-match
// becomes a button that DRAWS suggested pairings over the two columns;
// the operator confirms, clicks-to-link any pair manually, or skips a
// library entirely. Multiple audio libraries on each side (Music +
// Audiobooks + Podcasts, all type=artist) are a first-class case -
// only the operator knows which pairs with which, and the click-to-
// link flow makes that fast.
//
// State sources:
//   - GET /api/library-mapping/sides    = both columns + saved mappings
//   - POST /api/library-mapping/automap = compute suggestions overlay
//   - PUT  /api/library-mapping/save    = persist a manual or confirm pair
//   - DELETE /api/library-mapping/...   = reset one mapping
//   - POST /api/library-mapping/invalidate-auto = drop all auto rows

import { useEffect, useMemo, useState } from 'react';
import { api } from '../api';
import { useConfirm } from './ConfirmModal';
import type {
  LibraryMappingCandidate,
  LibraryMappingRow,
  ServerView,
} from '../api';

// Library shape returned by /sides. Same shape on both columns.
interface SideLib {
  library_id: string;
  library_name: string;
  library_type: string;
  item_count: number;
  source: 'mirror' | 'live';
}

// Per-source-library auto-match suggestion staged in state. Keyed by
// the source library's id so we can paint it next to the source card
// without a second lookup.
type SuggestionMap = Record<string, LibraryMappingCandidate | null>;

// Each unsaved auto-match pair gets its own colour from this
// palette; the source card and its suggested dest card share the
// colour so the link is visually unambiguous. Without this, both
// sides would share the same dashed amber border, leaving no way
// to see which source paired with which dest at a glance. Colours
// are stable in iteration order (palette cycles past index 7 - unlikely in
// practice with the typical library count, but harmless if it
// happens).
const PAIR_PALETTE = [
  '#4a7afc', // blue
  '#b160d6', // purple
  '#20b2aa', // teal
  '#e85a8d', // pink
  '#6fbf73', // lime
  '#f5a623', // amber
  '#4ec9d6', // cyan
  '#c44569', // magenta
];

interface PairColor { color: string; index: number; }

export function LibraryMappingTab({ servers }: { servers: ServerView[] }) {
  const [sourceServerId, setSourceServerId] = useState<string>('');
  const [destServerId, setDestServerId] = useState<string>('');
  const [sourceLibs, setSourceLibs] = useState<SideLib[]>([]);
  const [destLibs, setDestLibs] = useState<SideLib[]>([]);
  const [mappings, setMappings] = useState<LibraryMappingRow[]>([]);
  const [suggestions, setSuggestions] = useState<SuggestionMap>({});
  // Manual mapping needs explicit confirm, not auto-save on click.
  // Track both halves of the
  // pending pair independently so the operator can:
  //   * Click source A → pendingSource = A, pendingDest = null
  //   * Click dest B → pendingDest = B (preview shows A → B, NOT saved)
  //   * Click a different dest C → pendingDest = C (preview updates)
  //   * Click a different source D → pendingSource = D, pendingDest = null
  //   * Click Confirm → persists
  //   * Click Cancel → both clear
  const [pendingSource, setPendingSource] = useState<SideLib | null>(null);
  const [pendingDest, setPendingDest] = useState<SideLib | null>(null);
  const [busy, setBusy] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const [info, setInfo] = useState<string | null>(null);
  const [sourcesNote, setSourcesNote] = useState<string>('');

  const pickValid = !!sourceServerId && !!destServerId
    && sourceServerId !== destServerId;

  const serverLabel = (id: string): string => {
    const s = servers.find((x) => x.id === id);
    if (!s) return id;
    const backend = (s as ServerView & { service_type?: string }).service_type;
    return backend && backend !== 'plex'
      ? `${s.name} [${backend}]`
      : s.name;
  };

  // Mappings indexed by source_library_id for O(1) lookup while
  // rendering each source card.
  const mappingBySourceId = useMemo<Record<string, LibraryMappingRow>>(
    () => {
      const m: Record<string, LibraryMappingRow> = {};
      for (const row of mappings) m[row.source_library_id] = row;
      return m;
    },
    [mappings],
  );

  // Set of destination library ids that are currently mapped — used
  // to show a "(mapped from <src>)" hint on the dest column.
  const mappingsByDestId = useMemo<Record<string, LibraryMappingRow>>(
    () => {
      const m: Record<string, LibraryMappingRow> = {};
      for (const row of mappings) {
        if (row.dest_library_id) m[row.dest_library_id] = row;
      }
      return m;
    },
    [mappings],
  );

  // Per-pair colour assignment for the Auto-match preview. We assign
  // a colour from PAIR_PALETTE to every (unsaved) suggestion in
  // source-list iteration order, then index BOTH the source card and
  // its suggested-dest card by the same colour. Source libs already
  // mapped are skipped (the saved-mapping green dominates anyway).
  // Dest libs already mapped from a different source are also
  // skipped (the operator decision wins over the suggestion).
  const pairColors = useMemo<{
    bySource: Record<string, PairColor>;
    byDest: Record<string, PairColor>;
  }>(() => {
    const bySource: Record<string, PairColor> = {};
    const byDest: Record<string, PairColor> = {};
    let i = 0;
    for (const src of sourceLibs) {
      const sug = suggestions[src.library_id];
      if (!sug) continue;
      if (mappingBySourceId[src.library_id]) continue;
      const dstId = sug.dest_library.library_id;
      if (mappingsByDestId[dstId]) continue;
      const color = PAIR_PALETTE[i % PAIR_PALETTE.length];
      bySource[src.library_id] = { color, index: i + 1 };
      byDest[dstId] = { color, index: i + 1 };
      i++;
    }
    return { bySource, byDest };
  }, [sourceLibs, suggestions, mappingBySourceId, mappingsByDestId]);

  // ── Load both columns whenever the server pair changes ──
  const reloadSides = async (src: string, dst: string) => {
    if (!src || !dst || src === dst) {
      setSourceLibs([]);
      setDestLibs([]);
      setMappings([]);
      setSuggestions({});
      setPendingSource(null);
      setSourcesNote('');
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const r = await api.libraryMappingSides(src, dst);
      setSourceLibs(r.source_libraries);
      setDestLibs(r.dest_libraries);
      setMappings(r.mappings);
      // Surface which data source each column drew from so the
      // operator understands when they're seeing live-fetched
      // libraries (slower; reflects this moment) vs cached mirror
      // libraries (fast; reflects the last mirror sync).
      const srcKind: string = r.source_libraries[0]?.source ?? 'empty';
      const dstKind: string = r.dest_libraries[0]?.source ?? 'empty';
      const note = `Source: ${r.source_libraries.length} libraries `
        + `(${srcKind === 'empty' ? 'none found' : srcKind}). `
        + `Destination: ${r.dest_libraries.length} libraries `
        + `(${dstKind === 'empty' ? 'none found' : dstKind}).`;
      setSourcesNote(note);
    } catch (e) {
      setError(`Failed to load library lists: ${(e as Error).message}`);
    } finally {
      setBusy(false);
    }
  };

  useEffect(() => {
    setSuggestions({});
    setPendingSource(null);
    setPendingDest(null);
    setInfo(null);
    setError(null);
    void reloadSides(sourceServerId, destServerId);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sourceServerId, destServerId]);

  // ── Actions ──

  const runAutomap = async (persistHigh: boolean) => {
    if (!pickValid) return;
    setBusy(true);
    setError(null);
    setInfo(null);
    try {
      const r = await api.libraryMappingAutomap(
        sourceServerId, destServerId, persistHigh,
      );
      // Stage suggestions keyed by source library id; the UI uses
      // this to paint a "→ Tunes (98%)" hint on each source card.
      const next: SuggestionMap = {};
      for (const row of r.results) {
        next[row.source_library.library_id] = row.best;
      }
      setSuggestions(next);
      const matched = Object.values(next).filter((c) => c).length;
      // Count source libraries from the fresh automap response, not
      // the component's sourceLibs state, which can be stale relative
      // to ``r`` if the library list changed since it was loaded.
      const totalSrc = r.results.length;
      const unmatched = Math.max(0, totalSrc - matched);
      if (persistHigh) {
        setInfo(`Saved ${r.auto_saved} high-confidence pair(s). `
          + `Lower-confidence pairs are highlighted below — each pair has its own colour + number so the source and dest cards match. `
          + `Click a source card then a destination card to confirm or override any one.`);
      } else {
        setInfo(`Computed ${matched} suggested pair(s) (${unmatched} source librar${unmatched === 1 ? 'y' : 'ies'} had no match). `
          + `Each pair is highlighted with its own colour + number so the source and dest cards visibly link. `
          + `Suggestions are previews — they don't save until you click them.`);
      }
      // Re-read mappings so the post-persist UI reflects the bulk save.
      if (persistHigh) await reloadSides(sourceServerId, destServerId);
    } catch (e) {
      setError(`Automap failed: ${(e as Error).message}`);
    } finally {
      setBusy(false);
    }
  };

  // Click handlers implementing the two-column click-to-link flow.
  // Clicking a destination STAGES the pair as a
  // preview (highlighted, not saved). The operator confirms via the
  // bottom banner's Confirm button; clicks do not auto-save.
  const onClickSource = (lib: SideLib) => {
    // Clicking the already-pending source clears the whole pending
    // pair (matches "start over" intent).
    if (pendingSource?.library_id === lib.library_id) {
      setPendingSource(null);
      setPendingDest(null);
      return;
    }
    setPendingSource(lib);
    // Switching to a new source clears any previously-staged dest
    // (a different source/dest pair is the operator picking again).
    setPendingDest(null);
    setInfo(
      `Selected source: ${lib.library_name}. Now click a destination `
      + `library to preview the mapping — nothing saves until you confirm.`,
    );
  };

  const onClickDest = (lib: SideLib) => {
    if (!pendingSource) {
      setInfo(`Select a source library on the left first, then click a destination on the right to preview a mapping.`);
      return;
    }
    // Toggle: clicking the already-pending dest clears just the dest.
    if (pendingDest?.library_id === lib.library_id) {
      setPendingDest(null);
      setInfo(`Selected source: ${pendingSource.library_name}. Click a destination library to preview.`);
      return;
    }
    setPendingDest(lib);
    setInfo(
      `Preview: ${pendingSource.library_name} → ${lib.library_name}. `
      + `Click Confirm below to save, or pick a different destination to change the preview.`,
    );
  };

  const confirmPending = async () => {
    if (!pendingSource || !pendingDest) return;
    await savePair(pendingSource, pendingDest);
    setPendingSource(null);
    setPendingDest(null);
  };

  const cancelPending = () => {
    setPendingSource(null);
    setPendingDest(null);
    setInfo(null);
  };

  const savePair = async (src: SideLib, dst: SideLib | null) => {
    if (!pickValid) return;
    setBusy(true);
    setError(null);
    try {
      await api.libraryMappingSave({
        source_server_id: sourceServerId,
        source_library_id: src.library_id,
        source_library_name: src.library_name,
        dest_server_id: destServerId,
        dest_library_id: dst?.library_id ?? '',
        dest_library_name: dst?.library_name ?? '',
        source: 'operator',
      });
      setInfo(dst
        ? `Mapped ${src.library_name} → ${dst.library_name}.`
        : `Marked ${src.library_name} as explicit skip.`);
      await reloadSides(sourceServerId, destServerId);
    } catch (e) {
      setError(`Save failed: ${(e as Error).message}`);
    } finally {
      setBusy(false);
    }
  };

  const skipSource = (src: SideLib) => savePair(src, null);

  const resetSource = async (src: SideLib) => {
    setBusy(true);
    setError(null);
    try {
      await api.libraryMappingDelete(
        sourceServerId, src.library_id, destServerId,
      );
      setInfo(`Reset ${src.library_name}.`);
      await reloadSides(sourceServerId, destServerId);
    } catch (e) {
      setError(`Reset failed: ${(e as Error).message}`);
    } finally {
      setBusy(false);
    }
  };

  const confirm = useConfirm();
  const invalidateAuto = async () => {
    if (!pickValid) return;
    if (!(await confirm({
      body:
        `Drop every auto-suggested mapping between ${serverLabel(sourceServerId)} `
        + `and ${serverLabel(destServerId)}? Operator-confirmed rows survive.`,
      danger: true,
    }))) return;
    setBusy(true);
    setError(null);
    try {
      const r = await api.libraryMappingInvalidateAuto(
        sourceServerId, destServerId,
      );
      setInfo(`Dropped ${r.deleted} auto row(s).`);
      await reloadSides(sourceServerId, destServerId);
    } catch (e) {
      setError(`Invalidate failed: ${(e as Error).message}`);
    } finally {
      setBusy(false);
    }
  };

  // ── Render helpers ──

  const tierLabel = (tier: string): string => {
    if (tier === 'content') return 'GUID overlap';
    if (tier === 'path') return 'Path overlap';
    if (tier === 'type') return 'Library type';
    if (tier === 'name') return 'Name match';
    return tier || '—';
  };

  const confidencePct = (c: number) => Math.round((c || 0) * 100);

  const stateBadgeFor = (
    src: SideLib,
  ): { label: string; cls: string } | null => {
    const m = mappingBySourceId[src.library_id];
    if (!m) {
      const sug = suggestions[src.library_id];
      if (sug) {
        return {
          label: `suggested ${confidencePct(sug.confidence)}%`,
          cls: 'phase',
        };
      }
      return null;
    }
    if (m.source === 'operator' && !m.dest_library_id) {
      return { label: 'operator skip', cls: 'failed' };
    }
    if (m.source === 'operator') {
      return { label: 'confirmed', cls: 'good' };
    }
    return { label: `auto ${confidencePct(m.confidence)}%`, cls: 'phase' };
  };

  // Card style for the side columns. Selection / mapping highlights
  // happen via inline border color. When `pairColor` is provided the
  // suggested state uses that colour (per-pair palette) so the
  // operator can visually trace which source pairs with which dest.
  const sideCardStyle = (extras: {
    selected?: boolean;
    mapped?: boolean;
    suggested?: boolean;
    pairColor?: string;
  }): React.CSSProperties => {
    let border = '1px solid var(--border, rgba(255,255,255,0.08))';
    let bg: string | undefined;
    if (extras.selected) {
      border = '2px solid var(--accent, #4a7afc)';
      bg = 'var(--bg-panel, rgba(74,122,252,0.08))';
    } else if (extras.mapped) {
      border = '1px solid var(--good, #4caf50)';
      bg = 'rgba(76,175,80,0.06)';
    } else if (extras.suggested && extras.pairColor) {
      // Per-pair palette colour with ~12% tinted bg
      // (#XX1F suffix is the alpha byte in 8-digit hex).
      border = `2px solid ${extras.pairColor}`;
      bg = `${extras.pairColor}1F`;
    } else if (extras.suggested) {
      // Fallback (shouldn't fire once pairColors is wired through,
      // but kept defensive in case a suggestion is paired with a
      // mapped dest and we want to surface the source side anyway).
      border = '1px dashed var(--warn, #f5a623)';
      bg = 'rgba(245,166,35,0.04)';
    }
    return {
      border, background: bg,
      padding: '8px 10px',
      borderRadius: 4,
      cursor: 'pointer',
      transition: 'all 0.1s ease',
    };
  };

  return (
    <div>
      {error && <div className="banner error" style={{ marginBottom: 8 }}>{error}</div>}
      {info && <div className="banner info" style={{ marginBottom: 8 }}>{info}</div>}

      <div className="panel" style={{ marginBottom: 12 }}>
        <h3 style={{ marginTop: 0 }}>Library Mapping</h3>
        <p className="help" style={{ marginBottom: 8, fontSize: 13 }}>
          <strong>What this is:</strong> a saved equivalence table
          that tells the engine which library on the source server is
          the same content as which library on the destination server.
          A library named "Music" on one server and "Tunes" on another
          can hold the same albums; only you know which pairs with
          which. Mapping captures that knowledge once so every snapshot
          restore, direct transfer, and sync routes correctly between
          them.
        </p>
        <p className="help" style={{ marginBottom: 8, fontSize: 13 }}>
          <strong>What relies on it:</strong> snapshot restore (unmapped
          libraries get dropped from the payload before write), direct
          transfer (same library-pair filter), cross-backend safety
          (Plex {'<->'} Jellyfin / Emby Replace-mode operations require
          a mapping before they will touch the destination), and{' '}
          <em>Sync Subscriptions</em> below (server-scope subscriptions
          expand to per-library pairs at poll time by reading this
          table).
        </p>
        <p className="help" style={{ marginBottom: 8, fontSize: 13 }}>
          <strong>What this is not:</strong> mapping is a state file,
          not a job. Saving a row here does not move any data on its
          own — it just records the equivalence. To actually push
          data, run a job under <em>Run Job</em> or set up a
          subscription in the next tab.
        </p>
        <p className="help" style={{ marginBottom: 12, fontSize: 13 }}>
          <strong>How to use it:</strong> pick a source + destination
          server below. Both library columns populate immediately.
          Click <strong>Auto-match</strong> to fill in suggested
          pairings, or click a source library card then a destination
          library card to set the pairing yourself. Useful when
          multiple libraries share a type (e.g. Music + Audiobooks
          both <code>type=artist</code>) and only you know which
          should pair with which.
        </p>
        <div style={{ display: 'flex', gap: 14, alignItems: 'center', flexWrap: 'wrap' }}>
          <label style={{ display: 'flex', flexDirection: 'column', gap: 2, fontSize: 12 }}>
            <span><strong>Source server</strong></span>
            <select
              value={sourceServerId}
              onChange={(e) => setSourceServerId(e.target.value)}
              style={{ minWidth: 220 }}
            >
              <option value="">— pick a source —</option>
              {servers.map((s) => (
                <option key={s.id} value={s.id}>{serverLabel(s.id)}</option>
              ))}
            </select>
          </label>
          <span style={{ fontSize: 18, paddingTop: 18 }}>→</span>
          <label style={{ display: 'flex', flexDirection: 'column', gap: 2, fontSize: 12 }}>
            <span><strong>Destination server</strong></span>
            <select
              value={destServerId}
              onChange={(e) => setDestServerId(e.target.value)}
              style={{ minWidth: 220 }}
            >
              <option value="">— pick a destination —</option>
              {servers
                .filter((s) => s.id !== sourceServerId)
                .map((s) => (
                  <option key={s.id} value={s.id}>{serverLabel(s.id)}</option>
                ))}
            </select>
          </label>
          <div style={{ display: 'flex', gap: 8, paddingTop: 18 }}>
            <button
              type="button"
              onClick={() => void runAutomap(false)}
              disabled={!pickValid || busy}
              title="Compute auto-match suggestions and overlay them on the two columns. Suggestions don't save until you click them or use the persist button."
            >
              {busy ? 'Working…' : 'Auto-match (preview)'}
            </button>
            <button
              type="button"
              onClick={() => void runAutomap(true)}
              disabled={!pickValid || busy}
              title="Compute and persist every suggestion that crosses the auto-apply threshold (50%). Lower-confidence ones still need a manual click."
            >
              {busy ? 'Working…' : 'Auto-match + save high-confidence'}
            </button>
            <button
              type="button"
              className="danger"
              onClick={() => void invalidateAuto()}
              disabled={!pickValid || busy}
              title="Drop every auto-suggested mapping between these two servers. Operator-confirmed rows are preserved."
            >
              Drop auto rows
            </button>
          </div>
        </div>
        {pickValid && sourcesNote && (
          <p className="help" style={{ marginTop: 12, fontSize: 11, color: 'var(--text-dim)' }}>
            {sourcesNote}{' '}
            {(sourceLibs.some((l) => l.source === 'live')
              || destLibs.some((l) => l.source === 'live')) && (
              <span>
                One or both servers' libraries came from live API calls because
                the mirror DB hasn't been synced yet. Hit{' '}
                <strong>Servers &rsaquo; Overview &rsaquo; Sync all mirrors</strong>{' '}
                for a faster Auto-match (mirror reads are instant).
              </span>
            )}
          </p>
        )}
      </div>

      {pickValid && (sourceLibs.length > 0 || destLibs.length > 0) && (
        <div className="panel">
          <div
            style={{
              display: 'grid',
              gridTemplateColumns: '1fr 1fr',
              gap: 16,
            }}
          >
            {/* ─── Source column ─── */}
            <div>
              <h4 style={{ marginTop: 0, marginBottom: 8 }}>
                {serverLabel(sourceServerId)} ({sourceLibs.length} libraries)
              </h4>
              {sourceLibs.length === 0 ? (
                <div className="empty" style={{ fontSize: 12 }}>
                  No libraries visible. The server may not yet be reachable —
                  try Refresh on the Servers tab.
                </div>
              ) : (
                <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                  {sourceLibs.map((lib) => {
                    const m = mappingBySourceId[lib.library_id];
                    const sug = suggestions[lib.library_id];
                    const selected = pendingSource?.library_id === lib.library_id;
                    const mapped = !!(m && m.dest_library_id);
                    const skipped = !!(m && m.source === 'operator' && !m.dest_library_id);
                    const suggested = !mapped && !!sug;
                    const pair = pairColors.bySource[lib.library_id];
                    const badge = stateBadgeFor(lib);
                    return (
                      <div
                        key={lib.library_id}
                        onClick={() => onClickSource(lib)}
                        style={sideCardStyle({
                          selected, mapped, suggested,
                          pairColor: pair?.color,
                        })}
                        title={
                          selected
                            ? 'Click again to deselect, or click a destination library to map to.'
                            : mapped
                              ? `Currently mapped → ${m?.dest_library_name || m?.dest_library_id}. Click to remap.`
                              : sug
                                ? `Auto-match suggests → ${sug.dest_library.library_name} (${confidencePct(sug.confidence)}%, ${tierLabel(sug.tier)}). Click to select.`
                                : 'Click to select; then click a destination library on the right to map.'
                        }
                      >
                        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 8 }}>
                          <div style={{ flex: 1, minWidth: 0, display: 'flex', alignItems: 'center', gap: 6 }}>
                            {pair && (
                              <span
                                title={`Auto-match pair ${pair.index} — this source is suggested to be matched with the destination library highlighted in the same colour on the right.`}
                                style={{
                                  display: 'inline-flex',
                                  alignItems: 'center',
                                  justifyContent: 'center',
                                  width: 20, height: 20,
                                  borderRadius: '50%',
                                  background: pair.color,
                                  color: '#fff',
                                  fontSize: 10,
                                  fontWeight: 600,
                                  flexShrink: 0,
                                }}
                              >
                                {pair.index}
                              </span>
                            )}
                            <div style={{ flex: 1, minWidth: 0 }}>
                              <strong>{lib.library_name}</strong>
                              <div style={{ fontSize: 10, color: 'var(--text-dim)' }}>
                                {lib.library_type || 'unknown'} ·{' '}
                                {lib.item_count.toLocaleString()} item(s)
                              </div>
                            </div>
                          </div>
                          {badge && (
                            <span className={`tag ${badge.cls}`} style={{ fontSize: 10 }}>
                              {badge.label}
                            </span>
                          )}
                        </div>
                        {/* Inline action row for THIS source. */}
                        <div style={{ marginTop: 6, display: 'flex', gap: 4, flexWrap: 'wrap' }}>
                          {mapped && (
                            <span style={{ fontSize: 11 }}>
                              → <strong>{m?.dest_library_name || m?.dest_library_id}</strong>
                            </span>
                          )}
                          {skipped && (
                            <span style={{ fontSize: 11, color: 'var(--text-dim)' }}>
                              (skip on transfer)
                            </span>
                          )}
                          {sug && !mapped && (
                            <span style={{ fontSize: 11, color: 'var(--text-dim)' }}>
                              suggests → <em>{sug.dest_library.library_name}</em>
                            </span>
                          )}
                          <span style={{ flex: 1 }} />
                          <button
                            type="button"
                            onClick={(e) => { e.stopPropagation(); void skipSource(lib); }}
                            disabled={busy}
                            style={{ fontSize: 10, padding: '2px 6px' }}
                            title="Mark this source library as 'skip on transfer'."
                          >
                            Skip
                          </button>
                          {m && (
                            <button
                              type="button"
                              onClick={(e) => { e.stopPropagation(); void resetSource(lib); }}
                              disabled={busy}
                              className="danger"
                              style={{ fontSize: 10, padding: '2px 6px' }}
                              title="Drop the saved mapping for this row."
                            >
                              Reset
                            </button>
                          )}
                        </div>
                      </div>
                    );
                  })}
                </div>
              )}
            </div>

            {/* ─── Destination column ─── */}
            <div>
              <h4 style={{ marginTop: 0, marginBottom: 8 }}>
                {serverLabel(destServerId)} ({destLibs.length} libraries)
              </h4>
              {destLibs.length === 0 ? (
                <div className="empty" style={{ fontSize: 12 }}>
                  No libraries visible. The server may not yet be reachable —
                  try Refresh on the Servers tab.
                </div>
              ) : (
                <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                  {destLibs.map((lib) => {
                    const incoming = mappingsByDestId[lib.library_id];
                    const incomingName = incoming?.source_library_name
                      || incoming?.source_library_id;
                    // Suggestions: this dest is currently SUGGESTED for which sources?
                    const suggestedFrom = sourceLibs.find((s) => {
                      const sug = suggestions[s.library_id];
                      return sug?.dest_library.library_id === lib.library_id
                        && !mappingBySourceId[s.library_id];
                    });
                    // Highlight the pending-dest while the
                    // operator stages a manual pair, using the same
                    // selected style as a pending source card.
                    const isPendingDest = pendingDest?.library_id === lib.library_id;
                    const pair = pairColors.byDest[lib.library_id];
                    return (
                      <div
                        key={lib.library_id}
                        onClick={() => onClickDest(lib)}
                        style={sideCardStyle({
                          selected: isPendingDest,
                          mapped: !!incoming && !isPendingDest,
                          suggested: !!suggestedFrom && !incoming && !isPendingDest,
                          pairColor: pair?.color,
                        })}
                        title={
                          isPendingDest
                            ? `Pending: ${pendingSource?.library_name} → ${lib.library_name}. Click Confirm in the banner to save, or click a different destination to change.`
                            : pendingSource
                              ? `Click to preview mapping ${pendingSource.library_name} → ${lib.library_name}. Nothing saves until you click Confirm.`
                              : incoming
                                ? `Currently the destination for ${incomingName}.`
                                : suggestedFrom
                                  ? `Auto-match suggests this is the destination for ${suggestedFrom.library_name}. Click a source on the left to start mapping.`
                                  : `Click a source library on the left first, then this card to start a mapping preview.`
                        }
                      >
                        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 8 }}>
                          <div style={{ flex: 1, minWidth: 0, display: 'flex', alignItems: 'center', gap: 6 }}>
                            {pair && (
                              <span
                                title={`Auto-match pair ${pair.index} — this destination is suggested for the source library highlighted in the same colour on the left.`}
                                style={{
                                  display: 'inline-flex',
                                  alignItems: 'center',
                                  justifyContent: 'center',
                                  width: 20, height: 20,
                                  borderRadius: '50%',
                                  background: pair.color,
                                  color: '#fff',
                                  fontSize: 10,
                                  fontWeight: 600,
                                  flexShrink: 0,
                                }}
                              >
                                {pair.index}
                              </span>
                            )}
                            <div style={{ flex: 1, minWidth: 0 }}>
                              <strong>{lib.library_name}</strong>
                              <div style={{ fontSize: 10, color: 'var(--text-dim)' }}>
                                {lib.library_type || 'unknown'} ·{' '}
                                {lib.item_count.toLocaleString()} item(s)
                              </div>
                            </div>
                          </div>
                          {incoming && (
                            <span className="tag good" style={{ fontSize: 10 }}>
                              mapped
                            </span>
                          )}
                          {!incoming && suggestedFrom && (
                            <span className="tag phase" style={{ fontSize: 10 }}>
                              suggested
                            </span>
                          )}
                        </div>
                        {incoming && (
                          <div style={{ marginTop: 4, fontSize: 11 }}>
                            ← <strong>{incomingName}</strong>
                          </div>
                        )}
                        {!incoming && suggestedFrom && (
                          <div style={{ marginTop: 4, fontSize: 11, color: 'var(--text-dim)' }}>
                            ← <em>{suggestedFrom.library_name}</em> (suggested)
                          </div>
                        )}
                      </div>
                    );
                  })}
                </div>
              )}
            </div>
          </div>

          {/* Confirm/Cancel banner for manual mapping.
              No save happens until the operator clicks Confirm. The
              banner morphs between two states:
                * Source selected, dest still pending → "click a destination"
                * Both selected → "Source → Dest [Confirm] [Cancel]"
              An operator can re-click source or dest cards to adjust
              the preview without losing the staged pair. */}
          {pendingSource && !pendingDest && (
            <div
              className="banner info"
              style={{ marginTop: 12, display: 'flex', alignItems: 'center', gap: 8 }}
            >
              <strong>Pending source:</strong>
              <span>{pendingSource.library_name}</span>
              <span style={{ color: 'var(--text-dim)' }}>
                — click a destination library on the right to preview the mapping
                (nothing saves until you click Confirm). Use <em>Skip</em> on the
                source card to mark it skip-on-transfer instead.
              </span>
              <button
                type="button"
                onClick={cancelPending}
                style={{ marginLeft: 'auto', fontSize: 11 }}
              >
                Cancel
              </button>
            </div>
          )}
          {pendingSource && pendingDest && (
            <div
              className="banner info"
              style={{
                marginTop: 12,
                display: 'flex',
                alignItems: 'center',
                gap: 8,
                background: 'var(--bg-panel-alt, rgba(74,122,252,0.10))',
                border: '1px solid var(--accent, #4a7afc)',
                padding: 10,
              }}
            >
              <strong>Preview:</strong>
              <span>{pendingSource.library_name}</span>
              <span>→</span>
              <strong>{pendingDest.library_name}</strong>
              <span style={{ color: 'var(--text-dim)', fontSize: 11 }}>
                Not saved yet. Click Confirm to persist, or pick a different card
                to change the preview.
              </span>
              <span style={{ marginLeft: 'auto', display: 'flex', gap: 6 }}>
                <button
                  type="button"
                  className="primary"
                  onClick={() => void confirmPending()}
                  disabled={busy}
                  title={`Save ${pendingSource.library_name} → ${pendingDest.library_name} as an operator-confirmed mapping.`}
                >
                  Confirm mapping
                </button>
                <button
                  type="button"
                  onClick={cancelPending}
                  disabled={busy}
                  style={{ fontSize: 11 }}
                >
                  Cancel
                </button>
              </span>
            </div>
          )}
        </div>
      )}

      {pickValid && sourceLibs.length === 0 && destLibs.length === 0 && (
        <div className="empty" style={{ fontSize: 13 }}>
          Neither server returned any libraries. Make sure both are
          reachable from the Servers tab (the row's ping should be
          green). Library Mapping needs at least one library on each
          side to be useful.
        </div>
      )}
    </div>
  );
}
