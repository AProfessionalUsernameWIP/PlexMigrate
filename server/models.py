"""
Pydantic request / response schemas for the PlexMigrate server API.

Every endpoint in :mod:`server.app` validates its inbound JSON against
one of these models and (where applicable) shapes its outbound JSON
through one of them too. Keeping them in a separate module means the
route handlers stay focused on orchestration logic instead of payload
validation.

Field naming
------------
* Public field names use ``snake_case`` exactly matching the CLI flag
  names (e.g. ``strict_match`` mirrors ``--strict-match``). This means
  there is a 1:1 mapping between the form controls the frontend
  renders and the parameters the engine accepts - no translation
  table needed.
* Defaults match the CLI defaults in :func:`plexmigrate.build_parser`.

Note on optional fields:
We use ``Optional[X] = None`` rather than ``X | None = None`` to keep
the source readable under Python 3.9 (the engine's minimum). FastAPI /
Pydantic v2 understand both forms equally well.
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, model_validator


# ── Server-UID validator (Plan[SERVER-UID-IDENTITY] 2026-05-16) ─────────────
#
# Shared validator that rejects malformed server_id values at the API
# boundary. The runtime resolver gates separately on registry
# existence; this validator only enforces the format. ``None`` is
# always valid (fields are Optional).

def _validate_server_id_or_none(value: Any) -> Any:
    """Reject malformed server_id strings with a clear message.
    ``None`` / absent / empty string passes through unchanged (None
    triggers the resolver's name-fallback path)."""
    if value is None or value == "":
        return value
    if not isinstance(value, str):
        raise ValueError(
            f"server_id must be a string; got {type(value).__name__}."
        )
    # Late import to avoid pulling the whole registry module at
    # models.py import time (it imports media_db, persistence, etc.).
    from server.server_registry import is_valid_server_id
    if not is_valid_server_id(value):
        raise ValueError(
            f"server_id {value!r} is malformed. Expected "
            "`<plex|jellyfin|emby>_<uuid4_hex>` (e.g., "
            "`emby_a1b2c3d4e5f6...`)."
        )
    return value


def _validate_server_id_list_or_none(value: Any) -> Any:
    """List variant of ``_validate_server_id_or_none``. Validates
    every entry; ``None`` / empty list passes through. Entries that
    are None or empty string fall through (the resolver handles
    them per-entry)."""
    if value is None:
        return value
    if not isinstance(value, list):
        raise ValueError(
            f"server_id list must be a list; got {type(value).__name__}."
        )
    for entry in value:
        _validate_server_id_or_none(entry)
    return value


# ── Phase C (admin-management follow-up, 2026-05-15) - per-library metrics ──
#
# library_metrics: Optional[Dict[library_name, LibraryMetrics]] is the
# end user-controlled per-library matrix that replaces the four global
# include_* checkboxes. When set, library_metrics is the SOURCE OF
# TRUTH: the engine consults library_metrics[lib_name] to decide which
# metric tables to capture for that specific library. The legacy global
# include_* flags are retained for backward-compat input from older
# clients - the validator below expands them into library_metrics at
# parse time so internal code only ever has to look at the map.

class LibraryMetrics(BaseModel):
    """Per-library data-type filter. Default true everywhere (the
    pre-Phase-C default of "migrate every type for every library")."""
    watch_history: bool = Field(default=True)
    ratings: bool = Field(default=True)
    playlists: bool = Field(default=True)
    collections: bool = Field(default=True)


class UserCreateSpec(BaseModel):
    """
    Plan[RUN-JOB-UI] D-OWNER: one row from the user-creation preflight
    modal. The end user confirms each row before a cross-backend job
    submit; ``server/jobs.py`` (via ``services/user_creation.py``) walks
    the list at run start, calls ``adapter.create_user`` per row,
    persists the resulting ``backend_user_id`` into ``managed_users``,
    and only then lets the engine proceed to item-state writes.

    ``source_user_handle`` is the username on the SOURCE server (the
    end user-visible identifier the modal lists).
    ``target_username`` is the username the end user picked for the
    DESTINATION server; defaults to ``managed_users.display_name``
    on the source falling back to source username.
    ``temp_password`` is generated client-side or end user-typed; the
    backend never logs the plaintext value (per token-leak hygiene)
    and writes only the resulting Fernet-encrypted password into
    ``managed_users.service_password_enc`` for the destination row.
    ``target_user_policy`` is the optional ``POST /Users/{id}/Policy``
    body (Jellyfin / Emby); omitted means the destination's default
    policy applies.
    """
    source_user_handle: str = Field(
        ...,
        description=(
            "Username on the SOURCE server. Identifies which managed "
            "user this spec corresponds to for the modal's display + "
            "for the post-create mapping write into managed_users."
        ),
    )
    target_username: str = Field(
        ...,
        description=(
            "Username to assign on the DESTINATION server. Validated "
            "against existing usernames before submit; an empty or "
            "all-whitespace value is rejected at the Pydantic boundary."
        ),
    )
    temp_password: str = Field(
        ...,
        description=(
            "Plaintext password for the new destination user. Backend "
            "never logs the plaintext value; the encrypted form goes "
            "into managed_users.service_password_enc."
        ),
    )
    target_user_policy: Optional[Dict[str, Any]] = Field(
        default=None,
        description=(
            "Optional Jellyfin / Emby user policy. Omit to accept the "
            "destination's default. See Plan[MULTI-BACKEND] section 2.8 "
            "for the fields the adapter forwards (IsAdministrator, "
            "EnableAllFolders, EnabledFolders, etc.)."
        ),
    )

    @model_validator(mode="after")
    def _reject_empty_strings(self) -> "UserCreateSpec":
        """Empty / whitespace-only values are end user typos. Reject
        at the boundary so jobs.py never has to defend against them.
        Leaves the field-not-set case to Pydantic's own required-field
        machinery."""
        if not (self.source_user_handle or "").strip():
            raise ValueError("source_user_handle is required")
        if not (self.target_username or "").strip():
            raise ValueError("target_username is required")
        if not self.temp_password:
            raise ValueError("temp_password is required")
        return self


def _expand_legacy_include_flags_to_library_metrics(values: Any) -> Any:
    """
    Translator: when a client sends global include_* flags WITHOUT a
    library_metrics map, expand the flags into a per-library map (one
    entry per ``libraries[*]``, all using the same flag values). The
    engine's helper ``metrics_for_library`` always reads from the
    map, so this preserves the legacy end user workflow while moving
    the source of truth onto library_metrics.

    Called from every request model that owns ``libraries`` + the
    four ``include_*`` flags. No-op when library_metrics is already
    populated (the new client form wins).
    """
    if not isinstance(values, dict):
        return values
    lm = values.get("library_metrics")
    if lm:  # caller already supplied it; leave alone
        return values
    libs = values.get("libraries") or []
    if not isinstance(libs, list) or not libs:
        return values
    # Read the four legacy flags (default True matches the pre-Phase-C
    # implicit defaults baked into the include_* Field declarations).
    iwh = values.get("include_watch_history", True)
    ir = values.get("include_ratings", True)
    ip = values.get("include_playlists", True)
    ic = values.get("include_collections", True)
    # If every flag is True the legacy default is preserved
    # (capture-everything-for-every-library). If any flag is False, the
    # expansion makes the change visible per library too.
    expanded: Dict[str, Dict[str, bool]] = {}
    for lib in libs:
        if isinstance(lib, str) and lib.strip():
            expanded[lib.strip()] = {
                "watch_history": bool(iwh),
                "ratings": bool(ir),
                "playlists": bool(ip),
                "collections": bool(ic),
            }
    if expanded:
        values["library_metrics"] = expanded
    return values


def metrics_for_library(
    library_metrics: Optional[Dict[str, Any]],
    library_name: str,
    fallback_include_watch_history: bool = True,
    fallback_include_ratings: bool = True,
    fallback_include_playlists: bool = True,
    fallback_include_collections: bool = True,
) -> Dict[str, bool]:
    """
    Engine helper: return the effective per-metric booleans for one
    library. Reading order:

      1. If ``library_metrics`` has an entry for ``library_name``,
         that entry's metric flags are returned.
      2. Otherwise the four fallback values are used. This handles
         the case where library_metrics is unset (legacy global
         capture) and the case where a freshly-discovered library
         wasn't in the end user's selection map at submit time.

    The returned dict is plain ``{watch_history, ratings, playlists,
    collections}`` booleans so engine call sites can keep their
    existing local variables and just feed them from this helper.
    """
    if library_metrics and library_name in library_metrics:
        row = library_metrics[library_name]
        # Tolerate both Pydantic models and plain dicts (the engine
        # may see either depending on whether it received a fully-
        # parsed model or a raw JobRecord.params dict).
        get = (row.get if isinstance(row, dict) else (lambda k, d=None: getattr(row, k, d)))
        return {
            "watch_history": bool(get("watch_history", True)),
            "ratings": bool(get("ratings", True)),
            "playlists": bool(get("playlists", True)),
            "collections": bool(get("collections", True)),
        }
    return {
        "watch_history": bool(fallback_include_watch_history),
        "ratings": bool(fallback_include_ratings),
        "playlists": bool(fallback_include_playlists),
        "collections": bool(fallback_include_collections),
    }


# ── PR-3 / Phase D - data-type filter (include_* family) ──────────────────────
#
# The four boolean checkboxes the end user sees on every Run-Job and
# Schedules form. ``True`` (the default) means "migrate this data type;"
# ``False`` means "skip the gather AND any merge on the destination."
# Defaults reproduce v0.12.x behaviour exactly when no field is sent.
#
# Backward compat: existing API clients send ``skip_collections`` /
# ``skip_playlists`` (PR-1 / Phase B and earlier). A model_validator
# attached to each request schema maps those legacy fields onto the
# new include_* equivalents when the client didn't supply the include
# fields explicitly. The legacy fields stay on the model so old
# clients keep working unchanged.

def _apply_legacy_skip_flags(values: Any) -> Any:
    """
    Map legacy ``skip_collections`` / ``skip_playlists`` keys onto the
    new ``include_collections`` / ``include_playlists`` defaults during
    ``model_validator(mode="before")``. Called from every model that
    has both the legacy and new flags. No-op if the client supplied
    the new fields explicitly (those win).

    Watch history and ratings have no legacy ``skip_*`` counterparts
    (they were always migrated) so they only ever flow through the
    include_* path.
    """
    if not isinstance(values, dict):
        return values
    # Honour the new field if explicitly set. Otherwise translate the
    # legacy skip flag.
    if "include_playlists" not in values or values.get("include_playlists") is None:
        sp = values.get("skip_playlists")
        if sp is True:
            values["include_playlists"] = False
    if "include_collections" not in values or values.get("include_collections") is None:
        sc = values.get("skip_collections")
        if sc is True:
            values["include_collections"] = False
    return values


# ── Internal helpers ─────────────────────────────────────────────────────────

def _coalesce_dest_names(
    singular: Optional[str],
    plural: Optional[List[str]],
) -> List[str]:
    """
    Merge the singular and plural destination fields into one ordered
    list, dropping empties and duplicates while preserving first-seen
    order. ``plural`` takes precedence when both are supplied; the
    singular value is appended only if not already present. The result
    may be empty - the caller decides whether that's an error.

    Used by the model validators on :class:`RestoreJobIn` and
    :class:`DirectTransferIn` so the fan-out dispatch in
    :mod:`server.jobs` only ever has to look at ``dest_server_names``.
    """
    seen: Dict[str, None] = {}
    if plural:
        for n in plural:
            if isinstance(n, str):
                s = n.strip()
                if s and s not in seen:
                    seen[s] = None
    if singular and isinstance(singular, str):
        s = singular.strip()
        if s and s not in seen:
            seen[s] = None
    return list(seen.keys())


# ── Settings ─────────────────────────────────────────────────────────────────

class SettingsIn(BaseModel):
    """
    Body of ``POST /api/settings``. All fields are optional so a client
    can update one value (e.g. just the token) without re-sending the
    whole document. The server merges this on top of the existing
    saved document via :func:`server.persistence.save_settings`.
    """

    plex_url: Optional[str] = Field(
        default=None,
        description="Full Plex server URL, e.g. http://host.docker.internal:32400",
    )
    plex_token: Optional[str] = Field(
        default=None,
        description="Plex authentication token. Stored on the server only.",
    )
    output_dir: Optional[str] = Field(
        default=None,
        description="Default directory for .plexexport.json snapshots",
    )
    log_dir: Optional[str] = Field(
        default=None,
        description="Default directory for run logs",
    )
    workers: Optional[int] = Field(
        default=None, ge=1, le=128,
        description="Worker thread count for the engine (--workers)",
    )
    scrobble_workers: Optional[int] = Field(
        default=None, ge=1, le=64,
        description="Max simultaneous scrobble HTTP calls (--scrobble-workers)",
    )
    verbose: Optional[bool] = Field(
        default=None,
        description="DEBUG-level logging on console + run log (--verbose)",
    )
    # End user-controlled global on/off for the per-run log FILES
    # (``runtime.log`` / ``errors.log`` / ``media.log``). When disabled
    # the engine still runs and the dashboard / activity feed still
    # update - we just don't create the per-run files on disk. Default
    # true so existing installs keep their logs.
    run_logging_enabled: Optional[bool] = Field(
        default=None,
        description=(
            "Write per-run runtime.log / errors.log / media.log files. "
            "Default true. Turning off keeps the engine and dashboard "
            "working; it only suppresses the per-run files on disk."
        ),
    )
    # End user-controlled global on/off for the db-access audit log.
    # SEPARATE from run_logging_enabled because the audit log is a
    # forensic control, not a diagnostic convenience: toggling it is
    # gated behind the db_admin credential at the API layer and the
    # transition is self-documenting (the last/first log line records
    # who disabled / re-enabled it).
    audit_log_enabled: Optional[bool] = Field(
        default=None,
        description=(
            "Write db-access audit log entries. Default true. Toggling "
            "this is db_admin-gated via /api/settings/audit-log-toggle "
            "and the transition is self-documenting in the audit log "
            "itself. Plain /api/settings PATCH ignores this field - "
            "it is read-only outside the dedicated endpoint."
        ),
    )
    strict_match: Optional[bool] = Field(
        default=None,
        description="Require exactly one fuzzy title match (--strict-match / --no-strict-match)",
    )
    # PR-13 - snapshot retention. ``global`` is a ceiling; the
    # per-server map only applies when an entry is strictly LOWER
    # than the global.
    snapshot_retention_global: Optional[int] = Field(
        default=None, ge=1, le=10000,
        description="Maximum snapshots kept per server before retention sweep deletes the oldest.",
    )
    snapshot_retention_per_server: Optional[Dict[str, int]] = Field(
        default=None,
        description=(
            "Per-server override map: {server_id: int}. Applies only when "
            "strictly lower than snapshot_retention_global; higher values "
            "are ignored at enforcement time."
        ),
    )
    # Global default for the JSON-sidecar toggle on snapshot jobs. Each
    # snapshot job + schedule has its own per-run flag; when that flag is
    # left at its default (None), the engine falls back to this setting.
    prebuild_json_sidecar_default: Optional[bool] = Field(
        default=None,
        description=(
            "Global default for the JSON-sidecar toggle on snapshot jobs. "
            "When true, every snapshot job + schedule that hasn't explicitly "
            "set its own value will also render a .plexexport.json next to "
            "the .db at the end of the run. Per-job toggles override this."
        ),
    )
    # Owner-phase watch+ratings capture strategy. Surfaces under
    # Servers ▸ Run Defaults ▸ Snapshot Defaults. Per-server override is
    # accepted as ``snapshot_defaults_per_server[server_id]
    # .watch_ratings_filter_strategy``. See ``services.snapshotter
    # .snapshot_library`` for resolution + behaviour.
    watch_ratings_filter_strategy: Optional[str] = Field(
        default=None,
        description=(
            "Strategy for fetching watch-history + ratings on the owner "
            "phase. One of: \"smart\" (default - bulk-fetch when both "
            "wanted, server-side filter when only one), \"force_bulk\" "
            "(always bulk-fetch + local filter; best for rate-limited "
            "Plex servers), \"force_server_side\" (always server-side "
            "filter; best when wire-traffic from the server is the "
            "constraint)."
        ),
    )
    # Smart-mode size threshold for the bulk-fetch decision. See
    # services.snapshotter._should_use_bulk for the full decision
    # table. Setting to 0 effectively disables the size gate
    # (smart-mode behaves as "bulk whenever both metrics are wanted").
    # Setting to a very large number forces non-show libraries onto
    # the server-side path under smart-mode.
    smart_bulk_threshold_items: Optional[int] = Field(
        default=None,
        ge=0,
        description=(
            "Smart-mode size threshold. Under strategy=\"smart\", "
            "non-show libraries use the bulk-fetch path only when "
            "totalSize >= this value AND both watch+ratings are "
            "requested. Show libraries always bulk-fetch under "
            "smart-mode regardless of size. Default 5000."
        ),
    )
    # Snapshot integrity validation toggles (Feature 2). Two separate
    # flags so post-capture and pre-restore can be controlled
    # independently; default ON / OFF respectively per D4 / D5.
    validate_snapshot_after_capture: Optional[bool] = Field(
        default=None,
        description=(
            "When ON (default), the snapshot capture step runs a "
            "structural validator on the freshly-written .db file "
            "before considering the capture successful. Errors abort "
            "the job; warnings log without aborting."
        ),
    )
    validate_snapshot_before_restore: Optional[bool] = Field(
        default=None,
        description=(
            "When ON, the restore-from-snapshot endpoint runs the "
            "structural validator on the snapshot .db before "
            "submitting the restore job. Errors return 422; warnings "
            "log without aborting. Default OFF."
        ),
    )
    # Log rotation tunables. Applied to application-level log writers
    # (services.db_access_log today; auth / network / debug when their
    # writers ship). Per-run job logs are governed by their own
    # retention policy and are not size-rotated.
    log_rotate_max_size_mb: Optional[int] = Field(
        default=None, ge=1, le=10_000,
        description=(
            "Maximum size of each application log file in megabytes "
            "before it rolls to a backup. Default 50."
        ),
    )
    log_rotate_backup_count: Optional[int] = Field(
        default=None, ge=0, le=100,
        description=(
            "Number of rolled-over copies retained per application log "
            "file. 0 disables retention beyond the active file. "
            "Default 5."
        ),
    )
    # Per-server default overrides. Maps server_id → {field: value}
    # where ``field`` is one of the snapshot-time knobs that has a
    # sensible per-server interpretation. The Servers ▸ Advanced
    # Settings sub-tab is the UI surface for this; the Run-Job form
    # seeds its toggles from the resolved value when the end user
    # picks a source server.
    #
    # Recognised fields:
    #   prebuild_json_sidecar           (bool)
    #   include_watch_history           (bool)
    #   include_ratings                 (bool)
    #   include_playlists               (bool)
    #   include_collections             (bool)
    #   skip_playlist_prebuild          (bool)
    #   fast_collection_detection       (bool)
    #   watch_ratings_filter_strategy   (str: "smart" / "force_bulk" /
    #                                    "force_server_side")
    snapshot_defaults_per_server: Optional[Dict[str, Dict[str, Any]]] = Field(
        default=None,
        description=(
            "Per-server overrides for snapshot job defaults. "
            "Shape: {server_id: {field: value, ...}}. Missing fields "
            "fall back to the global default in this Settings model "
            "(or the model's built-in default when no global is set)."
        ),
    )
    # Resolution-tier policy for the direct-transfer pipeline. Tiers
    # 0 (DB GUID cache) and 1 (live API GUID match) are always active.
    # The two fallbacks below are end user-configurable. Snapshot /
    # import paths are NOT affected by this block - they continue to
    # run the resolver with both fallbacks active. The flip applies
    # only when direct_transfer.py reads these values at job start.
    transfer_resolution: Optional[Dict[str, bool]] = Field(
        default=None,
        description=(
            "Direct-transfer resolver-tier policy. Recognised keys: "
            "``allow_filepath_fallback`` (Tier 2, default true) and "
            "``allow_fuzzy_fallback`` (Tier 3, default false - fuzzy "
            "title matching can produce incorrect matches and is "
            "opt-in)."
        ),
    )
    # media.db retention + cascade-delete policy. The settings here
    # gate two distinct lifecycle behaviours: (a) what happens to
    # per-server rows in media.db when a server is removed from the
    # registry, and (b) future background pruning of stale data.
    # The prune-days fields are placeholders this round - the sweep
    # logic itself lands in a later commit.
    media_db_retention: Optional[Dict[str, Any]] = Field(
        default=None,
        description=(
            "media.db lifecycle policy. Recognised keys: "
            "``cascade_delete_on_server_remove`` (bool, default true - "
            "auto-purges per-server rows when a server is removed); "
            "``prevent_cascade_delete`` (bool, default false - operator "
            "opt-out, preserves rows even when cascade is enabled); "
            "``prune_stale_watch_events_days`` (int, default 0 / "
            "disabled - placeholder for future background sweep); "
            "``prune_stale_server_data_days`` (int, default 0 / "
            "disabled - same)."
        ),
    )
    library_walk: Optional[Dict[str, Any]] = Field(
        default=None,
        description=(
            "Library-walk job cadence + defaults. Recognised keys: "
            "``enabled`` (bool, default true); ``interval_seconds`` "
            "(int, default 86400, floored at 3600 by the scheduler); "
            "``stale_threshold_days`` (int, default 7 - the initial "
            "value of the Prune Missing Items day-threshold slider)."
        ),
    )
    # PR-12: per-server rate limit for user-token capture. See the
    # ``user_token_capture_throttle_per_hour`` entry in
    # ``server.persistence._DEFAULT_SETTINGS`` for the full rationale.
    user_token_capture_throttle_per_hour: Optional[int] = Field(
        default=None, ge=1, le=240,
        description=(
            "Per-server cap on user-token capture attempts. Each "
            "server add / update / reconnect fires one attempt; the "
            "gate skips the call when fewer than ``3600 / value`` "
            "seconds have elapsed since the last attempt for that "
            "server. Default 4 (one attempt every 15 minutes per "
            "server). Operator-triggered Refresh actions bypass the "
            "throttle. Ceiling 240 (every 15 seconds) is a sanity "
            "limit, not a recommended value."
        ),
    )
    # System Tunables - infrastructure-level knobs that used to be
    # hardcoded literals (HTTP timeouts, retry budgets, JWT TTLs,
    # SQLite busy timeouts, pool sizes, etc.). Free-form dict because
    # the list of recognised keys grows over time and the
    # ``services.tunables`` module is the single source of truth for
    # defaults + clamps. The Tunables UI is gated by
    # ``settings.tunables`` (root_admin only); a plain
    # ``settings.edit`` PATCH may write to this field too, but a
    # frontend with the proper role gate is what guards normal usage.
    tunables: Optional[Dict[str, Any]] = Field(
        default=None,
        description=(
            "System tunables (root_admin only via UI). See "
            "``services.tunables`` for the recognised keys + defaults."
        ),
    )
    # Per-server tunable overrides. Map: server_id → {key: value}.
    # Only a small subset of the global tunables make sense per-server:
    #   - ``plex_connect_timeout_seconds`` (slow/remote servers)
    #   - ``viewcount_increment_cap`` (weaker servers)
    # Resolution: per-server override → global tunable → built-in default.
    tunables_per_server: Optional[Dict[str, Dict[str, Any]]] = Field(
        default=None,
        description=(
            "Per-server overrides for the small set of tunables that "
            "have a sensible per-server interpretation. Shape: "
            "{server_id: {tunable_key: value}}. Resolved by "
            "``services.tunables.get_per_server(server_id, key)``."
        ),
    )
    # ETR colour multiplier for the dashboard stall thresholds.
    # Scales the per-phase amber/red windows by this factor (clamped
    # to [0.5, 2.0] at the read boundary). 1.0 = ship defaults.
    etr_color_multiplier: Optional[float] = Field(
        default=None,
        description=(
            "Multiplier applied to the dashboard's per-phase amber/red "
            "stall thresholds. Clamped to [0.5, 2.0]. Default 1.0."
        ),
    )
    # v0.13.x: library-level concurrency cap on the file-mediated restore
    # path. Replaces a hardcoded ``min(3, libraries)``. End users that
    # see Plex 429s during multi-library restores lower this; end users
    # with idle destinations and plenty of headroom can raise it. The
    # ``min(value, library_count)`` clamp still applies so setting this
    # above the actual library count has no effect beyond capping at
    # the count. Direct transfer is still serial in this release.
    restore_library_workers: Optional[int] = Field(
        default=None, ge=1, le=16,
        description=(
            "Max libraries processed in parallel during a file-mediated "
            "restore. Default 3 preserves today's behavior; lower it (e.g. "
            "to 1) when Plex rate-limits the multi-library API bursts."
        ),
    )
    # v0.13.x: same decoupling story as restore_library_workers, but for
    # snapshot. Snapshot today reuses the ``workers`` field as the
    # library-level pool size, so the two concurrency axes are
    # entangled. ``0`` inherits from ``workers`` (today's behavior);
    # any positive value caps libraries-in-parallel separately.
    snapshot_library_workers: Optional[int] = Field(
        default=None, ge=0, le=16,
        description=(
            "Max libraries snapshotted in parallel. 0 (default) = inherit "
            "from Worker threads, which preserves today's behavior. A "
            "positive value caps libraries-in-parallel independently of "
            "the per-library HTTP worker count."
        ),
    )
    # v0.13.x: per-fan-out destination concurrency cap. ``0`` (default)
    # means no cap - every destination runs in its own thread, matching
    # today's behavior. A positive integer caps the destination pool.
    # Independent of restore_library_workers: each destination still
    # uses its own within-job library concurrency separately.
    fan_out_destination_workers: Optional[int] = Field(
        default=None, ge=0, le=32,
        description=(
            "Max fan-out destinations processed in parallel. 0 (default) "
            "= no cap (current behavior - one thread per destination). "
            "Set to 1 to serialise destinations one at a time, useful "
            "when all destinations share a network bottleneck or the "
            "source Plex is the constraint."
        ),
    )


# ── PR-12 preflight acknowledgement (shared by every job-input model) ───────

class _PinPreflightAckFields(BaseModel):
    """
    Fields the frontend stamps onto a job submission when the end user
    has cleared the PR-12 PIN-preflight warning modal.

    The job endpoints re-map these to underscore-prefixed synthetic
    params on the JobRecord (see ``server.app._apply_preflight_ack``)
    so the engine reads them via the same ``rec.params["_..."]``
    convention as ``_trigger`` / ``_actor_username``.
    """
    pin_preflight_acknowledged: bool = Field(
        default=False,
        description=(
            "True if the operator clicked Continue anyway on the "
            "preflight modal. False (default) means the modal either "
            "did not surface or the operator did not need to clear it."
        ),
    )
    pin_preflight_at_risk: Optional[List[str]] = Field(
        default=None,
        description=(
            "The list of usernames the preflight flagged. Optional "
            "audit trail surfaced in the run log."
        ),
    )


# ── Job requests ─────────────────────────────────────────────────────────────

class SnapshotJobIn(_PinPreflightAckFields):
    """
    Body of ``POST /api/job/snapshot``. Every flag from the CLI snapshot
    side is mirrored here. Anything left blank falls back to the
    corresponding value in the saved settings.

    Multi-server (v0.9.0): the ``source_server_name`` field is the
    friendly name of a registered server (see ``GET /api/servers``).
    Required unless the request is being processed by the legacy
    ad-hoc CLI path that supplies ``plex_url`` + ``plex_token`` directly.
    """

    source_server_name: Optional[str] = Field(
        default=None,
        description="Friendly name of the registered server to snapshot from.",
    )
    # Plan[SERVER-UID-IDENTITY] 2026-05-16: stable per-row identifier
    # assigned at add_server time. Format `<service_type>_<uuid4_hex>`
    # (e.g., `emby_a1b2c3...`). PREFERRED over source_server_name;
    # disambiguates same-named servers across backends and survives
    # rename. None falls back to name-based lookup with a warning.
    source_server_id: Optional[str] = Field(
        default=None,
        description=(
            "Stable registry id of the source server "
            "(`<plex|jellyfin|emby>_<uuid4_hex>`). Preferred over "
            "source_server_name when both are supplied."
        ),
    )
    libraries: List[str] = Field(
        default_factory=list,
        description="Library names to snapshot. Empty = all libraries the server reports.",
    )
    output_dir: Optional[str] = Field(
        default=None,
        description="Override the default output directory for this run.",
    )
    workers: Optional[int] = Field(default=None, ge=1, le=128)
    scrobble_workers: Optional[int] = Field(default=None, ge=1, le=64)
    verbose: Optional[bool] = None
    log_dir: Optional[str] = None
    skip_collections: Optional[bool] = Field(
        default=None,
        description=(
            "Skip collection snapshot entirely. Useful for faster runs when "
            "collections are not needed or will be rebuilt on the target."
        ),
    )
    fast_collection_detection: Optional[bool] = Field(
        default=None,
        description=(
            "Use librarySectionUserID attribute to detect personal vs library-wide "
            "collections without a set lookup. Faster on modern Plex (≥1.32). "
            "Falls back to rating-key dedup automatically on older servers."
        ),
    )
    skip_playlists: Optional[bool] = Field(
        default=None,
        description="Skip playlist snapshot entirely - no playlists in output. Watch history, collections, and ratings are unaffected.",
    )
    skip_playlist_prebuild: Optional[bool] = Field(
        default=None,
        description=(
            "Skip the parallel pre-warm that fetches all playlist items upfront. "
            "Playlists still snapshot - the cache is built lazily on first use per server "
            "and shared across libraries so each server is still only fetched once. "
            "Use this to avoid the 'fetching / All Music' stall at job start without "
            "sacrificing playlist data."
        ),
    )
    # PR-3 / Phase D - four-flag data-type filter (default-true).
    include_watch_history: bool = Field(
        default=True,
        description="Include watch history (view counts + resume positions) in the snapshot.",
    )
    include_ratings: bool = Field(
        default=True,
        description="Include star ratings in the snapshot.",
    )
    include_playlists: bool = Field(
        default=True,
        description="Include playlists in the snapshot. Supersedes legacy skip_playlists.",
    )
    include_collections: bool = Field(
        default=True,
        description="Include collections in the snapshot. Supersedes legacy skip_collections.",
    )
    prebuild_json_sidecar: bool = Field(
        default=False,
        description=(
            "When true, after the snapshot .db is registered the engine "
            "renders a .plexexport.json sidecar next to it. Adds wall-clock "
            "time at the end of the run; the JSON is otherwise built on "
            "first Download click via the Exports panel."
        ),
    )
    # Per-job watch+ratings capture strategy override. Top of the
    # resolution chain (per-job → per-server → global → "smart"). When
    # None, the per-server / global value applies.
    watch_ratings_filter_strategy: Optional[str] = Field(
        default=None,
        description=(
            "Per-job override for the owner-phase watch+ratings strategy. "
            "One of: \"smart\", \"force_bulk\", \"force_server_side\". "
            "None inherits from the per-server override or the global "
            "default on Settings ▸ Run Defaults."
        ),
    )
    # Per-job user filter - mirrors DirectTransferIn.user_filter so the
    # end user can scope a snapshot to a subset of the source server's
    # users (owner + managed). Matching is by raw Plex identifier
    # (email for owner, username for managed). ``None`` (or omitted)
    # means "include every user the source server reports" - the
    # historical default. Empty list ``[]`` excludes ALL users and is
    # honoured as such (rare but legal). When the list contains the
    # owner email, owner-level data is captured; otherwise the snapshot
    # captures only managed-user data for the listed names.
    user_filter: Optional[List[str]] = Field(
        default=None,
        description=(
            "List of Plex identifiers (owner email + managed usernames) "
            "whose data the snapshot should capture. None = include all "
            "users the source server reports."
        ),
    )

    # Phase C (admin-management follow-up): per-library metric filter.
    # When set, this is the source of truth for which metrics each
    # library captures. Legacy global include_* flags are still
    # accepted on input and expanded into library_metrics by the
    # validator below.
    library_metrics: Optional[Dict[str, LibraryMetrics]] = Field(
        default=None,
        description=(
            "Per-library metric filter: {library_name: {watch_history, "
            "ratings, playlists, collections}}. When set, overrides the "
            "global include_* flags. When unset, the validator expands "
            "the global flags into this map at request-parse time so "
            "the engine only ever reads library_metrics."
        ),
    )

    # Plan[RUN-JOB-UI] work item 4: per-user fan-out toggle. Default
    # matches the adapter snapshotter's include_managed_users=True
    # kwarg. When False, the engine captures owner-only and skips
    # managed users; user_filter (above) is then effectively a no-op.
    include_managed_users: bool = Field(
        default=True,
        description=(
            "When True (default), the adapter snapshotter captures "
            "each managed user's per-user payload in addition to the "
            "owner. When False, owner-only capture. Plex engine is "
            "always per-user; this flag is meaningful only for "
            "Jellyfin / Emby today."
        ),
    )

    # Plan[MIXED-MEDIA-PLAYLISTS]-2026-05-16: per-run overrides for
    # the mixed-media playlist strategy. None means inherit from the
    # global tunable. Captured here so the snapshot UI can also expose
    # them for symmetry with restore + direct; the snapshotter doesn't
    # use them today (capture is media-type-agnostic), but the end user
    # frequently re-runs snapshot + restore as one logical action.
    mixed_media_behavior: Optional[Literal["skip", "dominant", "split"]] = None
    mixed_media_dominance_threshold: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    mixed_media_video_routing: Optional[Literal["library_agnostic", "library_dominant"]] = None
    mixed_media_logging: Optional[Literal["full", "decisions_only", "off"]] = None
    mixed_media_collision_handling: Optional[Literal["duplicate", "suffix", "skip"]] = None

    @model_validator(mode="before")
    @classmethod
    def _legacy_skip_flags(cls, values: Any) -> Any:
        values = _apply_legacy_skip_flags(values)
        values = _expand_legacy_include_flags_to_library_metrics(values)
        return values

    @model_validator(mode="after")
    def _validate_server_ids(self) -> "SnapshotJobIn":
        _validate_server_id_or_none(self.source_server_id)
        return self


class RestoreJobIn(_PinPreflightAckFields):
    """
    Body of ``POST /api/job/restore``. Mirrors the restore side of the CLI.

    Multi-server (v0.9.0): ``dest_server_name`` selects which registered
    server receives the imported data.

    Fan-out (v0.10.0, Feature 1): ``dest_server_names`` may carry a list
    of registered server names to import the same export into every
    target in one job. The single-server ``dest_server_name`` field is
    still accepted for backward compat; the validator below coalesces
    both inputs into ``dest_server_names`` so downstream code only ever
    reads the list form.
    """

    dest_server_name: Optional[str] = Field(
        default=None,
        description="Friendly name of the registered server to import into.",
    )
    dest_server_names: Optional[List[str]] = Field(
        default=None,
        description=(
            "Fan-out targets. When two or more names are supplied the job "
            "runs as a fan-out and writes to every named server in parallel. "
            "Mutually exclusive with dest_server_name (the single-server "
            "field is accepted for backward compat and folded into this list)."
        ),
    )
    # Plan[SERVER-UID-IDENTITY] 2026-05-16: stable per-row identifiers.
    # Preferred over the name variants; disambiguates same-named
    # servers across backends. None falls back to name lookup with a
    # warning. source_server_id is implicit for RestoreJobIn (no
    # source connection needed; the snapshot file IS the source).
    dest_server_id: Optional[str] = Field(
        default=None,
        description=(
            "Stable registry id of the destination server "
            "(`<plex|jellyfin|emby>_<uuid4_hex>`). Preferred over "
            "dest_server_name when both are supplied."
        ),
    )
    dest_server_ids: Optional[List[str]] = Field(
        default=None,
        description=(
            "Stable registry ids for fan-out destinations. Preferred "
            "over dest_server_names. Mutually exclusive with "
            "dest_server_id (the single-id field folds into this list)."
        ),
    )
    input_files: List[str] = Field(
        default_factory=list,
        description="One or more .plexexport.json paths (absolute, or relative to the server's working directory).",
    )
    workers: Optional[int] = Field(default=None, ge=1, le=128)
    scrobble_workers: Optional[int] = Field(default=None, ge=1, le=64)
    verbose: Optional[bool] = None
    log_dir: Optional[str] = None

    remap_old: Optional[str] = Field(
        default=None,
        description="Old root path prefix for --remap-path",
    )
    remap_new: Optional[str] = Field(
        default=None,
        description="New root path prefix for --remap-path",
    )
    strict_match: Optional[bool] = Field(
        default=None,
        description="If false, use first result when multiple fuzzy matches exist.",
    )
    # Retained for parity with the CLI flag, which is a documented no-op
    # since v0.2.0. We accept it from the form so the UI can label every
    # CLI option, but the value never changes engine behaviour.
    overwrite_playlists: Optional[bool] = Field(
        default=None,
        description="Backward-compat flag - no effect (all imports are additive since v0.2.0).",
    )
    # PR-1 / Phase B (skip-playlists end-to-end). Mirrors the existing
    # ``skip_playlists`` field on SnapshotJobIn and DirectTransferIn so
    # the import path can be told to skip the playlist phase entirely
    # - both the per-library restore_playlists call AND the wasted
    # top-level destination ``server.playlists()`` prefetch that used
    # to fire even with an empty playlist payload. Phase D supersedes
    # this with the four-flag ``include_*`` family below; the legacy
    # ``skip_playlists`` field stays accepted via the validator.
    skip_playlists: Optional[bool] = Field(
        default=None,
        description=(
            "Legacy. Use include_playlists instead. Skip the playlist "
            "phase of the import (both prefetch and per-library merge). "
            "Watch history, collections, and ratings are unaffected."
        ),
    )
    skip_collections: Optional[bool] = Field(
        default=None,
        description=(
            "Legacy. Use include_collections instead. Skip the "
            "collection phase of the import."
        ),
    )
    # PR-3 / Phase D - four-flag data-type filter (default-true).
    include_watch_history: bool = Field(
        default=True,
        description="Include watch history (view counts + resume positions) in the import.",
    )
    include_ratings: bool = Field(
        default=True,
        description="Include star ratings in the import.",
    )
    include_playlists: bool = Field(
        default=True,
        description="Include playlists in the import. Supersedes legacy skip_playlists.",
    )
    include_collections: bool = Field(
        default=True,
        description="Include collections in the import. Supersedes legacy skip_collections.",
    )
    # v0.13.x: restore mode. "merge" (default) is the legacy additive
    # behaviour: view counts only increase, ratings only set when target
    # has none, playlists/collections create-or-append. "replace" is the
    # opt-in point-in-time overwrite: view counts and ratings set to
    # exactly the snapshot's value (markUnplayed + re-scrobble when the
    # destination is currently higher), playlists/collections diff against
    # the snapshot and members not in the snapshot are removed. The
    # "no data deletions ever" project rule is honored only for "merge"
    # mode; "replace" carves an explicit, end user-gated exception.
    mode: str = Field(
        default="merge",
        pattern="^(merge|replace)$",
        description=(
            "Restoration mode. 'merge' (default) preserves newer "
            "destination activity; 'replace' overwrites to make the "
            "destination match the snapshot exactly."
        ),
    )
    # v0.13.x: safety belt that auto-captures a snapshot of the
    # destination BEFORE a Replace restore fires. When the restore
    # finishes, the end user has the pre-replace snapshot to roll back
    # if the wrong source snapshot was picked. Defaults to True; only
    # consulted when mode == 'replace'.
    auto_capture_before_replace: bool = Field(
        default=True,
        description=(
            "When mode == 'replace', auto-capture a snapshot of the "
            "destination before the restore runs so the operator has a "
            "recovery point. Ignored when mode == 'merge'."
        ),
    )
    # v0.13.x: required confirmation for Replace mode. The frontend's
    # typed-REPLACE modal sets this; an API caller wanting Replace must
    # set it explicitly. Submitting mode == 'replace' without this flag
    # set to true returns 400 from the API layer.
    confirm_replace: bool = Field(
        default=False,
        description=(
            "Required confirmation flag for mode == 'replace'. The UI "
            "sets this after the operator types REPLACE in the "
            "confirmation modal. API callers must set it explicitly."
        ),
    )
    # v0.13.x: sub-strategy for Merge mode's watch-count math.
    #   "higher" (default) - destination view count ends at
    #       max(stored, current). Add only the positive delta.
    #       Idempotent across re-runs (the legacy behaviour).
    #   "sum"   - destination view count ends at current + stored.
    #       Every captured play is added on top. NOT idempotent: a
    #       second run of the same job will double-count. End user
    #       opt-in for cases where the snapshot represents activity
    #       that genuinely happened on a different server and should
    #       contribute alongside, not replace.
    # Only consulted when mode == "merge"; Replace overwrites
    # unconditionally so the choice is moot there.
    merge_watch_strategy: str = Field(
        default="higher",
        pattern="^(higher|sum)$",
        description=(
            "Merge-mode watch-count math. 'higher' (default) keeps the "
            "larger of stored/current; 'sum' adds stored on top of "
            "current. Ignored when mode == 'replace'."
        ),
    )
    # Per-job user filter - mirrors the snapshot + direct-transfer
    # fields. Matching is by raw Plex identifier (email for owner,
    # username for managed). The list is the OPERATOR'S explicit
    # selection from the intersection of (users present in the
    # snapshot payload) and (users present on the destination server).
    # ``None`` = include every user the payload carries that ALSO has
    # a matching account on the destination. Users in the payload but
    # not on the destination are skipped server-side regardless of
    # this list (no destination user = nothing to restore to).
    user_filter: Optional[List[str]] = Field(
        default=None,
        description=(
            "List of Plex identifiers to restore. None = restore every "
            "user from the payload that also exists on the destination."
        ),
    )

    # Phase C: per-library metric filter. The restore engine looks
    # up each library it finds in the snapshot file; if the map has
    # an entry it's authoritative, otherwise the global include_*
    # flags apply. Restore doesn't get the legacy-expansion validator
    # because there is no input-time library list - libraries are
    # discovered at gather time from the snapshot payload.
    library_metrics: Optional[Dict[str, LibraryMetrics]] = Field(
        default=None,
        description=(
            "Per-library metric filter: {library_name: {watch_history, "
            "ratings, playlists, collections}}. Overrides global "
            "include_* flags per library. Libraries not in the map "
            "fall back to the global flags."
        ),
    )

    # Plan[RUN-JOB-UI] work item 3: D-RATE per-job mode picker. The
    # adapter restorer maps a source numeric rating onto BOTH a
    # numeric Rating AND an IsFavorite=true write on the destination
    # when the source value is at or above this threshold. Mapping:
    #   "Favorite >= 5 (default)" -> 5.0
    #   "Threshold-tunable"       -> end user-picked number
    #   "Numeric only"            -> 11.0 (above the max rating;
    #                                IsFavorite is never written)
    # ``None`` means "let the engine default fire" (5.0 today).
    favorite_threshold: Optional[float] = Field(
        default=None,
        description=(
            "Per-job override of the rating-to-favorite cross-mapping "
            "threshold. None means the engine default (5.0) applies. "
            "Set to 11.0 to disable IsFavorite writes entirely "
            "('Numeric only' UI selection)."
        ),
    )

    # Plan[RUN-JOB-UI] work item 4: per-user fan-out toggle. Same
    # semantics as on SnapshotJobIn; the adapter restorer also
    # iterates managed users when True.
    include_managed_users: bool = Field(
        default=True,
        description=(
            "When True (default), the adapter restorer applies the "
            "managed-user payloads found in the snapshot file. When "
            "False, owner-only restore. Meaningful only for Jellyfin "
            "/ Emby destinations today."
        ),
    )

    # Plan[RUN-JOB-UI] work item 2: D-OWNER user-creation specs.
    # End user-confirmed list of users to create on the destination
    # before the engine fires any item-state write. Empty / None is
    # the no-op; jobs.py only invokes services/user_creation.py when
    # this is non-empty.
    user_create_specs: Optional[List[UserCreateSpec]] = Field(
        default=None,
        description=(
            "Cross-backend user-creation list. Each spec is one row "
            "from the D-OWNER preflight modal. jobs.py walks the list "
            "via services/user_creation.py at run start; on partial "
            "failure the run aborts before any item-state write."
        ),
    )

    # Plan[CROSS-PLATFORM-PREFLIGHT] follow-up: end user-authored
    # per-job resolutions from the preflight modal. Keyed by
    # destination_server_id; same shape stored on schedule rows. Each
    # decision the modal collected (Map / Create / Drop / Accept) is
    # applied at write time by the engine: Drop decisions augment
    # user_filter to skip the user; Map decisions act as in-memory
    # per-job overrides consulted before the identity_map lookup.
    # None means the end user either had no cross-platform concerns
    # OR bypassed the modal entirely.
    cross_platform_resolutions: Optional[Dict[str, Any]] = Field(
        default=None,
        description=(
            "Per-destination operator resolutions from the "
            "cross-platform preflight modal. Keyed by "
            "destination_server_id; applied at write time so the "
            "operator's Drop / Map / Accept decisions ride through "
            "without requiring identity-map persistence."
        ),
    )

    # Plan[MIXED-MEDIA-PLAYLISTS]-2026-05-16: per-run overrides for
    # the mixed-media playlist strategy. None means inherit from the
    # global tunable. Engine consults this chain at restore time:
    # per-user override (in cross_platform_resolutions) > per-run
    # field (here) > global tunable.
    mixed_media_behavior: Optional[Literal["skip", "dominant", "split"]] = None
    mixed_media_dominance_threshold: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    mixed_media_video_routing: Optional[Literal["library_agnostic", "library_dominant"]] = None
    mixed_media_logging: Optional[Literal["full", "decisions_only", "off"]] = None
    mixed_media_collision_handling: Optional[Literal["duplicate", "suffix", "skip"]] = None

    @model_validator(mode="before")
    @classmethod
    def _legacy_skip_flags(cls, values: Any) -> Any:
        return _apply_legacy_skip_flags(values)

    @model_validator(mode="after")
    def _coalesce_destinations(self) -> "RestoreJobIn":
        """
        Reduce ``dest_server_name`` / ``dest_server_names`` to a single
        deduped list on ``dest_server_names``. The downstream dispatch
        in :mod:`server.jobs` then only has to inspect one field.
        """
        names = _coalesce_dest_names(self.dest_server_name, self.dest_server_names)
        # Pydantic v2 lets us assign back to fields on model_validator(after).
        object.__setattr__(self, "dest_server_names", names)
        # Mirror the first entry back into the singular field so older
        # error messages / logs that still print it stay meaningful.
        object.__setattr__(self, "dest_server_name", names[0] if names else None)
        return self

    @model_validator(mode="after")
    def _require_replace_confirmation(self) -> "RestoreJobIn":
        """
        Backend half of the two-layer Replace gate. The UI's typed-REPLACE
        modal sets ``confirm_replace=True`` on submit; a direct API caller
        wanting Replace mode must do the same explicitly. Submitting
        ``mode == "replace"`` without the flag is a 422 from Pydantic.
        Reason this lives on the model (not the route handler): every
        caller that builds a RestoreJobIn is gated identically - restore
        from file, restore from snapshot, scheduled restores, internal
        re-runs - so the rule belongs with the data, not at one endpoint.
        """
        if self.mode == "replace" and not self.confirm_replace:
            raise ValueError(
                "Replace restore requires confirm_replace=true. "
                "The typed-REPLACE confirmation modal sets this flag; "
                "API callers must set it explicitly to acknowledge the "
                "destructive semantics."
            )
        return self

    @model_validator(mode="after")
    def _validate_server_ids(self) -> "RestoreJobIn":
        _validate_server_id_or_none(self.dest_server_id)
        _validate_server_id_list_or_none(self.dest_server_ids)
        return self


# ── Schedule ─────────────────────────────────────────────────────────────────

class RestoreFromSnapshotIn(RestoreJobIn):
    """
    Body of ``POST /api/job/restore-from-snapshot``.

    Mirrors :class:`RestoreJobIn` exactly but resolves the input files
    from a registered snapshot in ``snapshots.db`` rather than asking
    the end user to point at on-disk ``.plexexport.json`` paths. The
    route handler materialises (or reuses the cached) JSON sidecar
    for the snapshot and forwards the call into the standard import
    queue with the sidecar path filled into ``input_files``.
    """
    snapshot_id: str = Field(
        ...,
        min_length=1,
        description=(
            "Registry id (UUID hex) of the snapshot to import from. "
            "Resolved via snapshots.db; the underlying .db must still "
            "be on disk for the JSON render to succeed."
        ),
    )


class ScheduleIn(BaseModel):
    """
    Body of ``POST /api/schedules`` (create) and ``PUT /api/schedules/{id}``
    (replace). A schedule describes a recurring snapshot run.

    Multi-server (v0.9.0): ``source_server_name`` chooses which
    registered server the schedule reads from. Required for v0.9.0+
    schedules; older schedules without this field are read by falling
    back to the first registered server (with a warning logged), so
    a v0.8.0 install still functions after upgrade.
    """

    id: Optional[str] = Field(
        default=None,
        description="Server-assigned UUID. Omit on create; required on update.",
    )
    name: str = Field(
        description="Human-readable name shown in the schedule list.",
    )
    source_server_name: Optional[str] = Field(
        default=None,
        description="Friendly name of the registered server this schedule snapshots from.",
    )
    # PR-Backends: forward-looking stable identifier (Path B from
    # Finding[BACKEND-FILTER-AUDIT]-2026-05-16.md). The scheduler
    # prefers ``source_server_id`` when present, falls back to
    # ``source_server_name`` for legacy schedules. Renaming a server
    # then doesn't break the schedule (server_id never changes).
    source_server_id: Optional[str] = Field(
        default=None,
        description="Stable registry id of the source server (preferred over name).",
    )
    # PR-Backends: backend discriminator for the source server. With
    # the registry allowing duplicate names across backends, the schedule
    # row must carry this to resolve unambiguously. Pre-PR-Backends
    # schedules omit the field; the scheduler reads "plex" as the
    # default on those rows so existing installs keep working.
    source_service_type: Literal["plex", "jellyfin", "emby"] = Field(
        default="plex",
        description="Backend of the source server (plex|jellyfin|emby).",
    )
    # Same discriminator for each fan-out destination, indexed in
    # parallel with dest_server_names. ``len`` should match
    # ``len(dest_server_names)``; the scheduler defaults missing entries
    # to "plex" for backward-compat with pre-PR-Backends rows.
    dest_service_types: Optional[List[Literal["plex", "jellyfin", "emby"]]] = Field(
        default=None,
        description="Per-destination backend (parallel array to dest_server_names).",
    )
    # Parallel array of dest server ids. Same len contract as
    # dest_service_types. Legacy schedules omit it; scheduler falls
    # back to dest_server_names + dest_service_types lookup.
    dest_server_ids: Optional[List[str]] = Field(
        default=None,
        description="Per-destination stable id (parallel array to dest_server_names).",
    )
    libraries: List[str] = Field(
        default_factory=list,
        description="Library names to include; empty = all libraries.",
    )
    output_dir: Optional[str] = Field(
        default=None,
        description="Override default output directory for this schedule.",
    )
    frequency: str = Field(
        default="daily",
        description="One of: hourly | daily | weekly",
    )
    hour: int = Field(default=3, ge=0, le=23,
                     description="Hour of day for daily / weekly runs (0-23, 24-hour clock).")
    minute: int = Field(default=0, ge=0, le=59,
                        description="Minute past the hour (0-59).")
    day_of_week: int = Field(default=0, ge=0, le=6,
                             description="Day of week for weekly runs: 0 = Monday … 6 = Sunday.")
    enabled: bool = Field(
        default=True,
        description="If false, the scheduler skips this entry without deleting it.",
    )
    strict_match: Optional[bool] = Field(
        default=None,
        description=(
            "Optional per-schedule override of strict_match. None = inherit "
            "the saved Settings value at fire time. False = allow first-of-many "
            "fuzzy title matches (mirrors --no-strict-match)."
        ),
    )
    # PR-3 / Phase D - four-flag data-type filter on schedules too.
    # Defaults preserve pre-Phase-D scheduled-snapshot behaviour exactly
    # (every type migrated). Schedules saved before this field landed
    # default to all-true on read because Pydantic supplies the
    # default at load time.
    include_watch_history: bool = Field(
        default=True,
        description="Include watch history in the scheduled snapshot.",
    )
    include_ratings: bool = Field(
        default=True,
        description="Include star ratings in the scheduled snapshot.",
    )
    include_playlists: bool = Field(
        default=True,
        description="Include playlists in the scheduled snapshot.",
    )
    include_collections: bool = Field(
        default=True,
        description="Include collections in the scheduled snapshot.",
    )
    prebuild_json_sidecar: bool = Field(
        default=False,
        description=(
            "Mirror of SnapshotJobIn.prebuild_json_sidecar - when true the "
            "scheduled snapshot also renders a .plexexport.json sidecar at "
            "the end of the run. Off by default to keep scheduled runs fast."
        ),
    )
    # v0.14 Per-Run Settings on schedules. All optional - None means
    # "inherit the global / per-server value at fire time," matching the
    # blank-input convention from the Run-Job form. Each non-None value
    # is forwarded into the snapshot JobRequest the scheduler builds.
    workers: Optional[int] = Field(
        default=None,
        ge=1,
        le=128,
        description="Per-schedule override of the workers count. None = use global default.",
    )
    scrobble_workers: Optional[int] = Field(
        default=None,
        ge=1,
        le=64,
        description="Per-schedule override of scrobble worker count. None = use global default.",
    )
    verbose: Optional[bool] = Field(
        default=None,
        description="Per-schedule verbose-logging override. None = use global default.",
    )
    log_dir: Optional[str] = Field(
        default=None,
        description="Per-schedule log directory override. None = use global default.",
    )
    skip_playlist_prebuild: Optional[bool] = Field(
        default=None,
        description=(
            "Skip the upfront playlist-cache warm for this schedule. "
            "None = use global default."
        ),
    )
    fast_collection_detection: Optional[bool] = Field(
        default=None,
        description=(
            "Use librarySectionUserID for fast personal-collection detection. "
            "None = use global default."
        ),
    )
    watch_ratings_filter_strategy: Optional[str] = Field(
        default=None,
        description=(
            "Per-schedule owner-phase watch+ratings strategy override. "
            "One of \"smart\", \"force_bulk\", \"force_server_side\". "
            "None or empty string = inherit per-server or global default."
        ),
    )
    # Per-schedule user filter - same semantics as SnapshotJobIn.user_filter.
    # Set when the end user wants the schedule to capture only a
    # subset of the source server's users. None = include all.
    user_filter: Optional[List[str]] = Field(
        default=None,
        description=(
            "List of Plex identifiers to include. None = capture every "
            "user the source server reports."
        ),
    )

    # ── Task 2 (admin-management plan follow-up, 2026-05-15) ────────────────
    # Schedules can fire snapshot OR restore OR direct-transfer jobs.
    # Default ``snapshot`` preserves pre-Task-2 behaviour: schedules
    # saved before this field landed read as ``snapshot`` because
    # Pydantic supplies the default at load time. Validators below
    # gate the per-mode requirements (restore needs input_files; direct
    # needs dest_server_names; restore Replace needs confirm_replace).
    mode: str = Field(
        default="snapshot",
        pattern="^(snapshot|restore|direct)$",
        description=(
            "Schedule mode. 'snapshot' (default) fires a snapshot job. "
            "'restore' fires a restore job against the configured input "
            "files and destinations. 'direct' fires a direct transfer "
            "from the source server to the listed destinations."
        ),
    )
    dest_server_names: Optional[List[str]] = Field(
        default=None,
        description=(
            "Destination server(s) for restore + direct modes. Two or "
            "more entries fan out the job. Ignored for snapshot mode."
        ),
    )
    input_files: Optional[List[str]] = Field(
        default=None,
        description=(
            "Snapshot file paths for restore mode. Required when "
            "mode == 'restore'; ignored otherwise."
        ),
    )
    restore_mode: Optional[str] = Field(
        default=None,
        pattern="^(merge|replace)$",
        description=(
            "For restore + direct modes: 'merge' (additive, idempotent) "
            "or 'replace' (destructive, requires confirm_replace=true). "
            "None inherits the global default. Ignored for snapshot mode."
        ),
    )
    auto_capture_before_replace: Optional[bool] = Field(
        default=None,
        description=(
            "For restore_mode == 'replace': capture a pre-replace "
            "rollback snapshot before the destructive write. None "
            "inherits the global default. Ignored otherwise."
        ),
    )
    confirm_replace: bool = Field(
        default=False,
        description=(
            "Required true when restore_mode == 'replace' on a schedule. "
            "Operator confirmation that this schedule should fire a "
            "destructive Replace on every tick."
        ),
    )
    merge_watch_strategy: Optional[str] = Field(
        default=None,
        pattern="^(higher|sum)$",
        description=(
            "Merge-mode watch-count math for restore + direct. 'higher' "
            "(default) is idempotent: destination view count ends at "
            "max(stored, current), so re-runs are no-ops once caught up. "
            "'sum' is additive: every fire adds the snapshot's stored "
            "counts on top of the destination's current counts. Allowed "
            "on schedules only when confirm_additive_merge=true; "
            "without that flag the validator rejects 'sum' to prevent "
            "silent compounding on scheduled re-fires."
        ),
    )
    confirm_additive_merge: bool = Field(
        default=False,
        description=(
            "Required true when merge_watch_strategy='sum' on a schedule. "
            "Operator confirmation that this schedule should fire an "
            "additive merge on every tick - each fire ADDS the snapshot's "
            "stored view counts on top of whatever the destination "
            "currently shows. This compounds: two fires double the "
            "stored contribution, three fires triple it, etc. Choose "
            "'sum' only when the schedule's source genuinely represents "
            "activity that should accumulate (e.g. a sibling server "
            "feeding its plays in)."
        ),
    )
    remap_old: Optional[str] = Field(
        default=None,
        description="Old root path prefix for --remap-path (restore + direct).",
    )
    remap_new: Optional[str] = Field(
        default=None,
        description="New root path prefix for --remap-path (restore + direct).",
    )

    # Phase C (admin-management follow-up, 2026-05-15): per-library
    # metric filter on schedules. Same shape and semantics as the
    # job-input models. When set, the scheduler forwards this to the
    # fired job and the engine consults it per library.
    library_metrics: Optional[Dict[str, LibraryMetrics]] = Field(
        default=None,
        description=(
            "Per-library metric filter for the fired job. Authoritative "
            "when set; the engine falls back to the global include_* "
            "flags for libraries not in the map."
        ),
    )

    # ── Schedules-alignment additions (2026-05-16) ─────────────────────
    # Fields below bring the schedule row to parity with the Run Job
    # submit payload so the Schedules editor can mirror the Run Job
    # form 1:1. Each one carries a sensible default that matches
    # pre-alignment behaviour, so existing schedule rows continue to
    # fire unchanged when read back through this validator.

    # D-OWNER (a): per-user fan-out toggle. Matches the adapter
    # engines' include_managed_users kwarg. True = capture managed
    # users alongside the owner; False = owner-only.
    include_managed_users: bool = Field(
        default=True,
        description=(
            "Include managed users in this schedule's capture. "
            "False = owner-only (engines skip every managed user)."
        ),
    )
    # D-RATE: per-job rating-mapping policy on cross-backend routes.
    # None / 'default' = Plex's "Favorite at or above 5" default.
    # 'tunable' = use rate_threshold. 'numeric_only' = never set
    # IsFavorite. Ignored on same-backend schedule fires.
    rate_mode: Optional[Literal["default", "tunable", "numeric_only"]] = Field(
        default=None,
        description=(
            "D-RATE policy for cross-backend schedule fires. None "
            "inherits 'default'; 'tunable' consults rate_threshold; "
            "'numeric_only' disables IsFavorite writes."
        ),
    )
    rate_threshold: Optional[float] = Field(
        default=None,
        ge=0.0,
        le=10.0,
        description=(
            "Rating threshold (stars, 0-10) used when rate_mode == "
            "'tunable'. Ignored otherwise."
        ),
    )
    # D-OWNER (b): cross-backend user-create specs. The scheduler
    # forwards this list at fire time so the adapter engine
    # provisions missing destination users before the data write.
    # End user confirms once at schedule-save time via the same
    # UserCreationModal Run Job uses; the resulting specs persist
    # here and re-apply on every fire (creates are idempotent).
    user_create_specs: Optional[List[UserCreateSpec]] = Field(
        default=None,
        description=(
            "Confirmed user-create specs for cross-backend schedule "
            "fires. Forwarded into the JobRequest at fire time so "
            "the destination has the necessary user rows before the "
            "engine writes per-user data."
        ),
    )
    # PIN-preflight acknowledgement persisted at save time. The
    # editor probes for cross-server PIN risk when the end user
    # clicks Save; if at-risk users exist, the end user must
    # acknowledge via PinPreflightModal. The acknowledgement clears
    # automatically when source_server_name changes (the validator
    # below handles this).
    pin_preflight_ack: bool = Field(
        default=False,
        description=(
            "Operator acknowledgement of cross-server PIN risk at "
            "schedule save time. Cleared automatically when "
            "source_server_name changes."
        ),
    )
    # Per-Run Settings parity: overwrite_playlists is exposed on
    # Run Job > Per-Run Settings > General. Schedules adopt the
    # same field so PerRunSettingsPanel renders identically on
    # both surfaces. No-op flag in the engine today (kept for
    # back-compat with v0.2.0).
    overwrite_playlists: Optional[bool] = Field(
        default=None,
        description=(
            "Per-schedule override of overwrite_playlists. None = "
            "inherit Run Defaults at fire time."
        ),
    )
    # Plan[CROSS-PLATFORM-PREFLIGHT] step 5: end user-authored
    # cross-platform resolution decisions stored on the schedule row
    # so fires reuse them deterministically (no end user at fire time
    # to ack ambiguities). Keyed by destination_server_id. The
    # schedule list endpoint computes resolutions_status per row by
    # comparing stored decisions against the current dest user
    # roster; needs_review surfaces when stored mappings rot.
    cross_platform_resolutions: Optional[Dict[str, Any]] = Field(
        default=None,
        description=(
            "Stored cross-platform preflight decisions, keyed by "
            "destination_server_id. None when no cross-platform "
            "concerns apply (single-backend schedule)."
        ),
    )

    # Plan[MIXED-MEDIA-PLAYLISTS]-2026-05-16: per-schedule overrides.
    # Schedule fire reads schedule row > settings > global default at
    # fire time.
    mixed_media_behavior: Optional[Literal["skip", "dominant", "split"]] = None
    mixed_media_dominance_threshold: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    mixed_media_video_routing: Optional[Literal["library_agnostic", "library_dominant"]] = None
    mixed_media_logging: Optional[Literal["full", "decisions_only", "off"]] = None
    mixed_media_collision_handling: Optional[Literal["duplicate", "suffix", "skip"]] = None

    @model_validator(mode="before")
    @classmethod
    def _legacy_include_to_library_metrics(cls, values: Any) -> Any:
        """Same expansion the job models run: when library_metrics is
        unset and the schedule carries global include_* flags + a
        ``libraries`` list, expand the flags into a per-library map."""
        return _expand_legacy_include_flags_to_library_metrics(values)

    @model_validator(mode="after")
    def _validate_mode_requirements(self) -> "ScheduleIn":
        """
        Mode-specific requirement gates:
          * restore mode requires at least one input_files entry
            and at least one dest_server_names entry.
          * direct mode requires source_server_name and at least one
            dest_server_names entry.
          * snapshot mode requires source_server_name.
          * restore_mode == 'replace' requires confirm_replace == True
            (end user opt-in to destructive scheduled writes).

        The legacy ScheduleIn (no mode field, snapshot-only) reads
        as ``mode == 'snapshot'`` and only the source_server_name
        requirement applies, matching pre-Task-2 behaviour.
        """
        m = (self.mode or "snapshot").strip()
        if m == "snapshot":
            # Snapshot mode never reads dest_server_names / input_files
            # / restore_mode; we leave them tolerated rather than
            # rejected so a UI mode-switch doesn't silently drop the
            # end user's stored restore-mode params.
            return self
        if m in ("restore", "direct"):
            dests = self.dest_server_names or []
            if not dests:
                raise ValueError(
                    f"Schedule mode={m!r} requires at least one entry in dest_server_names."
                )
        if m == "restore":
            files = self.input_files or []
            if not files:
                raise ValueError(
                    "Schedule mode='restore' requires at least one entry in input_files."
                )
        if m == "direct" and not (self.source_server_name or "").strip():
            raise ValueError(
                "Schedule mode='direct' requires source_server_name."
            )
        if (self.restore_mode or "") == "replace" and not self.confirm_replace:
            raise ValueError(
                "Schedule with restore_mode='replace' must set confirm_replace=true. "
                "Destructive scheduled writes require explicit operator opt-in."
            )
        # 2026-05-16 alignment: tunable D-RATE needs a threshold.
        # Other rate_mode values ignore rate_threshold entirely.
        if (self.rate_mode or "") == "tunable" and self.rate_threshold is None:
            raise ValueError(
                "Schedule with rate_mode='tunable' must also set rate_threshold "
                "(0.0-10.0 stars)."
            )
        return self

    @model_validator(mode="after")
    def _block_non_idempotent_merge_on_schedules(self) -> "ScheduleIn":
        """
        v0.13.x: defensive guard against ``merge_watch_strategy="sum"``
        on a schedule.

        ScheduleIn is snapshot-only today (no ``mode`` / ``merge_watch_strategy``
        fields), so this validator is a no-op on every existing payload.
        It exists ahead of the roadmap's scheduled-restore feature: the
        moment those fields are added to ScheduleIn, this guard kicks in
        without the implementer having to remember to add it.

        Why ``sum`` is unsafe on a schedule
        -----------------------------------
        Merge "sum" mode adds the snapshot's view count on top of the
        destination's current count. The math is deliberate for
        one-shot end user-driven jobs (e.g. consolidating plays from a
        retired server). But a schedule re-fires on every tick, and
        each fire would re-add the same stored counts, so the
        destination's view count grows linearly with the number of
        schedule fires - a silent corruption that's hard to recover
        from without the end user noticing.

        The "higher" strategy IS idempotent (max(stored, current)) and
        is the right choice for any automated re-run. The schedule
        creator must either pick "higher" or use Replace mode (which
        is gated by typed-REPLACE and the auto-capture safety belt).
        """
        # Task 2 (2026-05-15): ``self.mode`` now carries the schedule's
        # JOB mode (snapshot|restore|direct), not the merge/replace
        # restore-mode field. The restore-side mode lives in
        # ``self.restore_mode``; that's what gates this validator.
        #
        # Follow-up (2026-05-15): 'sum' is no longer hard-rejected.
        # An end user who genuinely wants additive merge on a
        # schedule can opt in via ``confirm_additive_merge=true``.
        # Without that flag we still refuse because the failure mode
        # is silent and compounding (each scheduled fire adds the
        # stored counts on top of the destination's current counts).
        restore_mode = getattr(self, "restore_mode", None)
        strategy = getattr(self, "merge_watch_strategy", None)
        confirmed = bool(getattr(self, "confirm_additive_merge", False))
        if (
            (restore_mode is None or restore_mode == "merge")
            and strategy == "sum"
            and not confirmed
        ):
            raise ValueError(
                "Schedules with merge_watch_strategy='sum' must set "
                "confirm_additive_merge=true. 'sum' adds the snapshot's "
                "stored counts on top of the destination's current "
                "counts every time the schedule fires, which compounds "
                "on each tick. Set the confirmation flag if that is "
                "what you want; otherwise pick 'higher' (idempotent) "
                "or Replace mode."
            )
        return self


# ── Outbound shapes ──────────────────────────────────────────────────────────
# We return plain dicts from most endpoints rather than typing every
# response - the snapshot payload in particular is loose by design
# (mirrors DashboardState.to_dashboard_frame()) and shapes change as the engine
# evolves. The TypeScript frontend has its own narrow types in
# ``frontend/src/api.ts`` for the fields it actually reads.

class DirectTransferIn(_PinPreflightAckFields):
    """
    Body of ``POST /api/job/direct``.

    Reads watch history / playlists / collections / ratings from
    ``source_server_name`` and writes them straight into
    ``dest_server_name`` without an intermediate file. All additive-
    only merge rules from the regular import path still apply.

    Fan-out (v0.10.0, Feature 1): when ``dest_server_names`` has two or
    more entries the job runs as a fan-out - the source is read once
    and the same playload is dispatched to every destination in
    parallel, each with its own thread-local DashboardState surfaced
    in the web UI as a destination card. The single-server
    ``dest_server_name`` field stays accepted for backward compat and
    is folded into ``dest_server_names`` by the validator below.
    """

    source_server_name: str = Field(
        description="Friendly name of the registered source server.",
    )
    # Plan[SERVER-UID-IDENTITY] 2026-05-16: stable per-row identifier.
    # Preferred over source_server_name; disambiguates same-named
    # servers across backends. None falls back to name lookup with
    # a warning.
    source_server_id: Optional[str] = Field(
        default=None,
        description=(
            "Stable registry id of the source server "
            "(`<plex|jellyfin|emby>_<uuid4_hex>`). Preferred over "
            "source_server_name when both are supplied."
        ),
    )
    dest_server_name: Optional[str] = Field(
        default=None,
        description=(
            "Friendly name of a single destination server. Backward-"
            "compat form - prefer ``dest_server_names`` for new clients."
        ),
    )
    dest_server_names: Optional[List[str]] = Field(
        default=None,
        description=(
            "Fan-out targets. When two or more names are supplied the job "
            "runs as a fan-out and copies to every destination in parallel."
        ),
    )
    # Same UID treatment for destinations.
    dest_server_id: Optional[str] = Field(
        default=None,
        description=(
            "Stable registry id of a single destination server. "
            "Preferred over dest_server_name when both are supplied."
        ),
    )
    dest_server_ids: Optional[List[str]] = Field(
        default=None,
        description=(
            "Stable registry ids for fan-out destinations. Preferred "
            "over dest_server_names. Single-id field folds into this list."
        ),
    )
    libraries: List[str] = Field(
        default_factory=list,
        description=(
            "Library names to transfer. Empty = every library present on both servers."
        ),
    )
    workers: Optional[int] = Field(default=None, ge=1, le=128)
    scrobble_workers: Optional[int] = Field(default=None, ge=1, le=64)
    verbose: Optional[bool] = None
    log_dir: Optional[str] = None
    remap_old: Optional[str] = None
    remap_new: Optional[str] = None
    strict_match: Optional[bool] = None
    # v0.9.6 Feature 4: limit per-user data transfer to this list of
    # raw Plex identifiers (managed-user usernames). ``None`` / absent
    # = all users included (the existing v0.9.5 behaviour). Empty list
    # = exclude every managed user; only the owner's data transfers.
    # Matching is by raw identifier, not display name - display names
    # are a pure rendering aid (Feature 3). The filter applies wholesale
    # to each managed user's block (watch history + playlists +
    # collections + ratings together). The owner's data always
    # transfers regardless of this list.
    user_filter: Optional[List[str]] = Field(
        default=None,
        description=(
            "List of managed-user identifiers to include. None = include all."
        ),
    )
    skip_collections: Optional[bool] = Field(
        default=None,
        description="Skip collection gather/transfer. Default False.",
    )
    fast_collection_detection: Optional[bool] = Field(
        default=None,
        description=(
            "Use librarySectionUserID for fast personal-collection detection. "
            "Requires Plex ≥1.32; falls back to rating-key dedup automatically."
        ),
    )
    skip_playlists: Optional[bool] = Field(
        default=None,
        description="Legacy. Use include_playlists instead. Skip playlist gather/transfer.",
    )
    # PR-3 / Phase D - four-flag data-type filter (default-true).
    include_watch_history: bool = Field(
        default=True,
        description="Include watch history in the transfer.",
    )
    include_ratings: bool = Field(
        default=True,
        description="Include star ratings in the transfer.",
    )
    include_playlists: bool = Field(
        default=True,
        description="Include playlists in the transfer. Supersedes legacy skip_playlists.",
    )
    include_collections: bool = Field(
        default=True,
        description="Include collections in the transfer. Supersedes legacy skip_collections.",
    )
    # v0.13.x: restore mode for the destination write phase. Mirrors
    # RestoreJobIn - see the longer commentary there. Direct transfers
    # write their data using the same restore_export_file primitive,
    # so the same merge / replace semantics apply.
    mode: str = Field(
        default="merge",
        pattern="^(merge|replace)$",
        description=(
            "Destination write mode. 'merge' (default) preserves newer "
            "destination activity; 'replace' overwrites to make the "
            "destination match the source exactly."
        ),
    )
    auto_capture_before_replace: bool = Field(
        default=True,
        description=(
            "When mode == 'replace', auto-capture a snapshot of every "
            "destination before the transfer fires so each destination "
            "has its own recovery point. Ignored when mode == 'merge'."
        ),
    )
    confirm_replace: bool = Field(
        default=False,
        description=(
            "Required confirmation flag for mode == 'replace'. The UI "
            "sets this after the operator types REPLACE in the "
            "confirmation modal. API callers must set it explicitly."
        ),
    )
    # v0.13.x: Merge sub-strategy for watch counts. Mirrors the
    # matching field on RestoreJobIn - see that field's docstring for
    # the higher / sum semantics. Direct transfers write with the same
    # restorer primitive, so the same choice applies.
    merge_watch_strategy: str = Field(
        default="higher",
        pattern="^(higher|sum)$",
        description=(
            "Merge-mode watch-count math. 'higher' (default) keeps the "
            "larger of stored/current; 'sum' adds stored on top of "
            "current. Ignored when mode == 'replace'."
        ),
    )
    # Per-job watch+ratings strategy override (same semantics as on
    # SnapshotJobIn). Direct transfers run an inline snapshot capture
    # for the source side, so the strategy applies there too.
    watch_ratings_filter_strategy: Optional[str] = Field(
        default=None,
        description=(
            "Per-job override for the owner-phase watch+ratings strategy. "
            "One of: \"smart\", \"force_bulk\", \"force_server_side\"."
        ),
    )

    # Phase C: per-library metric filter (same semantics as SnapshotJobIn).
    library_metrics: Optional[Dict[str, LibraryMetrics]] = Field(
        default=None,
        description=(
            "Per-library metric filter: {library_name: {watch_history, "
            "ratings, playlists, collections}}. Authoritative when set; "
            "global include_* flags are the fallback for libraries not "
            "in the map."
        ),
    )

    # Plan[RUN-JOB-UI] work items 2-4: same three end user-tunable
    # knobs as RestoreJobIn (see those fields' docstrings for the
    # full rationale). Direct transfer runs the adapter restorer
    # under the hood when the destination is Jellyfin / Emby; the
    # kwargs flow through identically.
    favorite_threshold: Optional[float] = Field(
        default=None,
        description=(
            "Per-job override of the rating-to-favorite cross-mapping "
            "threshold; None uses the engine default (5.0). 11.0 "
            "disables IsFavorite writes entirely."
        ),
    )
    include_managed_users: bool = Field(
        default=True,
        description=(
            "When True (default), the adapter engines fan out per "
            "managed user. When False, owner-only on both ends."
        ),
    )
    user_create_specs: Optional[List[UserCreateSpec]] = Field(
        default=None,
        description=(
            "Cross-backend user-creation list from the D-OWNER modal. "
            "Walked at run start by services/user_creation.py BEFORE "
            "any item-state write; partial failure aborts the run."
        ),
    )
    # Plan[CROSS-PLATFORM-PREFLIGHT] follow-up: see RestoreJobIn for
    # the full per-job override semantics. Same shape applies on direct
    # transfer; Drop/Map decisions are honoured at write time.
    cross_platform_resolutions: Optional[Dict[str, Any]] = Field(
        default=None,
        description=(
            "Per-destination operator resolutions from the "
            "cross-platform preflight modal. Keyed by "
            "destination_server_id; applied at write time."
        ),
    )

    # Plan[MIXED-MEDIA-PLAYLISTS]-2026-05-16: per-run overrides for
    # the mixed-media playlist strategy on direct transfer.
    mixed_media_behavior: Optional[Literal["skip", "dominant", "split"]] = None
    mixed_media_dominance_threshold: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    mixed_media_video_routing: Optional[Literal["library_agnostic", "library_dominant"]] = None
    mixed_media_logging: Optional[Literal["full", "decisions_only", "off"]] = None
    mixed_media_collision_handling: Optional[Literal["duplicate", "suffix", "skip"]] = None

    @model_validator(mode="before")
    @classmethod
    def _legacy_skip_flags(cls, values: Any) -> Any:
        values = _apply_legacy_skip_flags(values)
        values = _expand_legacy_include_flags_to_library_metrics(values)
        return values

    @model_validator(mode="after")
    def _coalesce_destinations(self) -> "DirectTransferIn":
        """
        Reduce ``dest_server_name`` / ``dest_server_names`` to a single
        deduped list on ``dest_server_names``. At least one destination
        must be present - an empty list raises ``ValueError`` so the
        request hits 422 before reaching the queue.
        """
        names = _coalesce_dest_names(self.dest_server_name, self.dest_server_names)
        if not names:
            raise ValueError(
                "DirectTransferIn requires at least one destination "
                "(dest_server_name or dest_server_names)."
            )
        object.__setattr__(self, "dest_server_names", names)
        object.__setattr__(self, "dest_server_name", names[0])
        return self

    @model_validator(mode="after")
    def _require_replace_confirmation(self) -> "DirectTransferIn":
        """
        Backend half of the two-layer Replace gate (see the matching
        validator on :class:`RestoreJobIn`). Direct transfers write
        with the same restorer primitive, so the same gate applies.
        """
        if self.mode == "replace" and not self.confirm_replace:
            raise ValueError(
                "Replace direct-transfer requires confirm_replace=true. "
                "The typed-REPLACE confirmation modal sets this flag; "
                "API callers must set it explicitly to acknowledge the "
                "destructive semantics."
            )
        return self

    @model_validator(mode="after")
    def _validate_server_ids(self) -> "DirectTransferIn":
        _validate_server_id_or_none(self.source_server_id)
        _validate_server_id_or_none(self.dest_server_id)
        _validate_server_id_list_or_none(self.dest_server_ids)
        return self


# ── PR-12 preflight ──────────────────────────────────────────────────────────

class PinPreflightIn(BaseModel):
    """
    Body of ``POST /api/job/preflight-pin-check``.

    The frontend posts the same scope it is about to use for the
    actual job submit so the backend can compute the at-risk
    managed-user list against the User Management database.
    ``mode`` determines whether the check applies (snapshot / direct
    yes, restore no).
    """
    mode: str = Field(
        description="snapshot | restore | direct",
    )
    source_server_name: Optional[str] = Field(
        default=None,
        description="Friendly name of the source server. Required for snapshot and direct.",
    )
    dest_server_names: Optional[List[str]] = Field(
        default=None,
        description="Direct-mode destination names (also fan-out targets).",
    )
    user_filter: Optional[List[str]] = Field(
        default=None,
        description=(
            "Optional narrow scope, mirroring DirectTransferIn.user_filter. "
            "None = every managed user on the relevant server(s) is in scope."
        ),
    )


# ── Item 3: cross-server PIN migration ──────────────────────────────────────

class PinMigrationSuggestion(BaseModel):
    """
    One row in a PIN-migration apply request: the end user confirmed
    that the PIN stored under ``source_server_id`` /
    ``source_username`` should be copied onto the (current-endpoint)
    server under ``target_username``. ``match_kind`` is echoed back
    from the suggestions GET so the apply log line records whether a
    machine_id or username fallback was the basis.
    """
    target_username: str
    source_server_id: str
    source_username: str
    match_kind: Optional[str] = None


class PinMigrationApplyIn(BaseModel):
    """Body of ``POST /api/servers/{id}/pin-migration/apply``."""
    suggestions: List[PinMigrationSuggestion] = Field(
        default_factory=list,
        description="Confirmed migrations the operator wants applied.",
    )


class ServerIn(BaseModel):
    """
    Body of ``POST /api/servers`` (create) and ``PUT /api/servers/{id}`` (update).

    On update, ``token`` is special: an empty string means "leave the
    saved token unchanged" so the frontend can submit the form without
    re-entering it. A non-empty string replaces the saved token.
    """

    name: str = Field(description="Friendly name shown in the UI and CLI.")
    url: str = Field(description="Full server URL, e.g. http://host.docker.internal:32400")
    token: str = Field(
        default="",
        description="Auth token / API key. Empty on update = keep existing.",
    )
    # PR-Backends: backend type discriminator. Existing registry rows
    # without ``service_type`` default to "plex" on read; new
    # registrations declare their backend explicitly. Drives which
    # adapter class wraps the connection.
    service_type: Literal["plex", "jellyfin", "emby"] = Field(
        default="plex",
        description="Backend type. 'plex' (default), 'jellyfin', or 'emby'.",
    )
    # Auto-fallback flow: when the end user clicked "Try fallback token"
    # on the Add Server form's Test result and got a working connection
    # via the borrowed token, the frontend submits this field. The
    # backend will store the borrowed token under ``token`` and stash
    # the end user's typed token (above) as a pending retry.
    use_fallback_from_server_id: Optional[str] = Field(
        default=None,
        description=(
            "When set, register using the borrowed token from this "
            "existing server. The token in the ``token`` field above "
            "is stashed as pending_token and surfaced as a retry chip "
            "on the Servers panel. Used for the new-server Plex.tv "
            "token-propagation-lag scenario."
        ),
    )


class TestUnsavedIn(BaseModel):
    """
    Body of ``POST /api/servers/test-unsaved`` (v0.10.0).

    Probe a URL+token combination without writing anything to the
    registry. The response includes the connected server's reported
    friendly name and machine identifier so the end user can confirm
    they're authenticating against the server they intended - a Plex
    account's token unlocks every server that account owns, so a
    successful connect does not by itself prove anything about which
    Plex install the URL actually points at.

    ``name`` is optional and present only so the UI can flag a
    mismatch between the friendly name the end user typed and the
    name the server reports for itself.
    """

    name: str = Field(default="", description="Operator's chosen friendly name (optional, for mismatch detection).")
    url: str = Field(description="Server URL to probe.")
    token: str = Field(description="Auth token / API key to probe with.")
    service_type: Literal["plex", "jellyfin", "emby"] = Field(
        default="plex",
        description="Backend type to probe as.",
    )


# ── User display-name editing (v0.9.6 Feature 3) ─────────────────────────────

class UserDisplayNameIn(BaseModel):
    """
    Body of ``PATCH /api/servers/{id}/user-display-name``.

    ``plex_id`` is the raw Plex identifier - owner email for the owner
    row, managed-user username for managed rows. ``display_name`` is
    the end user's chosen friendly name. An empty string clears the
    mapping (the UI then falls back to showing the raw identifier).
    """

    plex_id: str = Field(description="Raw Plex identifier - owner email or managed username.")
    display_name: str = Field(default="", description="Friendly name; empty clears.")


class DevTestRunIn(BaseModel):
    """
    Body of ``POST /api/dev/run-tests``.

    Developer-tool unit test runner. Gated by debug mode on the
    backend; the endpoint returns 403 when PLEXMIGRATE_DEBUG_MODE is
    unset.

    ``mode`` selects the test directory:

    * ``"synthetic"`` (default) -> tests_backend/ (the existing unit
      suite; safe at any time)
    * ``"structural"`` -> tests_structural/ (asserts against a temp
      copy of real artefacts; never modifies originals)
    * ``"live"`` -> tests_live/ (read-only operations against a
      configured live Plex server; requires confirm_live=True)

    ``pytest_filter`` is an optional ``-k`` selector. Sanitised at the
    backend boundary; allowed characters are alphanumerics, dot,
    colon, underscore, hyphen, brackets, and space.

    ``confirm_live`` must be True when ``mode == "live"``; the UI
    surfaces a typed confirmation prompt before submitting.
    """

    mode: str = Field(
        default="synthetic",
        description='One of "synthetic" / "structural" / "live".',
    )
    pytest_filter: Optional[str] = Field(
        default=None,
        description=(
            'Optional pytest -k selector. Sanitised at the boundary. '
            'None or empty runs every test in the selected mode.'
        ),
    )
    confirm_live: bool = Field(
        default=False,
        description='Must be True when mode=="live". Ignored otherwise.',
    )


class EtaPredictLibrarySpec(BaseModel):
    """One library's metadata passed to the ETA predictor. The
    frontend already has access to library name / type / item count
    from ``/api/servers/{id}/libraries`` and forwards the same three
    fields here; the predictor uses ``library_type`` to pick the right
    bucket key dimension and ``items_count`` as the regression
    variable inside the bucket."""
    name: str = Field(default="")
    library_type: str = Field(default="")
    items_count: Optional[int] = Field(
        default=None,
        description=(
            "Operator-visible size from the source server's library "
            "list. The trainer regresses duration against this value, "
            "so a larger library yields a proportionally larger "
            "estimate. Null is acceptable (the regression falls back "
            "to intercept-only, equivalent to the historical mean)."
        ),
    )


class EtaPredictIn(BaseModel):
    """Body of ``POST /api/eta/predict``. The job-form preview hits
    this on every change with the current selection; the backend
    rolls per-library + per-metric estimates into a whole-job
    estimate plus a per-library breakdown."""

    mode: str = Field(
        default="snapshot",
        description='One of "snapshot" / "restore" / "direct".',
    )
    source_server_id: str = Field(
        default="",
        description=(
            "The server whose learned timings to consult. For a "
            "snapshot or direct-transfer job the source server; "
            "for a restore the destination server (its writes are "
            "what the engine times)."
        ),
    )
    libraries: List[EtaPredictLibrarySpec] = Field(default_factory=list)
    metrics_enabled: Dict[str, bool] = Field(
        default_factory=dict,
        description=(
            "Four-flag dict {watch_history, ratings, playlists, "
            "collections}. Missing keys default to ON to match the "
            "LibraryMetrics shape; explicit False suppresses that "
            "metric's contribution to the rollup."
        ),
    )
    user_count: int = Field(
        default=1,
        description="Owner (1) + managed users included in the run.",
    )
    workers: int = Field(
        default=1,
        description="Worker-pool size that will run the libraries in parallel.",
    )
    bulk_strategy: str = Field(
        default="smart",
        description=(
            'Watch+ratings strategy: "smart" / "force_bulk" / '
            '"force_server_side". Used as a bucket-key dimension so '
            "the predictor learns per-strategy as well as per-server."
        ),
    )


class UserCopyIn(BaseModel):
    """
    Body of ``POST /api/users/copy_to_destination``. The ad-hoc
    single-user copy flow that surfaces under Servers > User
    Management. Different from D-OWNER's batch flow on the Run Job
    form: this is one user, immediate creation, no queued job.

    The endpoint walks the same ``services/user_creation.py``
    two-phase create-then-link helper as the Run Job D-OWNER path,
    so partial-failure rollback semantics match exactly. The follow-
    up data transfer (when the end user checks 'run automatic
    transfer' in the frontend) is a separate ``/api/job/direct``
    call orchestrated by the frontend; this endpoint never blocks on
    item-state writes.
    """

    source_server_id: str = Field(
        ...,
        description=(
            "Registry id of the server the operator is copying FROM. "
            "Used as the spec's source_user_handle context + for the "
            "managed_users mapping write so the engine can resolve "
            "source -> destination on later runs."
        ),
    )
    target_server_id: str = Field(
        ...,
        description=(
            "Registry id of the server the user will be created on. "
            "Must be a Jellyfin or Emby backend; Plex destinations "
            "are rejected because Plex cannot create users via API."
        ),
    )
    source_user_handle: str = Field(
        ...,
        description=(
            "Username on the source server. Carried into the "
            "managed_users mapping row so later runs can resolve "
            "source identity -> destination identity."
        ),
    )
    target_username: str = Field(
        ...,
        description="Username to assign on the destination.",
    )
    temp_password: str = Field(
        ...,
        description=(
            "Plaintext password for the new destination user. Backend "
            "never logs the plaintext; encrypted at rest in "
            "managed_users.service_password_enc."
        ),
    )
    target_user_policy: Optional[Dict[str, Any]] = Field(
        default=None,
        description=(
            "Optional Jellyfin / Emby user policy "
            "(IsAdministrator, EnableAllFolders, EnabledFolders, ...)."
            " Omit to accept the destination's default."
        ),
    )


class UserIdentityMapIn(BaseModel):
    """Body of ``POST /api/users/identity_map``. Adds one
    (server_a, handle_a) <-> (server_b, handle_b) link to
    media.db's user_identity_map table. The pair is bidirectional;
    a duplicate insert (same A + B in the same order) is a no-op.

    Used by the standalone User Mapping panel to wire same-person-
    different-handle relationships across servers (same-backend OR
    cross-backend). The engine consults the map at user-filter
    resolution time so cross-server transfers find each user's
    counterpart even when usernames differ.
    """

    server_a_id: str = Field(..., description="Registry id of the first server.")
    user_a_handle: str = Field(..., description="Username on the first server.")
    server_b_id: str = Field(..., description="Registry id of the second server.")
    user_b_handle: str = Field(..., description="Username on the second server.")
    source: str = Field(
        default="manual",
        description=(
            "Provenance. 'manual' for operator-authored mappings, "
            "'auto_copy' for mappings written by the copy_to_destination "
            "flow."
        ),
    )

    @model_validator(mode="after")
    def _reject_empty_strings(self) -> "UserIdentityMapIn":
        if not (self.server_a_id or "").strip():
            raise ValueError("server_a_id is required")
        if not (self.user_a_handle or "").strip():
            raise ValueError("user_a_handle is required")
        if not (self.server_b_id or "").strip():
            raise ValueError("server_b_id is required")
        if not (self.user_b_handle or "").strip():
            raise ValueError("user_b_handle is required")
        return self


class EtaBackfillIn(BaseModel):
    """Body of ``POST /api/eta/backfill``. Gap-A of the post-cutover
    review: warm-start the new ETA engine from the historical
    ``run_timings`` table so the predictor's tier-1 cells light up
    without waiting for the end user to manually exercise every
    bucket from scratch."""

    reset_first: bool = Field(
        default=False,
        description=(
            "True wipes every bucket before replaying history so the "
            "weights exactly reflect run_timings. False (default) adds "
            "to existing weights - safe to re-run on an already-trained "
            "install."
        ),
    )


class EtaResetIn(BaseModel):
    """Body of ``POST /api/eta/reset``. End user's D-RESET escape
    hatch for the 'I just upgraded the server's storage and the
    old timings are wrong' case. Behind a typed-confirmation UI
    prompt."""

    server_id: str = Field(
        default="",
        description="Required; empty is a no-op so a typo cannot wipe everything.",
    )
    confirm: str = Field(
        default="",
        description=(
            'Operator must type "RESET" verbatim on the UI prompt '
            "and the frontend forwards it here. Backend rejects the "
            "request when this does not match exactly."
        ),
    )


class DbImportIn(BaseModel):
    """Body of ``POST /api/database/import/{table_id}`` and the
    archive variant. Carries the end user's typed-REPLACE
    confirmation alongside the JSON payload that was downloaded
    from a previous export."""

    confirm: str = Field(
        default="",
        description=(
            'Operator must type "REPLACE" verbatim. Import is '
            "replace-only - the target table is wiped before "
            "the supplied rows are inserted, so an accidental "
            "submit could wipe live data."
        ),
    )
    payload: Dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "The exported JSON document. For single-table imports "
            "this is the plexbackup.dbexport.v1 shape; for archive "
            "imports this is the plexbackup.archive.v1 shape."
        ),
    )


