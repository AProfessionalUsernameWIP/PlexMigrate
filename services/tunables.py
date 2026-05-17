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
    # USER-MGMT-IDENTITY-AUDIT cosmetic follow-on: substitute the
    # stored ``display_name`` for the raw ``username`` in log lines
    # and run-history fields that reference a user. Default False
    # (logs read the raw handle every existing end user is used to).
    # When true, services.user_display.display_for_logging substitutes
    # the managed_users.display_name when present and falls back to
    # the username when no display_name is stored. Pure presentation
    # tweak; routing / identity_map logic is unaffected.
    "log_use_display_name": False,
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
    # ``services.snapshotter._should_cache_payload_to_media_db``)
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

    # ── Adaptive ETA / training engine (Danger) ────────────────────
    # All five values shape the engine-anchored progress ETA the
    # dashboard surfaces as "Estimated remaining". They were
    # hardcoded constants until the 2026-05-16 cold-start undershoot
    # bug surfaced (a 6-minute run jumped from "Calculating..." to
    # "Almost done" because the cold-start prediction was 70s).
    # Surfaced under the Danger tab because tuning these can
    # legitimately make the dashboard's "remaining" estimate
    # misleading; end users should change them only with intent.

    # Floor for the predicted ETR during the wall-clock decay
    # window (before the engine has discovered any work to
    # anchor against). Expressed as a fraction of predicted_total.
    # 0.15 means: "even if the cold-start prediction is way off, the
    # displayed remaining never drops below 15% of it." Once the
    # engine reports progress totals, the anchored math kicks in and
    # can grow past the original prediction if the run is slower.
    "eta_wallclock_floor_fraction": 0.15,

    # The observed-ratio (actual_elapsed / expected_elapsed)
    # calibration is clamped between these bounds so a single
    # anomalous phase cannot teleport the displayed ETA. 0.25 means
    # "never display less than 25% of the predicted remaining";
    # 4.0 means "never display more than 4x the predicted remaining."
    "eta_calibration_ratio_min": 0.25,
    "eta_calibration_ratio_max": 4.0,

    # Completion fraction at which calibration blend reaches full
    # weight. Before this, the displayed value blends from neutral
    # (1.0) toward the observed ratio; this protects against early
    # noisy timing dominating the estimate. 0.25 = full calibration
    # weight at 25% complete; smaller = react faster but jumpier.
    "eta_calibration_blend_threshold": 0.25,

    # Asymmetric deflation on the calibration's speedup side. When
    # the engine appears faster than predicted (observed_ratio < 1.0)
    # it is often because lightweight metrics finished first and the
    # item-completion fraction outran the actual time-completion
    # fraction. Multiplying the speedup credit by this factor (default
    # 0.5 = "credit 50% of the apparent speedup") keeps a quick early
    # phase from cratering the displayed ETA. Set to 1.0 for the old
    # symmetric behaviour; lower values are more conservative.
    "eta_calibration_deflation_strength": 0.5,

    # Library completion fraction the dashboard requires before it
    # will display "Almost done" instead of the literal remaining
    # seconds. 0.85 means: until 85% of the engine's items are
    # complete, even a sub-5-second projected ETA shows as a number
    # (no "Almost done" copy).
    "eta_almost_done_progress_threshold": 0.85,

    # Per-label cold-start defaults the trainer's tier-5 fallback
    # uses on a brand-new install with zero history. A JSON object;
    # keys are the time_operation labels. Each value is a two-element
    # list [fixed_seconds, seconds_per_item] so the cold-start guess
    # scales with library size: a 200-item Music library no longer
    # inherits the same per-step time as a 50k-item Movies library.
    # End users may also pass a single scalar for fixed-only timing;
    # the loader accepts either shape.
    "eta_tier5_defaults_seconds": {
        "snapshot_watch_history":  [15.0, 0.002],
        "snapshot_ratings":        [10.0, 0.001],
        "snapshot_playlists":      [10.0, 0.0],
        "snapshot_collections":    [10.0, 0.0],
        "bulk_fetch_for_filters":  [3.0, 0.0005],
        "restore_watch_history":   [15.0, 0.003],
        "restore_ratings":         [10.0, 0.002],
        "restore_playlists":       [10.0, 0.0],
        "restore_collections":     [10.0, 0.0],
        "direct_library_transfer": [10.0, 0.005],
    },

    # ETA cascade: when true (default), the cross-server fallback tiers
    # (3 and 4) are skipped so the prediction never borrows another
    # server's data. A brand-new server lands at tier 5 (rate-based
    # default) until it has its own training data. Set false to restore
    # the legacy cross-server cold-start fallback.
    "eta_strict_per_server": True,

    # ETA latency offset: post-regression multiplier driven by current
    # vs trained-time ping. The four tunables below shape the
    # asymmetric clamp; defaults are bounded and conservative on both
    # sides. Set ``eta_latency_offset_enabled`` to false to disable the
    # multiplier entirely (predictions stay at the regression value).
    "eta_latency_offset_enabled": True,
    "eta_latency_inflation_strength": 0.5,
    "eta_latency_inflation_cap": 2.0,
    "eta_latency_deflation_strength": 0.3,
    "eta_latency_deflation_floor": 0.8,

    # ETA engine concurrency model: the snapshotter's owner phase uses
    # a 4-worker pool to run the four metric gathers concurrently, and
    # the per-user phase uses an 8-worker pool. These tunables let the
    # predictor stay in sync if the engine's pool sizes change.
    "eta_metric_parallelism": 4,
    "eta_user_parallelism": 8,

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
    # locked at add-server time per Plan[SERVER-UID-IDENTITY] and
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

    # ── Plan[PLAYLIST-MANAGEMENT] 2026-05-16 (end user-locked) ──────
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
    # automatically. Default OFF per end user (opt-in) to avoid
    # surprise API load on end users who haven't tuned thresholds.
    "playlist_cache_background_refresh_enabled": False,
    # Plex Home user auth path for Playlist Mgmt copies. Default
    # 'owner_token' uses the owner's token + the Home user's UserId
    # in the URL/body (matches existing _apply_per_user_block
    # pattern). 'per_user_token' uses the Home user's own
    # PIN-unlocked token from managed_users (more isolated; needs
    # PIN). End user can switch at runtime for debugging.
    "playlist_mgmt_plex_home_auth_mode": "owner_token",

    # 2026-05-17 (operator request): same-user no-op short-circuit
    # for Playlist Mgmt copies. 'skip' (default) returns success +
    # skipped=True without doing any work when the resolved source +
    # destination are the same (server, user). 'duplicate' lets the
    # copy proceed and creates a second playlist under the same user.
    "playlist_mgmt_same_user_behavior": "skip",

    # ── Plan[MIXED-MEDIA-PLAYLISTS] 2026-05-16 (end user-locked) ────
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
    return int(get("plex_retry_total_budget"))


