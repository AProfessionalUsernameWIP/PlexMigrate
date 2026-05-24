"""
Import pipeline for Hestia-MediaManager.

Contains the four additive-merge import functions (watch history, playlists,
collections, ratings), the per-file orchestrator (restore_export_file), and
the top-level runner (run_restore) that manages the dashboard / progress display
and parallelises across multiple export files.

The implementation is split across submodules so each restore concern is
browsable on its own; this ``__init__`` is purely the public facade that
re-exports the symbols every external caller and test references via
``from services.restorer import …``:

* ``watch``       — ``restore_watch_history``, ``_scrobble``,
                    ``_set_resume_position``, ``_compute_merge_views_to_add``.
* ``playlists``   — ``restore_playlists`` + the chunked Playlist helpers and
                    the playlist-type classification helpers.
* ``collections`` — ``restore_collections`` + chunked Collection helpers.
* ``ratings``     — ``restore_ratings`` + the ``_rate_item`` HTTP helper.
* ``engine``      — ``restore_export_file`` and ``run_restore`` orchestrators.

NOTE: never ``from services.state import _lib_successes`` or
``_lib_failures`` - those names are ContextVar-backed via
``state.__getattr__`` (PEP 562). A ``from`` import evaluates the proxy
ONCE at module load and freezes the resolved value (None at that
moment) in this module's namespace forever, so subsequent
``reset_run_state`` writes are invisible. Always access them via
``state._lib_successes`` / ``state._lib_failures`` at use sites.
"""

# ── Re-exported from services.restore_replace_sweep ──────────────────────────
# The Replace-mode dest-only playlist sweep + its pure helpers live in
# their own module. Re-exported so the import surface is unchanged:
# server/direct_transfer.py still does
# ``from services.restorer import purge_dest_only_playlists``.
from services.restore_replace_sweep import (
    compute_dest_only_titles,
    is_playlist_in_restored_library_scope,
    purge_dest_only_playlists,
)

# ── Playlist module re-exports ───────────────────────────────────────────────
# The playlist-type classification helpers, the chunked playlist
# create/add wrappers, and ``restore_playlists`` itself live in
# ``services.restorer.playlists``. They stay importable through the
# package facade because the public test suite (``test_resolver``,
# ``test_restorer_smart_playlists``) and ``services.mixed_media``
# reach them via ``services.restorer.<name>``.
from services.restorer.playlists import (
    _PLAYLIST_CHUNK_SIZE,
    _ITEM_TYPE_TO_PLEX_PLAYLIST_TYPE,
    _SECTION_TYPE_TO_PLAYLIST_TYPE,
    _playlist_type_for_section,
    _plex_playlist_type_for_items,
    _filter_to_dominant_playlist_type,
    _create_playlist_chunked,
    _add_to_playlist_chunked,
    restore_playlists,
)

# ── Collection module re-exports ─────────────────────────────────────────────
# Collection-side counterparts to the playlist module: the chunked
# Collection create/add wrappers and ``restore_collections`` itself
# live in ``services.restorer.collections``.
from services.restorer.collections import (
    _create_collection_chunked,
    _add_to_collection_chunked,
    restore_collections,
)

# ── Watch module re-exports ──────────────────────────────────────────────────
# Watch-history primitives live in ``services.restorer.watch``. The
# Plex HTTP helpers (``_scrobble`` / ``_set_resume_position``) are
# re-exported here because ``test_restorer_write_status`` and
# ``test_engine_integration`` monkeypatch them via the facade name.
from services.restorer.watch import (
    _compute_merge_views_to_add,
    _scrobble,
    _set_resume_position,
    restore_watch_history,
)

# ── Ratings module re-exports ────────────────────────────────────────────────
# ``restore_ratings`` and the Plex direct ``_rate_item`` HTTP helper
# moved to ``services.restorer.ratings``. The facade re-export keeps
# ``test_restorer_write_status`` (which targets
# ``services.restorer._rate_item``) working.
from services.restorer.ratings import (
    _rate_item,
    restore_ratings,
)

# ── Engine module re-exports ─────────────────────────────────────────────────
# The two orchestrators ``restore_export_file`` and ``run_restore``
# live in ``services.restorer.engine``. ``server/jobs.py``,
# ``server/fan_out.py``, and ``server/direct_transfer.py`` all import
# them via the facade names below.
from services.restorer.engine import (
    restore_export_file,
    run_restore,
)