class EtaFlushIn(BaseModel):
    """Body of ``POST /api/eta/flush``. Settings ETA Training panel's
    'Flush all training data' button: clears every bucket and
    (optionally) the underlying run_timings history."""

    include_run_timings: bool = Field(
        default=False,
        description=(
            "True also wipes the run_timings table so the auto-backfill "
            "on next boot cannot repopulate buckets from prior history. "
            "False (default) clears buckets only; a subsequent backfill "
            "would restore them from run_timings."
        ),
    )
    confirm: str = Field(
        default="",
        description=(
            'Operator must type "FLUSH" verbatim on the UI prompt and '
            "the frontend forwards it here. Backend rejects the request "
            "when this does not match exactly."
        ),
    )


class JobStatusOut(BaseModel):
    """
    Shape returned by ``GET /api/job``.

    The full live dashboard payload is delivered via the WebSocket
    (see :mod:`server.ws`); this endpoint is for clients that just
    want a once-off snapshot - for example, the frontend on first
    page load before the socket opens.
    """

    state: str = Field(description="idle | queued | running | stopping | completed | failed | cancelled")
    job_id: Optional[str] = None
    mode: Optional[str] = Field(default=None, description="snapshot | import")
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    error: Optional[str] = None
    dashboard: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Last known DashboardState.to_dashboard_frame() - null when idle.",
    )


