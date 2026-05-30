// Issue 8 (refactorplan.md): the ETR colour multiplier and the
// Restoration Summary row cap used to live in module-level `let`s.
// Mutating them after the settings load did not re-render React, so
// the dashboard could render with stale defaults. They are now
// DashboardPanel state surfaced through DashboardConfigContext; these
// tests assert a consumer reflects the loaded value after the async
// settings fetch resolves.

import { describe, expect, it, vi, beforeEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import { renderWithAuth } from '../test-helpers/auth';
import type {
  ContainerResult, DashboardFrame, DashboardState, JobPayload,
} from '../api';

// vi.mock is hoisted; the per-test settings payload has to be reachable
// from the factory, so it lives in a vi.hoisted ref.
const { settingsRef } = vi.hoisted(() => ({
  settingsRef: { current: {} as Record<string, unknown> },
}));

vi.mock('../api', async (importOriginal) => {
  const mod = (await importOriginal()) as typeof import('../api');
  return {
    ...mod,
    api: {
      ...mod.api,
      getSettings: vi.fn(() => Promise.resolve(settingsRef.current)),
    },
  };
});

beforeEach(() => {
  vi.clearAllMocks();
  settingsRef.current = {};
});


function _dashboardState(over: Partial<DashboardState> = {}): DashboardState {
  return {
    libraries: [], activity: [], completed: 0, skipped: 0, failed: 0,
    guid_hits: 0, filepath_hits: 0, suffix_hits: 0, fuzzy_hits: 0,
    unresolved: 0, threads: {}, paused: false, start_time: 0,
    log_dir: '', now: Date.now() / 1000,
    ...over,
  };
}

function _frame(over: Partial<DashboardFrame> = {}): DashboardFrame {
  return {
    type: 'dashboard_frame',
    server_ts: Date.now() / 1000,
    dashboard: _dashboardState(),
    job: null,
    jobs: [],
    fan_out: null,
    servers_network: [],
    ...over,
  };
}

function _restoreJob(): JobPayload {
  return {
    job_id: 'job-1', mode: 'restore', state: 'running',
    queued_at: 0, started_at: 0, finished_at: null, error: null,
    run_log_dir: null, params: {},
  };
}

function _container(name: string): ContainerResult {
  return {
    name, library: 'Music', total: 1, restored: 1, skipped: 0,
    skipped_items: [],
  };
}

function _staleMergingFrame(): DashboardFrame {
  // A 'merging' item ~45s into its phase. STALL_THRESHOLDS.merging is
  // { amber: 30, red: 60 }, so at the x1.0 default it reads 'amber';
  // an x2.0 multiplier widens amber to 60s and it becomes 'normal'.
  const staleAt = Date.now() / 1000 - 45;
  return _frame({
    dashboard: _dashboardState({
      current_items: [{
        library: 'Music', type: 'track', title: 'Stuck Track',
        started_at: staleAt, phase: 'merging', phase_started_at: staleAt,
      }],
    }),
  });
}


describe('DashboardPanel - Issue 8 settings-driven config', () => {
  it('a stalled merging item is amber at the default multiplier', async () => {
    // settingsRef stays {} -> no etr_color_multiplier -> 1.0 default.
    const { DashboardPanel } = await import('./DashboardPanel');
    renderWithAuth(<DashboardPanel snapshot={_staleMergingFrame()} />);
    await waitFor(() => {
      expect(document.querySelector('.stall-amber')).not.toBeNull();
    });
  });

  it('an updated etr_color_multiplier re-renders the stall colour', async () => {
    // x2.0 widens merging's amber threshold to 60s; the ~45s item is
    // then 'normal'. With the pre-fix module global the mutation would
    // not have re-rendered and the row would stay amber.
    settingsRef.current = { etr_color_multiplier: 2.0 };
    const { DashboardPanel } = await import('./DashboardPanel');
    renderWithAuth(<DashboardPanel snapshot={_staleMergingFrame()} />);
    await waitFor(() => {
      expect(document.querySelector('.stall-amber')).toBeNull();
    });
    // The item is still on screen, just no longer flagged stalled.
    expect(screen.getByText('Stuck Track')).toBeInTheDocument();
  });

  it('the restoration-summary row cap reflects the loaded tunable', async () => {
    settingsRef.current = {
      tunables: { restoration_summary_panel_max_items: 2 },
    };
    const frame = _frame({
      job: _restoreJob(),
      dashboard: _dashboardState({
        container_summary: {
          playlists: ['A', 'B', 'C', 'D', 'E'].map(_container),
        },
      }),
    });
    const { DashboardPanel } = await import('./DashboardPanel');
    renderWithAuth(<DashboardPanel snapshot={frame} />);
    // The default cap of 10 would not scroll 5 rows; once the settings
    // load drops the cap to 2 the panel switches to "showing 2 of 5".
    expect(await screen.findByText(/showing 2 of 5/i)).toBeInTheDocument();
  });
});
