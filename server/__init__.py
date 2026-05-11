"""
PlexMigrate server package — v0.8.0.

This package is a *wrapper* around the existing PlexMigrate engine
(``services/`` + ``plexmigrate.py``). It does NOT reimplement export,
import, resolution, dashboard tracking, or logging. It only adds a
FastAPI + WebSocket layer on top of the engine so that the same
functionality is reachable from a browser as well as the CLI.

Module layout
-------------
* ``app``               — FastAPI application factory and route handlers.
* ``models``            — Pydantic request / response schemas.
* ``persistence``       — JSON-file storage for schedules and settings.
* ``jobs``              — Single-worker job queue that drives the engine.
* ``schedules``         — Background scheduler that fires saved schedules.
* ``ws``                — WebSocket broadcaster that streams dashboard snapshots.
* ``log_browser``       — Read-only access to ``plex_logs/``.
* ``export_browser``    — Read-only access to ``plex_exports/``.
* ``runtime_patches``   — Monkey-patches the engine for headless server use.

Importing ``server.app`` (which uvicorn does) triggers all setup. The
CLI entry point (``plexmigrate.py``) does not import this package, so
the terminal mode is entirely unaffected by anything here.
"""

# Single source of truth for the server's wire-protocol version. The
# frontend echoes this in its handshake so an old browser tab attached
# to a freshly-rebuilt backend can detect a mismatch and prompt the
# user to reload.
# Bumped to "2" in v0.9.0 alongside the multi-server registry. The
# breaking change for clients is that GET /api/settings no longer
# carries plex_url / plex_token (those live in /api/servers/* now);
# the legacy fields are tolerated on POST for one release.
SERVER_API_VERSION = "2"
