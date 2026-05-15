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

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, model_validator


# ── PR-3 / Phase D - data-type filter (include_* family) ──────────────────────
#
# The four boolean checkboxes the operator sees on every Run-Job and
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
    # Per-server default overrides. Maps server_id → {field: value}
    # where ``field`` is one of the snapshot-time knobs that has a
    # sensible per-server interpretation. The Servers ▸ Advanced
    # Settings sub-tab is the UI surface for this; the Run-Job form
    # seeds its toggles from the resolved value when the operator
    # picks a source server.
    #
    # Recognised fields:
    #   prebuild_json_sidecar         (bool)
    #   include_watch_history         (bool)
    #   include_ratings               (bool)
    #   include_playlists             (bool)
    #   include_collections           (bool)
    #   skip_playlist_prebuild        (bool)
    #   fast_collection_detection     (bool)
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
    # The two fallbacks below are operator-configurable. Snapshot /
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


# ── Job requests ─────────────────────────────────────────────────────────────

class SnapshotJobIn(BaseModel):
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

    @model_validator(mode="before")
    @classmethod
    def _legacy_skip_flags(cls, values: Any) -> Any:
        return _apply_legacy_skip_flags(values)


class RestoreJobIn(BaseModel):
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


# ── Schedule ─────────────────────────────────────────────────────────────────

class RestoreFromSnapshotIn(RestoreJobIn):
    """
    Body of ``POST /api/job/restore-from-snapshot``.

    Mirrors :class:`RestoreJobIn` exactly but resolves the input files
    from a registered snapshot in ``snapshots.db`` rather than asking
    the operator to point at on-disk ``.plexexport.json`` paths. The
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


# ── Outbound shapes ──────────────────────────────────────────────────────────
# We return plain dicts from most endpoints rather than typing every
# response - the snapshot payload in particular is loose by design
# (mirrors DashboardState.to_dashboard_frame()) and shapes change as the engine
# evolves. The TypeScript frontend has its own narrow types in
# ``frontend/src/api.ts`` for the fields it actually reads.

class DirectTransferIn(BaseModel):
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

    @model_validator(mode="before")
    @classmethod
    def _legacy_skip_flags(cls, values: Any) -> Any:
        return _apply_legacy_skip_flags(values)

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


class ServerIn(BaseModel):
    """
    Body of ``POST /api/servers`` (create) and ``PUT /api/servers/{id}`` (update).

    On update, ``token`` is special: an empty string means "leave the
    saved token unchanged" so the frontend can submit the form without
    re-entering it. A non-empty string replaces the saved token.
    """

    name: str = Field(description="Friendly name shown in the UI and CLI.")
    url: str = Field(description="Full Plex server URL, e.g. http://host.docker.internal:32400")
    token: str = Field(
        default="",
        description="Plex auth token. Empty on update = keep existing.",
    )


class TestUnsavedIn(BaseModel):
    """
    Body of ``POST /api/servers/test-unsaved`` (v0.10.0).

    Probe a URL+token combination without writing anything to the
    registry. The response includes the connected server's reported
    friendly name and machine identifier so the operator can confirm
    they're authenticating against the server they intended - a Plex
    account's token unlocks every server that account owns, so a
    successful connect does not by itself prove anything about which
    Plex install the URL actually points at.

    ``name`` is optional and present only so the UI can flag a
    mismatch between the friendly name the operator typed and the
    name the server reports for itself.
    """

    name: str = Field(default="", description="Operator's chosen friendly name (optional, for mismatch detection).")
    url: str = Field(description="Plex server URL to probe.")
    token: str = Field(description="Plex auth token to probe with.")


# ── User display-name editing (v0.9.6 Feature 3) ─────────────────────────────

class UserDisplayNameIn(BaseModel):
    """
    Body of ``PATCH /api/servers/{id}/user-display-name``.

    ``plex_id`` is the raw Plex identifier - owner email for the owner
    row, managed-user username for managed rows. ``display_name`` is
    the operator's chosen friendly name. An empty string clears the
    mapping (the UI then falls back to showing the raw identifier).
    """

    plex_id: str = Field(description="Raw Plex identifier - owner email or managed username.")
    display_name: str = Field(default="", description="Friendly name; empty clears.")


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
