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
  renders and the parameters the engine accepts — no translation
  table needed.
* Defaults match the CLI defaults in :func:`plexmigrate.build_parser`.

Note on optional fields:
We use ``Optional[X] = None`` rather than ``X | None = None`` to keep
the source readable under Python 3.9 (the engine's minimum). FastAPI /
Pydantic v2 understand both forms equally well.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


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
        description="Default directory for .plexbackup.json exports",
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


# ── Job requests ─────────────────────────────────────────────────────────────

class ExportJobIn(BaseModel):
    """
    Body of ``POST /api/job/export``. Every flag from the CLI export
    side is mirrored here. Anything left blank falls back to the
    corresponding value in the saved settings.

    Multi-server (v0.9.0): the ``source_server_name`` field is the
    friendly name of a registered server (see ``GET /api/servers``).
    Required unless the request is being processed by the legacy
    ad-hoc CLI path that supplies ``plex_url`` + ``plex_token`` directly.
    """

    source_server_name: Optional[str] = Field(
        default=None,
        description="Friendly name of the registered server to export from.",
    )
    libraries: List[str] = Field(
        default_factory=list,
        description="Library names to export. Empty = all libraries the server reports.",
    )
    output_dir: Optional[str] = Field(
        default=None,
        description="Override the default output directory for this run.",
    )
    workers: Optional[int] = Field(default=None, ge=1, le=128)
    scrobble_workers: Optional[int] = Field(default=None, ge=1, le=64)
    verbose: Optional[bool] = None
    log_dir: Optional[str] = None


class ImportJobIn(BaseModel):
    """
    Body of ``POST /api/job/import``. Mirrors the import side of the CLI.

    Multi-server (v0.9.0): ``dest_server_name`` selects which registered
    server receives the imported data.
    """

    dest_server_name: Optional[str] = Field(
        default=None,
        description="Friendly name of the registered server to import into.",
    )
    input_files: List[str] = Field(
        default_factory=list,
        description="One or more .plexbackup.json paths (absolute, or relative to the server's working directory).",
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
        description="Backward-compat flag — no effect (all imports are additive since v0.2.0).",
    )


# ── Schedule ─────────────────────────────────────────────────────────────────

class ScheduleIn(BaseModel):
    """
    Body of ``POST /api/schedules`` (create) and ``PUT /api/schedules/{id}``
    (replace). A schedule describes a recurring export run.

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
        description="Friendly name of the registered server this schedule exports from.",
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


# ── Outbound shapes ──────────────────────────────────────────────────────────
# We return plain dicts from most endpoints rather than typing every
# response — the snapshot payload in particular is loose by design
# (mirrors DashboardState.snapshot()) and shapes change as the engine
# evolves. The TypeScript frontend has its own narrow types in
# ``frontend/src/api.ts`` for the fields it actually reads.

class DirectTransferIn(BaseModel):
    """
    Body of ``POST /api/job/direct``.

    Reads watch history / playlists / collections / ratings from
    ``source_server_name`` and writes them straight into
    ``dest_server_name`` without an intermediate file. All additive-
    only merge rules from the regular import path still apply.
    """

    source_server_name: str = Field(
        description="Friendly name of the registered source server.",
    )
    dest_server_name: str = Field(
        description="Friendly name of the registered destination server.",
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
    # Matching is by raw identifier, not display name — display names
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


# ── User display-name editing (v0.9.6 Feature 3) ─────────────────────────────

class UserDisplayNameIn(BaseModel):
    """
    Body of ``PATCH /api/servers/{id}/user-display-name``.

    ``plex_id`` is the raw Plex identifier — owner email for the owner
    row, managed-user username for managed rows. ``display_name`` is
    the operator's chosen friendly name. An empty string clears the
    mapping (the UI then falls back to showing the raw identifier).
    """

    plex_id: str = Field(description="Raw Plex identifier — owner email or managed username.")
    display_name: str = Field(default="", description="Friendly name; empty clears.")


class JobStatusOut(BaseModel):
    """
    Shape returned by ``GET /api/job``.

    The full live dashboard payload is delivered via the WebSocket
    (see :mod:`server.ws`); this endpoint is for clients that just
    want a once-off snapshot — for example, the frontend on first
    page load before the socket opens.
    """

    state: str = Field(description="idle | queued | running | stopping | completed | failed | cancelled")
    job_id: Optional[str] = None
    mode: Optional[str] = Field(default=None, description="export | import")
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    error: Optional[str] = None
    dashboard: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Last known DashboardState.snapshot() — null when idle.",
    )
