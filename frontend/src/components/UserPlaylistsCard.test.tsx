// Tests for UserPlaylistsCard, focused on Smart Playlist mode. Copy
// mode shows regular playlists and hides smart ones; Smart Playlist
// mode does the inverse and adds a per-row decoded-filter inspector.

import { describe, expect, it, vi, beforeEach } from 'vitest';
import { fireEvent, screen, waitFor } from '@testing-library/react';
import { renderWithAuth } from '../test-helpers/auth';

const PLAYLISTS = [
  { playlist_id: 'reg-1', name: 'Road Trip', item_count: 10, is_smart: false },
  { playlist_id: 'sm-1', name: 'Fire Tracks', item_count: 0, is_smart: true },
];

const FIRE_PREVIEW = {
  playlist_name: 'Fire Tracks',
  filter: {
    library_type: 'artist',
    library_name: 'Music',
    libtype: 'track',
    root: {
      kind: 'clause',
      field: 'genre',
      operator: '',
      values: ['Rock'],
      value_kind: 'tag',
      unresolved_ids: [],
    },
    sort: [],
    limit: null,
    unresolved_source_ids: [],
    description: 'track where genre is Rock',
  },
};

vi.mock('../api', async (importOriginal) => {
  const mod = (await importOriginal()) as typeof import('../api');
  return {
    ...mod,
    api: {
      ...mod.api,
      playlistMgmtListPlaylists: vi.fn().mockResolvedValue({
        server_id: 'plex-a', user_id: 'u1', from_cache: false,
        fetched_at: 0, playlists: PLAYLISTS,
      }),
      previewSmartPlaylist: vi.fn().mockResolvedValue(FIRE_PREVIEW),
    },
  };
});

beforeEach(() => {
  vi.clearAllMocks();
});

function renderCard(smartMode: boolean) {
  return renderWithAuth(
    <UserPlaylistsCard
      side="source"
      serverId="plex-a"
      serverLabel="Plex A"
      userId="u1"
      username="root"
      role="owner"
      selectedPlaylistIds={new Set()}
      onTogglePlaylist={vi.fn()}
      smartMode={smartMode}
    />,
  );
}

// UserPlaylistsCard is imported lazily inside each test so the api
// mock above is fully installed first.
let UserPlaylistsCard: typeof import('./UserPlaylistsCard').UserPlaylistsCard;

describe('UserPlaylistsCard smart mode', () => {
  it('copy mode shows regular playlists and hides smart ones', async () => {
    ({ UserPlaylistsCard } = await import('./UserPlaylistsCard'));
    renderCard(false);
    expect(await screen.findByText('Road Trip')).toBeInTheDocument();
    expect(screen.queryByText('Fire Tracks')).not.toBeInTheDocument();
  });

  it('smart mode shows smart playlists and hides regular ones', async () => {
    ({ UserPlaylistsCard } = await import('./UserPlaylistsCard'));
    renderCard(true);
    expect(await screen.findByText('Fire Tracks')).toBeInTheDocument();
    expect(screen.queryByText('Road Trip')).not.toBeInTheDocument();
  });

  it('decodes and renders a smart playlist filter when inspected', async () => {
    const { api } = await import('../api');
    ({ UserPlaylistsCard } = await import('./UserPlaylistsCard'));
    renderCard(true);
    fireEvent.click(await screen.findByRole('button', { name: /inspect filter/i }));
    await waitFor(() => {
      expect(api.previewSmartPlaylist).toHaveBeenCalledWith('plex-a', 'sm-1');
    });
    expect(
      await screen.findByText('track where genre is Rock'),
    ).toBeInTheDocument();
    // The structured tree renders the leaf clause (exact match: the
    // description above the tree also contains "genre is Rock").
    expect(screen.getByText('genre is Rock')).toBeInTheDocument();
  });
});
