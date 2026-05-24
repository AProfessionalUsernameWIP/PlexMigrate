// Schedule resolution editor — opens from a schedule-list row's
// "Edit resolutions" action when the schedule has stored
// cross_platform_resolutions. Lets the end user update decisions
// per destination without re-walking the full schedule create flow.
//
// Reads the schedule's stored resolutions, re-runs the schedule
// preflight (POST /api/schedules/cross-platform-preflight) to get a
// fresh CrossPlatformPreflightReport per destination (the dest user
// roster may have drifted since the schedule was saved), opens the
// CrossPlatformPreflightModal with the stored decisions as the
// initial state, and on Continue PATCHes /api/schedules/{id}/resolutions.
//
// Saving without changes still calls the PATCH endpoint so the
// schedule's resolutions_status badge re-computes against the
// current dest roster.

import { api } from '../api';
import type {
  PreflightResponse,
  CrossPlatformPreflightAck,
  Schedule,
} from '../api';
import { CrossPlatformPreflightModal } from './CrossPlatformPreflightModal';
import { errorText } from '../utils/format';
import { useResourceQuery } from '../hooks/useResourceQuery';
import { Modal } from './Modal';

interface Props {
  open: boolean;
  schedule: Schedule;
  destinationLabels?: Record<string, string>;
  onClose: () => void;
  // Fires after a successful PATCH; the parent can refresh its
  // schedule list to pick up the new resolutions_status badge.
  onSaved: () => void;
}

export function ScheduleResolutionEditor({
  open,
  schedule,
  destinationLabels,
  onClose,
  onSaved,
}: Props) {
  // Re-runs the schedule preflight whenever the editor opens or the
  // schedule changes; skips the call entirely while closed.
  const { data: response, loading, error, setError } = useResourceQuery<PreflightResponse | null>(
    () => (
      open
        ? api.schedulesCrossPlatformPreflight(schedule as unknown as Record<string, unknown>)
        : Promise.resolve(null)
    ),
    [open, schedule],
    null,
  );

  if (!open) return null;

  if (loading) {
    return (
      <div
        style={{
          position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.55)',
          zIndex: 1000, display: 'flex', alignItems: 'center', justifyContent: 'center',
        }}
      >
        <div className="panel" style={{ padding: 20, fontSize: 13 }}>
          Loading preflight…
        </div>
      </div>
    );
  }

  if (error || !response) {
    return (
      <Modal onClose={onClose} align="center" width={480}>
          <div className="banner error" style={{ fontSize: 13 }}>
            Could not load preflight: {error || 'unknown error'}
          </div>
          <div style={{ marginTop: 12, display: 'flex', justifyContent: 'flex-end' }}>
            <button onClick={onClose}>Close</button>
          </div>
      </Modal>
    );
  }

  return (
    <CrossPlatformPreflightModal
      open={open}
      response={response}
      destinationLabels={destinationLabels}
      initialAcks={(schedule.cross_platform_resolutions as Record<string, CrossPlatformPreflightAck>) || undefined}
      onCancel={onClose}
      onContinue={async (acks) => {
        if (!schedule.id) {
          onClose();
          return;
        }
        try {
          await api.schedulesUpdateResolutions(schedule.id, { resolutions: acks });
          onSaved();
          onClose();
        } catch (e) {
          setError(errorText(e));
        }
      }}
    />
  );
}
