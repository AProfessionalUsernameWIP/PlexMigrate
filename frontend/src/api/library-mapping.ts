// Library mapping REST helpers extracted from the monolithic ``api``
// const during Phase 3c. Includes the side-by-side library listing
// (``libraryMappingSides``) used by the Servers > Library Mapping
// sub-tab; the matcher reads mirror DB GUID/path fingerprints with a
// live fallback.

import { http } from './core';
import type {
  LeafCounts,
  LibraryMappingCandidate,
  LibraryMappingLib,
  LibraryMappingRow,
} from './types';

export const libraryMappingApi = {
  // ── Library mapping ───────────────────────────────────────────────
  // Four endpoints back the Servers > Library Mapping sub-tab. The
  // matcher reads mirror DB GUID/path fingerprints; auto rows survive
  // until the source or dest server's mirror is re-synced.

  libraryMappingAutomap: (
    sourceServerId: string,
    destServerId: string,
    persistHighConfidence: boolean = false,
  ) =>
    http<{
      source_server_id: string;
      dest_server_id: string;
      auto_saved: number;
      results: Array<{
        source_library: LibraryMappingLib;
        best: null | LibraryMappingCandidate;
        alternatives: LibraryMappingCandidate[];
      }>;
    }>(`/api/library-mapping/automap`, {
      method: 'POST',
      body: JSON.stringify({
        source_server_id: sourceServerId,
        dest_server_id: destServerId,
        persist_high_confidence: persistHighConfidence,
      }),
    }),

  libraryMappingList: (sourceServerId: string, destServerId: string) =>
    http<{
      source_server_id: string;
      dest_server_id: string;
      mappings: LibraryMappingRow[];
    }>(
      `/api/library-mapping/list?source_server_id=${encodeURIComponent(sourceServerId)}&dest_server_id=${encodeURIComponent(destServerId)}`,
    ),

  libraryMappingPreflight: (body: {
    source_server_id: string;
    dest_server_id: string;
    library_names: string[];
    mode: 'merge' | 'replace';
    ignore_library_mapping?: boolean;
    // Per-run library name overrides
    // map. Keys are source library names; values are destination
    // library names (empty string for an explicit skip). Engine
    // consults BEFORE the saved mapping table; preflight does the
    // same so the warning surface reflects what the engine will do.
    library_mapping_overrides?: Record<string, string>;
  }) =>
    http<{
      same_server: boolean;
      cross_backend_replace_refusal: string | null;
      library_warnings: Array<{
        library: string;
        status: 'unmapped' | 'operator_skip' | 'auto_unconfirmed' | 'per_run_route' | 'per_run_skip';
        detail: string;
      }>;
      any_unmapped: boolean;
    }>(`/api/library-mapping/preflight`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  libraryMappingSave: (body: {
    source_server_id: string;
    source_library_id: string;
    source_library_name?: string;
    dest_server_id: string;
    dest_library_id: string;
    dest_library_name?: string;
    source?: 'operator' | 'auto';
    confidence?: number;
    tier?: string;
    notes?: string | null;
  }) =>
    http<{ ok: boolean }>(`/api/library-mapping/save`, {
      method: 'PUT',
      body: JSON.stringify(body),
    }),

  libraryMappingDelete: (
    sourceServerId: string,
    sourceLibraryId: string,
    destServerId: string,
  ) =>
    http<{ deleted: number }>(
      `/api/library-mapping/${encodeURIComponent(sourceServerId)}/${encodeURIComponent(sourceLibraryId)}/${encodeURIComponent(destServerId)}`,
      { method: 'DELETE' },
    ),

  libraryMappingInvalidateAuto: (
    sourceServerId?: string | null,
    destServerId?: string | null,
  ) =>
    http<{ deleted: number }>(`/api/library-mapping/invalidate-auto`, {
      method: 'POST',
      body: JSON.stringify({
        source_server_id: sourceServerId || '',
        dest_server_id: destServerId || '',
      }),
    }),

  // Fetches BOTH servers' library lists side-by-side (mirror-
  // first, live fallback). Drives the two-column Library
  // Mapping UI so the operator sees every library on both servers
  // regardless of mirror sync state, including the case where multiple
  // libraries on each side share a type and only the operator knows
  // which should pair with which.
  libraryMappingSides: (sourceServerId: string, destServerId: string) =>
    http<{
      source_server_id: string;
      dest_server_id: string;
      source_libraries: Array<{
        library_id: string;
        library_name: string;
        library_type: string;
        item_count: number;
        // Per-libtype leaf counts pulled from the registry's cached
        // last_libraries (populated by the library walk on Refresh).
        // Missing keys mean "the walk didn't capture this leaf type
        // yet"; UI falls back to item_count when leaf_counts is empty.
        leaf_counts?: LeafCounts;
        source: 'mirror' | 'live';
      }>;
      dest_libraries: Array<{
        library_id: string;
        library_name: string;
        library_type: string;
        item_count: number;
        leaf_counts?: LeafCounts;
        source: 'mirror' | 'live';
      }>;
      mappings: LibraryMappingRow[];
    }>(
      `/api/library-mapping/sides?source_server_id=${encodeURIComponent(sourceServerId)}&dest_server_id=${encodeURIComponent(destServerId)}`,
    ),
};
