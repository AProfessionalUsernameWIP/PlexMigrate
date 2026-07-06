"""
Tunables: hot-reloadable knobs that used to be hardcoded literals.

These live under ``settings.json["tunables"]`` so they're inspectable +
editable + survive container rebuilds. The module owns:

* The default value for every tunable (single source of truth)
* Typed accessor functions (``plex_connect_timeout()`` etc.) that
  call-sites use instead of hardcoded literals
* An mtime-based cache: the on-disk file is re-read only when its
  modification time changes, so per-call lookups are cheap

Hot-reload contract: every getter returns the current on-disk value as
of the latest save. Some values take effect immediately (timeouts read
per call); others have a short lag (scheduler loops read on the next
iteration; HTTPAdapter pool sizes need a session rebuild via
``services.auth.invalidate_sessions()``, which ``save_settings`` will
trigger). Restart is never required.

See ``server/models.py`` for the SettingsIn schema and
``server/persistence.py`` for the on-disk shape.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Dict


log = logging.getLogger("plexmigrate.services.tunables")


# ── Defaults ────────────────────────────────────────────────────────────────
#
# When a tunable isn't present in settings.json (fresh install, missing
# key from an older config) the getter returns the value below. The
# values match what was hardcoded before this module existed, so
# behaviour is byte-for-byte identical until the end user changes
# something.

_DEFAULTS: Dict[str, Any] = {
    # ── Networking & Retry ──────────────────────────────────────────
    # Plex connect timeout - used by library_walk on long-running scans
    # (was timeout=120 at server/library_walk.py).
    "plex_connect_timeout_seconds": 120,
    # urllib3.Retry total budget - how many retry attempts before
    # giving up on a Plex HTTP call. Was total=4 in services/auth.py
    # (v0.9.6 bumped from 2→4 to absorb Plex 429s).
    "plex_retry_total_budget": 4,
    # urllib3.Retry backoff factor - exponential delay multiplier
    # between retries. Was 0.5 in services/auth.py.
    "plex_retry_backoff_factor": 0.5,
    # Server "Test Connection" + ping endpoint timeouts.
    "server_ping_timeout_seconds": 10,
    "server_probe_timeout_seconds": 15,
    # Scrobble read/write timeouts - direct-transfer hot path.
    "scrobble_get_timeout_seconds": 10,
    "scrobble_put_timeout_seconds": 15,

    # ── Performance ─────────────────────────────────────────────────
    # Hard ceiling for the workers slider on the Job Form / Run Defaults.
    # The user-set ``workers`` value is clamped to this ceiling.
    "workers_default_cap": 32,
    "scrobble_workers_default_cap": 32,
    # Per-feature caps used by importer / direct-transfer.
    "import_user_workers_cap": 8,
    "import_library_workers_cap": 4,
    # Scrobble safety: never POST a viewCount increment greater than
    # this in one batch. Prevents pathological cases from spamming
    # Plex with thousands of synthetic plays.
    "viewcount_increment_cap": 200,
    # plexapi's PlexPartialObject auto-reloads partial objects the
    # moment you read an attribute whose value is None/[]. The reload
    # bundles includeMarkers + includeChapters which can trigger Plex
    # intro/chapter analysis (20-30s per item on shows). For our
    # bulk-fetched filter paths (snapshot_watch_history,
    # snapshot_ratings) this is catastrophic on libraries where most
    # items lack viewCount or userRating - every unrated episode
    # would trigger a per-item reload during the local filter.
    #
    # Default false (disabled). End users flip true ONLY as a
    # diagnostic / recovery escape hatch - e.g. if a future plexapi
    # or Plex version genuinely needs reload to surface data the
    # bulk response omits. Wrong value = ratings-takes-hours.
    "plexapi_autoreload_enabled": False,
    # USER-MGMT-IDENTITY-AUDIT R-4: strict identity resolution.
    # When false (default), the user_resolution helper falls back to
    # case-insensitive username match (step 3 of the 5-step chain)
    # when neither identity_map nor backend_user_id match. Behaviour
    # end users are used to.
    # When true, the chain stops after step 2 (backend_user_id direct
    # match within service_type); unresolved source users are
    # explicitly skipped + surfaced in the run log instead of being
    # silently routed via name match. Use this when every user MUST be
    # explicitly mapped before the engine writes anything for them.
    "strict_identity_resolution": False,
    # The five-layer switch
    # model for the user-activity filter + auto-tombstone sweeper.
    # Every layer defaults OFF so an operator who touches nothing
    # sees identical behaviour to today. The adoption ladder,
    # in short:
    #   Layer 1: sweeper daemon runs at all?
    #   Layer 2: engine hot paths drop failing users?
    #   Layer 3: auth_error counts toward auto-tombstone?
    #   Layer 4: unreachable counts toward auto-tombstone?
    #   (Layer 5 is per-server, lives on the registry row, not here.)
    "user_activity_sweeper_enabled": False,
    "user_activity_filter_enabled": False,
    "auto_tombstone_on_auth_error": False,
    "auto_tombstone_on_unreachable": False,
    # PLEXMIGRATE_DATA_DIR resolves to a system path (e.g. /var, /etc)
    # and the operator confirms they really want app state under it.
    # Default False so a misconfigured env var fails fast at startup
    # instead of silently planting .keyfile/.auth_secret in a
    # privileged location.
    "allow_system_data_dir": False,
    # Skip the synchronous _build_scan_cache pre-warm in the Plex-
    # native restore engine. On giant TV libraries the pre-warm blocks
    # the watch-history phase 30-60s before the first resolver fires.
    # Disabling it loses some matching speed but starts the resolvers
    # immediately; the resolver's own coordinator handles the on-demand
    # build path as a fallback.
    "restore_skip_scan_cache_prewarm": False,
    # Sweep cadence + auto-tombstone threshold. Clamps in the typed
    # accessors below.
    "user_activity_sweep_interval_hours": 12,
    "user_activity_consecutive_failure_threshold": 3,
    # USER-MGMT-IDENTITY-AUDIT cosmetic follow-on: substitute the
    # stored ``display_name`` for the raw ``username`` in log lines
    # and run-history fields that reference a user. Default False
    # (logs read the raw handle every existing end user is used to).
    # When true, services.identity.user_display.display_for_logging substitutes
    # the managed_users.display_name when present and falls back to
    # the username when no display_name is stored. Pure presentation
    # tweak; routing / identity_map logic is unaffected.
    "log_use_display_name": False,
    # When a managed_users row joins an
    # identity_map equivalence class — either auto-linked at sync via
    # backend_user_id or manually wired in the User Mapping panel — and
    # any other row in the class already has a PIN stored, copy that
    # PIN into the row's backend-natural PIN column. Same-backend
    # rows backfill plex_home_pin_enc / emby_easy_pin_enc /
    # jellyfin_easy_pin_enc as appropriate; cross-backend rows
    # backfill into the destination row's natural PIN column (a Plex
    # Home PIN's value lands in an Emby row's emby_easy_pin_enc).
    # Additive ONLY: never overwrites an existing PIN. Default True
    # so freshly-registered servers inherit the operator's stored PIN
    # automatically; set False to require explicit PIN entry on every
    # row regardless of identity_map state.
    "auto_backfill_pin_from_identity_links": True,
    # media.db caching during snapshot + direct-transfer runs. When
    # false (default), the engine's payload-direct snapshot writer
    # (Rule 1) and direct-transfer's in-memory pipeline both run
    # without ingesting payloads into media.db. The .db snapshot file
    # and JSON sidecar are unaffected - they're built from the live
    # payloads.
    #
    # Auto-seed exception: when the end user hasn't opted in (tunable
    # left at default false) AND media.db has no rows for the server
    # being captured, the engine treats the run as a "first-time
    # seed" and DOES ingest. This populates the resolver Tier 0
    # GUID cache so subsequent direct-transfer / restore runs against
    # this server can short-circuit live API GUID lookups. Once that
    # one-time seed has happened, the tunable's false default takes
    # over and subsequent runs stop writing to media.db until the
    # end user explicitly flips it on.
    #
    # The auto-seed lives at the resolution boundary (see
    # ``services.snapshot.plex_native.snapshotter._should_cache_payload_to_media_db``)
    # rather than in this tunable's value, so settings.json stays a
    # clean reflection of end user intent - auto-seed is a one-shot
    # behaviour, not a stored state.
    "cache_snapshot_payloads_to_media_db": False,

    # ── Polling & Maintenance ───────────────────────────────────────
    # How often the frontend pings registered servers for connectivity.
    # Was 30_000 in frontend/src/components/ServersPanel.tsx.
    "frontend_server_ping_interval_ms": 30000,
    # How often the scheduler loop checks for due jobs.
    # Was _TICK_SECONDS = 30 in server/schedules.py.
    "scheduler_tick_seconds": 30,
    # How often the auth_db sweeps expired refresh tokens.
    "refresh_token_cleanup_interval_seconds": 3600,
    # v0.13.x: how long a generated/prebuilt ``.plexexport.json`` sidecar
    # is kept on disk before the background sweep reaps it. The sidecar
    # is rendered on demand from the snapshot ``.db`` (or pre-built on
    # the snapshot job when the end user opts in); after the download
    # window, holding it on disk just consumes space. Default 300s
    # (5 min). Lower for an aggressive cleanup; raise if end users
    # download infrequently and want first-click-instant for longer.
    # Set to 0 to disable the sweep entirely - sidecars then persist
    # until manually deleted or until the snapshot row is removed.
    "snapshot_sidecar_ttl_seconds": 300,

    # ── Limits ──────────────────────────────────────────────────────
    # Seed for snapshot_retention_global (a separate top-level setting).
    # The seed only matters on first install; after that the
    # top-level value wins.
    "snapshot_retention_global_default": 30,
    # Refresh-token lifetime (7 days). Was REFRESH_TOKEN_TTL_SECONDS
    # in server/auth_router.py.
    "refresh_token_ttl_seconds": 7 * 24 * 60 * 60,
    # JWT access-token lifetime (30 minutes). Was JWT_TTL_SECONDS in
    # server/auth_router.py.
    "jwt_access_token_ttl_seconds": 30 * 60,
    # Cap on a single log-browser read response. Was 16 MiB at
    # server/log_browser.py.
    "log_read_max_bytes": 16 * 1024 * 1024,

    # ── Danger Zone ─────────────────────────────────────────────────
    # SQLite PRAGMA busy_timeout for the main connection pool.
    # Was timeout=30.0 in auth_db / media_db / snapshot_registry.
    "sqlite_busy_timeout_main_seconds": 30,
    # SQLite read-only / short-lived connection timeout.
    # Was timeout=10.0 in snapshot_registry's get-style helpers.
    "sqlite_busy_timeout_short_seconds": 10,
    # requests.adapters.HTTPAdapter pool sizes. Setting these too
    # large wastes file descriptors; too small starves concurrent Plex
    # calls. ``http_pool_connections`` was 4 in services/auth.py;
    # ``http_pool_maxsize_cap`` was the 10 fallback in _make_retry_adapter.
    "http_pool_connections": 4,
    "http_pool_maxsize_cap": 10,

    # NOTE: ``watch_ratings_filter_strategy`` is intentionally NOT a
    # tunable - it's a snapshot run-behaviour choice that end users
    # tuning a migration may want to flip without root_admin
    # escalation. It lives at the top level of settings.json next to
    # ``prebuild_json_sidecar_default``, surfaced under
    # Servers ▸ Run Defaults ▸ Snapshot Defaults, with per-server
    # overrides on Servers ▸ Advanced Settings.

    # ── Dashboard ETR colour multiplier ─────────────────────────────
    # Scales the per-phase amber/red thresholds in
    # DashboardPanel.tsx STALL_THRESHOLDS. 1.0 = ship defaults.
    # <1.0 = more sensitive (warns sooner); >1.0 = more lenient.
    "etr_color_multiplier": 1.0,

    # ── Power-user: reveal the per-run "Ignore library mapping" toggle
    # ─────────────────────────────────────────────────────────────────
    # The restorer consults the library_mappings table to route
    # source-library names to destination-library names when they
    # don't match exactly. This is a SAFETY feature: an operator who
    # has set up a mapping doesn't want a one-off run to bypass it
    # silently. But occasionally a power user needs to force a legacy
    # exact-name-only restore (e.g. debugging a mapping suspicion).
    # When this tunable is True, the Run Job UI reveals a per-run
    # "Ignore library mapping" checkbox. When False (the default),
    # the checkbox stays hidden and every restore consults mappings.
    "reveal_ignore_library_mapping_toggle": False,

    # ── Per-user gather strategy override ────────────────────────────
    # The "smart" watch+ratings strategy in
    # ``_should_use_bulk`` always-bulks show/artist libraries because
    # ``section.totalSize`` is the container count (shows / artists)
    # while the actual filter scans leaves (episodes / tracks) which
    # are 5-30x more. That heuristic is owner-centric: the admin's
    # watch history typically spans many episodes so bulk amortizes.
    # For per-user passes most managed users have sparse watch history
    # (often <100 episodes); pulling the whole 40k-item leaf list once
    # per user wastes bandwidth + wall-clock.
    #
    # ``true`` flips per-user passes on show/artist libraries to the
    # server-side filter path even when the smart heuristic would have
    # picked bulk. Each user's gather then asks Plex for the items
    # they've actually watched / rated, which is cheap when sparse and
    # only marginally more expensive when not. Owner pass behaviour is
    # unchanged.
    #
    # Default false to preserve historical behaviour; flip true if
    # snapshot runs on multi-home-user TV / Music libraries spend
    # most of their wall-clock in per-user bulk-fetch lines.
    "snapshot_user_pass_prefer_server_side": False,

    # ── Activity feed: owner display style ──────────────────────────
    # How the Plex owner is rendered in the dashboard activity feed +
    # run logs. Three values:
    #   "plex_owner" (default): always show "Plex Owner".
    #   "custom_name": substitute the end user-configured display name
    #       for the owner's Plex.tv email when one exists. Falls back to
    #       "Plex Owner" when no custom name has been set on the source
    #       server's user_display_names map.
    #   "custom_name_owner": as above but appends " (owner)" so the
    #       owner is unambiguous when a managed user happens to share a
    #       similar custom name.
    # Unknown values fall through to "plex_owner".
    "owner_display_style": "plex_owner",

    # ── UI & Display caps ────────────────────────────────────────────
    # Phase 3 of the dashboard/log reorg. These tunables control
    # end user-visible caps + delays on the frontend. Backend reads
    # them so a future server-side renderer (or analytics export)
    # picks up the same values; today's primary readers are the
    # frontend components.
    #
    # Restoration summary panel cap. Default 10 visible items; the
    # rest scroll inside the panel. Floor 1, ceiling 500 (a hard cap
    # so pathological values don't bury the end user's viewport).
    "restoration_summary_panel_max_items": 10,
    # User-count tooltip delay (ms). Default 600ms - long enough that
    # incidental hover doesn't fire, short enough to feel responsive.
    # Clamped to [100, 3000] at the read boundary.
    "user_count_tooltip_delay_ms": 600,
    # User-count tooltip visible list cap. Default 20 names + a
    # "+N more" overflow. Ceiling 200 so the tooltip never grows
    # beyond a reasonable popover.
    "user_count_tooltip_max_items": 20,

    # Show the prefixed server UID (`<service_type>_<uuid4_hex>`) in
    # the Servers > Overview registered-server table. Default False
    # so existing end users don't suddenly see a new column on
    # upgrade; opt-in for end users who diagnose schedules / API
    # calls / log lines that reference servers by id. The format is
    # locked at add-server time and
    # never changes for a registered row.
    "servers_panel_show_server_uid": False,

    # ── Schedules tab: editor visibility ────────────────────────────
    # Controls how the Run-Job-shaped editor renders on the Schedules
    # sub-tab. Two values:
    #   "full" (default): the editor renders open on tab load, bound to
    #       the currently selected schedule (or to a "new schedule"
    #       draft if no row is selected). End user sees the full spec
    #       at a glance without an extra click.
    #   "on_create": the editor is hidden until the end user clicks
    #       "+ New Schedule" or "Edit" on a saved row. Mirrors the
    #       pre-redesign inline-editor flow.
    # Unknown values fall through to "full".
    "schedules_editor_visibility": "full",

    # ── Danger: developer-mode runtime toggle ───────────────────────
    # When True, the in-app Developer tab + /api/dev/* endpoints
    # become accessible WITHOUT setting PLEXMIGRATE_DEBUG_MODE on the
    # container. Gated behind the Tunables panel's Danger Zone +
    # "I understand" gate + root_admin permission, so flipping this
    # is a three-click conscious decision.
    #
    # Precedence: the env var ALWAYS WINS when set truthy. This
    # tunable is only consulted when the env var is unset or falsy.
    # That preserves the existing "production set the env var as a
    # hard gate" invariant for anyone who relied on it.
    #
    # Default False. A debug-mode build that ships with this on by
    # accident is a CVE; the default must stay False.
    "developer_mode_enabled": False,

    # ── Playlist Management cache ──────────────────────────────────
    # Cache layer governing how often the engine re-reads playlists
    # from a backend. Per-user playlist roster + items live in
    # server_data/playlist_cache.db. Snapshots can opt into the
    # cache when fresh to avoid the live API roundtrip.
    "playlist_cache_enabled": True,
    # Recommended refresh window before the end user-facing "stale"
    # badge fires. UI surfaces a Refresh button when older. End user
    # locked at 30 minutes.
    "playlist_cache_refresh_interval_seconds": 1800,
    # When a snapshot capture runs, use the cache for playlists if
    # the cache was refreshed within this window. Otherwise hit the
    # live API. End user locked at 15 minutes.
    "playlist_cache_snapshot_threshold_seconds": 900,
    # Hard invalidation: cache rows older than this are dropped on
    # read regardless of behaviour above. End user locked at 12 hours.
    "playlist_cache_max_age_seconds": 43200,
    # When True, a background thread refreshes stale caches
    # automatically. Default ON now
    # that the UI also reads cache rows directly without firing
    # blocking live fetches, so the cache freshness needs to come
    # from somewhere — this thread.
    "playlist_cache_background_refresh_enabled": True,
    # Background refresh cadence in seconds. Operator-
    # locked default 15 minutes; tunable [60s, 86400s]. The thread
    # iterates every (server, user) pair currently in the cache and
    # re-fetches via the adapter. Lower values keep playlists fresh
    # but increase API load against source servers.
    "playlist_cache_background_refresh_interval_seconds": 900,
    # Plex Home user auth path for Playlist Mgmt copies. Default
    # 'owner_token' uses the owner's token + the Home user's UserId
    # in the URL/body (matches existing _apply_per_user_block
    # pattern). 'per_user_token' uses the Home user's own
    # PIN-unlocked token from managed_users (more isolated; needs
    # PIN). End user can switch at runtime for debugging.
    "playlist_mgmt_plex_home_auth_mode": "owner_token",

    # Same-user no-op short-circuit
    # for Playlist Mgmt copies. 'skip' (default) returns success +
    # skipped=True without doing any work when the resolved source +
    # destination are the same (server, user). 'duplicate' lets the
    # copy proceed and creates a second playlist under the same user.
    "playlist_mgmt_same_user_behavior": "skip",

    # ── Playlist Transfer batch ────────────────────────────────────
    # Default worker count for the batch primitive's internal
    # ThreadPoolExecutor. Higher = faster on LAN; risk = source/dest
    # API rate-limits trip. Operator can override per-submit via the
    # batch payload; runtime override is clamped to [1, 64]. Section 8a Q2.
    "playlist_mgmt_batch_workers": 8,
    # Hard ceiling on items per batch submission. The UI's Playlist
    # Transfer page reads this as the per-submit max + offers a runtime
    # override in [1, max]. Defends against pathological multi-user
    # selections that would block the queue for hours. Section 8a Q3.
    "playlist_mgmt_batch_max_size": 200,
    # Per-source-server semaphore depth: at most this many in-flight
    # copy_playlist calls per source server, regardless of total
    # batch parallelism. Protects against single-server rate-limit
    # storms when two batches share a source. Section 8a Q7.
    #
    # The original locked default of 4
    # was halving throughput on the typical single-source batch
    # (batch_workers default 8, all items sharing one source -> only
    # 4 in-flight at once). Raised to 8 so the per-source cap matches
    # the worker default; operators who run concurrent batches against
    # the same source can lower this to reintroduce the rate-limit
    # safety.
    "playlist_mgmt_batch_per_source_workers": 8,

    # How the fuzzy-title resolver
    # handles AMBIGUOUS matches (multiple candidates remain after
    # title + type + artist/show filtering AND after the album /
    # path-tail tiebreakers have narrowed the set). Values:
    #   * 'strict'  - refuse to guess; record the item as missed.
    #                 Default for safety (no surprise wrong-track
    #                 writes). Mirrors the engine resolver's
    #                 strict_match=True behaviour.
    #   * 'first'   - pick the first candidate plexapi returned.
    #                 Useful when the operator knows their library
    #                 has duplicates but trusts Plex's ordering.
    #   * 'all'     - include EVERY candidate in the destination
    #                 playlist. Operator gets all versions of the
    #                 song they wanted; cleanup is manual.
    "playlist_mgmt_fuzzy_ambiguous_behavior": "strict",

    # Per-playlist parallelism for the
    # resolution loop. Each item's tier walk (GUID -> full-path ->
    # path-tail -> fuzzy) is independent once the path-tail index is
    # built, so resolution can run wide. Default 4 hits a sweet spot
    # against the per-source semaphore (which gates the BATCH-level
    # parallelism); operators on rate-limited Plex servers can drop
    # this to 1. Clamped to [1, 16].
    "playlist_mgmt_item_resolve_workers": 4,

    # Max age of the persisted
    # path-tail/full-path indexes in seconds. Indexes older than this
    # are discarded + rebuilt on next access. Default 86400 (1 day);
    # clamp [60, 30 days]. Set to 0 to disable persistence
    # entirely (always rebuild from a fresh walk).
    "playlist_cache_path_index_max_age_seconds": 86400,

    # Debounce delay (ms)
    # between the operator picking a destination server in the
    # Playlist Transfer UI and the frontend firing the pre-warm
    # POST. Default 3000 (3s) so a quick mis-click doesn't trigger
    # a build; clamp [0, 10000]. Set to 0 to fire immediately.
    "playlist_mgmt_prewarm_delay_ms": 3000,

    # ── Mixed-media playlists ──────────────────────────────────────
    # How J/E mixed-media playlists are handled when restoring to
    # Plex (which forbids mixed). 'skip' (default) silently skips
    # with logging; 'dominant' writes a single playlist using the
    # dominant media-type's items; 'split' writes N type-suffixed
    # playlists (e.g., "Workout [Audio]").
    "mixed_media_behavior": "skip",
    # Dominance threshold for 'dominant' mode. If the top media-type
    # is below this ratio, falls back to 'split' for the tied types.
    # End user locked default 0.60.
    "mixed_media_dominance_threshold": 0.60,
    # In 'library_dominant' video routing, when the playlist has a
    # tie between movies + TV counts, fall back to library-agnostic
    # (cross-library) or pick alphabetical?
    "mixed_media_video_routing": "library_agnostic",
    # Name-collision handling when the destination already has a
    # playlist with the same name. 'duplicate' (default, matches
    # existing engine behaviour); 'suffix' (e.g., "Workout (2)");
    # 'skip' (don't write; warn).
    "mixed_media_collision_handling": "duplicate",
    # Logging verbosity for mixed-media decisions. 'full' (every
    # decision logged); 'decisions_only' (only when non-default
    # action taken); 'off' (silent except errors).
    "mixed_media_logging": "full",

    # ── Engine Mirror DB ───────────────────────────────────────────
    # Global default mode for the per-server metadata mirror. Enum:
    # "auto" (consult mirror with freshness probe; D2) or
    # "always-live" (bypass mirror reads; resolution goes live).
    "engine_mirror_mode": "auto",
    # Force a full re-sync of any section whose mirror_synced_at is
    # older than this many seconds, regardless of probe verdict.
    # Default 24h. Clamp [0, 30d].
    "engine_mirror_max_age_seconds": 86400,
    # Background refresher cadence. Each tick probes all registered
    # servers + prunes old drift_events. Clamp [60, 86400].
    "engine_mirror_refresh_interval_seconds": 3600,
    # One-shot ack flag: first-run dialog has been shown. Set true
    # by the FE after the operator answers Yes / Not now.
    "engine_mirror_first_run_dialog_seen": False,
    # Drift event retention window (D4). Clamp [1, 365].
    "engine_mirror_drift_event_retention_days": 30,
    # Operator-facing warning threshold when mirror DB grows past
    # this many MB. Clamp [50, 10000].
    "engine_mirror_db_size_mb_warning": 500,
    # D3: even in always-live mode, the live walk writes through
    # to the mirror so flipping back to auto later is instant.
    "engine_mirror_always_live_writethrough": True,
    # D5: first-run "Yes" blocks vs returns immediately while the
    # background sync runs. Default false = immediate return +
    # background-threaded.
    "engine_mirror_first_run_blocking": False,
    # D5: how many sections per server walk in parallel during
    # first-run sync. Clamp [1, 8].
    "engine_mirror_first_run_workers": 2,
    # D6: snapshot writes through to mirror per section. Tunable
    # off only for forensic snapshots that must not touch mirror.
    "engine_mirror_snapshot_writethrough": True,
    # D8: symmetric source + dest mirroring for direct + fan-out.
    "engine_mirror_source_side": True,
    # XF: cross-feed Direction 1 — bootstrap mirror from
    # playlist_cache rows on each sync. Free data; zero API cost.
    "engine_mirror_bootstrap_from_playlist_cache": True,
    # Scheduler-driven pre-warm of mirror state ahead of a
    # scheduled job's fire time. Clamp [0, 3600].
    "schedule_prewarm_lead_seconds": 300,
    # Master kill-switch for scheduler-driven pre-warm.
    "schedule_prewarm_enabled": True,

    # ── Developer Console ───────────────────────────────────────────
    # The "Server Commands" developer
    # console - a root-admin-only top-level tab that exposes direct
    # per-item state mutation (watch count, ratings, favorites,
    # resume position, last-played date), raw per-call backend API
    # passthrough, and playlist / collection membership editing
    # against any registered server. It is a TESTING tool: it lets
    # the operator change state, snapshot it, restore it, and verify
    # round-trips without leaving the app.
    #
    # Default False: the console is a power-user / debugging surface,
    # off for normal operators. A developer or the test harness opts
    # in via settings.json -> tunables.dev_console_enabled. Even when
    # True, the tab + endpoints are additionally gated to the
    # root_admin role - the tunable is the second lock, not the only
    # one.
    "dev_console_enabled": False,
    # Heartbeat cadence for the dev-console WebSocket. The server the
    # operator is actively viewing gets the ACTIVE rate; a client with
    # no server selected gets the slower IDLE rate. Heartbeats are
    # job-status pings only - they never call a media-server API - so
    # the rate just governs how fast the "background job" banner
    # updates. Clamp [2, 600] / [5, 3600].
    "dev_console_heartbeat_active_seconds": 10,
    "dev_console_heartbeat_idle_seconds": 60,
    # Hard cap on how long a single dev-console connect attempt may
    # block before the request fails with 504 instead of leaving the
    # panel stuck on "Connecting...". Clamp [3, 120].
    "dev_console_connect_timeout_seconds": 25,
    # Background-refresh cadence for the per-server Server Commands
    # mirror database. The mirror is what the console panel reads, so
    # normal browsing never calls a media-server API. Per-server
    # overridable so one server can be tuned independently while the
    # operator works on it. Clamp [30, 86400].
    "dev_console_mirror_sync_seconds": 300,

    # ── Backend-agnostic affinity translation ───────────────────────
    # Per-user item
    # affinity has two faces - a numeric rating (Plex userRating,
    # 0-10) and a binary favorite (Jellyfin/Emby IsFavorite). When a
    # favorited item is restored onto a rating-only backend (Plex) it
    # carries no numeric rating of its own, so this value is written
    # in its place. A favorite is a strong positive signal, so the
    # default sits at the top of the 0-10 scale; lower it if you
    # treat favorites as merely "liked". Clamp [0.0, 10.0]. The
    # reverse direction (rating -> favorite) uses the per-job
    # favorite_threshold (engine default 5.0).
    "favorite_as_rating_value": 10.0,
}


# Allowed string-enum values for tunables whose accessor validates the
# stored value before returning it. Centralised here so the persistence
# layer, the accessor, and the UI all agree on the surface.
_OWNER_DISPLAY_STYLES = frozenset(("plex_owner", "custom_name", "custom_name_owner"))
_SCHEDULES_EDITOR_VISIBILITY = frozenset(("full", "on_create"))


def defaults() -> Dict[str, Any]:
    """Return a copy of the default values, for UI seeding."""
    return dict(_DEFAULTS)


# ── mtime-cached read ───────────────────────────────────────────────────────
#
# Tunables are read on every call to ``plex_connect_timeout()`` and
# friends, so the per-call cost has to be near-zero. The cache holds
# the parsed ``tunables`` sub-document keyed by the settings.json
# mtime; load_settings() / json.load() runs only when the file
# actually changed.

_LOCK = threading.Lock()
_CACHE_MTIME: float = -1.0
_CACHE_DATA: Dict[str, Any] = {}


def _read_tunables_block() -> Dict[str, Any]:
    """
    Return the merged ``tunables`` dict (defaults + on-disk overrides).
    Cheap on a cache hit; reads the file when settings.json mtime
    advances past the last cached value.
    """
    global _CACHE_MTIME, _CACHE_DATA
    # Local import keeps the dependency graph one-way (persistence
    # doesn't need to know about tunables).
    from server.persistence import _settings_path, load_settings
    try:
        mtime = _settings_path().stat().st_mtime
    except OSError:
        mtime = -1.0
    with _LOCK:
        if mtime == _CACHE_MTIME and _CACHE_DATA:
            return _CACHE_DATA
        try:
            settings = load_settings()
        except Exception:  # pragma: no cover (defensive)
            settings = {}
        raw = settings.get("tunables") if isinstance(settings, dict) else None
        merged = dict(_DEFAULTS)
        if isinstance(raw, dict):
            merged.update(raw)
        _CACHE_MTIME = mtime
        _CACHE_DATA = merged
        return merged


def invalidate_cache() -> None:
    """
    Drop the cached tunables block so the next getter call re-reads
    from disk. Save paths that bypass mtime (e.g. tests) can call this
    to force a refresh.
    """
    global _CACHE_MTIME, _CACHE_DATA
    with _LOCK:
        _CACHE_MTIME = -1.0
        _CACHE_DATA = {}


def get(key: str) -> Any:
    """
    Return the current value for ``key``. Falls back to the default
    when the on-disk document omits it. Unknown keys raise KeyError
    so a typo in a call site fails loudly rather than silently
    reading a missing default.
    """
    if key not in _DEFAULTS:
        raise KeyError(f"Unknown tunable: {key!r}")
    return _read_tunables_block().get(key, _DEFAULTS[key])


# ── Per-server tunable resolution ───────────────────────────────────────────
#
# Only the small set of tunables whose value can sensibly differ per
# server. Resolution: per-server override → global tunable → built-in
# default. Other tunables stay global-only and call ``get()`` directly.

_PER_SERVER_CAPABLE = frozenset((
    "plex_connect_timeout_seconds",
    "viewcount_increment_cap",
    "dev_console_mirror_sync_seconds",
))


def get_per_server(server_id: Optional[str], key: str) -> Any:
    """
    Resolve ``key`` for ``server_id``. When ``server_id`` is None or
    the key isn't per-server-capable, falls through to the global
    ``get(key)`` path. Unknown keys raise KeyError (same as ``get``).
    """
    if key not in _DEFAULTS:
        raise KeyError(f"Unknown tunable: {key!r}")
    if server_id and key in _PER_SERVER_CAPABLE:
        try:
            from server.persistence import load_settings
            settings = load_settings() or {}
        except Exception:
            settings = {}
        per_srv = (settings.get("tunables_per_server") or {}).get(server_id) or {}
        raw = per_srv.get(key)
        if raw is not None:
            return raw
    return get(key)


# ── Typed accessors (call-site API) ─────────────────────────────────────────
#
# These are thin typed wrappers over ``get()``. Call sites import the
# function they need and never touch the dict directly. Each function
# is documented with the literal it replaced, so a future grep
# "what was hardcoded here?" surfaces the answer.

# Networking & Retry
def plex_connect_timeout(server_id: Optional[str] = None) -> int:
    """
    Was ``timeout=120`` in server/library_walk.py.

    Per-server: pass ``server_id`` and the value falls back to the
    global tunable when no override exists. Used by library_walk and
    any other call site that knows which server it's talking to.
    """
    return int(get_per_server(server_id, "plex_connect_timeout_seconds"))


def plex_retry_total_budget() -> int:
    # CONSOLE-12: coerce-with-default + floor so a malformed stored
    # value can't crash the read. Default 4 (see _DEFAULTS).
    return max(1, _coerce_int(get("plex_retry_total_budget"), 4))


def plex_retry_backoff_factor() -> float:
    return float(get("plex_retry_backoff_factor"))


def server_ping_timeout() -> int:
    # CONSOLE-12: coerce-with-default + floor. Default 10 (see _DEFAULTS).
    return max(1, _coerce_int(get("server_ping_timeout_seconds"), 10))


def server_probe_timeout() -> int:
    return int(get("server_probe_timeout_seconds"))


def scrobble_get_timeout() -> int:
    # CONSOLE-12: coerce-with-default + floor. Default 10 (see _DEFAULTS).
    return max(1, _coerce_int(get("scrobble_get_timeout_seconds"), 10))


def scrobble_put_timeout() -> int:
    return int(get("scrobble_put_timeout_seconds"))


# Performance
def workers_default_cap() -> int:
    # CONSOLE-12: coerce-with-default + floor. Default 32 (see _DEFAULTS).
    return max(1, _coerce_int(get("workers_default_cap"), 32))


def scrobble_workers_default_cap() -> int:
    return int(get("scrobble_workers_default_cap"))


def import_user_workers_cap() -> int:
    # CONSOLE-12: coerce-with-default + floor. Default 8 (see _DEFAULTS).
    return max(1, _coerce_int(get("import_user_workers_cap"), 8))


def import_library_workers_cap() -> int:
    return int(get("import_library_workers_cap"))


def viewcount_increment_cap(server_id: Optional[str] = None) -> int:
    """Per-server: weaker servers benefit from a lower batch cap."""
    return int(get_per_server(server_id, "viewcount_increment_cap"))


def log_use_display_name() -> bool:
    """
    USER-MGMT-IDENTITY-AUDIT cosmetic toggle. True flips
    services.identity.user_display.display_for_logging into "substitute
    managed_users.display_name where present" mode. Logs read more
    naturally ("Crystal Jean did X" vs "crystalj1 did X") for
    non-power-users while leaving raw handles in the database
    untouched. Default false preserves every existing log line.
    """
    return bool(get("log_use_display_name"))


def auto_backfill_pin_from_identity_links() -> bool:
    """
    True iff a managed_users row joining an identity_map equivalence
    class should auto-inherit any PIN already stored on another row in
    the class. Default True so freshly-registered servers + newly-
    mapped users pick up the operator's PIN without re-entry.

    Additive only: backfill never overwrites a non-empty PIN column.
    When False, every row's PIN column is whatever the operator typed
    on that specific row (the legacy behaviour).
    """
    return bool(get("auto_backfill_pin_from_identity_links"))


def strict_identity_resolution() -> bool:
    """
    USER-MGMT-IDENTITY-AUDIT R-4 toggle. True flips the
    services.identity.user_resolution.resolve_destination_user chain into
    strict mode: it stops after step 2 (backend_user_id direct
    match) and refuses to fall back to case-insensitive username
    match. End users who want every cross-server user routing to
    come from an explicit identity_map row (or end user-confirmed
    preflight resolution) flip this true. Default false matches
    the lenient behaviour every existing flow assumed before this
    audit landed.
    """
    return bool(get("strict_identity_resolution"))


# ── Active-user-filter accessors ────────────────────────────────────────


def user_activity_sweeper_enabled() -> bool:
    """Master switch on the background user-activity sweeper.
    When False (default) the sweeper daemon never runs probes."""
    return bool(get("user_activity_sweeper_enabled"))


def user_activity_filter_enabled() -> bool:
    """Master switch on the engine-wide user-health filter.
    When False (default) services.user_management.activity_filter.list_active_users
    behaves exactly like today's list_managed_users(include_hidden=
    False) — tombstone gate only. When True, also drops users
    whose consecutive_auth_failures > 0."""
    return bool(get("user_activity_filter_enabled"))


def auto_tombstone_on_auth_error() -> bool:
    """When True an auth_error (401) probe result counts toward the
    auto-tombstone threshold. Off by default; recording still
    happens regardless, only triggering changes."""
    return bool(get("auto_tombstone_on_auth_error"))


def auto_tombstone_on_unreachable() -> bool:
    """When True an unreachable probe result counts toward the
    auto-tombstone threshold. Off by default. WARNING: a server
    outage long enough to span N sweep cycles would tombstone the
    entire roster, not just one user — leave off unless your
    threshold is generous enough to absorb routine maintenance."""
    return bool(get("auto_tombstone_on_unreachable"))


def user_activity_sweep_interval_hours() -> int:
    """How often the sweeper probes each enabled server. Clamped
    to [1, 168] (one hour to one week). Default 12."""
    try:
        v = int(get("user_activity_sweep_interval_hours"))
    except (TypeError, ValueError):
        return 12
    return max(1, min(168, v))


def user_activity_consecutive_failure_threshold() -> int:
    """Number of consecutive failing probes before the sweeper
    auto-tombstones a user (provided every other switch in §10.3
    of Finding[ACTIVE-USER-FILTER] lines up). Clamped to [1, 100].
    Default 3."""
    try:
        v = int(get("user_activity_consecutive_failure_threshold"))
    except (TypeError, ValueError):
        return 3
    return max(1, min(100, v))


def plexapi_autoreload_enabled() -> bool:
    """
    True iff plexapi's implicit per-item auto-reload should stay
    enabled on objects we touch. Default false (disabled) - when
    false, ``services.resolver._disable_autoreload`` actively turns
    ``_autoReload`` off on every object passed to it; when true, the
    helper is a no-op and vanilla plexapi behaviour returns.
    Diagnostic escape hatch only.
    """
    return bool(get("plexapi_autoreload_enabled"))


def dev_console_enabled() -> bool:
    """
    True iff the "Server Commands" developer console is exposed.
    Gates BOTH the top-level frontend tab and the
    ``/api/dev-console/*`` REST + WebSocket surface. Even when True,
    every endpoint is additionally root_admin-gated; this tunable is
    the on/off master switch, the role check is the access control.

    Defaults to False; a developer or the test harness opts in via
    ``settings.json`` -> ``tunables.dev_console_enabled``.
    """
    return bool(get("dev_console_enabled"))


def dev_console_heartbeat_active_seconds() -> int:
    """Heartbeat cadence for the dev-console server the operator is
    actively viewing. Clamp [2, 600]."""
    raw = get("dev_console_heartbeat_active_seconds")
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return 10
    return max(2, min(v, 600))


def dev_console_heartbeat_idle_seconds() -> int:
    """Heartbeat cadence for a dev-console client with no server
    selected. Clamp [5, 3600]."""
    raw = get("dev_console_heartbeat_idle_seconds")
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return 60
    return max(5, min(v, 3600))


def dev_console_connect_timeout_seconds() -> int:
    """Hard cap on a single dev-console connect attempt before the
    request fails with 504. Clamp [3, 120]."""
    raw = get("dev_console_connect_timeout_seconds")
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return 25
    return max(3, min(v, 120))


def dev_console_mirror_sync_seconds(server_id: Optional[str] = None) -> int:
    """Background-refresh cadence for a server's Server Commands mirror
    database. Per-server overridable. Clamp [30, 86400]."""
    raw = get_per_server(server_id, "dev_console_mirror_sync_seconds")
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return 300
    return max(30, min(v, 86400))


def cache_snapshot_payloads_to_media_db() -> bool:
    """
    True iff the end user has explicitly opted into ingesting
    snapshot / direct-transfer payloads into media.db on every run.
    Default false - the .db snapshot artifact and JSON sidecar are
    payload-direct (Rule 1) and don't need media.db. See the
    ``_should_cache_payload_to_media_db`` resolver in snapshotter for
    the first-run auto-seed exception layered on top of this value.
    """
    return bool(get("cache_snapshot_payloads_to_media_db"))


# Polling & Maintenance
def frontend_server_ping_interval_ms() -> int:
    # CONSOLE-12: coerce-with-default + floor. Default 30000 (see _DEFAULTS).
    return max(1, _coerce_int(get("frontend_server_ping_interval_ms"), 30000))


def scheduler_tick_seconds() -> int:
    """Was _TICK_SECONDS = 30 in server/schedules.py."""
    # CONSOLE-12: coerce-with-default + floor. Default 30.
    return max(1, _coerce_int(get("scheduler_tick_seconds"), 30))


def refresh_token_cleanup_interval_seconds() -> int:
    # CONSOLE-12: coerce-with-default + floor. Default 3600 (see _DEFAULTS).
    return max(
        1, _coerce_int(get("refresh_token_cleanup_interval_seconds"), 3600)
    )


def snapshot_sidecar_ttl_seconds() -> int:
    """
    How long a generated ``.plexexport.json`` sidecar is kept on disk
    before the background sweep reaps it. ``0`` disables the sweep
    (sidecars persist until the snapshot row is removed). Read on
    every sweep tick - changes take effect within one cadence.
    """
    # CONSOLE-12: coerce-with-default only -- no floor, because 0 is a
    # documented value (disables the sweep). Default 300 (see _DEFAULTS).
    return _coerce_int(get("snapshot_sidecar_ttl_seconds"), 300)


# Limits
def snapshot_retention_global_default() -> int:
    return int(get("snapshot_retention_global_default"))


def refresh_token_ttl_seconds() -> int:
    """Was REFRESH_TOKEN_TTL_SECONDS in server/auth_router.py."""
    # CONSOLE-12: coerce-with-default + floor. Default 7 days.
    return max(1, _coerce_int(get("refresh_token_ttl_seconds"), 7 * 24 * 60 * 60))


def jwt_access_token_ttl_seconds() -> int:
    """Was JWT_TTL_SECONDS in server/auth_router.py."""
    # CONSOLE-12: coerce-with-default + floor. Default 30 minutes.
    return max(1, _coerce_int(get("jwt_access_token_ttl_seconds"), 30 * 60))


def log_read_max_bytes() -> int:
    """Was _MAX_READ_BYTES = 16 * 1024 * 1024 in server/log_browser.py."""
    # CONSOLE-12: coerce-with-default + floor. Default 16 MiB.
    return max(1, _coerce_int(get("log_read_max_bytes"), 16 * 1024 * 1024))


# Danger Zone
def sqlite_busy_timeout_main() -> int:
    # CONSOLE-12: coerce-with-default + floor. Default 30 (see _DEFAULTS).
    return max(1, _coerce_int(get("sqlite_busy_timeout_main_seconds"), 30))


def sqlite_busy_timeout_short() -> int:
    return int(get("sqlite_busy_timeout_short_seconds"))


def favorite_as_rating_value() -> float:
    """Numeric rating (0-10) written in place of a favorite when a
    favorited item is restored onto a rating-only backend (Plex).
    See _DEFAULTS for the rationale. Clamped to [0.0, 10.0]; a bad
    config falls through to the 10.0 default."""
    try:
        v = float(get("favorite_as_rating_value"))
    except Exception:
        v = 10.0
    return max(0.0, min(10.0, v))


def http_pool_connections() -> int:
    # CONSOLE-12: coerce-with-default + floor. Default 4 (see _DEFAULTS).
    return max(1, _coerce_int(get("http_pool_connections"), 4))


def http_pool_maxsize_cap() -> int:
    # CONSOLE-12: coerce-with-default + floor. Default 10 (see _DEFAULTS).
    return max(1, _coerce_int(get("http_pool_maxsize_cap"), 10))


def snapshot_user_pass_prefer_server_side() -> bool:
    """
    When True, per-user gathers on show/artist libraries override the
    smart-strategy always-bulk rule and use the server-side filter
    path instead. Owner pass is unaffected. See the tunable docstring
    in ``_DEFAULTS`` for the rationale (sparse watch history per
    managed user makes the server-side filter dramatically cheaper).
    """
    return bool(get("snapshot_user_pass_prefer_server_side") or False)


# Dashboard ETR colour multiplier
def etr_color_multiplier() -> float:
    """
    Scales the dashboard's per-phase amber/red thresholds. Clamped to
    [0.5, 2.0] at the read boundary so a bad value can't bury phase
    health under absurdly long or short windows.
    """
    raw = float(get("etr_color_multiplier") or 1.0)
    if raw < 0.5:
        return 0.5
    if raw > 2.0:
        return 2.0
    return raw


# Activity-feed owner display
def owner_display_style() -> str:
    """
    End user-visible label style for the Plex owner in the dashboard
    activity feed. Returns one of ``"plex_owner"``, ``"custom_name"``,
    or ``"custom_name_owner"``. Unknown values fall through to
    ``"plex_owner"`` so a typo in settings.json never crashes a run.

    The actual label resolution lives in :mod:`services.user_management.labels`,
    which combines this style with the live dashboard's cached
    ``user_display_names`` map.
    """
    raw = str(get("owner_display_style") or "plex_owner")
    if raw not in _OWNER_DISPLAY_STYLES:
        return "plex_owner"
    return raw


# UI & Display caps
def _coerce_int(raw: Any, default: int) -> int:
    """
    Defensive int() that uses ``default`` only when ``raw`` is None or
    fails to coerce. Unlike ``int(raw or default)``, this preserves 0
    (which is below clamp floors but should still go through the
    clamp logic so the end user's intent is visible).
    """
    if raw is None:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def restoration_summary_panel_max_items() -> int:
    """
    Visible-row cap for the in-dashboard Restoration Summary panel.
    Clamped to [1, 500] at the read boundary so a malformed value in
    settings.json cannot blow up the viewport or hide every row.
    """
    raw = _coerce_int(get("restoration_summary_panel_max_items"), 10)
    if raw < 1:
        return 1
    if raw > 500:
        return 500
    return raw


def user_count_tooltip_delay_ms() -> int:
    """
    Hover delay (ms) before the user-count tooltip reveals its list.
    Clamped to [100, 3000] so the tooltip is neither instant nor
    effectively unreachable.
    """
    raw = _coerce_int(get("user_count_tooltip_delay_ms"), 600)
    if raw < 100:
        return 100
    if raw > 3000:
        return 3000
    return raw


def user_count_tooltip_max_items() -> int:
    """
    How many user names the user-count tooltip lists before the
    "... +N more" overflow line. Clamped to [1, 200] so the popover
    never grows beyond a reasonable size.
    """
    raw = _coerce_int(get("user_count_tooltip_max_items"), 20)
    if raw < 1:
        return 1
    if raw > 200:
        return 200
    return raw


def servers_panel_show_server_uid() -> bool:
    """
    True iff the end user opted into showing the prefixed server UID
    (``<service_type>_<uuid4_hex>``) in the Servers > Overview
    registered-server table. Default False so existing end users see
    no UI change on upgrade. Boolean read: any truthy value enables;
    anything else (including missing key / typo / None) returns False.
    """
    raw = get("servers_panel_show_server_uid")
    return bool(raw) if raw is not None else False


def schedules_editor_visibility() -> str:
    """
    How the Run-Job-shaped editor renders on the Schedules sub-tab.
    Returns one of ``"full"`` (editor always open on tab load, bound
    to the currently selected schedule or a "new schedule" draft) or
    ``"on_create"`` (editor hidden until New / Edit is clicked).
    Unknown values fall through to ``"full"``.
    """
    raw = str(get("schedules_editor_visibility") or "full")
    if raw not in _SCHEDULES_EDITOR_VISIBILITY:
        return "full"
    return raw


# Danger Zone: developer-mode runtime toggle
def developer_mode_enabled() -> bool:
    """
    True iff the end user has flipped the in-Tunables developer-mode
    switch. The env-var precedence in :mod:`server.debug_mode` means
    this is only consulted when ``PLEXMIGRATE_DEBUG_MODE`` is unset
    or falsy; the env var ALWAYS WINS when set. See the
    ``server.debug_mode`` module docstring for the full precedence
    explanation.

    Default False. A non-bool stored value reads as False so a typo
    in settings.json cannot accidentally unlock the Developer tab.
    """
    raw = get("developer_mode_enabled")
    return raw is True


# ── Playlist Management cache tunables ──────────────────────────────────────

def playlist_cache_enabled() -> bool:
    """Master switch for the playlist cache. When False, every read
    goes to the live backend API."""
    raw = get("playlist_cache_enabled")
    return bool(raw) if raw is not None else True


def playlist_cache_refresh_interval_seconds() -> int:
    """Default refresh window for the cache. Clamped to [60, 86400]."""
    raw = _coerce_int(get("playlist_cache_refresh_interval_seconds"), 1800)
    if raw < 60:
        return 60
    if raw > 86400:
        return 86400
    return raw


def playlist_cache_snapshot_threshold_seconds() -> int:
    """Snapshot can read from the cache if it was refreshed within
    this window. Clamped to [60, 43200]."""
    raw = _coerce_int(get("playlist_cache_snapshot_threshold_seconds"), 900)
    if raw < 60:
        return 60
    if raw > 43200:
        return 43200
    return raw


def playlist_cache_max_age_seconds() -> int:
    """Hard invalidation: cache rows older than this are dropped on
    read. Clamped to [3600, 604800]."""
    raw = _coerce_int(get("playlist_cache_max_age_seconds"), 43200)
    if raw < 3600:
        return 3600
    if raw > 604800:
        return 604800
    return raw


def playlist_cache_background_refresh_enabled() -> bool:
    """Default ON. When True, a
    background thread refreshes stale caches automatically. Operators
    on rate-limited backends can flip this False to revert to manual
    + on-pick refresh only."""
    raw = get("playlist_cache_background_refresh_enabled")
    return bool(raw) if raw is not None else True


def playlist_cache_background_refresh_interval_seconds() -> int:
    """Cadence in seconds for the background playlist-cache refresher
    thread. Operator-locked default 900 (15 min); clamped
    to [60, 86400] so callers can't disable it by tuning to 0 (use
    the enabled toggle instead) or starve the loop with a multi-week
    interval that effectively never fires."""
    raw = get("playlist_cache_background_refresh_interval_seconds")
    try:
        val = int(raw) if raw is not None else 900
    except (TypeError, ValueError):
        return 900
    if val < 60:
        return 60
    if val > 86400:
        return 86400
    return val


_PLEX_HOME_AUTH_MODES = frozenset(("owner_token", "per_user_token"))


def playlist_mgmt_plex_home_auth_mode() -> str:
    """Plex Home user auth path for Playlist Mgmt copies. Default
    'owner_token'. Unknown values fall back to the default."""
    raw = get("playlist_mgmt_plex_home_auth_mode")
    if not isinstance(raw, str) or raw not in _PLEX_HOME_AUTH_MODES:
        return "owner_token"
    return raw


_PLAYLIST_MGMT_SAME_USER_MODES = frozenset(("skip", "duplicate"))


def playlist_mgmt_same_user_behavior() -> str:
    """How a Playlist Mgmt copy whose (source server, source user)
    resolves to the same (dest server, dest user) is handled. The
    common case: the end user dragged a playlist onto themselves in
    fan-out mode and didn't notice.

    Values:
      * ``'skip'`` (default) — recognize the no-op and return a
        skipped result immediately, no source-load / resolve / write.
      * ``'duplicate'`` — proceed with the copy. Produces a second
        playlist with the same content under the same user. Some end
        users intentionally do this to bulk-fork a playlist before
        editing one side.

    Unknown values fall back to 'skip'."""
    raw = get("playlist_mgmt_same_user_behavior")
    if not isinstance(raw, str) or raw not in _PLAYLIST_MGMT_SAME_USER_MODES:
        return "skip"
    return raw


# ── Playlist Transfer batch tunables ────────────────────────────────────────


def playlist_mgmt_batch_workers() -> int:
    """Default worker count for the batch primitive's
    ThreadPoolExecutor. Default 8, operator-tunable. Clamped to
    [1, 64] (the upper
    bound is a soft sanity limit; 64 already saturates most LAN setups
    and exposes the source server to enough concurrent reads to trip
    rate limits without the per-source semaphore in
    playlist_mgmt_batch_per_source_workers).

    Unknown / non-numeric values fall back to the default."""
    raw = get("playlist_mgmt_batch_workers")
    try:
        val = int(raw) if raw is not None else 8
    except (TypeError, ValueError):
        return 8
    if val < 1:
        return 1
    if val > 64:
        return 64
    return val


def playlist_mgmt_batch_max_size() -> int:
    """Hard ceiling on items per batch submission. Per Plan section
    8a Q3: default 200, operator-tunable. The UI reads this as the
    per-submit max + offers a runtime override in [1, max]. Clamped
    to [1, 10000].

    Unknown / non-numeric values fall back to the default."""
    raw = get("playlist_mgmt_batch_max_size")
    try:
        val = int(raw) if raw is not None else 200
    except (TypeError, ValueError):
        return 200
    if val < 1:
        return 1
    if val > 10000:
        return 10000
    return val


def playlist_mgmt_batch_per_source_workers() -> int:
    """Per-source-server semaphore depth: at most this many in-flight
    copy_playlist calls per source server, regardless of total batch
    parallelism. Per Plan section 8a Q7: default 4, operator-tunable.
    Clamped to [1, 32]. Setting this AT or ABOVE
    playlist_mgmt_batch_workers effectively disables the per-source
    cap (the global pool becomes the bottleneck).

    Unknown / non-numeric values fall back to the default."""
    raw = get("playlist_mgmt_batch_per_source_workers")
    try:
        val = int(raw) if raw is not None else 4
    except (TypeError, ValueError):
        return 4
    if val < 1:
        return 1
    if val > 32:
        return 32
    return val


_PLAYLIST_FUZZY_AMBIGUOUS_MODES = frozenset(("strict", "first", "all"))


def playlist_mgmt_prewarm_delay_ms() -> int:
    """Debounce delay (milliseconds) between destination-server
    pick + the pre-warm POST. Operator-locked default
    3000; clamp [0, 10000]. The frontend reads this from the
    settings endpoint and uses it as the setTimeout interval."""
    raw = get("playlist_mgmt_prewarm_delay_ms")
    try:
        val = int(raw) if raw is not None else 3000
    except (TypeError, ValueError):
        return 3000
    if val < 0:
        return 0
    if val > 10000:
        return 10000
    return val


def playlist_cache_path_index_max_age_seconds() -> int:
    """Max age (seconds) of the persisted path-tail / full-path
    indexes before they're treated as stale + rebuilt.
    Operator-locked default 86400 (1 day). Clamp [0, 30 days].
    Set to 0 to disable persistence entirely."""
    raw = get("playlist_cache_path_index_max_age_seconds")
    try:
        val = int(raw) if raw is not None else 86400
    except (TypeError, ValueError):
        return 86400
    if val < 0:
        return 0
    # 30-day cap; longer than that and stale data drift becomes
    # surprising for operators.
    if val > 30 * 86400:
        return 30 * 86400
    return val


def playlist_mgmt_item_resolve_workers() -> int:
    """Per-playlist parallelism for the item resolution loop. Each
    item's tier walk is independent once the path-tail index is
    built, so resolution can run wide. Operator-locked
    default 4; clamped to [1, 16]. Setting to 1 disables the
    parallelism and reverts to the sequential loop."""
    raw = get("playlist_mgmt_item_resolve_workers")
    try:
        val = int(raw) if raw is not None else 4
    except (TypeError, ValueError):
        return 4
    if val < 1:
        return 1
    if val > 16:
        return 16
    return val


def playlist_mgmt_fuzzy_ambiguous_behavior() -> str:
    """How the playlist-copy fuzzy-title resolver handles AMBIGUOUS
    matches (multiple candidates survive title + type + artist/show
    filtering AND the album / path-tail tiebreakers). Operator-locked.

    Values:
      * 'strict' (default) — refuse to guess; the item is recorded
        as missed. Safest; no surprise wrong-track writes.
      * 'first' — pick the first candidate plexapi returned.
      * 'all' — include every candidate in the destination playlist.

    Unknown values fall back to 'strict'."""
    raw = get("playlist_mgmt_fuzzy_ambiguous_behavior")
    if not isinstance(raw, str) or raw not in _PLAYLIST_FUZZY_AMBIGUOUS_MODES:
        return "strict"
    return raw


# ── Mixed-media playlist tunables ───────────────────────────────────────────

_MIXED_MEDIA_BEHAVIORS = frozenset(("skip", "dominant", "split"))
_MIXED_MEDIA_VIDEO_ROUTING = frozenset(("library_agnostic", "library_dominant"))
_MIXED_MEDIA_COLLISION = frozenset(("duplicate", "suffix", "skip"))
_MIXED_MEDIA_LOGGING = frozenset(("full", "decisions_only", "off"))


def mixed_media_behavior() -> str:
    """How J/E mixed-media playlists are handled when restoring to
    Plex. 'skip' (default) / 'dominant' / 'split'."""
    raw = get("mixed_media_behavior")
    if not isinstance(raw, str) or raw not in _MIXED_MEDIA_BEHAVIORS:
        return "skip"
    return raw


def mixed_media_dominance_threshold() -> float:
    """Top media-type ratio required for 'dominant' mode to fire.
    Clamped to [0.0, 1.0]. Default 0.60."""
    raw = get("mixed_media_dominance_threshold")
    try:
        val = float(raw) if raw is not None else 0.60
    except (TypeError, ValueError):
        return 0.60
    if val < 0.0:
        return 0.0
    if val > 1.0:
        return 1.0
    return val


def mixed_media_video_routing() -> str:
    """In library_dominant video routing, fall back to
    library_agnostic on movies+TV ties (default)."""
    raw = get("mixed_media_video_routing")
    if not isinstance(raw, str) or raw not in _MIXED_MEDIA_VIDEO_ROUTING:
        return "library_agnostic"
    return raw


def mixed_media_collision_handling() -> str:
    """Name-collision behaviour when dest already has the playlist
    name. 'duplicate' (default) / 'suffix' / 'skip'."""
    raw = get("mixed_media_collision_handling")
    if not isinstance(raw, str) or raw not in _MIXED_MEDIA_COLLISION:
        return "duplicate"
    return raw


def mixed_media_logging() -> str:
    """Verbosity for mixed-media engine decisions. 'full' /
    'decisions_only' / 'off'."""
    raw = get("mixed_media_logging")
    if not isinstance(raw, str) or raw not in _MIXED_MEDIA_LOGGING:
        return "full"
    return raw


# ── Engine Mirror DB tunables ───────────────────────────────────────────────

_ENGINE_MIRROR_MODES = frozenset(("auto", "always-live"))


def engine_mirror_mode() -> str:
    """Global default for mirror mode. 'auto' (consult mirror with
    probe; default) or 'always-live' (skip mirror, go live)."""
    raw = get("engine_mirror_mode")
    if not isinstance(raw, str) or raw not in _ENGINE_MIRROR_MODES:
        return "auto"
    return raw


def engine_mirror_max_age_seconds() -> int:
    """Force a full re-sync of any section whose mirror_synced_at is
    older than this many seconds, regardless of probe verdict.
    Default 24h. Clamp [0, 30d]."""
    raw = get("engine_mirror_max_age_seconds")
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return 86400
    return max(0, min(v, 30 * 86400))


def engine_mirror_refresh_interval_seconds() -> int:
    """Background refresher cadence in seconds. Clamp [60, 86400]."""
    raw = get("engine_mirror_refresh_interval_seconds")
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return 3600
    return max(60, min(v, 86400))


def engine_mirror_first_run_dialog_seen() -> bool:
    """One-shot ack flag for the first-run dialog."""
    return bool(get("engine_mirror_first_run_dialog_seen"))


def engine_mirror_drift_event_retention_days() -> int:
    """Drift event retention window. Clamp [1, 365]."""
    raw = get("engine_mirror_drift_event_retention_days")
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return 30
    return max(1, min(v, 365))


def engine_mirror_db_size_mb_warning() -> int:
    """Mirror DB size warning threshold (MB). Clamp [50, 10000]."""
    raw = get("engine_mirror_db_size_mb_warning")
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return 500
    return max(50, min(v, 10000))


def engine_mirror_always_live_writethrough() -> bool:
    """When always-live mode is selected, still update the mirror
    as a side effect (D3). Default true."""
    raw = get("engine_mirror_always_live_writethrough")
    if raw is None:
        return True
    return bool(raw)


def engine_mirror_first_run_blocking() -> bool:
    """D5: first-run 'Yes' blocks vs returns immediately."""
    return bool(get("engine_mirror_first_run_blocking"))


def engine_mirror_first_run_workers() -> int:
    """D5: parallel sections per server during first-run sync.
    Clamp [1, 8]."""
    raw = get("engine_mirror_first_run_workers")
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return 2
    return max(1, min(v, 8))


def engine_mirror_snapshot_writethrough() -> bool:
    """D6: snapshot's per-section bulk write-through to mirror.
    Default true."""
    raw = get("engine_mirror_snapshot_writethrough")
    if raw is None:
        return True
    return bool(raw)


def engine_mirror_source_side() -> bool:
    """D8: source servers also get mirrored. Default true."""
    raw = get("engine_mirror_source_side")
    if raw is None:
        return True
    return bool(raw)


def engine_mirror_bootstrap_from_playlist_cache() -> bool:
    """XF: cross-feed Direction 1. Default true."""
    raw = get("engine_mirror_bootstrap_from_playlist_cache")
    if raw is None:
        return True
    return bool(raw)


def schedule_prewarm_lead_seconds() -> int:
    """How far ahead of a scheduled job to fire mirror pre-warm.
    Clamp [0, 3600]."""
    raw = get("schedule_prewarm_lead_seconds")
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return 300
    return max(0, min(v, 3600))


def schedule_prewarm_enabled() -> bool:
    """Master kill-switch for scheduler-driven pre-warm."""
    raw = get("schedule_prewarm_enabled")
    if raw is None:
        return True
    return bool(raw)
