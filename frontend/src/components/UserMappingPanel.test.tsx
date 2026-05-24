// Tests for the Rerun Auto-Link button developer added to
// UserMappingPanel as part of the USER-MGMT-IDENTITY-AUDIT follow-up.
// Validates that the button: renders, calls the API, and surfaces
// success / failure messages.

import { describe, expect, it, vi } from 'vitest';
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

  it('surfaces the pairs_written count inline on success', async () => {
    const { UserMappingPanel } = await import('./UserMappingPanel');
    renderWithAuth(<UserMappingPanel allServers={[]} />);
    const btn = await screen.findByRole('button', { name: /rerun auto-link/i });
    fireEvent.click(btn);
    // The panel surfaces the result inline (setInfo), not via window.alert.
    expect(
      await screen.findByText(/added 2 new pairs?/i),
    ).toBeInTheDocument();
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
    // The panel surfaces the failure inline (setError), not via window.alert.
    expect(
      await screen.findByText(/auto-link failed/i),
    ).toBeInTheDocument();
  });
});