def plex_retry_backoff_factor() -> float:
    return float(get("plex_retry_backoff_factor"))


def server_ping_timeout() -> int:
    return int(get("server_ping_timeout_seconds"))


def server_probe_timeout() -> int:
    return int(get("server_probe_timeout_seconds"))


def scrobble_get_timeout() -> int:
    return int(get("scrobble_get_timeout_seconds"))


def scrobble_put_timeout() -> int:
    return int(get("scrobble_put_timeout_seconds"))


# Performance
def workers_default_cap() -> int:
    return int(get("workers_default_cap"))


def scrobble_workers_default_cap() -> int:
    return int(get("scrobble_workers_default_cap"))


def import_user_workers_cap() -> int:
    return int(get("import_user_workers_cap"))


def import_library_workers_cap() -> int:
    return int(get("import_library_workers_cap"))


def viewcount_increment_cap(server_id: Optional[str] = None) -> int:
    """Per-server: weaker servers benefit from a lower batch cap."""
    return int(get_per_server(server_id, "viewcount_increment_cap"))


def log_use_display_name() -> bool:
    """
    USER-MGMT-IDENTITY-AUDIT cosmetic toggle. True flips
    services.user_display.display_for_logging into "substitute
    managed_users.display_name where present" mode. Logs read more
    naturally ("Crystal Jean did X" vs "crystalj1 did X") for
    non-power-users while leaving raw handles in the database
    untouched. Default false preserves every existing log line.
    """
    return bool(get("log_use_display_name"))


