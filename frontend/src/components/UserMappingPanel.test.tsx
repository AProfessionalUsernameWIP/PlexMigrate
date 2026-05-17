// Tests for the Rerun Auto-Link button developer added to
// UserMappingPanel as part of the USER-MGMT-IDENTITY-AUDIT follow-up.
// Validates that the button: renders, calls the API, and surfaces
// success / failure messages.

import { describe, expect, it, vi, beforeEach } from 'vitest';
import { fireEvent, screen, waitFor } from '@testing-library/react';
import { renderWithAuth } from '../test-helpers/auth';

// Mock api before the panel imports it.
vi.mock('../api', async (importOriginal) => {
  const mod = (await importOriginal()) as typeof import('../api');
  return {
    ...mod,
    api: {
      ...mod.api,
      listUserIdentityMaps: vi.fn().mockResolvedValue({ maps: [] }),
      rerunAutoLinkIdentityMap: vi.fn().mockResolvedValue({
        pairs_written: 2,
        pairs_skipped_duplicate: 0,
        groups_seen: 1,
      }),
    },
  };
});

// Stub window.alert so the button-click flow doesn't blow up.
beforeEach(() => {
  vi.spyOn(window, 'alert').mockImplementation(() => {});
});

describe('UserMappingPanel - Rerun Auto-Link button', () => {
  it('renders the Rerun auto-link button', async () => {
    const { UserMappingPanel } = await import('./UserMappingPanel');
    renderWithAuth(<UserMappingPanel allServers={[]} />);
    expect(await screen.findByRole('button', { name: /rerun auto-link/i })).toBeInTheDocument();
  });

  it('calls rerunAutoLinkIdentityMap on click', async () => {
    const { api } = await import('../api');
    const { UserMappingPanel } = await import('./UserMappingPanel');
    renderWithAuth(<UserMappingPanel allServers={[]} />);
    const btn = await screen.findByRole('button', { name: /rerun auto-link/i });
    fireEvent.click(btn);
    await waitFor(() => {
      expect(api.rerunAutoLinkIdentityMap).toHaveBeenCalled();
    });
  });

  it('alerts the operator with the pairs_written count on success', async () => {
    const { UserMappingPanel } = await import('./UserMappingPanel');
    renderWithAuth(<UserMappingPanel allServers={[]} />);
    const btn = await screen.findByRole('button', { name: /rerun auto-link/i });
    fireEvent.click(btn);
    await waitFor(() => {
      expect(window.alert).toHaveBeenCalledWith(
        expect.stringMatching(/added 2 new pairs?/i),
      );
    });
  });

  it('surfaces the error string when the API call fails', async () => {
    const { api } = await import('../api');
    (api.rerunAutoLinkIdentityMap as ReturnType<typeof vi.fn>).mockRejectedValueOnce(
      new Error('backend exploded'),
    );
    const { UserMappingPanel } = await import('./UserMappingPanel');
    renderWithAuth(<UserMappingPanel allServers={[]} />);
    const btn = await screen.findByRole('button', { name: /rerun auto-link/i });
    fireEvent.click(btn);
    await waitFor(() => {
      expect(window.alert).toHaveBeenCalledWith(
        expect.stringMatching(/auto-link failed/i),
      );
    });
  });
});
