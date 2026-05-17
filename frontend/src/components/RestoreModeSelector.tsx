// Shared widget for choosing Merge vs Replace on any form that drives
// a restore or direct-transfer job.
//
// Three places use it (all on Run Job, Schedules, and Servers > Run
// Defaults): each owns its own state for ``mode`` /
// ``autoCaptureBeforeReplace`` and feeds the values in as props. The
// confirmation modal that gates Replace submission is a separate
// component (ReplaceConfirmModal); this selector is just the in-form
// widget.
//
// Naming pair (v0.13.x): Merge = additive (current default), Replace
// = point-in-time overwrite. Pros/cons live in the Help page's
// "How to Use" sub-tab and on the InfoTips below.

import { InfoTip } from './InfoTip';

export type RestoreMode = 'merge' | 'replace';
// v0.13.x: sub-strategy under Merge for the watch-count math.
//   higher = destination ends at max(stored, current). Idempotent;
//            the legacy default.
//   sum    = destination ends at current + stored. End user opt-in;
//            NOT idempotent across re-runs.
// Only consulted when mode === 'merge'; in 'replace' mode Plex's
// view counts are overwritten unconditionally.
export type MergeWatchStrategy = 'higher' | 'sum';

interface Props {
  mode: RestoreMode;
  autoCaptureBeforeReplace: boolean;
  // v0.13.x optional - omitting these (or providing them on a parent
  // that doesn't surface the sub-toggle) keeps the legacy single-row
  // Merge radio. Providing both renders the sub-radios under Merge.
  mergeWatchStrategy?: MergeWatchStrategy;
  onMergeWatchStrategyChange?: (strategy: MergeWatchStrategy) => void;
  onModeChange: (mode: RestoreMode) => void;
  onAutoCaptureChange: (enabled: boolean) => void;
  // Disambiguates the radio buttons when multiple selectors live on
  // the same page (e.g. Restore tab + Direct Transfer tab in the
  // same JobFormPanel). Each instance gets unique radio name + ids.
  idPrefix: string;
  // Optional: hide the auto-capture line. Used in places like
  // ServerAdvancedSettingsPanel where it's set globally and the
  // per-job form would override it anyway.
  hideAutoCapture?: boolean;
  // When true, the selector is read-only - both radios disabled and
  // the auto-capture checkbox can't be flipped. Used in the
  // confirmation modal's recap section.
  disabled?: boolean;
}