# ── Cross-platform preflight (Plan[CROSS-PLATFORM-PREFLIGHT] step 3) ────────
#
# Wire types for POST /api/jobs/cross-platform-preflight,
# POST /api/schedules/cross-platform-preflight,
# POST /api/jobs/inline-create-user,
# and PATCH /api/schedules/{id}/resolutions.
#
# Mirrors the dataclasses in services/restorer_adapter.py
# (DryRunReport, UserResolutionRecord, etc.) and the TypeScript shapes
# documented in Plan[UI-FOR-PREFLIGHT]-2026-05-16.md.
# developer mirrors these in frontend/src/api.ts.

ProposedResolution = Literal[
    "identity_map",
    "direct_match",
    "single_admin_fallback",
    "role_flip_ack",
    "tombstone_blocked",
    "zero_row_skip",
    "no_match",
    "multi_admin_collapse",
]

OverallVerdict = Literal["ok", "ack_required", "blocked"]

SourceRole = Literal["owner", "admin", "managed"]

DestRole = Literal["owner", "admin", "managed"]

ResolutionAction = Literal["map", "create", "drop", "accept_proposed"]


class UserRowCountsOut(BaseModel):
    watch_history: int = 0
    ratings: int = 0
    playlists: int = 0
    collections: int = 0


