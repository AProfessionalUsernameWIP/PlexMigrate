"""Replace-mode destination-only sweep for restore.

Replace-mode restore makes the destination an exact mirror of the
source: a destination playlist or collection whose title is absent
from the source snapshot is one the end user has chosen to overwrite,
so it is deleted. This module owns that sweep and its pure helpers.

  * :func:`compute_dest_only_titles` - set-difference of dest titles
    against source titles (used for collection deletion too).
  * :func:`is_playlist_in_restored_library_scope` - library-scope guard
    so the sweep never deletes a playlist whose items live in a library
    the run did not restore.
  * :func:`purge_dest_only_playlists` - the orchestration sweep.

Extracted from ``services/restorer.py``, which re-exports
``purge_dest_only_playlists`` and ``compute_dest_only_titles`` so
existing importers (``server/direct_transfer.py``) are unaffected.
"""

from __future__ import annotations

import logging
from typing import Any, List, Set


def compute_dest_only_titles(
    source_titles: Set[str],
    dest_titles: List[str],
) -> List[str]:
    """Return dest titles that have no counterpart in ``source_titles``.

    Pure helper for Replace-mode container deletion: a destination
    playlist or collection whose title is absent from the source
    snapshot is, in Replace semantics, a row the end user has chosen
    to overwrite — it should be deleted so the destination becomes an
    exact mirror of the source. Title is the identity key for both
    playlists and collections in this codebase (no GUID is carried).
    """
    return [t for t in dest_titles if t not in source_titles]


def is_playlist_in_restored_library_scope(
    item_library_keys: Set[str],
    restored_library_keys: Set[str],
) -> bool:
    """Pure helper: return True iff every item's library key is in the
    set of restored library keys, AND the playlist has at least one
    item (so its library scope is determinable).

    Why this matters: a Replace-mode
    direct transfer where the source had ONE audio library ("Music")
    and the destination had TWO ("Music" + "Audio Files") wiped out
    the dest-only library's playlists because the previous sweep
    scoped by Plex's broad ``playlistType`` family — both libraries
    share ``playlistType="audio"``. Library-level scoping prevents
    that by only allowing deletion of playlists whose items all live
    inside libraries the operator explicitly chose to restore.

    Empty playlists return False (conservative): no items means we
    can't classify the playlist's library scope, so we don't risk
    deleting it.
    """
    if not item_library_keys:
        return False
    return item_library_keys.issubset(restored_library_keys)


def purge_dest_only_playlists(
    server: Any,
    source_titles_in_scope: Set[str],
    restored_library_keys: Set[str],
    logger: logging.Logger,
) -> int:
    """Replace-mode orchestration sweep: delete destination playlists
    whose title is absent from the source AND whose items live entirely
    inside libraries the operator chose to restore.

    Plex playlists are server-wide objects. The previous version of
    this helper scoped by ``playlistType`` (audio / video / photo)
    which is too coarse — two distinct audio libraries on the same
    server share ``playlistType="audio"`` and a Replace run that only
    transferred one of them would still see the other library's
    playlists as deletion candidates. Library-level scoping uses each
    item's ``librarySectionID`` to keep the sweep inside the libraries
    actually in scope for the run.

    Arguments:
        ``server``: plexapi.PlexServer the sweep runs against.
        ``source_titles_in_scope``: union of source playlist titles
            across every library the run restored. A dest playlist
            whose title is in this set is preserved (it has a source
            counterpart).
        ``restored_library_keys``: set of destination-side library
            section keys (string form of plexapi's ``Section.key``)
            the run actually touched. A dest playlist with at least
            one item from a library NOT in this set is preserved —
            its items live somewhere we did not restore, so deleting
            it would exceed the run's blast radius.
        ``logger``: shared run logger; per-playlist outcomes are
            logged at INFO.

    Returns the count of playlists deleted. Best-effort: per-playlist
    failures (delete API error, item enumeration error) are caught,
    logged, and counted as zero so one stuck row never blocks the
    rest of the sweep.
    """
    if not restored_library_keys:
        return 0
    try:
        all_dest = list(server.playlists())
    except Exception as exc:
        logger.warning(
            "Replace: dest playlist list failed (%s); skipping dest-only sweep.",
            exc,
        )
        return 0
    deleted = 0
    for pl in all_dest:
        title = getattr(pl, "title", "") or ""
        if not title:
            continue
        # Compute the playlist's library scope by inspecting its
        # items' ``librarySectionID``. Each plexapi media item carries
        # this field as an int; we coerce to str to compare against
        # ``restored_library_keys`` (which is also strings).
        try:
            item_lib_keys: Set[str] = set()
            empty_playlist = True
            for it in (pl.items() or []):
                empty_playlist = False
                lib_key = getattr(it, "librarySectionID", None)
                if lib_key is None:
                    # Items missing the field force a conservative
                    # skip — we can't classify a partial library set.
                    item_lib_keys = set()
                    break
                item_lib_keys.add(str(lib_key))
        except Exception as exc:
            logger.debug(
                "Replace: enumerate items for playlist %r failed (%s); "
                "skipping (conservative).",
                title, exc,
            )
            continue
        if empty_playlist:
            logger.debug(
                "Replace: dest playlist %r is empty — can't classify "
                "library scope; skipping.", title,
            )
            continue
        if not is_playlist_in_restored_library_scope(
            item_lib_keys, restored_library_keys,
        ):
            logger.debug(
                "Replace: dest playlist %r has items outside restored "
                "library scope (item libs=%s, restored=%s); skipping.",
                title, sorted(item_lib_keys), sorted(restored_library_keys),
            )
            continue
        if title in source_titles_in_scope:
            continue
        try:
            pl.delete()
            deleted += 1
            logger.info(
                "Replace: deleted destination-only playlist %r "
                "(libraries=%s) — absent from source.",
                title, sorted(item_lib_keys),
            )
        except Exception as exc:
            logger.warning(
                "Replace: delete of dest-only playlist %r failed: %s",
                title, exc,
            )
    return deleted
