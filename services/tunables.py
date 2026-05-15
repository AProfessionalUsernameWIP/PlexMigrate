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
# behaviour is byte-for-byte identical until the operator changes
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
    # Default false (disabled). Operators flip true ONLY as a
    # diagnostic / recovery escape hatch - e.g. if a future plexapi
    # or Plex version genuinely needs reload to surface data the
    # bulk response omits. Wrong value = ratings-takes-hours.
    "plexapi_autoreload_enabled": False,
    # media.db caching during snapshot + direct-transfer runs. When
    # false (default), the engine's payload-direct snapshot writer
    # (Rule 1) and direct-transfer's in-memory pipeline both run
    # without ingesting payloads into media.db. The .db snapshot file
    # and JSON sidecar are unaffected - they're built from the live
    # payloads.
    #
    # Auto-seed exception: when the operator hasn't opted in (tunable
    # left at default false) AND media.db has no rows for the server
    # being captured, the engine treats the run as a "first-time
    # seed" and DOES ingest. This populates the resolver Tier 0
    # GUID cache so subsequent direct-transfer / restore runs against
    # this server can short-circuit live API GUID lookups. Once that
    # one-time seed has happened, the tunable's false default takes
    # over and subsequent runs stop writing to media.db until the
    # operator explicitly flips it on.
    #
    # The auto-seed lives at the resolution boundary (see
    # ``services.snapshotter._should_cache_payload_to_media_db``)
    # rather than in this tunable's value, so settings.json stays a
    # clean reflection of operator intent - auto-seed is a one-shot
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
    # the snapshot job when the operator opts in); after the download
    # window, holding it on disk just consumes space. Default 300s
    # (5 min). Lower for an aggressive cleanup; raise if operators
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
    # tunable - it's a snapshot run-behaviour choice that operators
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
}


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
    True iff the operator has explicitly opted into ingesting
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