class DestUserOptionOut(BaseModel):
    """One entry in the picker's available-destinations list."""
    backend_user_id: str
    username: str
    role: DestRole
    is_tombstoned: bool = False


class UserResolutionOut(BaseModel):
    source_username: str
    source_role: SourceRole
    source_row_counts: UserRowCountsOut
    proposed_resolution: ProposedResolution
    proposed_dest_user_id: Optional[str] = None
    proposed_dest_username: Optional[str] = None
    proposed_dest_role: Optional[DestRole] = None
    needs_ack: bool = False
    blocks_submit: bool = False
    warnings: List[str] = Field(default_factory=list)
    available_dest_users: List[DestUserOptionOut] = Field(default_factory=list)


class LibraryTypeNoteOut(BaseModel):
    source_library: str
    source_type: str
    dest_type_used: str
    message: str


class TombstoneNoteOut(BaseModel):
    dest_username: str
    reason: str


class ZeroRowSkipOut(BaseModel):
    source_username: str
    empty_signals: List[str] = Field(default_factory=list)
    filter_flags_in_effect: List[str] = Field(default_factory=list)
    message: str


class CrossPlatformPreflightReport(BaseModel):
    """One destination's preflight report. The enclosing
    PreflightResponse carries one of these per destination_server_id."""
    source_kind: str
    dest_kind: str
    source_server_id: str
    dest_server_id: str
    is_cross_platform: bool
    source_admin_count: int
    dest_admin_count: int
    resolutions: List[UserResolutionOut] = Field(default_factory=list)
    smart_playlists_skipped: int = 0
    smart_playlist_names: List[str] = Field(default_factory=list)
    library_type_notes: List[LibraryTypeNoteOut] = Field(default_factory=list)
    tombstoned_users_excluded: List[TombstoneNoteOut] = Field(default_factory=list)
    zero_row_skipped: List[ZeroRowSkipOut] = Field(default_factory=list)
    overall_verdict: OverallVerdict
    blocking_reasons: List[str] = Field(default_factory=list)


