// Run Job > Library Mapping for this run.
//
// Per-run-only library mapping editor with the same two-column
// click-to-link UX as Server Syncing > Library Mapping
// (LibraryMappingTab.tsx). Edits in this panel build a temporary
// per-run map that ships with the job submission payload as
// ``library_mapping_overrides``; the SHARED Library Mapping table
// is NEVER touched here. Server Syncing's two-column editor is the
// place to declare permanent mappings; this is the place to declare
// one-off ones for a single run.
//
// Why the two-column shape rather than a per-row dropdown:
//   * Operators already know this UX from Server Syncing >
//     Library Mapping. Same affordances, same mental model.
//   * Source-side data may be a snapshot's library list (when the
//     source server is offline), not the current source server's
//     live libraries. The two-column layout reads identically
//     whether the source data is live or captured.
//   * Click → click → Confirm is more deliberate than a dropdown
//     for the destructive case ("I'm declaring a per-run mapping
//     that will route this library here").
//
// Considered an advanced option:
//   * Collapsed by default. Summary line shows the active-pair
//     count.
//   * The cross-backend Replace refusal banner stays OUTSIDE the
//     collapsible because it actively blocks Submit and the
//     operator should never miss it.

import { useEffect, useMemo, useState } from 'react';
import { api } from '../api';
import type { LeafCounts, LibraryDescriptor } from '../api';


interface SideLib {
  library_id: string;
  library_name: string;
  library_type: string;
  item_count: number;
  // Per-libtype leaf counts. Named-key LeafCounts (not a loose
  // Record<string, number>) so a backend key rename surfaces as a
  // type error rather than silently rendering nothing.
  leaf_counts?: LeafCounts;
  source: 'mirror' | 'live';
}


// Render a richer per-library
// counts line so the operator sees the cost-of-move at a glance:
//
//   - Movie    → "1,205 movies · 42 collections · 7 playlists"
//   - Show     → "12 shows · 87 seasons · 312 episodes · 3 playlists"
//   - Artist   → "120 artists · 850 albums · 4,500 tracks · 14 playlists"
//   - Anything else → top-level count + (when present) playlists.
//
// All leaf counts are optional. Missing entries are silently
// dropped from the rendered line so an older Refresh
// doesn't render "0 tracks" noise. The playlists key
// comes from playlist_cache.db's primary_library_id index, so it
// only shows up when the playlist cache has been warmed for that
// server.
function _fmtCounts(
  libtype: string,
  topLevel: number,
  leaf: LeafCounts | undefined,
): string {
  const parts: string[] = [];
  const fmt = (n: number) => n.toLocaleString();
  const t = (libtype || '').toLowerCase();
  if (t === 'movie') {
    parts.push(`${fmt(topLevel)} movie${topLevel === 1 ? '' : 's'}`);
    if (leaf?.collections) {
      parts.push(`${fmt(leaf.collections)} collection${leaf.collections === 1 ? '' : 's'}`);
    }
  } else if (t === 'show') {
    parts.push(`${fmt(topLevel)} show${topLevel === 1 ? '' : 's'}`);
    if (leaf?.seasons) {
      parts.push(`${fmt(leaf.seasons)} season${leaf.seasons === 1 ? '' : 's'}`);
    }
    if (leaf?.episodes) {
      parts.push(`${fmt(leaf.episodes)} episode${leaf.episodes === 1 ? '' : 's'}`);
    }
  } else if (t === 'artist') {
    parts.push(`${fmt(topLevel)} artist${topLevel === 1 ? '' : 's'}`);
    if (leaf?.albums) {
      parts.push(`${fmt(leaf.albums)} album${leaf.albums === 1 ? '' : 's'}`);
    }
    if (leaf?.tracks) {
      parts.push(`${fmt(leaf.tracks)} track${leaf.tracks === 1 ? '' : 's'}`);
    }
  } else if (topLevel > 0) {
    parts.push(`${fmt(topLevel)} item${topLevel === 1 ? '' : 's'}`);
  }
  // Playlists per library (any libtype) — surfaces only when the
  // playlist cache has been warmed; comes from
  // playlist_cache.db.count_per_library on the live side or
  // snapshot.db's playlists table on the snapshot side.
  if (leaf?.playlists) {
    parts.push(`${fmt(leaf.playlists)} playlist${leaf.playlists === 1 ? '' : 's'}`);
  }
  return parts.join(' · ');
}