def strict_identity_resolution() -> bool:
    """
    USER-MGMT-IDENTITY-AUDIT R-4 toggle. True flips the
    services.user_resolution.resolve_destination_user chain into
    strict mode: it stops after step 2 (backend_user_id direct
    match) and refuses to fall back to case-insensitive username
    match. End users who want every cross-server user routing to
    come from an explicit identity_map row (or end user-confirmed
    preflight resolution) flip this true. Default false matches
    the lenient behaviour every existing flow assumed before this
    audit landed.
    """
    return bool(get("strict_identity_resolution"))


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
    return int(get("frontend_server_ping_interval_ms"))


def scheduler_tick_seconds() -> int:
    """Was _TICK_SECONDS = 30 in server/schedules.py."""
    return int(get("scheduler_tick_seconds"))


def refresh_token_cleanup_interval_seconds() -> int:
    return int(get("refresh_token_cleanup_interval_seconds"))


def snapshot_sidecar_ttl_seconds() -> int:
    """
    How long a generated ``.plexexport.json`` sidecar is kept on disk
    before the background sweep reaps it. ``0`` disables the sweep
    (sidecars persist until the snapshot row is removed). Read on
    every sweep tick - changes take effect within one cadence.
    """
    return int(get("snapshot_sidecar_ttl_seconds"))


# Limits
def snapshot_retention_global_default() -> int:
    return int(get("snapshot_retention_global_default"))


def refresh_token_ttl_seconds() -> int:
    """Was REFRESH_TOKEN_TTL_SECONDS in server/auth_router.py."""
    return int(get("refresh_token_ttl_seconds"))


def jwt_access_token_ttl_seconds() -> int:
    """Was JWT_TTL_SECONDS in server/auth_router.py."""
    return int(get("jwt_access_token_ttl_seconds"))


def log_read_max_bytes() -> int:
    """Was _MAX_READ_BYTES = 16 * 1024 * 1024 in server/log_browser.py."""
    return int(get("log_read_max_bytes"))


# Danger Zone
def sqlite_busy_timeout_main() -> int:
    return int(get("sqlite_busy_timeout_main_seconds"))


def sqlite_busy_timeout_short() -> int:
    return int(get("sqlite_busy_timeout_short_seconds"))


def eta_wallclock_floor_fraction() -> float:
    """Floor for predicted ETR wall-clock decay, as a fraction of
    predicted_total. Clamped to [0.0, 1.0] so a bad config can't
    push the floor above the prediction itself."""
    try:
        v = float(get("eta_wallclock_floor_fraction"))
    except Exception:
        v = 0.15
    return max(0.0, min(1.0, v))


def eta_calibration_ratio_bounds() -> tuple:
    """``(min, max)`` clamps for the observed/expected ratio used to
    calibrate the anchored ETR. Defaults (0.25, 4.0). Bad values
    fall through to defaults."""
    try:
        lo = float(get("eta_calibration_ratio_min"))
        hi = float(get("eta_calibration_ratio_max"))
    except Exception:
        lo, hi = 0.25, 4.0
    if lo <= 0 or hi <= 0 or hi <= lo:
        lo, hi = 0.25, 4.0
    return (lo, hi)


def eta_calibration_blend_threshold() -> float:
    """Completion fraction at which calibration reaches full weight.
    Clamped to (0, 1]."""
    try:
        v = float(get("eta_calibration_blend_threshold"))
    except Exception:
        v = 0.25
    if v <= 0:
        v = 0.25
    return min(1.0, v)


def eta_calibration_deflation_strength() -> float:
    """Multiplier on the speedup credit when ``observed_ratio < 1.0``.
    Lower values yield diminishing returns on apparent speedups so the
    displayed ETA does not crater when lightweight metrics finish
    first. Clamped to [0, 1]; defaults to 0.5."""
    try:
        v = float(get("eta_calibration_deflation_strength"))
    except Exception:
        v = 0.5
    return max(0.0, min(1.0, v))