class PreflightResponse(BaseModel):
    """Uniform wrapper used by both preflight endpoints.

    Single-destination jobs return one entry in ``reports``; fan-out /
    schedule responses return N entries keyed by
    ``destination_server_id``. ``aggregate_verdict`` is the worst-of
    across every report so the frontend's Submit gate has one boolean
    to read."""
    reports: Dict[str, CrossPlatformPreflightReport] = Field(default_factory=dict)
    aggregate_verdict: OverallVerdict = "ok"


class UserResolutionDecisionIn(BaseModel):
    """One end user decision in the preflight ack body."""
    source_username: str
    action: ResolutionAction
    # Populated when action='map'
    dest_user_id: Optional[str] = None
    final_role: Optional[Literal["admin", "managed"]] = None
    # Populated when action='create'
    create_username: Optional[str] = None
    create_role: Optional[Literal["admin", "managed"]] = None
    create_password: Optional[str] = None
    admin_acknowledgement: Optional[bool] = None


class CrossPlatformPreflightAckIn(BaseModel):
    """What the modal posts back at Continue.

    One per destination_server_id - the frontend may post several
    when a fan-out job's modal had a tab per destination."""
    source_server_id: str = ""
    dest_server_id: str
    resolutions: List[UserResolutionDecisionIn] = Field(default_factory=list)
    apply_col_scope_prefix: bool = True
    persist_as_identity_map: bool = False


