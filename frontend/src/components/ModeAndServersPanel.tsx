// ── Mode & Servers panel ─────────────────────────────────────────────
//
// Top-level form panel covering the job's mode (snapshot / restore /
// direct) and the source + destination server pickers. End user's
// first decisions on the form; everything below this panel is gated
// until enough server selections are present for the active mode.

import type { PingResult, ServerView } from '../api';
import { InfoTip } from './InfoTip';
import { ServerPicker } from './ServerPicker';

export type Mode = 'snapshot' | 'restore' | 'direct';

interface Props {
  mode: Mode;
  onModeChange: (m: Mode) => void;
  sourceServerName: string;
  onSourceServerChange: (id: string) => void;
  destServerNames: Set<string>;
  onDestServerNamesChange: (next: Set<string>) => void;
  workflowServers: ServerView[];
  pings: Record<string, PingResult>;
  // True when the active mode's selection prerequisites are
  // satisfied. Drives the "Pick a Source Server..." banner inside
  // this panel; the parent still owns the gated fieldset below.
  serversReady: boolean;
}

export function ModeAndServersPanel(props: Props) {
  const {
    mode,
    onModeChange,
    sourceServerName,
    onSourceServerChange,
    destServerNames,
    onDestServerNamesChange,
    workflowServers,
    pings,
    serversReady,
  } = props;
  return (
    <div className="panel">
      <h2>Mode &amp; Servers</h2>
      <label className="field">
        <span className="label">
          Operation
          <InfoTip topicId="job-modes" />
        </span>
        <span className="help">
          <strong>Snapshot</strong> writes a .db. <strong>Restore</strong> reads one back into Plex.
          <strong> Direct</strong> pipes source to destination in memory.
        </span>
        <select value={mode} onChange={(e) => onModeChange(e.target.value as Mode)}>
          <option data-testid="job-mode-snapshot" value="snapshot">Snapshot - save data from a Plex server</option>
          <option data-testid="job-mode-restore" value="restore">Restore - restore data to a Plex server</option>
          <option data-testid="job-mode-direct" value="direct">Direct transfer - read from one server, write to another</option>
        </select>
      </label>

      <div className="grid-2">
        {/* ── Source selector (left) ──────────────────────────── */}
        {mode === 'restore' ? (
          <div className="field">
            <span className="label">Source Server</span>
            <span className="help">
              Not applicable for import - the source is the export file(s) you pick below.
            </span>
          </div>
        ) : (
          <div className="field" data-testid="job-source-server-select">
            <span className="label">
              Source Server
              <InfoTip topicId="source-server" />
            </span>
            <span className="help">
              The Plex server this operation will read from. Status refreshes every 30 seconds.
            </span>
            <ServerPicker
              value={sourceServerName}
              onChange={(id) => onSourceServerChange(id)}
              servers={workflowServers}
              pings={pings}
              excludeIds={mode === 'direct' ? destServerNames : undefined}
            />
          </div>
        )}

        {/* ── Destination selector (right) ─────────────────────── */}
        {mode === 'snapshot' ? (
          <div className="field">
            <span className="label">Destination Server</span>
            <span className="help">
              Not applicable for snapshot - the snapshot will be saved to the configured output directory
              (see <strong>Settings</strong> or override below).
            </span>
          </div>
        ) : (
          <div className="field" data-testid="job-dest-server-select">
            <span className="label">
              Destination Server{destServerNames.size > 1 ? 's (Fan-out)' : 's'}
              <InfoTip topicId="destination-fanout" />
            </span>
            <span className="help">
              Where this job writes to. Select 2+ to fan-out (one source feeds many destinations in parallel).
            </span>
            <ServerPicker
              multi
              values={destServerNames}
              onMultiChange={onDestServerNamesChange}
              servers={workflowServers}
              pings={pings}
              excludeIds={mode === 'direct' && sourceServerName
                ? new Set([sourceServerName])
                : undefined}
            />
            {destServerNames.size > 1 && (
              <div className="banner info" style={{ marginTop: 8 }}>
                Fan-out enabled - this job will write to <strong>{destServerNames.size}</strong> destinations in parallel.
                Each destination has its own dashboard card, run-log directory, and error tracking.
              </div>
            )}
          </div>
        )}
      </div>

      {/* Selection-state indicator helps the user understand why the
          lower panels are disabled when they're disabled.            */}
      {!serversReady && (
        <div className="banner info" style={{ marginTop: 8 }}>
          {mode === 'snapshot' && 'Pick a Source Server to continue.'}
          {mode === 'restore' && 'Pick at least one Destination Server to continue.'}
          {mode === 'direct' && 'Pick a Source Server and at least one Destination Server to continue.'}
        </div>
      )}
    </div>
  );
}
