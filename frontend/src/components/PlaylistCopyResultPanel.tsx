// Post-copy result surface. Shows the end user exactly what happened
// in the destination after they clicked Deploy:
//
// - Success / failure tone at the top
// - Counts: items_written / items_skipped_no_match / items_failed
// - Per-error list (errors[] from PlaylistCopyResult)
// - Wall-clock elapsed
//
// Items skipped for no-match are NOT errors — per Plan section 4.7
// they're informational ("couldn't find a matching item on the
// destination via GUID; this is normal cross-backend behavior").

import type { PlaylistCopyResult } from '../api';

// Each entry pairs a copy result with the source playlist's name so the
// multi-deploy result list can be labelled. `playlistName` may be empty
// for legacy single-result callers; in that case we just render "Copy
// result" without a per-playlist header.
export interface CopyResultEntry {
  playlistName?: string;
  // Source user the playlist came from. Shown in the multi-deploy
  // result header so the end user can disambiguate when the same
  // playlist name exists under multiple source users.
  sourceUsername?: string;
  // Destination user the copy targeted. Required for cartesian
  // multi-deploys where the same source playlist fans out to several
  // dest users; otherwise the end user can't tell which row failed.
  destUsername?: string;
  destServerLabel?: string;
  result: PlaylistCopyResult;
}

interface SingleProps {
  result: PlaylistCopyResult | null;
  destServerLabel?: string;
  results?: undefined;
  onDismiss: () => void;
}

interface MultiProps {
  result?: undefined;
  destServerLabel?: undefined;
  results: CopyResultEntry[];
  onDismiss: () => void;
}

type Props = SingleProps | MultiProps;

export function PlaylistCopyResultPanel(props: Props) {
  const { onDismiss } = props;
  if ('results' in props && props.results !== undefined) {
    if (props.results.length === 0) return null;
    return (
      <div className="panel" style={{ marginTop: 12 }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', gap: 12, marginBottom: 8 }}>
          <strong style={{ fontSize: 14 }}>Copy results ({props.results.length})</strong>
          <button onClick={onDismiss} style={{ fontSize: 11 }}>Dismiss all</button>
        </div>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
          {props.results.map((entry, i) => (
            <SingleResult
              key={`${entry.destServerLabel}-${entry.playlistName}-${entry.sourceUsername}-${i}`}
              result={entry.result}
              destServerLabel={entry.destServerLabel}
              playlistName={entry.playlistName}
              sourceUsername={entry.sourceUsername}
              destUsername={entry.destUsername}
              embedded
            />
          ))}
        </div>
      </div>
    );
  }
  const single = (props as SingleProps).result;
  if (!single) return null;
  return (
    <SingleResult
      result={single}
      destServerLabel={(props as SingleProps).destServerLabel}
      onDismiss={onDismiss}
    />
  );
}

interface SingleResultProps {
  result: PlaylistCopyResult;
  destServerLabel?: string;
  playlistName?: string;
  sourceUsername?: string;
  destUsername?: string;
  onDismiss?: () => void;
  embedded?: boolean;
}

function SingleResult({ result, destServerLabel, playlistName, sourceUsername, destUsername, onDismiss, embedded }: SingleResultProps) {
  const tone = result.success
    ? { bg: 'rgba(34, 197, 94, 0.10)', border: 'var(--success, #16a34a)', label: 'Copy succeeded' }
    : { bg: 'rgba(239, 68, 68, 0.10)', border: 'var(--bad, #ef4444)', label: 'Copy failed' };
  return (
    <div
      className={embedded ? '' : 'panel'}
      style={{
        marginTop: embedded ? 0 : 12,
        background: tone.bg,
        border: `1px solid ${tone.border}`,
        padding: embedded ? '8px 10px' : undefined,
        borderRadius: embedded ? 4 : undefined,
      }}
    >
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', gap: 12, marginBottom: 8 }}>
        <strong style={{ color: tone.border, fontSize: 14 }}>
          {playlistName ? `${tone.label} — ${playlistName}` : tone.label}
          {(sourceUsername || destUsername) && (
            <span style={{ color: 'var(--text-dim)', fontSize: 11, fontWeight: 400, marginLeft: 6 }}>
              {sourceUsername && destUsername
                ? `(${sourceUsername} → ${destUsername})`
                : sourceUsername
                  ? `(from ${sourceUsername})`
                  : `(to ${destUsername})`}
            </span>
          )}
        </strong>
        {onDismiss && <button onClick={onDismiss} style={{ fontSize: 11 }}>Dismiss</button>}
      </div>

      <div style={{ fontSize: 12, marginBottom: 8 }}>
        <span style={{ color: 'var(--text-dim)' }}>
          {destServerLabel ? `On ${destServerLabel}. ` : ''}
        </span>
        Elapsed: <strong>{result.elapsed_seconds.toFixed(1)}s</strong>
        {result.new_playlist_id && (
          <>
            {' · '}
            <span style={{ color: 'var(--text-dim)' }}>New playlist id: </span>
            <code style={{ fontSize: 11 }}>{result.new_playlist_id}</code>
          </>
        )}
      </div>

      <div style={{ display: 'flex', gap: 16, fontSize: 12, marginBottom: 8 }}>
        <div>
          <span style={{ color: 'var(--text-dim)' }}>Written: </span>
          <strong style={{ color: 'var(--success, #16a34a)' }}>
            {result.items_written}
          </strong>
        </div>
        <div>
          <span style={{ color: 'var(--text-dim)' }}>Skipped (no match): </span>
          <strong>{result.items_skipped_no_match}</strong>
        </div>
        <div>
          <span style={{ color: 'var(--text-dim)' }}>Failed: </span>
          <strong style={{ color: result.items_failed > 0 ? 'var(--bad, #ef4444)' : undefined }}>
            {result.items_failed}
          </strong>
        </div>
      </div>

      {result.items_skipped_no_match > 0 && (
        <div style={{ fontSize: 11, color: 'var(--text-dim)', marginBottom: 8 }}>
          Items skipped because no matching destination row was found via GUID.
          This is normal cross-backend behavior when the destination's library
          doesn't carry the same identifier set.
        </div>
      )}

      {result.errors.length > 0 && (
        <div>
          <div style={{ fontSize: 12, color: 'var(--bad, #ef4444)', marginBottom: 4 }}>
            <strong>Errors:</strong>
          </div>
          <ul
            style={{
              listStyle: 'disc',
              paddingLeft: 22,
              margin: 0,
              fontSize: 11,
              maxHeight: 160,
              overflowY: 'auto',
            }}
          >
            {result.errors.map((e, i) => (
              <li key={`${i}-${String(e).slice(0, 32)}`} style={{ marginBottom: 2 }}>{e}</li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