export function RestoreModeSelector({
  mode,
  autoCaptureBeforeReplace,
  mergeWatchStrategy,
  onMergeWatchStrategyChange,
  onModeChange,
  onAutoCaptureChange,
  idPrefix,
  hideAutoCapture = false,
  disabled = false,
}: Props) {
  const mergeId = `${idPrefix}-mode-merge`;
  const replaceId = `${idPrefix}-mode-replace`;
  const autoCaptureId = `${idPrefix}-auto-capture`;
  const radioName = `${idPrefix}-restore-mode`;
  const mwsName = `${idPrefix}-merge-watch-strategy`;
  const mwsHigherId = `${idPrefix}-mws-higher`;
  const mwsSumId = `${idPrefix}-mws-sum`;
  // Only render the sub-strategy block when the parent wired up the
  // controlled state. Keeps existing callers that don't care about
  // the sub-toggle (the confirmation modal recap, future call sites)
  // rendering the same single-row Merge radio as before.
  const showMergeStrategy =
    mergeWatchStrategy !== undefined && onMergeWatchStrategyChange !== undefined;

  return (
    <fieldset
      style={{
        border: '1px solid var(--border, #2d3548)',
        borderRadius: 6,
        padding: '10px 12px',
        margin: '8px 0',
      }}
    >
      <legend style={{ padding: '0 6px', fontSize: 12, color: 'var(--text-dim)' }}>
        Restoration mode
      </legend>

      <label htmlFor={mergeId} style={{ display: 'flex', alignItems: 'flex-start', gap: 8, marginBottom: 6 }}>
        <input
          id={mergeId}
          type="radio"
          name={radioName}
          value="merge"
          checked={mode === 'merge'}
          disabled={disabled}
          onChange={() => onModeChange('merge')}
          style={{ marginTop: 3 }}
        />
        <span style={{ flex: 1 }}>
          <strong>Merge Restore</strong>{' '}
          <span style={{ color: 'var(--text-dim)', fontSize: 12 }}>
            (default · safe · additive)
          </span>
          <InfoTip>
            <strong>Safe and idempotent.</strong> View counts only{' '}
            <em>increase</em>, ratings only set when the destination
            has none, playlists and collections create-or-append.
            Newer destination activity is preserved. Useless for true
            disaster recovery (your Jan 2 watches won't revert to a
            Jan 1 snapshot), but you can re-run it any time without
            losing data. This is the default for every restore.
          </InfoTip>
        </span>
      </label>

      {/* ── Merge sub-strategy: watch-count math (v0.13.x) ─────────
            Only shown when the parent passed both controlled props.
            Inset under the Merge radio so the visual hierarchy makes
            it clear this is a Merge-only sub-decision. Dimmed when
            mode !== 'merge' since the choice has no effect under
            Replace (which overwrites unconditionally). */}
      {showMergeStrategy && (
        <div
          style={{
            marginLeft: 26,
            marginBottom: 6,
            paddingLeft: 10,
            borderLeft: '2px solid var(--border, #2d3548)',
            opacity: mode === 'merge' ? 1 : 0.45,
            color: mode === 'merge' ? 'inherit' : 'var(--text-dim)',
          }}
        >
          <div style={{ fontSize: 12, color: 'var(--text-dim)', marginBottom: 4 }}>
            Watch-count math
            <InfoTip>
              Only consulted when Merge is selected (Replace overwrites
              unconditionally). <strong>Higher value</strong> is the
              legacy behaviour - the destination view count ends at
              <em> max(stored, current)</em>, so re-running the same
              job is a no-op. <strong>Combine totals</strong> adds the
              snapshot's count on top of what's already on the
              destination, so the destination ends at
              <em> current + stored</em>. Combine is NOT idempotent: a
              second run with the same snapshot doubles the count.
              Use Combine when the snapshot represents real plays on a
              different server that should contribute alongside the
              destination's own plays, not replace them.
            </InfoTip>
          </div>
          <label
            htmlFor={mwsHigherId}
            style={{ display: 'flex', alignItems: 'flex-start', gap: 6, marginBottom: 4, fontSize: 13 }}
          >
            <input
              id={mwsHigherId}
              type="radio"
              name={mwsName}
              value="higher"
              checked={mergeWatchStrategy === 'higher'}
              disabled={disabled || mode !== 'merge'}
              onChange={() => onMergeWatchStrategyChange!('higher')}
              style={{ marginTop: 3 }}
            />
            <span style={{ flex: 1 }}>
              <strong>Higher value</strong>{' '}
              <span style={{ color: 'var(--text-dim)', fontSize: 11 }}>
                (default · idempotent)
              </span>
              <br />
              <span style={{ color: 'var(--text-dim)', fontSize: 11 }}>
                Destination ends at the larger of stored or current.
                Re-running has no effect.
              </span>
            </span>
          </label>
          <label
            htmlFor={mwsSumId}
            style={{ display: 'flex', alignItems: 'flex-start', gap: 6, fontSize: 13 }}
          >
            <input
              id={mwsSumId}
              type="radio"
              name={mwsName}
              value="sum"
              checked={mergeWatchStrategy === 'sum'}
              disabled={disabled || mode !== 'merge'}
              onChange={() => onMergeWatchStrategyChange!('sum')}
              style={{ marginTop: 3 }}
            />
            <span style={{ flex: 1 }}>
              <strong>Combine totals</strong>{' '}
              <span style={{ color: 'var(--text-dim)', fontSize: 11 }}>
                (additive · re-runs double-count)
              </span>
              <br />
              <span style={{ color: 'var(--text-dim)', fontSize: 11 }}>
                Destination ends at current + stored. Only run once per
                snapshot.
              </span>
            </span>
          </label>
        </div>
      )}

      <label htmlFor={replaceId} style={{ display: 'flex', alignItems: 'flex-start', gap: 8 }}>
        <input
          id={replaceId}
          type="radio"
          name={radioName}
          value="replace"
          checked={mode === 'replace'}
          disabled={disabled}
          onChange={() => onModeChange('replace')}
          style={{ marginTop: 3 }}
        />
        <span style={{ flex: 1 }}>
          <strong style={{ color: 'var(--warn, #f5a623)' }}>Replace Restore</strong>{' '}
          <span style={{ color: 'var(--text-dim)', fontSize: 12 }}>
            (destructive · point-in-time)
          </span>
          <InfoTip>
            <strong>Destructive.</strong> Sets view counts and ratings
            to exactly the snapshot's value (resets them if currently
            higher). Playlists and collections lose members that
            aren't in the snapshot. The destination becomes a faithful
            copy of what the snapshot captured. Used for disaster
            recovery or "undo my last week" scenarios. Newer
            destination activity is overwritten. The typed-REPLACE
            confirmation appears on submit; the auto-capture safety
            belt (below) snapshots the destination first so you can
            roll back if you picked the wrong snapshot.
          </InfoTip>
        </span>
      </label>

      {!hideAutoCapture && (
        <label
          htmlFor={autoCaptureId}
          style={{
            display: 'flex',
            alignItems: 'flex-start',
            gap: 8,
            marginTop: 10,
            paddingLeft: 26,
            color: mode === 'replace' ? 'inherit' : 'var(--text-dim)',
          }}
        >
          <input
            id={autoCaptureId}
            type="checkbox"
            checked={autoCaptureBeforeReplace}
            disabled={disabled || mode !== 'replace'}
            onChange={(e) => onAutoCaptureChange(e.target.checked)}
            style={{ marginTop: 3 }}
          />
          <span style={{ flex: 1, fontSize: 13 }}>
            Capture destination snapshot before replacing{' '}
            <span style={{ color: 'var(--text-dim)' }}>(recommended)</span>
            <InfoTip>
              When enabled, a fresh snapshot of the destination is
              captured automatically <em>before</em> the Replace
              runs - so if you picked the wrong source snapshot, the
              auto-capture is your rollback point. The restore aborts
              if this pre-snapshot fails, on the theory that you'd
              rather not destroy data without a recovery point.
              Only consulted when mode is Replace.
            </InfoTip>
          </span>
        </label>
      )}
    </fieldset>
  );
}