class InlineCreateUserIn(BaseModel):
    """Body for POST /api/jobs/inline-create-user."""
    destination_server_id: str
    username: str
    role: Literal["admin", "managed"]
    initial_password: Optional[str] = None
    acknowledgement: bool = False

    @model_validator(mode="after")
    def _admin_requires_acknowledgement(self) -> "InlineCreateUserIn":
        if self.role == "admin" and not self.acknowledgement:
            raise ValueError(
                "Creating an administrator requires the operator's "
                "explicit acknowledgement (acknowledgement=true)."
            )
        if not (self.username or "").strip():
            raise ValueError("username must be non-empty.")
        return self


class InlineCreateUserResponse(BaseModel):
    """Returned by POST /api/jobs/inline-create-user."""
    user: DestUserOptionOut
    was_newly_created: bool


class ScheduleResolutionsPatchIn(BaseModel):
    """Body for PATCH /api/schedules/{id}/resolutions.

    End user can update the stored decisions for one or more
    destinations on an existing schedule without re-walking the
    schedule create flow. ``resolutions`` is keyed by
    destination_server_id."""
    resolutions: Dict[str, CrossPlatformPreflightAckIn] = Field(default_factory=dict)


ResolutionsStatus = Literal["ok", "auto_fallback", "needs_review"]