interface MappingPreflightResult {
  same_server: boolean;
  cross_backend_replace_refusal: string | null;
  library_warnings: Array<{
    library: string;
    status: 'unmapped' | 'operator_skip' | 'auto_unconfirmed' | 'per_run_route' | 'per_run_skip';
    detail: string;
  }>;
  any_unmapped: boolean;
}

interface Props {
  enabled: boolean;
  // The effective source server id for this run. For restore-from-
  // snapshot the parent derives this from the snapshot's captured
  // server_id; for direct + restore-from-file it comes from the
  // form's source picker.
  sourceServerId: string;
  // Destination server ids (multi-dest fan-out is allowed; the
  // editor displays the FIRST destination's library list inline
  // and notes the others).
  destServerIds: string[];
  // The libraries the operator has ticked for migration. Used to
  // visually distinguish "active" source-side libraries from
  // "available but not selected this run."
  selectedLibraryNames: string[];
  // The full source-side library inventory. For restore-from-
  // snapshot mode the parent passes the snapshot's libraries
  // (just names); for direct mode it passes the live source
  // server's LibraryDescriptor list.
  sourceLibraries: LibraryDescriptor[];
  // The "Ignore library mapping" checkbox state lives in the
  // parent because Submit's payload reads it.
  ignoreLibraryMapping: boolean;
  onIgnoreLibraryMappingChange: (next: boolean) => void;
  // Per-run mapping map (source library name → destination library
  // name; empty string means explicit per-run skip). Lives in the
  // parent so Submit can read it into the payload.
  libraryMappingOverrides: Record<string, string>;
  onLibraryMappingOverridesChange: (next: Record<string, string>) => void;
  // Parent's preflight result (already fetched). Reused so we don't
  // double-fetch; banners render at the bottom of this panel.
  mappingPreflight: MappingPreflightResult | null;
  // Restore mode drives whether auto-unconfirmed warnings show.
  restoreMode: 'merge' | 'replace';
  // When source is a snapshot (rather than a live
  // server), the source-side counts reflect what the SNAPSHOT
  // captured - which may be less than the live library has now
  // (e.g. a snapshot that captured only watched items). The panel
  // surfaces a reminder so operators don't read the smaller counts
  // as a bug.
  sourceIsSnapshot: boolean;
}