def eta_almost_done_progress_threshold() -> float:
    """Library-completion fraction below which the dashboard refuses
    to render 'Almost done'. Clamped to [0, 1]."""
    try:
        v = float(get("eta_almost_done_progress_threshold"))
    except Exception:
        v = 0.85
    return max(0.0, min(1.0, v))


def eta_strict_per_server() -> bool:
    """When true (default), the cascade skips tiers 3-4 so cross-server
    data never influences a per-server prediction."""
    try:
        return bool(get("eta_strict_per_server"))
    except Exception:
        return True


def eta_latency_offset_enabled() -> bool:
    try:
        return bool(get("eta_latency_offset_enabled"))
    except Exception:
        return True


def _eta_latency_clamped(name: str, default: float,
                         lo: float, hi: float) -> float:
    """Read one of the four latency-shape tunables; clamp to a sane
    range so a typo in settings.json can't produce a nonsensical
    multiplier."""
    try:
        v = float(get(name))
    except Exception:
        return default
    if v < lo or v > hi:
        return default
    return v


def eta_latency_inflation_strength() -> float:
    return _eta_latency_clamped("eta_latency_inflation_strength", 0.5, 0.0, 2.0)


def eta_latency_inflation_cap() -> float:
    return _eta_latency_clamped("eta_latency_inflation_cap", 2.0, 1.0, 5.0)


def eta_latency_deflation_strength() -> float:
    return _eta_latency_clamped("eta_latency_deflation_strength", 0.3, 0.0, 1.0)


def eta_latency_deflation_floor() -> float:
    return _eta_latency_clamped("eta_latency_deflation_floor", 0.8, 0.1, 1.0)


def eta_metric_parallelism() -> int:
    """Owner-phase metric-pool size; matches the snapshotter's 4-worker
    ThreadPoolExecutor for the owner gather. End users rarely need to
    change this."""
    try:
        v = int(get("eta_metric_parallelism"))
    except Exception:
        return 4
    return max(1, v)


def eta_user_parallelism() -> int:
    """Per-user pool size; matches the snapshotter's 8-worker
    ThreadPoolExecutor for the managed-user gather."""
    try:
        v = int(get("eta_user_parallelism"))
    except Exception:
        return 8
    return max(1, v)


def eta_tier5_defaults_seconds() -> dict:
    """Per-label cold-start defaults. Each value is either a
    ``[fixed_seconds, seconds_per_item]`` pair OR a single scalar
    (interpreted as ``[scalar, 0.0]``). Returns the parsed dict on
    success; falls back to an empty dict on any failure so callers
    resort to their hardcoded last-resort floor."""
    raw = get("eta_tier5_defaults_seconds")
    if not isinstance(raw, dict):
        return {}
    out: dict = {}
    for k, v in raw.items():
        try:
            if isinstance(v, (list, tuple)) and len(v) >= 2:
                fixed = float(v[0])
                per_item = float(v[1])
                if fixed >= 0 and per_item >= 0:
                    out[str(k)] = [fixed, per_item]
            else:
                f = float(v)
                if f > 0:
                    out[str(k)] = f
        except (TypeError, ValueError):
            continue
    return out


def http_pool_connections() -> int:
    return int(get("http_pool_connections"))


def http_pool_maxsize_cap() -> int:
    return int(get("http_pool_maxsize_cap"))


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

    The actual label resolution lives in :mod:`services.user_labels`,
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


# ── Playlist Management cache tunables (Plan[PLAYLIST-MANAGEMENT]) ──────────

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
    """Default OFF per end user. When True, a background thread
    refreshes stale caches automatically."""
    raw = get("playlist_cache_background_refresh_enabled")
    return bool(raw) if raw is not None else False


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


# ── Mixed-media playlist tunables (Plan[MIXED-MEDIA-PLAYLISTS]) ─────────────

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