class CrossPlatformPreflightJobIn(BaseModel):
    """Body for POST /api/jobs/cross-platform-preflight.

    Accepts a subset of the restore submit shape: either ``input_files``
    (file-based restore) or ``snapshot_id`` (snapshot-registry restore),
    plus destination(s). Filter flags + per-user fan-out flag mirror
    the submit body. Direct-transfer preflight (live source enumeration)
    is a v2 follow-up.

    Destinations may be specified by name (legacy frontend path) OR by
    stable UID (current frontend path; Plan[SERVER-UID-IDENTITY] 2026-05-16).
    The resolver prefers IDs when present; name-only resolution falls
    through to the registry's name-collision-warning path.
    """
    snapshot_id: Optional[str] = None
    input_files: List[str] = Field(default_factory=list)
    dest_server_name: Optional[str] = None
    dest_server_names: Optional[List[str]] = None
    # Plan[SERVER-UID-IDENTITY] 2026-05-16: stable UID destinations.
    # Frontend sends these alongside the name fields for backward compat.
    # Validated against the shared is_valid_server_id helper.
    dest_server_id: Optional[str] = None
    dest_server_ids: Optional[List[str]] = None
    include_watch_history: bool = True
    include_ratings: bool = True
    include_playlists: bool = True
    include_collections: bool = True
    include_managed_users: bool = True
    user_filter: Optional[List[str]] = None

    @model_validator(mode="after")
    def _at_least_one_source(self) -> "CrossPlatformPreflightJobIn":
        if not self.snapshot_id and not (self.input_files or []):
            raise ValueError(
                "preflight requires either snapshot_id or input_files."
            )
        any_dest = bool(
            self.dest_server_name
            or self.dest_server_names
            or self.dest_server_id
            or self.dest_server_ids
        )
        if not any_dest:
            raise ValueError(
                "preflight requires at least one destination "
                "(dest_server_id / dest_server_ids / dest_server_name / "
                "dest_server_names)."
            )
        return self

    @model_validator(mode="after")
    def _validate_server_ids(self) -> "CrossPlatformPreflightJobIn":
        _validate_server_id_or_none(self.dest_server_id)
        _validate_server_id_list_or_none(self.dest_server_ids)
        return self

    def resolved_destinations(self) -> List[str]:
        """Coalesce all four destination fields into one ordered list
        of identifiers the endpoint can iterate. Prefers IDs (more
        specific) over names; falls back to names for legacy callers.

        Returned values may be a mix of UIDs and names; the endpoint's
        per-destination resolver (``server_registry.get_server_by_id``
        + ``get_server_by_name`` fallback chain) handles either shape.
        """
        out: List[str] = []
        # IDs first (more specific identifier).
        for sid in (self.dest_server_ids or []):
            if sid and sid not in out:
                out.append(sid)
        if self.dest_server_id and self.dest_server_id not in out:
            out.append(self.dest_server_id)
        # Names fall through for callers that didn't supply IDs.
        for name in (self.dest_server_names or []):
            if name and name not in out:
                out.append(name)
        if self.dest_server_name and self.dest_server_name not in out:
            out.append(self.dest_server_name)
        return out


