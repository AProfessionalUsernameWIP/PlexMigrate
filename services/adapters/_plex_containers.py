"""Plex playlist + collection mixin.

Per-user playlists (CRUD) and per-library collections (CRUD). Smart
playlists live in :class:`PlexSmartPlaylistMixin`; this mixin only
covers the regular (item-list) form of each container.

Mixed into :class:`PlexAdapter`. Depends on:
  * ``self._server`` / ``self._server_for`` — admin + per-user
    PlexServer instances.
  * ``self._local_account_id_by_username`` state for the
    ``list_playlists`` accountID filter (built lazily on first call).
  * The module-level ``_music_aware_guids`` + ``_safe_file_path``
    helpers re-exported from ``plex`` so the same metadata extraction
    rule used by the snapshot path is used here for ItemRef
    materialisation.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from plexapi.collection import Collection
from plexapi.playlist import Playlist
from plexapi.server import PlexServer

from services.resolver import _safe_file_path

from . import (
    CollectionSpec,
    ItemRef,
    PlaylistSpec,
    UserContext,
    WriteResult,
)


log = logging.getLogger("plexmigrate.services.adapters.plex")


# Plex `:/scrobble` URL chunking for playlist + collection adds.
# Matches the existing chunk size used by ``services/restorer.py:
# _create_playlist_chunked`` (~8KB URL cap with comma-separated
# rating keys). Per-backend tunable so Jellyfin / Emby can pick their
# own threshold.
_PLEX_CONTAINER_CHUNK_SIZE = 100


class PlexContainersMixin:
    """Playlist + collection CRUD methods for :class:`PlexAdapter`.

    Owns the regular (item-list) form of both container kinds plus
    their chunking helpers + the per-user account-id resolver used
    by ``list_playlists``."""

    def _local_account_id_for_username(self, username: str) -> Optional[int]:
        """Resolve a Plex Home username to its LOCAL SystemAccount.id
        (1, 2, 3, ...). Builds + caches the username → local_id map on
        first call.

        Why this exists: Plex's per-playlist ``accountID`` attribute
        is the LOCAL account id, not the Plex.tv user id we carry in
        managed_users.backend_user_id. To filter playlists to a
        specific Plex Home user via plexapi's client-side filter, we
        need the local id. systemAccounts() exposes both (.id =
        local, .name = username) so a one-shot map is all we need.

        Returns ``None`` when no match is found (e.g. the username is
        an email — the owner case — and the SystemAccount stores the
        Plex.tv username instead). The caller treats None as "skip
        the accountID filter" so the bare playlists() call returns the
        admin-token view (i.e. the owner's playlists)."""
        if not username:
            return None
        if self._local_account_id_by_username is None:
            mapping: Dict[str, int] = {}
            try:
                accts = self._server.systemAccounts() or []
            except Exception as exc:
                log.debug("systemAccounts() unavailable for map: %s", exc)
                accts = []
            for acct in accts:
                name = (getattr(acct, "name", "") or "").strip()
                if not name:
                    continue
                try:
                    local_id = int(getattr(acct, "id", 0) or 0)
                except (TypeError, ValueError):
                    local_id = 0
                if local_id <= 0:
                    continue
                mapping[name.lower()] = local_id
            self._local_account_id_by_username = mapping
            log.debug(
                "PlexAdapter: built username→local_account_id map "
                "with %d entries", len(mapping),
            )
        return self._local_account_id_by_username.get(username.lower())

    def list_playlists(self, user_context: UserContext) -> List[PlaylistSpec]:
        """All non-smart playlists for ``user_context``. Smart playlists
        are still returned so the engine can log + skip them with the
        criteria preserved.

        ``PlexServer.playlists()`` returns playlists visible to the
        authenticated token. When the auth falls back to the ADMIN
        token (per_user_token mode + no saved per-user token OR plain
        owner_token mode) and the target user is a managed user, a
        bare call returns the OWNER's playlists; caching those under
        the managed user's key is visible cross-user contamination.

        plexapi's ``playlists(**kwargs)`` applies kwargs as a
        client-side filter on the returned Playlist objects'
        attributes. Each Playlist carries an ``accountID`` attribute
        set by the server — the LOCAL SystemAccount id (1, 2, 3, ...)
        of the user who owns it. We resolve the target user's local
        id via :meth:`_local_account_id_for_username` and pass it as
        ``accountID=<local_id>`` so plexapi narrows to that user's
        playlists.

        Owner case: ``username`` is typically the owner's email (from
        myPlexAccount), which doesn't appear in systemAccounts (those
        use the Plex.tv username). The lookup returns None and we
        skip the filter — the admin-token bare call IS the owner's
        view, which returns the owner's playlists correctly.
        """
        # Imported at call time so the module-level helper still lives
        # in plex.py (where Plex's item-type → mb-scheme rewrite lives).
        from .plex import _music_aware_guids
        server = self._server_for(user_context)
        target_username = (getattr(user_context, "username", "") or "").strip()
        is_admin_ctx = bool(getattr(user_context, "is_admin", True))
        # Per-user-token path: ``server`` was built with the target user's
        # own token, so ``server.playlists()`` IS already scoped to that
        # user. Applying the local-SystemAccount-id filter on top would
        # be wrong — when Plex serves a managed user's playlists through
        # a Plex Home child token, the ``accountID`` attribute often
        # surfaces as the OWNER's local id (1), which means the filter
        # would reject every row and we'd cache 0 playlists for a user
        # who actually has them. We skip the filter entirely on the
        # per-user-token path.
        #
        # Admin-token path: ``server.playlists()`` returns the OWNER's
        # view regardless of the target user. We rely on the local-id
        # filter to narrow that down to the target managed user's rows.
        local_id = (
            None if not is_admin_ctx
            else self._local_account_id_for_username(target_username)
        )
        try:
            playlists = server.playlists() or []
        except Exception as exc:
            log.warning("server.playlists() failed: %s", exc)
            return []
        if not is_admin_ctx:
            log.info(
                "PlexAdapter.list_playlists: per-user-token path for %r "
                "returned %d playlists (no local_id filter applied).",
                target_username, len(playlists),
            )
        elif local_id is not None and local_id > 1:
            # Admin token + non-owner target: server.playlists() returned
            # the owner's view; narrow to the target user's rows by
            # matching Playlist.accountID against their local SystemAccount
            # id. Robust against plexapi kwarg-filter quirks (type
            # coercion, missing attribute, etc.).
            before = len(playlists)
            kept = []
            seen_account_ids: set = set()
            for pl in playlists:
                try:
                    pl_account_id = getattr(pl, "accountID", None)
                    seen_account_ids.add(pl_account_id)
                    # Cast both sides to int for comparison since plexapi
                    # sometimes surfaces accountID as int and sometimes
                    # as a numeric string depending on Plex version.
                    if pl_account_id is not None and int(pl_account_id) == int(local_id):
                        kept.append(pl)
                except (TypeError, ValueError):
                    continue
            log.info(
                "PlexAdapter.list_playlists: filtered %d → %d playlists "
                "for user %r (local_id=%d). accountIDs observed: %s",
                before, len(kept), target_username, local_id,
                sorted(str(x) for x in seen_account_ids if x is not None),
            )
            playlists = kept
        # Build the section-id -> title map ONCE up-front instead of
        # calling ``library.sectionByID(...)`` per playlist. On a Plex
        # server with many playlists each per-playlist lookup costs a
        # round-trip even when plexapi caches the section object: the
        # cache only kicks in AFTER the first hit per id, and the
        # ``server.library.sectionByID`` call's internal validation
        # always re-fetches the lightweight section list. One bulk
        # call to ``library.sections()`` covers every primary library
        # the playlists might reference.
        section_id_to_title: Dict[str, str] = {}
        try:
            for _s in (server.library.sections() or []):
                _k = str(getattr(_s, "key", "") or "")
                if _k:
                    section_id_to_title[_k] = getattr(_s, "title", "") or ""
        except Exception as exc:
            log.debug(
                "PlexAdapter.list_playlists: section map build failed: %s",
                exc,
            )
        out: List[PlaylistSpec] = []
        for pl in playlists:
            # Skip the slow ``pl.items()`` round-trip for SMART
            # playlists. The Plex server runs the filter server-side
            # on every items() call for smart playlists, so even one
            # of these on a big library can take seconds, and the
            # user's view can contain many smart playlists. The
            # Playlist Mgmt UI disables smart playlists for transfer
            # anyway (criteria don't port across servers), so we can
            # record them with an empty items_tuple. Their
            # is_smart=True flag tells the UI to gray + tag them; no
            # items data is required for that surface. Matches the
            # snapshot warmer's behavior at
            # services/snapshotter.py:build_playlist_cache.
            is_smart = bool(getattr(pl, "smart", False))
            items_tuple: Tuple[Any, ...] = ()
            if not is_smart:
                try:
                    items_tuple = tuple(
                        ItemRef(
                            backend_item_id=str(getattr(it, "ratingKey", "")),
                            guids=tuple(_music_aware_guids(it)),
                            # Surface ``library_id`` so the Replace-mode
                            # dest-only sweep can scope deletions by the
                            # actual library each item lives in, not the
                            # too-coarse Plex playlistType family. Without
                            # this, a dest-only audio library's playlists
                            # could be deleted during a Music-only restore
                            # because they share the 'audio' playlistType.
                            library_id=(
                                str(getattr(it, "librarySectionID", ""))
                                if getattr(it, "librarySectionID", None) is not None
                                else None
                            ),
                            title=getattr(it, "title", "") or "",
                        )
                        for it in (pl.items() or [])
                    )
                except Exception:
                    items_tuple = ()
            smart_filter = None
            try:
                smart_filter = getattr(pl, "content", None)
            except Exception:
                smart_filter = None
            # Surface playlistType + the primary library
            # so the Playlist Mgmt UI can group per-user playlists by
            # source library + so video / photo playlists are first-
            # class. Primary library = the section holding the
            # majority of items; ties resolve to first-encountered
            # (stable within one Plex response). Empty playlists land
            # with both None so the UI shows "(no library)".
            playlist_type = str(getattr(pl, "playlistType", "") or "")
            section_counts: Dict[str, int] = {}
            for ref in items_tuple:
                lib_id = ref.library_id or ""
                if not lib_id:
                    continue
                section_counts[lib_id] = (
                    section_counts.get(lib_id, 0) + 1
                )
            primary_library_id: Optional[str] = None
            primary_library_name: Optional[str] = None
            if section_counts:
                primary_library_id = max(
                    section_counts.items(), key=lambda kv: kv[1],
                )[0]
                # Look the name up in the precomputed map (built ONCE
                # above via ``library.sections()``) rather than paying
                # a per-playlist ``library.sectionByID`` round-trip.
                primary_library_name = (
                    section_id_to_title.get(primary_library_id) or None
                )
            out.append(PlaylistSpec(
                playlist_id=str(getattr(pl, "ratingKey", "")),
                name=getattr(pl, "title", "") or "",
                is_smart=is_smart,
                smart_filter_json=smart_filter,
                items=items_tuple,
                playlist_type=playlist_type,
                primary_library_id=primary_library_id,
                primary_library_name=primary_library_name,
            ))
        return out

    def get_playlist(
        self,
        playlist_id: str,
        *,
        user_context: UserContext,
    ) -> Optional[PlaylistSpec]:
        """Fetch ONE playlist's full PlaylistSpec (name + is_smart +
        items) in a single round-trip. Avoids the wasteful
        list_playlists()+get_playlist_items() pair, which can cost
        minutes on multi-playlist users."""
        if not playlist_id:
            return None
        try:
            rk = int(playlist_id)
        except (TypeError, ValueError):
            return None
        server = self._server_for(user_context)
        try:
            pl = server.fetchItem(rk)
        except Exception as exc:
            log.debug("get_playlist %s fetch failed: %s", playlist_id, exc)
            return None
        try:
            items_tuple = self._items_to_refs(pl)
        except Exception as exc:
            log.debug("get_playlist %s items extraction failed: %s",
                      playlist_id, exc)
            items_tuple = ()
        smart_filter = None
        try:
            smart_filter = getattr(pl, "content", None)
        except Exception:
            smart_filter = None
        return PlaylistSpec(
            playlist_id=str(getattr(pl, "ratingKey", "") or playlist_id),
            name=getattr(pl, "title", "") or "",
            is_smart=bool(getattr(pl, "smart", False)),
            smart_filter_json=smart_filter,
            items=items_tuple,
        )

    def _items_to_refs(self, pl: Any) -> Tuple[ItemRef, ...]:
        """Shared item-to-ItemRef materialisation used by both
        get_playlist + get_playlist_items so the metadata extraction
        stays in one place."""
        from .plex import _music_aware_guids
        return tuple(
            ItemRef(
                backend_item_id=str(getattr(it, "ratingKey", "")),
                guids=tuple(_music_aware_guids(it)),
                title=getattr(it, "title", "") or "",
                file_path=_safe_file_path(it),
                item_type=getattr(it, "type", "") or "",
                artist=(
                    getattr(it, "grandparentTitle", "") or ""
                    if (getattr(it, "type", "") or "") == "track"
                    else ""
                ),
                show_title=(
                    getattr(it, "grandparentTitle", "") or ""
                    if (getattr(it, "type", "") or "") == "episode"
                    else ""
                ),
                album=(
                    getattr(it, "parentTitle", "") or ""
                    if (getattr(it, "type", "") or "") == "track"
                    else ""
                ),
            )
            for it in (pl.items() or [])
        )

    def get_playlist_items(
        self,
        playlist_id: str,
        *,
        user_context: UserContext,
    ) -> tuple:
        """Single-playlist item fetch via ``server.fetchItem(ratingKey)``
        + ``.items()``. Avoids the full ``server.playlists()`` walk when
        the caller only needs one playlist (Playlist Management copy
        path, cache refresh).

        Also surfaces ``file_path`` on each ItemRef so the
        orchestrator's path-tail fallback can resolve cross-server
        copies when GUIDs don't match (common for music tracks)."""
        from .plex import _music_aware_guids
        if not playlist_id:
            return ()
        try:
            rk = int(playlist_id)
        except (TypeError, ValueError):
            return ()
        server = self._server_for(user_context)
        try:
            pl = server.fetchItem(rk)
        except Exception as exc:
            log.debug("get_playlist_items %s fetch failed: %s", playlist_id, exc)
            return ()
        try:
            return tuple(
                ItemRef(
                    backend_item_id=str(getattr(it, "ratingKey", "")),
                    guids=tuple(_music_aware_guids(it)),
                    title=getattr(it, "title", "") or "",
                    file_path=_safe_file_path(it),
                    # Surface item_type + artist (tracks) + show_title
                    # (episodes) so the destination's
                    # resolve_by_fuzzy_title can disambiguate
                    # same-titled items. Critical for music libraries
                    # where mbid:// GUIDs miss and several artists
                    # may have a song called "Lion" / "Stoke the fire".
                    item_type=getattr(it, "type", "") or "",
                    artist=(
                        getattr(it, "grandparentTitle", "") or ""
                        if (getattr(it, "type", "") or "") == "track"
                        else ""
                    ),
                    show_title=(
                        getattr(it, "grandparentTitle", "") or ""
                        if (getattr(it, "type", "") or "") == "episode"
                        else ""
                    ),
                    # parentTitle on a Plex track = album. Lets the
                    # destination's fuzzy resolver disambiguate two
                    # same-titled tracks across different albums.
                    album=(
                        getattr(it, "parentTitle", "") or ""
                        if (getattr(it, "type", "") or "") == "track"
                        else ""
                    ),
                )
                for it in (pl.items() or [])
            )
        except Exception:
            return ()

    def create_playlist(
        self,
        name: str,
        items: List[ItemRef],
        *,
        user_context: UserContext,
    ) -> str:
        """Create + chunked-add. Mirrors ``restorer._create_playlist_chunked``
        - first chunk in ``Playlist.create``, subsequent chunks via
        ``addItems`` to stay under Plex's ~8KB ``uri=`` cap."""
        server = self._server_for(user_context)
        plex_items = self._resolve_items(server, items)
        if not plex_items:
            raise ValueError("create_playlist called with no resolvable items")
        first_chunk = plex_items[:_PLEX_CONTAINER_CHUNK_SIZE]
        rest = plex_items[_PLEX_CONTAINER_CHUNK_SIZE:]
        playlist = Playlist.create(server, name, items=first_chunk)
        for i in range(0, len(rest), _PLEX_CONTAINER_CHUNK_SIZE):
            playlist.addItems(rest[i:i + _PLEX_CONTAINER_CHUNK_SIZE])
        return str(getattr(playlist, "ratingKey", ""))

    def add_to_playlist(
        self,
        playlist_id: str,
        items: List[ItemRef],
        *,
        user_context: UserContext,
    ) -> int:
        server = self._server_for(user_context)
        try:
            playlist = server.fetchItem(int(playlist_id))
        except Exception as exc:
            raise ValueError(f"playlist {playlist_id!r} not found: {exc}") from exc
        plex_items = self._resolve_items(server, items)
        added = 0
        for i in range(0, len(plex_items), _PLEX_CONTAINER_CHUNK_SIZE):
            chunk = plex_items[i:i + _PLEX_CONTAINER_CHUNK_SIZE]
            playlist.addItems(chunk)
            added += len(chunk)
        return added

    # ── Collections ────────────────────────────────────────────────────

    def list_collections(
        self, library_id: Optional[str] = None,
    ) -> List[CollectionSpec]:
        """Per-library collections. ``library_id=None`` walks every
        library (slow on large servers). Matches ``section.collections()``
        per current snapshotter usage."""
        from .plex import _music_aware_guids
        sections = []
        if library_id is None:
            sections = list(self._server.library.sections())
        else:
            try:
                sections = [self._server.library.sectionByID(int(library_id))]
            except Exception as exc:
                log.warning("sectionByID(%r) failed: %s", library_id, exc)
                return []
        out: List[CollectionSpec] = []
        for section in sections:
            try:
                coll_list = section.collections() or []
            except Exception as exc:
                log.warning(
                    "section %r collections() failed: %s",
                    getattr(section, "title", ""), exc,
                )
                continue
            sec_id = str(getattr(section, "key", ""))
            for coll in coll_list:
                try:
                    items_tuple = tuple(
                        ItemRef(
                            backend_item_id=str(getattr(it, "ratingKey", "")),
                            guids=tuple(_music_aware_guids(it)),
                            title=getattr(it, "title", "") or "",
                        )
                        for it in (coll.items() or [])
                    )
                except Exception:
                    items_tuple = ()
                out.append(CollectionSpec(
                    collection_id=str(getattr(coll, "ratingKey", "")),
                    name=getattr(coll, "title", "") or "",
                    library_id=sec_id,
                    items=items_tuple,
                ))
        return out

    def create_collection(
        self,
        name: str,
        items: List[ItemRef],
        *,
        library_id: Optional[str] = None,
    ) -> str:
        if library_id is None:
            raise ValueError(
                "Plex collections require library_id (collections are library-scoped)"
            )
        try:
            section = self._server.library.sectionByID(int(library_id))
        except Exception as exc:
            raise ValueError(f"unknown library_id {library_id!r}: {exc}") from exc
        plex_items = self._resolve_items(self._server, items)
        if not plex_items:
            raise ValueError("create_collection called with no resolvable items")
        first_chunk = plex_items[:_PLEX_CONTAINER_CHUNK_SIZE]
        rest = plex_items[_PLEX_CONTAINER_CHUNK_SIZE:]
        collection = Collection.create(self._server, name, section, items=first_chunk)
        for i in range(0, len(rest), _PLEX_CONTAINER_CHUNK_SIZE):
            collection.addItems(rest[i:i + _PLEX_CONTAINER_CHUNK_SIZE])
        return str(getattr(collection, "ratingKey", ""))

    def add_to_collection(
        self,
        collection_id: str,
        items: List[ItemRef],
    ) -> int:
        try:
            collection = self._server.fetchItem(int(collection_id))
        except Exception as exc:
            raise ValueError(f"collection {collection_id!r} not found: {exc}") from exc
        plex_items = self._resolve_items(self._server, items)
        added = 0
        for i in range(0, len(plex_items), _PLEX_CONTAINER_CHUNK_SIZE):
            chunk = plex_items[i:i + _PLEX_CONTAINER_CHUNK_SIZE]
            collection.addItems(chunk)
            added += len(chunk)
        return added

    # ── Replace-mode deletions ─────────────────────────────────────────

    def delete_playlist(
        self,
        playlist_id: str,
        *,
        user_context: UserContext,
    ) -> WriteResult:
        server = self._server_for(user_context)
        try:
            playlist = server.fetchItem(int(playlist_id))
        except Exception as exc:
            return WriteResult.fail(
                f"playlist {playlist_id!r} not found: {exc}"
            )
        try:
            playlist.delete()
        except Exception as exc:
            return WriteResult.fail(
                f"playlist {playlist_id!r} delete failed: {exc}"
            )
        return WriteResult.ok()

    def delete_collection(
        self,
        collection_id: str,
    ) -> WriteResult:
        try:
            collection = self._server.fetchItem(int(collection_id))
        except Exception as exc:
            return WriteResult.fail(
                f"collection {collection_id!r} not found: {exc}"
            )
        try:
            collection.delete()
        except Exception as exc:
            return WriteResult.fail(
                f"collection {collection_id!r} delete failed: {exc}"
            )
        return WriteResult.ok()

    # ── Helpers ────────────────────────────────────────────────────────

    def _resolve_items(
        self, server: PlexServer, items: List[ItemRef],
    ) -> List:
        """Convert ``ItemRef.backend_item_id`` -> plexapi items via
        ``fetchItem``. The engine has already resolved cross-server
        GUID matching upstream; by the time these write methods are
        called, ``backend_item_id`` is the destination server's
        ratingKey."""
        resolved = []
        for ref in items:
            if not ref.backend_item_id:
                continue
            try:
                resolved.append(server.fetchItem(int(ref.backend_item_id)))
            except Exception as exc:
                log.warning(
                    "fetchItem(%r) failed: %s; skipping",
                    ref.backend_item_id, exc,
                )
                continue
        return resolved
