// Compact mode badge rendered in the Servers panel Overview row for
// each registered server. States:
//   [Mirror · 2h ago] - auto mode, mirror is current
//   [Mirror · Drifted] - auto mode, latest probe found drift
//   [Live] - always-live mode (no mirror reads)
//   [Mirror · Not synced] - auto mode but mirror is empty (cold start)
//
// Clicking the badge opens a small popover with:
//   - last sync timestamp
//   - drift event count last 7 days
//   - "Sync now" button
//   - mode override radio (Auto / Always-live / Use global)
//   - "Invalidate" button (admin-gated server-side)

import { useEffect, useRef, useState } from 'react';
import { api } from '../api';
import { pausableInterval } from '../utils/pausableInterval';
import { useConfirm } from './ConfirmModal';


export interface ServerMirrorStateRow {
  server_id: string;
  backend: string;
  first_sync_at: number | null;
  last_full_sync_at: number | null;
  last_drift_check_at: number | null;
  mode_override: 'auto' | 'always-live' | null;
  url_fingerprint: string | null;
  token_hash: string | null;
  item_count: number;
  last_drift_event_at: number | null;
}


export interface ServerMirrorBadgeProps {
  state: ServerMirrorStateRow;
  globalMode: 'auto' | 'always-live';
  onChange: () => void;
}


function formatAge(ts: number | null): string {
  if (ts === null) return 'never';
  const now = Date.now() / 1000;
  const diff = now - ts;
  if (diff < 60) return 'just now';
  if (diff < 3600) return `${Math.round(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.round(diff / 3600)}h ago`;
  return `${Math.round(diff / 86400)}d ago`;
}


export function ServerMirrorBadge({
  state,
  globalMode,
  onChange,
}: ServerMirrorBadgeProps) {
  const [open, setOpen] = useState<boolean>(false);
  const [busy, setBusy] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);

  const effectiveMode = state.mode_override || globalMode;
  const hasItems = state.item_count > 0;

  let label: string;
  let cls: string;
  if (effectiveMode === 'always-live') {
    label = 'Live';
    cls = 'badge live';
  } else if (!hasItems) {
    label = 'Mirror · Not synced';
    cls = 'badge mirror-empty';
  } else if (
    state.last_drift_event_at !== null
    && state.last_full_sync_at !== null
    && state.last_drift_event_at > state.last_full_sync_at
  ) {
    label = 'Mirror · Drifted';
    cls = 'badge mirror-drifted';
  } else {
    label = `Mirror · ${formatAge(state.last_full_sync_at)}`;
    cls = 'badge mirror-fresh';
  }

  const [syncStatus, setSyncStatus] = useState<string | null>(null);
  // FECORE-12: hold the active poll disposer so an in-flight poll is
  // cancelled if the component unmounts before its 5 ticks elapse.
  const stopPollRef = useRef<(() => void) | null>(null);
  useEffect(() => () => stopPollRef.current?.(), []);
  const doSync = async () => {
    setBusy(true);
    setError(null);
    setSyncStatus(null);
    try {
      // The endpoint launches a background walker and returns
      // immediately. ``status`` is 'started' (new walker) or
      // 'in_progress' (a walker was already running for this
      // server). Surface that so the operator sees the click did
      // something even though the badge can't update mid-flight.
      const r = await api.syncServerMirror(state.server_id);
      setSyncStatus(
        r.status === 'in_progress'
          ? 'A sync is already running for this server. The badge will update when it finishes.'
          : 'Sync started in the background. The badge will update when it finishes.',
      );
      onChange();
      // Poll the parent's state refresh a few times so the operator
      // sees the new ``last_full_sync_at`` as soon as the walker
      // finishes (rather than waiting for the panel's own poll
      // cadence). 5 polls at 2s intervals covers the common
      // "small library, walker finishes in seconds" case.
      let i = 0;
      // FECORE-12: cancel any previous poll, then store the new disposer
      // in the ref so unmount cleanup can stop it mid-flight.
      stopPollRef.current?.();
      stopPollRef.current = pausableInterval(() => {
        onChange();
        i += 1;
        if (i >= 5) {
          stopPollRef.current?.();
          stopPollRef.current = null;
        }
      }, 2000);
    } catch (e) {
      setError((e as Error).message || 'Sync failed.');
    } finally {
      setBusy(false);
    }
  };

  const doSetMode = async (mode: 'auto' | 'always-live' | null) => {
    setBusy(true);
    setError(null);
    try {
      await api.setServerMirrorMode(state.server_id, mode);
      onChange();
    } catch (e) {
      setError((e as Error).message || 'Mode change failed.');
    } finally {
      setBusy(false);
    }
  };

  const confirm = useConfirm();
  const doInvalidate = async () => {
    if (!(await confirm({
      body: `Invalidate mirror for ${state.server_id}? Next job pays cold-start cost.`,
      danger: true,
    }))) return;
    setBusy(true);
    setError(null);
    try {
      await api.invalidateServerMirror(state.server_id);
      onChange();
    } catch (e) {
      setError((e as Error).message || 'Invalidate failed.');
    } finally {
      setBusy(false);
    }
  };

  return (
    <span className="server-mirror-badge-wrap" style={{ position: 'relative' }}>
      <button
        type="button"
        className={cls}
        onClick={() => setOpen(!open)}
        aria-expanded={open}
        title={`Items: ${state.item_count} · Backend: ${state.backend}`}
      >
        {label}
      </button>
      {open && (
        <div
          className="server-mirror-badge-popover"
          role="dialog"
          aria-label={`Mirror controls for ${state.server_id}`}
        >
          <div className="row">
            <span>Items:</span>
            <strong>{state.item_count.toLocaleString()}</strong>
          </div>
          <div className="row">
            <span>Last sync:</span>
            <strong>{formatAge(state.last_full_sync_at)}</strong>
          </div>
          <div className="row">
            <span>Last drift:</span>
            <strong>{formatAge(state.last_drift_event_at)}</strong>
          </div>
          {error && (
            <div className="banner error" role="alert">
              {error}
            </div>
          )}
          {syncStatus && !error && (
            <div className="banner info" role="status">
              {syncStatus}
            </div>
          )}
          <fieldset>
            <legend>Mode</legend>
            <label>
              <input
                type="radio"
                name={`mirror-mode-${state.server_id}`}
                checked={state.mode_override === null}
                disabled={busy}
                onChange={() => doSetMode(null)}
              />
              Use global ({globalMode})
            </label>
            <label>
              <input
                type="radio"
                name={`mirror-mode-${state.server_id}`}
                checked={state.mode_override === 'auto'}
                disabled={busy}
                onChange={() => doSetMode('auto')}
              />
              Auto
            </label>
            <label>
              <input
                type="radio"
                name={`mirror-mode-${state.server_id}`}
                checked={state.mode_override === 'always-live'}
                disabled={busy}
                onChange={() => doSetMode('always-live')}
              />
              Always live
            </label>
          </fieldset>
          <div className="actions">
            <button type="button" disabled={busy} onClick={doSync}>
              Sync now
            </button>
            <button
              type="button"
              className="danger"
              disabled={busy}
              onClick={doInvalidate}
            >
              Invalidate
            </button>
            <button type="button" onClick={() => setOpen(false)}>
              Close
            </button>
          </div>
        </div>
      )}
    </span>
  );
}