# ── Playlist Management (Plan[PLAYLIST-MANAGEMENT]-2026-05-16) ──────────────
#
# Wire types for the six /api/playlist-mgmt/* endpoints that let the
# end user copy ONE playlist from a source user (column A) to a
# destination user (column B). Same shapes developer mirrors into
# frontend/src/api.ts.

class PlaylistSpec(BaseModel):
    """End user-facing playlist row: enough to render in the picker."""
    playlist_id: str
    name: str
    item_count: int
    is_smart: bool = False


class PlaylistItem(BaseModel):
    """One item inside a playlist. ``type`` is the leaf media type
    (movie / episode / audio / audiobook / book / photo / musicvideo)
    so the mixed-media detector + cross-backend resolver can read
    it without re-fetching."""
    title: str
    guids: List[str] = Field(default_factory=list)
    type: str = ""
    duration_ms: Optional[int] = None


class PlaylistDetail(BaseModel):
    """Full playlist payload returned by the detail endpoint.
    End user-facing surface; the UI's items list reads from this."""
    playlist_id: str
    name: str
    is_smart: bool
    items: List[PlaylistItem] = Field(default_factory=list)
    fetched_at: float       # epoch seconds; cache or live
    from_cache: bool = False


class PlaylistCopyIn(BaseModel):
    """Body for POST /api/playlist-mgmt/copy.

    All three server ids carry the prefixed UID format from
    Plan[SERVER-UID-IDENTITY] (`<plex|jellyfin|emby>_<uuid4_hex>`);
    the shared validator rejects malformed values at the API
    boundary. ``dest_playlist_name`` defaults to None (preserve
    source name) but the end user can rename via the modal."""
    source_server_id: str
    source_user_id: str
    source_playlist_id: str
    dest_server_id: str
    dest_user_id: str
    dest_playlist_name: Optional[str] = None

    @model_validator(mode="after")
    def _validate_ids(self) -> "PlaylistCopyIn":
        for sid in (self.source_server_id, self.dest_server_id):
            _validate_server_id_or_none(sid)
        if not (self.source_server_id or "").strip():
            raise ValueError("source_server_id is required.")
        if not (self.dest_server_id or "").strip():
            raise ValueError("dest_server_id is required.")
        if not (self.source_user_id or "").strip():
            raise ValueError("source_user_id is required.")
        if not (self.dest_user_id or "").strip():
            raise ValueError("dest_user_id is required.")
        if not (self.source_playlist_id or "").strip():
            raise ValueError("source_playlist_id is required.")
        return self


class PlaylistCopyResult(BaseModel):
    """Returned by POST /api/playlist-mgmt/copy. End user-facing
    counts + per-error reasons surfaced in the UI result panel."""
    success: bool
    new_playlist_id: Optional[str] = None
    items_written: int = 0
    items_skipped_no_match: int = 0
    items_failed: int = 0
    errors: List[str] = Field(default_factory=list)
    elapsed_seconds: float = 0.0
    # 2026-05-17 (operator request): when copy_playlist detects that
    # source and destination resolve to the same (server, user) the
    # orchestrator returns success=True + skipped=True without doing
    # any work. The default is enforced by the
    # ``playlist_mgmt_same_user_behavior`` tunable (skip vs.
    # duplicate). ``skip_reason`` carries a human-readable string the
    # ActiveDeploysPanel surfaces inline.
    skipped: bool = False
    skip_reason: Optional[str] = None


class CacheStatus(BaseModel):
    """Per-(server_id, user_id) playlist-cache freshness snapshot.
    Returned by GET /api/playlist-mgmt/cache/status (list form).
    UI renders a badge next to each user row."""
    server_id: str
    user_id: str
    last_refreshed_at: float
    playlists_count: int = 0
    age_seconds: float = 0.0
    is_stale: bool = False  # age > snapshot_threshold tunable


class PlaylistCacheRefreshIn(BaseModel):
    """Body for POST /api/playlist-mgmt/cache/refresh (per-user) +
    /api/playlist-mgmt/cache/refresh-server (per-server bulk)."""
    server_id: str
    user_id: Optional[str] = None  # None = all users on server (bulk path)

    @model_validator(mode="after")
    def _validate_id(self) -> "PlaylistCacheRefreshIn":
        _validate_server_id_or_none(self.server_id)
        if not (self.server_id or "").strip():
            raise ValueError("server_id is required.")
        return self


class PlaylistCacheRefreshResult(BaseModel):
    """Returned by cache-refresh endpoints. Per-user refresh
    surfaces one row; per-server bulk surfaces a list."""
    server_id: str
    user_id: Optional[str]    # None on bulk-result aggregate row
    refreshed_at: float
    playlists_count: int = 0
    items_count: int = 0
    duration_ms: int = 0
    error: Optional[str] = None
