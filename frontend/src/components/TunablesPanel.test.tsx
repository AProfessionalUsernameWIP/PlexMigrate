// Tests for the Identity & log presentation toggle block developer
// added to TunablesPanel as part of the USER-MGMT-IDENTITY-AUDIT
// cosmetic + strict-mode tunables.

import { describe, expect, it, vi, beforeEach } from 'vitest';
import { fireEvent, screen, waitFor } from '@testing-library/react';
import { renderWithAuth } from '../test-helpers/auth';

vi.mock('../api', async (importOriginal) => {
  const mod = (await importOriginal()) as typeof import('../api');
  return {
    ...mod,
    api: {
      ...mod.api,
      // TunablesPanel loads settings + the server registry on mount.
      getSettings: vi.fn().mockResolvedValue({
        tunables: {},
        tunables_per_server: {},
        tooltips_enabled: true,
        auto_rotate_user_tokens_on_refresh: false,
        pin_migration_username_string_fallback: false,
      }),
      listServers: vi.fn().mockResolvedValue([]),
      saveSettings: vi.fn().mockResolvedValue({ ok: true }),
    },
  };
});

beforeEach(() => {
  vi.clearAllMocks();
});

describe('TunablesPanel - Identity & log presentation', () => {
  it('renders the strict identity resolution toggle on the UI tab', async () => {
    const { TunablesPanel } = await import('./TunablesPanel');
    renderWithAuth(<TunablesPanel />);
    // Click the UI tab so the block is visible.
    // TunablesPanel renders the tab strip with explicit role="tab"
    // (see TunablesPanel.tsx); the implicit role="button" is shadowed.
    const uiTab = await screen.findByRole('tab', { name: /ui & display/i });
    fireEvent.click(uiTab);
    expect(
      await screen.findByText(/strict identity resolution/i),
    ).toBeInTheDocument();
  });

  it('renders the log_use_display_name toggle on the UI tab', async () => {
    const { TunablesPanel } = await import('./TunablesPanel');
    renderWithAuth(<TunablesPanel />);
    // TunablesPanel renders the tab strip with explicit role="tab"
    // (see TunablesPanel.tsx); the implicit role="button" is shadowed.
    const uiTab = await screen.findByRole('tab', { name: /ui & display/i });
    fireEvent.click(uiTab);
    expect(
      await screen.findByText(/use display name in logs/i),
    ).toBeInTheDocument();
  });

  it('both toggles default to off', async () => {
    const { TunablesPanel } = await import('./TunablesPanel');
    renderWithAuth(<TunablesPanel />);
    // TunablesPanel renders the tab strip with explicit role="tab"
    // (see TunablesPanel.tsx); the implicit role="button" is shadowed.
    const uiTab = await screen.findByRole('tab', { name: /ui & display/i });
    fireEvent.click(uiTab);
    // Find the two toggles by their associated label text.
    const strictLabel = await screen.findByText(/strict identity resolution/i);
    const strictCheckbox = strictLabel.closest('label')!
      .querySelector('input[type="checkbox"]') as HTMLInputElement;
    expect(strictCheckbox.checked).toBe(false);

    const logLabel = await screen.findByText(/use display name in logs/i);
    const logCheckbox = logLabel.closest('label')!
      .querySelector('input[type="checkbox"]') as HTMLInputElement;
    expect(logCheckbox.checked).toBe(false);
  });
});
