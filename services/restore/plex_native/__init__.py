"""Public facade re-exporting restore functions and helpers from submodules."""

# CRITICAL: Do NOT ``from services.state import _lib_successes`` or
# ``_lib_failures``. Those names are ContextVar-backed via
# ``state.__getattr__`` (PEP 562). A ``from`` import evaluates the proxy
# ONCE at module load and freezes the resolved value (None at that
# moment) in this module's namespace forever, so subsequent
# ``reset_run_state`` writes are invisible. Always access them via
# ``state._lib_successes`` / ``state._lib_failures`` at use sites.

from services.restore.replace_sweep import (
    compute_dest_only_titles,
    is_playlist_in_restored_library_scope,
    purge_dest_only_playlists,
)

from services.restore.plex_native.playlists import (
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

from services.restore.plex_native.collections import (
    _create_collection_chunked,
    _add_to_collection_chunked,
    restore_collections,
)

from services.restore.plex_native.watch import (
    _compute_merge_views_to_add,
    _scrobble,
    _set_resume_position,
    restore_watch_history,
)

from services.restore.plex_native.ratings import (
    _rate_item,
    restore_ratings,
)

from services.restore.plex_native.engine import (
    restore_export_file,
    run_restore,
)
