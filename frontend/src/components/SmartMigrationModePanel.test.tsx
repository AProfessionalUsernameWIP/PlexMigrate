// Tests for SmartMigrationModePanel: the per-playlist migration-mode
// picker. Pure presentational component, so no api mock is needed.

import { describe, expect, it, vi } from 'vitest';
import { fireEvent, screen } from '@testing-library/react';
import { renderWithAuth } from '../test-helpers/auth';
import { SmartMigrationModePanel } from './SmartMigrationModePanel';
import type { QueuedPlaylist, SmartMigrateMode } from './PlaylistMgmtDeployBar';

function makeEntry(id: string, name: string, mode: SmartMigrateMode): QueuedPlaylist {
  return {
    sourceUserId: 'u1',
    sourceApiUserId: 'u1',
    sourceUsername: 'root',
    playlist: { playlist_id: id, name, item_count: 0, is_smart: true },
    smartMode: mode,
  };
}

describe('SmartMigrationModePanel', () => {
  it('renders nothing when the queue is empty', () => {
    renderWithAuth(
      <SmartMigrationModePanel queued={[]} onSetMode={vi.fn()} onSetAll={vi.fn()} />,
    );
    expect(screen.queryByText('Migration mode')).not.toBeInTheDocument();
  });

  it('renders a row per queued smart playlist plus the mode summary', () => {
    renderWithAuth(
      <SmartMigrationModePanel
        queued={[
          makeEntry('pl-1', 'Fire Tracks', 'filter'),
          makeEntry('pl-2', 'Chill Vibes', 'hard_copy'),
        ]}
        onSetMode={vi.fn()}
        onSetAll={vi.fn()}
      />,
    );
    expect(screen.getByText('Fire Tracks')).toBeInTheDocument();
    expect(screen.getByText('Chill Vibes')).toBeInTheDocument();
    // The summary counts both modes.
    expect(screen.getByText(/1 filter/)).toBeInTheDocument();
  });

  it('calls onSetMode when a per-playlist select changes', () => {
    const onSetMode = vi.fn();
    renderWithAuth(
      <SmartMigrationModePanel
        queued={[makeEntry('pl-1', 'Fire Tracks', 'filter')]}
        onSetMode={onSetMode}
        onSetAll={vi.fn()}
      />,
    );
    fireEvent.change(screen.getByRole('combobox'), {
      target: { value: 'hard_copy' },
    });
    expect(onSetMode).toHaveBeenCalledTimes(1);
    expect(onSetMode.mock.calls[0][1]).toBe('hard_copy');
  });

  it('calls onSetAll from the Set all buttons', () => {
    const onSetAll = vi.fn();
    renderWithAuth(
      <SmartMigrationModePanel
        queued={[makeEntry('pl-1', 'Fire Tracks', 'filter')]}
        onSetMode={vi.fn()}
        onSetAll={onSetAll}
      />,
    );
    fireEvent.click(screen.getByRole('button', { name: /hard copy/i }));
    expect(onSetAll).toHaveBeenCalledWith('hard_copy');
  });
});
