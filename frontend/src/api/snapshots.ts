// Snapshot registry + legacy archive REST helpers extracted from the
// monolithic ``api`` const during Phase 3c. Includes the in-snapshot
// per-library counts and per-snapshot users listing so the Restore form
// can intersect users without leaving this module.

import { http } from './core';
import type {
  ExportArchive,
  ServerUsersResponse,
  Snapshot,
} from './types';

export const snapshotsApi = {
  // List users captured inside a snapshot .db. Reads the
  // snapshot_users table. Returns the same ServerUser shape so the
  // Restore form can intersect snapshot users with destination users
  // by ``plex_id`` (managed) and ``kind === "owner"`` (owner).
  listSnapshotUsers: (snapshotId: string) =>
    http<ServerUsersResponse>(`/api/snapshots/${encodeURIComponent(snapshotId)}/users`),

  // Snapshots (registry-backed).
  // Reads return ``{snapshots: Snapshot[]}``; writes are db_admin
  // gated and carry ``{db_admin_username, db_admin_password}`` in
  // the body.
  listSnapshots: () =>
    http<{ snapshots: Snapshot[] }>('/api/snapshots'),
  snapshotDownloadUrl: (id: string) =>
    `/api/snapshots/${encodeURIComponent(id)}/download`,
  // Streams the raw .db file. No render, no cache - canonical
  // artifact straight off disk. Companion to snapshotDownloadUrl
  // (which produces the .plexexport.json sidecar).
  snapshotDbDownloadUrl: (id: string) =>
    `/api/snapshots/${encodeURIComponent(id)}/download-db`,
  deleteSnapshot: (
    id: string,
    // ``keep_json: true`` moves the cached JSON sidecar (or
    // materialises one first if not cached) to the JSON-archive
    // directory before dropping the .db + registry row. The file
    // shows up in the JSON Archives panel afterward.
    body: {
      db_admin_username: string;
      db_admin_password: string;
      keep_json?: boolean;
    },
  ) =>
    http<{
      deleted: string;
      file_removed: boolean;
      json_removed: boolean;
      json_archived: boolean;
      archived_path: string | null;
      errors: string[];
    }>(
      `/api/snapshots/${encodeURIComponent(id)}`,
      { method: 'DELETE', body: JSON.stringify(body) },
    ),
  deleteAllSnapshotsForServer: (
    serverId: string,
    body: { db_admin_username: string; db_admin_password: string },
  ) =>
    http<{ deleted: number; errors: number }>(
      `/api/snapshots/server/${encodeURIComponent(serverId)}`,
      { method: 'DELETE', body: JSON.stringify(body) },
    ),

  // Reassign orphan snapshot rows into a target server. Admin-gated;
  // non-destructive (rewrites server_id only, never deletes files).
  // The Exports panel already merges orphan rows at display time, so
  // end users rarely need this; it exists for end users who want the
  // underlying registry to be clean rather than display-side-clean.
  mergeOrphanSnapshots: (serverId: string) =>
    http<{ checked: number; reassigned: number; no_op: number }>(
      `/api/snapshots/server/${encodeURIComponent(serverId)}/merge-orphans`,
      { method: 'POST', body: JSON.stringify({}) },
    ),

  // Legacy on-disk ``.plexexport.json`` files relocated to
  // ``snapshots/legacy/`` by the snapshot-registry migration. Read-only
  // browse + download, db_admin-gated delete.
  listLegacySnapshots: () =>
    http<ExportArchive[]>('/api/snapshots/legacy'),
  legacySnapshotDownloadUrl: (name: string) =>
    `/api/snapshots/legacy/${encodeURIComponent(name)}/download`,
  deleteLegacySnapshot: (
    name: string,
    body: { db_admin_username: string; db_admin_password: string },
  ) =>
    http<{ deleted: string }>(
      `/api/snapshots/legacy/${encodeURIComponent(name)}`,
      { method: 'DELETE', body: JSON.stringify(body) },
    ),
  // Clear every .plexexport.json archive in <output_dir>/legacy/.
  // db_admin gated. Returns counts of files deleted + per-file
  // errors (best-effort sweep, doesn't abort on one failure).
  deleteAllLegacyArchives: (
    body: { db_admin_username: string; db_admin_password: string },
  ) =>
    http<{ deleted: number; errors: string[] }>(
      '/api/snapshots/legacy',
      { method: 'DELETE', body: JSON.stringify(body) },
    ),

  // Per-library counts read straight
  // from a snapshot.db on disk. Returns one row per library_sections
  // entry with item_count + watch_events / ratings / playlists /
  // collections aggregated by section_key. Drives the source-side
  // counts in the Run Job > Library Mapping panel when source is a
  // snapshot (operator can't read live source server when it's
  // offline; the snapshot.db has the same per-library data).
  snapshotLibraryCounts: (snapshotId: string) =>
    http<{
      libraries: Array<{
        section_key: number;
        section_title: string;
        section_type: string;
        // Top-level item count for the library's primary media
        // type (movie / show / artist) - NOT the union across all
        // media_types. Matches what the live side reports for the
        // same library, so the two columns of the mapping panel
        // are comparable.
        item_count: number;
        // Per-media_type
        // breakdown so the panel can render the right hierarchy:
        //   - artist library: { artist: 1100, album: 5500, track: 7402 }
        //   - show library:   { show: 12, season: 87, episode: 312 }
        //   - movie library:  { movie: 411 }
        // Snapshots that captured only leaves (e.g. watch_history-
        // only) may not include parent rows; those keys are simply
        // absent and the renderer falls back to summing across what
        // IS present.
        media_type_counts: Record<string, number>;
        // Distinct hierarchy counts derived from the snapshot's
        // items table (which carries show_title /
        // season_index / artist / album). Keys: artists / series /
        // seasons / albums. Absent keys mean the snapshot pre-dates
        // migration v17 (no hierarchy columns) or the library type has no
        // such hierarchy.
        hierarchy_counts: Record<string, number>;
        watch_events: number;
        ratings: number;
        playlists: number;
        collections: number;
      }>;
    }>(`/api/snapshots/${encodeURIComponent(snapshotId)}/library-counts`),
};