export function RunJobLibraryMappingPanel(props: Props) {
  const {
    enabled, sourceServerId, destServerIds, selectedLibraryNames,
    sourceLibraries, ignoreLibraryMapping, onIgnoreLibraryMappingChange,
    libraryMappingOverrides, onLibraryMappingOverridesChange,
    mappingPreflight, restoreMode, sourceIsSnapshot,
  } = props;

  const destServerId = destServerIds[0] || '';

  // Destination-side library list fetched from /api/library-mapping/
  // sides (same endpoint the Server Syncing editor uses). We ignore
  // the source_libraries field of the response — we already have a
  // richer source-side list from the parent (which may be the
  // snapshot's captured library list).
  const [destLibs, setDestLibs] = useState<SideLib[]>([]);
  const [loading, setLoading] = useState<boolean>(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  // Click-to-link pending pair (matches LibraryMappingTab's flow).
  const [pendingSource, setPendingSource] = useState<string | null>(null);
  const [pendingDest, setPendingDest] = useState<SideLib | null>(null);
  // Collapsed by default per operator note: this is an advanced
  // option used for one-off runs (e.g. source server offline so
  // operator can't set up a permanent mapping in Server Syncing).
  const [expanded, setExpanded] = useState<boolean>(false);
  const [info, setInfo] = useState<string | null>(null);

  // Fetch destination libraries whenever (source, dest) change.
  useEffect(() => {
    if (!enabled || !sourceServerId || !destServerId
        || sourceServerId === destServerId) {
      setDestLibs([]);
      setLoadError(null);
      return;
    }
    let cancelled = false;
    setLoading(true);
    setLoadError(null);
    api.libraryMappingSides(sourceServerId, destServerId).then(
      (r) => { if (!cancelled) setDestLibs(r.dest_libraries); },
      (e) => { if (!cancelled) setLoadError(`Could not load destination libraries: ${(e as Error).message}`); },
    ).finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [enabled, sourceServerId, destServerId]);

  // Index for quick lookups while rendering.
  const overrideKeys = useMemo(
    () => new Set(Object.keys(libraryMappingOverrides || {})),
    [libraryMappingOverrides],
  );

  // Set of destination library names currently used by an override
  // (for visually marking dest cards as "taken" on the right column).
  const destOverridesByName = useMemo<Record<string, string>>(() => {
    const m: Record<string, string> = {};
    for (const [src, dst] of Object.entries(libraryMappingOverrides || {})) {
      if (dst) m[dst.toLowerCase()] = src;
    }
    return m;
  }, [libraryMappingOverrides]);

  const overrideCount = overrideKeys.size;

  // ── Action helpers ──────────────────────────────────────────────

  const setOverride = (sourceLibName: string, destLibName: string) => {
    const next = { ...(libraryMappingOverrides || {}) };
    next[sourceLibName] = destLibName;
    onLibraryMappingOverridesChange(next);
  };

  const setPerRunSkip = (sourceLibName: string) => {
    const next = { ...(libraryMappingOverrides || {}) };
    next[sourceLibName] = '';
    onLibraryMappingOverridesChange(next);
  };

  const clearOverride = (sourceLibName: string) => {
    const next = { ...(libraryMappingOverrides || {}) };
    delete next[sourceLibName];
    onLibraryMappingOverridesChange(next);
  };

  const clearAllOverrides = () => {
    onLibraryMappingOverridesChange({});
    setPendingSource(null);
    setPendingDest(null);
    setInfo(null);
  };

  const onClickSource = (libName: string) => {
    if (pendingSource === libName) {
      // Re-click clears the pending pair (start-over intent).
      setPendingSource(null);
      setPendingDest(null);
      setInfo(null);
      return;
    }
    setPendingSource(libName);
    setPendingDest(null);
    setInfo(
      `Selected source: ${libName}. Click a destination library on `
      + `the right to preview a per-run mapping — nothing applies `
      + `until you click Confirm.`,
    );
  };

  const onClickDest = (lib: SideLib) => {
    if (!pendingSource) {
      setInfo(
        'Click a source library on the left first, then a '
        + 'destination on the right to preview a per-run mapping.',
      );
      return;
    }
    if (pendingDest?.library_id === lib.library_id) {
      setPendingDest(null);
      setInfo(`Selected source: ${pendingSource}. Click a destination library to preview.`);
      return;
    }
    setPendingDest(lib);
    setInfo(
      `Preview: ${pendingSource} → ${lib.library_name}. Click Confirm `
      + `below to add this to the per-run map, or pick a different `
      + `destination to change the preview.`,
    );
  };

  const confirmPending = () => {
    if (!pendingSource || !pendingDest) return;
    setOverride(pendingSource, pendingDest.library_name);
    setInfo(`Per-run mapping added: ${pendingSource} → ${pendingDest.library_name}.`);
    setPendingSource(null);
    setPendingDest(null);
  };

  const cancelPending = () => {
    setPendingSource(null);
    setPendingDest(null);
    setInfo(null);
  };

  // Skip the pending source (no dest needed).
  const skipPendingSource = () => {
    if (!pendingSource) return;
    setPerRunSkip(pendingSource);
    setInfo(`Per-run skip set for ${pendingSource}.`);
    setPendingSource(null);
    setPendingDest(null);
  };

  // ── Style helpers (parallel to LibraryMappingTab's sideCardStyle) ─

  const sideCardStyle = (extras: {
    selected?: boolean;
    mapped?: boolean;
    pending?: boolean;
  }): React.CSSProperties => {
    let border = '1px solid var(--border, rgba(255,255,255,0.08))';
    let bg: string | undefined;
    if (extras.selected || extras.pending) {
      border = '2px solid var(--accent, #4a7afc)';
      bg = 'var(--bg-panel, rgba(74,122,252,0.08))';
    } else if (extras.mapped) {
      border = '1px solid var(--good, #4caf50)';
      bg = 'rgba(76,175,80,0.06)';
    }
    return {
      border, background: bg,
      padding: '6px 10px',
      borderRadius: 4,
      cursor: 'pointer',
      transition: 'all 0.1s ease',
    };
  };

  if (!enabled) return null;

  const tableDisabled = ignoreLibraryMapping
    || !destServerId
    || sourceServerId === destServerId;

  // Cross-backend refusal banner stays OUTSIDE the collapsible
  // because it actively blocks Submit. Operator should never have
  // to expand a section to see why their job is rejected.
  const refusal = mappingPreflight?.cross_backend_replace_refusal;

  return (
    <div className="panel" style={{ marginBottom: 12 }}>
      {refusal && (
        <div className="banner error" style={{ fontSize: 12, marginBottom: 8 }}>
          <strong>Cross-backend Replace refused.</strong>{' '}
          {refusal}{' '}
          You can declare an operator-confirmed mapping under{' '}
          <em>Server Syncing &rsaquo; Library Mapping</em>, or expand
          the advanced section below and set per-run mappings for
          each library you want to route.
        </div>
      )}

      <details open={expanded} onToggle={(e) => setExpanded((e.target as HTMLDetailsElement).open)}>
        <summary
          style={{
            cursor: 'pointer',
            fontSize: 13,
            padding: '6px 0',
            userSelect: 'none',
          }}
        >
          <strong>Library Mapping for this run</strong>{' '}
          <span style={{ color: 'var(--text-dim)', fontSize: 11 }}>
            (advanced &mdash;{' '}
            {overrideCount > 0
              ? `${overrideCount} per-run mapping${overrideCount === 1 ? '' : 's'}`
              : 'no per-run mappings'}
            {ignoreLibraryMapping ? ', mapping ignored' : ''}
            )
          </span>
        </summary>

        <div style={{ paddingTop: 8 }}>
          <div className="banner info" style={{ fontSize: 11, marginBottom: 8 }}>
            <strong>This run only.</strong> Pairs you build here do
            NOT save to the shared Library Mapping table. Use this
            when you need a one-off mapping &mdash; for example,
            restoring from a snapshot while the source server is
            offline so you can&apos;t open <em>Server Syncing
            &rsaquo; Library Mapping</em> to declare a permanent
            mapping. For ongoing equivalence between two servers,
            set up a real mapping there instead.
          </div>

          {sourceIsSnapshot && (
            <div
              className="banner"
              style={{
                fontSize: 11, marginBottom: 8,
                background: 'rgba(245, 166, 35, 0.08)',
                border: '1px solid rgba(245, 166, 35, 0.35)',
                color: 'var(--text, inherit)',
                padding: '8px 10px',
                borderRadius: 4,
              }}
            >
              <strong style={{ color: 'var(--warn, #f5a623)' }}>
                Source is a snapshot.
              </strong>{' '}
              The counts on the left reflect what THIS SNAPSHOT
              captured, not the live source library. A snapshot
              that captured only watched items will report smaller
              numbers than the destination&apos;s live count for
              the same library &mdash; that&apos;s expected, not a
              bug. The mapping editor still works correctly
              against whatever the snapshot carries.
            </div>
          )}

          <label
            style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 12, marginBottom: 8 }}
            title="When checked, the engine ignores BOTH the per-run mappings below and the saved Library Mapping table for this run, and falls back to exact-name matching between source and destination libraries."
          >
            <input
              type="checkbox"
              checked={ignoreLibraryMapping}
              onChange={(e) => onIgnoreLibraryMappingChange(e.target.checked)}
            />
            <span>
              <strong>Ignore library mapping</strong>
              <span style={{ color: 'var(--text-dim)', marginLeft: 6 }}>
                (use exact-name match only)
              </span>
            </span>
          </label>

          {loadError && (
            <div className="banner error" style={{ fontSize: 12, marginBottom: 8 }}>
              {loadError}
            </div>
          )}
          {info && (
            <div className="banner info" style={{ fontSize: 11, marginBottom: 8 }}>
              {info}
            </div>
          )}

          {!sourceServerId && (
            <div className="empty" style={{ fontSize: 12 }}>
              {sourceIsSnapshot
                ? 'Pick a snapshot above to see its captured library list.'
                : 'Pick a source server above to load its libraries.'}
            </div>
          )}

          {sourceServerId && !destServerId && (
            <div className="empty" style={{ fontSize: 12 }}>
              Pick a destination server above to load its libraries.
            </div>
          )}

          {sourceServerId && destServerId && sourceServerId === destServerId && (
            <p style={{ color: 'var(--text-dim)', fontSize: 11, marginTop: 0 }}>
              Same-server job: the mapping table is not consulted
              (exact-name match always wins for same-server transfers).
              Per-run mappings have no effect.
            </p>
          )}

          {sourceServerId && destServerId && sourceServerId !== destServerId && (
            <div style={{ opacity: tableDisabled ? 0.5 : 1, pointerEvents: tableDisabled ? 'none' : 'auto' }}>
              {sourceLibraries.length === 0 ? (
                <div className="empty" style={{ fontSize: 12 }}>
                  Source library list is empty &mdash; the form
                  hasn&apos;t loaded any libraries yet for this run.
                </div>
              ) : loading && destLibs.length === 0 ? (
                <div className="empty" style={{ fontSize: 12 }}>
                  Loading destination libraries…
                </div>
              ) : (
                <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16 }}>
                  {/* ─── Source column ─── */}
                  <div>
                    <h4
                      style={{ marginTop: 0, marginBottom: 8 }}
                      title={sourceIsSnapshot
                        ? 'Counts reflect what the snapshot captured, not the live source library. May be smaller than the destination side for the same library — that is expected.'
                        : 'Live source server library list. Counts are from the most recent library walk (Refresh on the Servers tab).'}
                    >
                      Source ({sourceLibraries.length}{' '}
                      {sourceLibraries.length === 1 ? 'library' : 'libraries'})
                      {sourceIsSnapshot && (
                        <span
                          style={{ marginLeft: 6, fontSize: 11, color: 'var(--warn, #f5a623)', fontWeight: 'normal' }}
                          title="Snapshot-side counts may be less than the live library — they reflect only what the snapshot captured."
                        >
                          (from snapshot)
                        </span>
                      )}
                    </h4>
                    <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                      {sourceLibraries.map((lib) => {
                        const overrideTo = libraryMappingOverrides[lib.name];
                        const isMapped = overrideTo !== undefined;
                        const isSkipped = overrideTo === '';
                        const isSelected = pendingSource === lib.name;
                        const isCheckedForRun = selectedLibraryNames.includes(lib.name);
                        return (
                          <div
                            key={lib.name}
                            onClick={() => onClickSource(lib.name)}
                            style={{
                              ...sideCardStyle({
                                selected: isSelected,
                                mapped: isMapped && !isSelected,
                              }),
                              opacity: isCheckedForRun ? 1 : 0.6,
                            }}
                            title={
                              isSelected
                                ? 'Click again to deselect, or click a destination library to preview a pair.'
                                : isMapped
                                  ? (isSkipped
                                    ? `Per-run skip set for ${lib.name}. Click to override or use Reset to drop the per-run mapping.`
                                    : `Per-run mapping: ${lib.name} → ${overrideTo}. Click to remap.`)
                                  : 'Click to select; then click a destination library on the right to set a per-run mapping.'
                            }
                          >
                            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 8 }}>
                              <div style={{ flex: 1, minWidth: 0 }}>
                                <strong>{lib.name}</strong>
                                <div style={{ fontSize: 10, color: 'var(--text-dim)' }}>
                                  {(() => {
                                    // Prefer richer
                                    // per-libtype counts. LibraryDescriptor
                                    // has leaf_counts populated by the
                                    // library walk; falls back to
                                    // top-level item count when missing.
                                    const counts = _fmtCounts(
                                      lib.type,
                                      lib.count,
                                      lib.leaf_counts,
                                    );
                                    return counts || (lib.type || 'unknown');
                                  })()}
                                  {!isCheckedForRun && (
                                    <> · <em>not selected for this run</em></>
                                  )}
                                </div>
                              </div>
                              {isMapped && !isSkipped && (
                                <span className="tag good" style={{ fontSize: 10 }}>per-run</span>
                              )}
                              {isSkipped && (
                                <span className="tag failed" style={{ fontSize: 10 }}>skip</span>
                              )}
                            </div>
                            {isMapped && !isSkipped && (
                              <div style={{ fontSize: 11, marginTop: 4 }}>
                                → <strong>{overrideTo}</strong>
                              </div>
                            )}
                            {isMapped && (
                              <div style={{ marginTop: 6, display: 'flex', gap: 4 }}>
                                <span style={{ flex: 1 }} />
                                <button
                                  type="button"
                                  onClick={(e) => { e.stopPropagation(); clearOverride(lib.name); }}
                                  style={{ fontSize: 10, padding: '2px 6px' }}
                                  title="Drop this row's per-run mapping; the engine falls back to the saved table for this library."
                                >
                                  Reset
                                </button>
                              </div>
                            )}
                          </div>
                        );
                      })}
                    </div>
                  </div>

                  {/* ─── Destination column ─── */}
                  <div>
                    <h4 style={{ marginTop: 0, marginBottom: 8 }}>
                      Destination ({destLibs.length}{' '}
                      {destLibs.length === 1 ? 'library' : 'libraries'})
                    </h4>
                    {destLibs.length === 0 ? (
                      <div className="empty" style={{ fontSize: 12 }}>
                        No destination libraries returned. Check that
                        the destination server is reachable on the
                        Servers tab.
                      </div>
                    ) : (
                      <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                        {destLibs.map((d) => {
                          const incomingSrc = destOverridesByName[d.library_name.toLowerCase()];
                          const isPending = pendingDest?.library_id === d.library_id;
                          return (
                            <div
                              key={d.library_id}
                              onClick={() => onClickDest(d)}
                              style={sideCardStyle({
                                pending: isPending,
                                mapped: !!incomingSrc && !isPending,
                              })}
                              title={
                                isPending
                                  ? `Pending: ${pendingSource} → ${d.library_name}. Click Confirm in the banner to add this to the per-run map.`
                                  : pendingSource
                                    ? `Click to preview pair: ${pendingSource} → ${d.library_name}.`
                                    : incomingSrc
                                      ? `Currently the per-run destination for ${incomingSrc}.`
                                      : 'Click a source library on the left first.'
                              }
                            >
                              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 8 }}>
                                <div style={{ flex: 1, minWidth: 0 }}>
                                  <strong>{d.library_name}</strong>
                                  <div style={{ fontSize: 10, color: 'var(--text-dim)' }}>
                                    {_fmtCounts(d.library_type, d.item_count, d.leaf_counts)
                                      || (d.library_type || 'unknown')}
                                  </div>
                                </div>
                                {incomingSrc && (
                                  <span className="tag good" style={{ fontSize: 10 }}>per-run</span>
                                )}
                              </div>
                              {incomingSrc && (
                                <div style={{ fontSize: 11, marginTop: 4 }}>
                                  ← <strong>{incomingSrc}</strong>
                                </div>
                              )}
                            </div>
                          );
                        })}
                      </div>
                    )}
                  </div>
                </div>
              )}

              {/* Pending-pair banner: click-to-link flow's Confirm step */}
              {pendingSource && !pendingDest && (
                <div
                  className="banner info"
                  style={{ marginTop: 12, display: 'flex', alignItems: 'center', gap: 8, fontSize: 12 }}
                >
                  <strong>Pending source:</strong>
                  <span>{pendingSource}</span>
                  <span style={{ color: 'var(--text-dim)' }}>
                    — click a destination on the right, or skip this
                    library for this run.
                  </span>
                  <span style={{ marginLeft: 'auto', display: 'flex', gap: 6 }}>
                    <button
                      type="button"
                      onClick={skipPendingSource}
                      style={{ fontSize: 11 }}
                      title="Mark this source library as 'skip for this run only'."
                    >
                      Skip this run
                    </button>
                    <button type="button" onClick={cancelPending} style={{ fontSize: 11 }}>
                      Cancel
                    </button>
                  </span>
                </div>
              )}
              {pendingSource && pendingDest && (
                <div
                  className="banner info"
                  style={{
                    marginTop: 12, display: 'flex', alignItems: 'center', gap: 8, fontSize: 12,
                    background: 'var(--bg-panel-alt, rgba(74,122,252,0.10))',
                    border: '1px solid var(--accent, #4a7afc)',
                    padding: 10,
                  }}
                >
                  <strong>Preview:</strong>
                  <span>{pendingSource}</span>
                  <span>→</span>
                  <strong>{pendingDest.library_name}</strong>
                  <span style={{ color: 'var(--text-dim)', fontSize: 11 }}>
                    Not applied yet. Click Confirm to add to the per-run
                    map, or pick a different card to change the preview.
                  </span>
                  <span style={{ marginLeft: 'auto', display: 'flex', gap: 6 }}>
                    <button
                      type="button"
                      className="primary"
                      onClick={confirmPending}
                      style={{ fontSize: 11 }}
                      title={`Add ${pendingSource} → ${pendingDest.library_name} to this run's mapping.`}
                    >
                      Confirm mapping
                    </button>
                    <button type="button" onClick={cancelPending} style={{ fontSize: 11 }}>
                      Cancel
                    </button>
                  </span>
                </div>
              )}

              {overrideCount > 0 && (
                <div style={{ marginTop: 8 }}>
                  <button
                    type="button"
                    onClick={clearAllOverrides}
                    style={{ fontSize: 11 }}
                    title="Clear every per-run mapping; everything falls back to the saved mapping table."
                  >
                    Clear all per-run mappings
                  </button>
                </div>
              )}

              {tableDisabled && ignoreLibraryMapping && (
                <p style={{ color: 'var(--text-dim)', fontSize: 11, marginTop: 6 }}>
                  Per-run editor disabled because <em>Ignore library
                  mapping</em> is on &mdash; this run uses exact-name
                  match regardless of what's in the per-run map or the
                  saved table.
                </p>
              )}
              {destServerIds.length > 1 && (
                <p style={{ color: 'var(--text-dim)', fontSize: 11, marginTop: 6 }}>
                  Per-run map shows the FIRST destination&apos;s library
                  list inline. The same pairs apply to all{' '}
                  {destServerIds.length} destinations in this fan-out
                  run, so the override target names must exist on every
                  destination.
                </p>
              )}
            </div>
          )}

          {!mappingPreflight?.same_server
           && mappingPreflight?.library_warnings
           && mappingPreflight.library_warnings.length > 0 && (
            <div
              className={`banner ${mappingPreflight.any_unmapped || restoreMode === 'replace' ? 'warning' : 'info'}`}
              style={{ marginTop: 10, fontSize: 12 }}
            >
              <strong>Mapping warnings ({mappingPreflight.library_warnings.length}):</strong>
              <ul style={{ marginTop: 6, marginBottom: 0, paddingLeft: 18 }}>
                {mappingPreflight.library_warnings.map((w) => (
                  <li key={w.library} style={{ marginBottom: 4 }}>
                    <strong>{w.library}</strong> ({w.status.replace(/_/g, ' ')}): {w.detail}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      </details>
    </div>
  );
}
