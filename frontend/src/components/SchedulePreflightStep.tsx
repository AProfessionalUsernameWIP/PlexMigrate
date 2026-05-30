// Schedule-save preflight step. Thin wrapper around
// CrossPlatformPreflightModal for the schedule create / edit flow.
//
// At Save time SchedulesPanel calls
// POST /api/schedules/cross-platform-preflight with the schedule's
// draft body. If verdict !== 'ok' the modal opens; on Continue the
// per-destination decisions get persisted into
// schedule.cross_platform_resolutions before the schedule create/edit
// is POSTed. Since schedules fire unattended, the end user's decisions
// MUST be made up-front (no end user at fire time to answer the modal).

import type {
  PreflightResponse,
  CrossPlatformPreflightAck,
} from '../api';
import { CrossPlatformPreflightModal } from './CrossPlatformPreflightModal';

interface Props {
  open: boolean;
  response: PreflightResponse | null;
  destinationLabels?: Record<string, string>;
  initialAcks?: Record<string, CrossPlatformPreflightAck>;
  onCancel: () => void;
  onContinue: (acks: Record<string, CrossPlatformPreflightAck>) => void;
}

export function SchedulePreflightStep(props: Props) {
  return <CrossPlatformPreflightModal {...props} />;
}
